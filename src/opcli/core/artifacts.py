"""Core logic for ``opcli artifacts init`` and ``opcli artifacts build``.

``init`` discovers artifacts and writes ``artifacts.yaml``.
``build`` reads the plan, invokes pack tools, and writes
``artifacts-generated.yaml``.
"""

from __future__ import annotations

import glob as globmod
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from opcli.core.discovery import discover_artifacts
from opcli.core.exceptions import ConfigurationError, OpcliError
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import (
    dump_artifacts_generated,
    dump_artifacts_plan,
    load_artifacts_generated,
    load_artifacts_plan,
)
from opcli.models.artifacts import (
    CharmArtifact,
    RockArtifact,
    SnapArtifact,
)
from opcli.models.artifacts_generated import (
    ArtifactOutput,
    ArtifactsGenerated,
    CharmArtifactOutput,
    CharmFile,
    GeneratedCharm,
    GeneratedResource,
    GeneratedRock,
    GeneratedSnap,
)

logger = logging.getLogger(__name__)

_ARTIFACTS_YAML = "artifacts.yaml"
_ARTIFACTS_GENERATED_YAML = "artifacts-generated.yaml"

_PACK_COMMANDS: dict[str, list[str]] = {
    "charm": ["charmcraft", "pack"],
    "rock": ["rockcraft", "pack"],
    "snap": ["snapcraft", "pack"],
}

_ROCKCRAFT_ENV = {"ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS": "1"}

_OUTPUT_GLOBS: dict[str, str] = {
    "charm": "*.charm",
    "rock": "*.rock",
    "snap": "*.snap",
}


@dataclass
class _CIContext:
    """GitHub Actions environment variables needed to produce CI-format outputs."""

    run_id: str
    owner: str  # lowercased GITHUB_REPOSITORY_OWNER
    repo: str  # repository name only (not org/repo)
    sha: str  # GITHUB_SHA[:7]


def _get_ci_context() -> _CIContext | None:
    """Return GitHub Actions context if running inside GitHub Actions, else ``None``.

    Reads ``GITHUB_ACTIONS``, ``GITHUB_RUN_ID``, ``GITHUB_REPOSITORY_OWNER``,
    ``GITHUB_REPOSITORY``, and ``GITHUB_SHA`` from the environment.

    Raises:
        ConfigurationError: If ``GITHUB_ACTIONS=true`` but required variables
            are missing or empty.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None

    run_id = os.environ.get("GITHUB_RUN_ID", "")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").lower()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")

    missing = [
        name
        for name, val in [
            ("GITHUB_RUN_ID", run_id),
            ("GITHUB_REPOSITORY_OWNER", owner),
            ("GITHUB_REPOSITORY", repository),
            ("GITHUB_SHA", sha),
        ]
        if not val.strip()
    ]
    if missing:
        msg = (
            f"GITHUB_ACTIONS=true but required variables are missing: "
            f"{', '.join(missing)}"
        )
        raise ConfigurationError(msg)

    repo = repository.split("/", 1)[-1]
    if not repo.strip():
        msg = "GITHUB_REPOSITORY must be in 'owner/repo' format, got: {repository!r}"
        raise ConfigurationError(msg)
    return _CIContext(run_id=run_id, owner=owner, repo=repo, sha=sha[:7])


def _push_rock_to_ghcr(
    rock: GeneratedRock, ci: _CIContext, root: Path
) -> GeneratedRock:
    """Push a locally-built rock to GHCR and return an updated ``GeneratedRock``.

    The rock ``.rock`` file is pushed to
    ``ghcr.io/<owner>/<repo>/<name>:<sha7>`` using ``skopeo copy``.
    The returned object has ``output.image`` set and no ``output.file``.

    Raises:
        OpcliError: If the rock file does not exist.
        SubprocessError: If the skopeo push fails.
    """
    if not rock.output.file:
        msg = f"Rock '{rock.name}' has no local file to push to GHCR."
        raise OpcliError(msg)

    rock_path = Path(rock.output.file)
    if not rock_path.is_absolute():
        rock_path = (root / rock_path).resolve()
    if not rock_path.exists():
        msg = f"Rock file not found: {rock_path}"
        raise OpcliError(msg)

    image_ref = f"ghcr.io/{ci.owner}/{ci.repo}/{rock.name}:{ci.sha}"
    run_command(
        [
            "skopeo",
            "--insecure-policy",
            "copy",
            f"oci-archive:{rock_path}",
            f"docker://{image_ref}",
        ],
        cwd=str(root),
    )
    logger.info("Pushed rock '%s' to %s", rock.name, image_ref)
    return GeneratedRock(
        name=rock.name,
        **{"rockcraft-yaml": rock.rockcraft_yaml},
        output=ArtifactOutput(image=image_ref),
    )


def _to_ci_charm(charm: GeneratedCharm, ci: _CIContext) -> GeneratedCharm:
    """Return a copy of *charm* with CI artifact-reference output."""
    return GeneratedCharm(
        name=charm.name,
        **{"charmcraft-yaml": charm.charmcraft_yaml},
        output=CharmArtifactOutput.model_validate(
            {"artifact": f"built-charm-{charm.name}", "run-id": ci.run_id}
        ),
        resources=charm.resources,
    )


def _to_ci_snap(snap: GeneratedSnap, ci: _CIContext) -> GeneratedSnap:
    """Return a copy of *snap* with CI artifact-reference output."""
    return GeneratedSnap(
        name=snap.name,
        **{"snapcraft-yaml": snap.snapcraft_yaml},
        output=ArtifactOutput.model_validate(
            {"artifact": f"built-snap-{snap.name}", "run-id": ci.run_id}
        ),
    )


def artifacts_init(root: Path, *, force: bool = False) -> Path:
    """Discover artifacts and write ``artifacts.yaml``.

    Returns:
        The path to the written file.

    Raises:
        ConfigurationError: If the file already exists and *force* is False.
    """
    dest = root / _ARTIFACTS_YAML
    if dest.exists() and not force:
        msg = f"{_ARTIFACTS_YAML} already exists. Use --force to overwrite."
        raise ConfigurationError(msg)

    plan = discover_artifacts(root)
    dump_artifacts_plan(plan, dest)
    logger.info(
        "Wrote %s (%d charms, %d rocks, %d snaps)",
        dest,
        len(plan.charms),
        len(plan.rocks),
        len(plan.snaps),
    )
    return dest


def _snapshot_outputs(pack_dir: Path, kind: str) -> set[str]:
    """Return the set of existing output files in *pack_dir* for *kind*."""
    return set(globmod.glob(str(pack_dir / _OUTPUT_GLOBS[kind])))


def _pick_new_output(
    before: set[str], after: set[str], kind: str, pack_dir: Path
) -> str:
    """Return the new output file produced by pack, relative to repo root.

    Three cases:
    1. New files appeared (``after - before`` non-empty) — use those.
    2. No files at all — raise error.
    3. Same files before and after — the build overwrote an existing file in
       place.  Unambiguous only when there is exactly one file; otherwise we
       cannot determine which file was just produced.
    """
    new_files = sorted(after - before)
    if new_files:
        if len(new_files) > 1:
            logger.warning(
                "Multiple new %s files in %s; using %s",
                _OUTPUT_GLOBS[kind],
                pack_dir,
                new_files[0],
            )
        return new_files[0]

    # No new files — check overwrite-in-place case.
    if not after:
        msg = f"No {_OUTPUT_GLOBS[kind]} found in {pack_dir} after pack"
        raise OpcliError(msg)

    if len(after) == 1:
        # Exactly one pre-existing file; the build overwrote it in place.
        return next(iter(after))

    # Multiple pre-existing files, none added — cannot determine which was built.
    msg = (
        f"Cannot determine which {_OUTPUT_GLOBS[kind]} in {pack_dir} was just "
        "built: the pack tool overwrote an existing file but multiple "
        f"{_OUTPUT_GLOBS[kind]} files already exist. "
        "Use a dedicated pack-dir that does not contain pre-existing output files."
    )
    raise OpcliError(msg)


def _relative_to_root(path_str: str, root: Path) -> str:
    """Return *path_str* as a ``./``-prefixed relative path from *root*.

    The ``./`` prefix makes the path unambiguously local (required by Juju
    when distinguishing a local charm/rock from a CharmHub reference).
    """
    resolved = Path(path_str).resolve()
    try:
        rel = str(resolved.relative_to(root.resolve()))
        return f"./{rel}"
    except ValueError as exc:
        msg = f"Built artifact {resolved} is outside repository root {root}"
        raise OpcliError(msg) from exc


# Pattern: {name}_{distro}-{version}-{arch}.charm
# e.g. aproxy_ubuntu-22.04-amd64.charm → distro=ubuntu, version=22.04
_CHARM_FILENAME_RE = re.compile(
    r"^.+_(?P<distro>[a-z]+)-(?P<version>\d+\.\d+)-[^.]+\.charm$"
)


def _parse_base_from_charm_path(path: str) -> str | None:
    """Return the base string (e.g. ``ubuntu@22.04``) parsed from a charm filename.

    Returns ``None`` if the filename does not follow the expected
    ``{name}_{distro}-{version}-{arch}.charm`` convention.
    """
    filename = Path(path).name
    m = _CHARM_FILENAME_RE.match(filename)
    if not m:
        return None
    return f"{m.group('distro')}@{m.group('version')}"


def _pick_new_charm_outputs(
    before: set[str], after: set[str], pack_dir: Path
) -> list[str]:
    """Return all charm files produced by pack, relative paths TBD by caller.

    ``charmcraft pack`` always rebuilds **all** declared bases in a single
    invocation, so the complete set of output files is always ``after``.
    The ``before`` snapshot is only used to detect the error case where the
    pack produced nothing.

    Cases:
    1. Files present after pack — return all of them (sorted for determinism).
    2. No files at all — raise error.
    """
    if not after:
        msg = f"No *.charm found in {pack_dir} after pack"
        raise OpcliError(msg)

    return sorted(after)


def _resolve_pack_dir(yaml_path: Path, pack_dir_str: str | None, root: Path) -> Path:
    """Resolve the directory from which the pack command should run."""
    if pack_dir_str:
        return (root / pack_dir_str).resolve()
    return yaml_path.parent.resolve()


def _with_rock_symlink(
    yaml_path: Path,
    pack_dir: Path,
) -> tuple[Path | None, bool]:
    """Prepare a ``rockcraft.yaml`` symlink in *pack_dir* if needed.

    Rockcraft always looks for a file literally named ``rockcraft.yaml`` in
    the working directory, regardless of the actual filename of the craft YAML.
    This function creates ``<pack_dir>/rockcraft.yaml → <relative-path>`` when
    the source file is not already named ``rockcraft.yaml`` and located in
    ``pack_dir``.

    The symlink target is always **relative** so that it remains valid when
    rockcraft copies the pack-dir into a managed LXC container (where the
    host absolute path does not exist).

    Returns ``(symlink_path, created)`` where *created* is ``True`` when this
    call created the symlink (and the caller must remove it afterwards).

    Raises:
        ConfigurationError: If a real (non-symlink) file already exists at the
            target location.
    """
    target = pack_dir / "rockcraft.yaml"  # always the standard name
    if target == yaml_path:
        # Already named rockcraft.yaml and already in pack_dir — no symlink needed.
        return None, False

    if target.exists() and not target.is_symlink():
        msg = (
            f"A regular file already exists at {target}. "
            "Remove it or set pack-dir to a directory without a rockcraft.yaml."
        )
        raise ConfigurationError(msg)
    if target.is_symlink():
        target.unlink()
    target.symlink_to(os.path.relpath(yaml_path, pack_dir))
    return target, True


def _build_rock(rock: RockArtifact, root: Path) -> GeneratedRock:
    yaml_path = (root / rock.rockcraft_yaml).resolve()
    if not yaml_path.is_file():
        msg = f"rockcraft-yaml not found: {rock.rockcraft_yaml}"
        raise ConfigurationError(msg)
    pack_dir = _resolve_pack_dir(yaml_path, rock.pack_dir, root)
    if not pack_dir.is_dir():
        msg = f"pack-dir not found: {rock.pack_dir}"
        raise ConfigurationError(msg)

    before = _snapshot_outputs(pack_dir, "rock")

    symlink_path, symlink_created = _with_rock_symlink(yaml_path, pack_dir)
    try:
        run_command([*_PACK_COMMANDS["rock"]], cwd=str(pack_dir), env=_ROCKCRAFT_ENV)
    finally:
        if symlink_created and symlink_path and symlink_path.exists():
            symlink_path.unlink()

    after = _snapshot_outputs(pack_dir, "rock")
    new_output = _pick_new_output(before, after, "rock", pack_dir)
    output_file = _relative_to_root(new_output, root)
    return GeneratedRock(
        name=rock.name,
        **{"rockcraft-yaml": rock.rockcraft_yaml},
        output=ArtifactOutput(file=output_file),
    )


def _build_charm(
    charm: CharmArtifact,
    root: Path,
) -> GeneratedCharm:
    yaml_path = (root / charm.charmcraft_yaml).resolve()
    if not yaml_path.is_file():
        msg = f"charmcraft-yaml not found: {charm.charmcraft_yaml}"
        raise ConfigurationError(msg)
    pack_dir = _resolve_pack_dir(yaml_path, charm.pack_dir, root)
    if not pack_dir.is_dir():
        msg = f"pack-dir not found: {charm.pack_dir}"
        raise ConfigurationError(msg)

    before = _snapshot_outputs(pack_dir, "charm")
    run_command([*_PACK_COMMANDS["charm"]], cwd=str(pack_dir))
    after = _snapshot_outputs(pack_dir, "charm")
    new_outputs = _pick_new_charm_outputs(before, after, pack_dir)
    charm_files = [
        CharmFile(
            path=_relative_to_root(p, root),
            base=_parse_base_from_charm_path(p),
        )
        for p in new_outputs
    ]

    resources: dict[str, GeneratedResource] = {}
    for res_name, res_def in charm.resources.items():
        resources[res_name] = GeneratedResource(
            type=res_def.type,
            rock=res_def.rock,
        )

    return GeneratedCharm(
        name=charm.name,
        **{"charmcraft-yaml": charm.charmcraft_yaml},
        output=CharmArtifactOutput(files=charm_files),
        resources=resources if resources else None,
    )


def _build_snap(snap: SnapArtifact, root: Path) -> GeneratedSnap:
    yaml_path = (root / snap.snapcraft_yaml).resolve()
    if not yaml_path.is_file():
        msg = f"snapcraft-yaml not found: {snap.snapcraft_yaml}"
        raise ConfigurationError(msg)
    pack_dir = _resolve_pack_dir(yaml_path, snap.pack_dir, root)
    if not pack_dir.is_dir():
        msg = f"pack-dir not found: {snap.pack_dir}"
        raise ConfigurationError(msg)

    before = _snapshot_outputs(pack_dir, "snap")
    run_command([*_PACK_COMMANDS["snap"]], cwd=str(pack_dir))
    after = _snapshot_outputs(pack_dir, "snap")
    new_output = _pick_new_output(before, after, "snap", pack_dir)
    output_file = _relative_to_root(new_output, root)
    return GeneratedSnap(
        name=snap.name,
        **{"snapcraft-yaml": snap.snapcraft_yaml},
        output=ArtifactOutput(file=output_file),
    )


def artifacts_build(
    root: Path,
    *,
    charm_names: list[str] | None = None,
    rock_names: list[str] | None = None,
    snap_names: list[str] | None = None,
) -> Path:
    """Build artifacts and write ``artifacts-generated.yaml``.

    If *charm_names*, *rock_names*, or *snap_names* are given, only
    those artifacts are built.  Otherwise all declared artifacts are built.

    Returns:
        The path to the written file.

    Raises:
        ConfigurationError: If ``artifacts.yaml`` does not exist.
        OpcliError: If a build fails or no output file is found.
    """
    plan_path = root / _ARTIFACTS_YAML
    if not plan_path.exists():
        msg = f"{_ARTIFACTS_YAML} not found. Run 'opcli artifacts init' first."
        raise ConfigurationError(msg)

    plan = load_artifacts_plan(plan_path)

    # If any type filter is provided, unspecified types default to empty so
    # that `--charm foo` builds only the charm, not all rocks/snaps too.
    any_filter = (
        charm_names is not None or rock_names is not None or snap_names is not None
    )
    if any_filter:
        rock_names = rock_names if rock_names is not None else []
        charm_names = charm_names if charm_names is not None else []
        snap_names = snap_names if snap_names is not None else []

    rocks_to_build = _filter_by_name(plan.rocks, rock_names, "rock")
    charms_to_build = _filter_by_name(plan.charms, charm_names, "charm")
    snaps_to_build = _filter_by_name(plan.snaps, snap_names, "snap")

    gen_rocks = [_build_rock(r, root) for r in rocks_to_build]
    gen_charms = [_build_charm(c, root) for c in charms_to_build]
    gen_snaps = [_build_snap(s, root) for s in snaps_to_build]

    # In GitHub Actions, rewrite outputs to CI-format references.
    ci = _get_ci_context()
    if ci is not None:
        gen_rocks = [_push_rock_to_ghcr(r, ci, root) for r in gen_rocks]
        gen_charms = [_to_ci_charm(c, ci) for c in gen_charms]
        gen_snaps = [_to_ci_snap(s, ci) for s in gen_snaps]

    generated = ArtifactsGenerated(
        rocks=gen_rocks,
        charms=gen_charms,
        snaps=gen_snaps,
    )

    dest = root / _ARTIFACTS_GENERATED_YAML
    dump_artifacts_generated(generated, dest)
    logger.info("Wrote %s", dest)
    return dest


def artifacts_matrix(root: Path) -> dict[str, list[dict[str, str]]]:
    """Read ``artifacts.yaml`` and return a GitHub Actions matrix dict.

    The returned dict has a single key ``"include"`` whose value is a list of
    entries — one per artifact — each with ``"name"`` and ``"type"`` keys.
    Rocks come first, then charms, then snaps.

    The result is JSON-serialisable and suitable for use as a GitHub Actions
    ``strategy.matrix`` value via ``$GITHUB_OUTPUT``.

    Raises:
        ConfigurationError: If ``artifacts.yaml`` does not exist.
    """
    plan_path = root / _ARTIFACTS_YAML
    if not plan_path.exists():
        msg = f"{_ARTIFACTS_YAML} not found. Run 'opcli artifacts init' first."
        raise ConfigurationError(msg)

    plan = load_artifacts_plan(plan_path)
    include: list[dict[str, str]] = []
    for rock in plan.rocks:
        include.append({"name": rock.name, "type": "rock"})
    for charm in plan.charms:
        include.append({"name": charm.name, "type": "charm"})
    for snap in plan.snaps:
        include.append({"name": snap.name, "type": "snap"})

    return {"include": include}


def artifacts_collect(root: Path, partial_paths: list[Path]) -> Path:
    """Merge partial ``artifacts-generated.yaml`` files into one.

    In CI, each matrix build job produces a partial file containing only the
    artifact it built.  This function merges all partials into a single
    ``artifacts-generated.yaml`` and re-fills charm resource ``file``/``image``
    references from the merged rock outputs (rocks and charms build in parallel
    so charm partials have ``null`` resource refs at build time).

    Args:
        root: Repository root; the merged file is written here.
        partial_paths: Paths to the partial ``artifacts-generated.yaml`` files.

    Returns:
        The path to the written merged file.

    Raises:
        ConfigurationError: If *partial_paths* is empty or a path does not exist.
    """
    if not partial_paths:
        msg = "No partial artifacts-generated.yaml files provided to collect."
        raise ConfigurationError(msg)

    for p in partial_paths:
        if not p.exists():
            msg = f"Partial artifacts-generated.yaml not found: {p}"
            raise ConfigurationError(msg)

    all_rocks: list[GeneratedRock] = []
    all_charms: list[GeneratedCharm] = []
    all_snaps: list[GeneratedSnap] = []

    for p in partial_paths:
        partial = load_artifacts_generated(p)
        all_rocks.extend(partial.rocks)
        all_charms.extend(partial.charms)
        all_snaps.extend(partial.snaps)

    # Reject duplicate artifact names — each name must appear in exactly one partial.
    for kind, names in (
        ("rock", [r.name for r in all_rocks]),
        ("charm", [c.name for c in all_charms]),
        ("snap", [s.name for s in all_snaps]),
    ):
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            msg = (
                f"Duplicate {kind} name(s) across collected partials: "
                f"{', '.join(sorted(dupes))}. Each artifact must appear in "
                "exactly one partial file."
            )
            raise ConfigurationError(msg)

    # Validate that every rock referenced by a charm resource is present.
    rocks_by_name: dict[str, GeneratedRock] = {r.name: r for r in all_rocks}
    for charm in all_charms:
        for res_name, res in (charm.resources or {}).items():
            if res.rock and res.rock not in rocks_by_name:
                msg = (
                    f"Charm '{charm.name}' resource '{res_name}' references rock "
                    f"'{res.rock}' which was not found in the collected partials. "
                    f"Ensure the rock build job partial is included."
                )
                raise ConfigurationError(msg)

    generated = ArtifactsGenerated(
        rocks=all_rocks,
        charms=all_charms,
        snaps=all_snaps,
    )
    dest = root / _ARTIFACTS_GENERATED_YAML
    dump_artifacts_generated(generated, dest)
    logger.info("Wrote merged %s", dest)
    return dest


def _filter_by_name[T: (RockArtifact, CharmArtifact, SnapArtifact)](
    items: list[T],
    names: list[str] | None,
    kind: str,
) -> list[T]:
    """Return items filtered by *names*, or all if *names* is None.

    ``None`` means "no filter — build all".  An empty list means "build none".
    """
    if names is None:
        return items
    name_set = set(names)
    available = {item.name for item in items}
    unknown = name_set - available
    if unknown:
        msg = f"Unknown {kind}(s): {', '.join(sorted(unknown))}"
        raise ConfigurationError(msg)
    return [item for item in items if item.name in name_set]


def artifacts_localize(root: Path) -> int:
    """Update ``artifacts-generated.yaml`` with local charm file paths.

    In CI, charm outputs are recorded as ``artifact + run-id`` references.
    Before running integration tests, the workflow downloads the charm
    artifacts to the working directory.  This command scans the project tree
    for ``.charm`` files and rewrites ``artifacts-generated.yaml`` so that
    each charm with only a CI artifact reference gets an ``output.files``
    entry pointing to the discovered local file.

    Returns the number of charms that were localised.

    Raises:
        ConfigurationError: If ``artifacts-generated.yaml`` is not found.
    """
    gen_path = root / _ARTIFACTS_GENERATED_YAML
    if not gen_path.exists():
        msg = f"{_ARTIFACTS_GENERATED_YAML} not found."
        raise ConfigurationError(msg)

    generated = load_artifacts_generated(gen_path)

    updated = 0
    for charm in generated.charms:
        if charm.output.files:
            continue  # Already has local files — nothing to do.
        if not charm.output.artifact:
            continue  # No CI ref either — skip.

        # Search for .charm files whose name starts with the charm name.
        pattern = str(root / "**" / f"{charm.name}*.charm")
        matches = sorted(globmod.glob(pattern, recursive=True))
        if not matches:
            logger.warning(
                "No .charm file found for charm '%s' (pattern: %s).",
                charm.name,
                pattern,
            )
            continue

        if len(matches) > 1:
            logger.warning(
                "Multiple .charm files found for charm '%s'; using %s.",
                charm.name,
                matches[0],
            )

        charm.output.files = [CharmFile(path=matches[0])]
        logger.info("Localised charm '%s' → %s", charm.name, matches[0])
        updated += 1

    if updated:
        dump_artifacts_generated(generated, gen_path)
        logger.info("Updated %s with %d localised charm(s).", gen_path, updated)

    return updated
