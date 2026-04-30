"""Core logic for ``opcli artifacts`` commands.

``init`` discovers artifacts and writes ``artifacts.yaml``.
``build`` reads the plan, invokes pack tools, and writes
``artifacts-generated.yaml``.
``fetch`` downloads a completed CI run's artifacts so tests can run locally.
"""

from __future__ import annotations

import glob as globmod
import json
import logging
import os
import platform
import re
import time
from dataclasses import dataclass
from pathlib import Path

from opcli.core.discovery import discover_artifacts
from opcli.core.exceptions import ConfigurationError, OpcliError, SubprocessError
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
    ArtifactsGenerated,
    CharmOutput,
    GeneratedCharm,
    GeneratedResource,
    GeneratedRock,
    GeneratedSnap,
    RockOutput,
    SnapOutput,
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


def _current_arch() -> str:
    """Return the normalised architecture of the current machine.

    Maps ``x86_64`` → ``amd64`` and ``aarch64`` → ``arm64``.
    All other values are returned as-is (lower-cased).
    """
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return machine


def _push_rock_to_ghcr(
    rock: GeneratedRock, ci: _CIContext, root: Path
) -> GeneratedRock:
    """Push a locally-built rock to GHCR and return an updated ``GeneratedRock``.

    The rock ``.rock`` file is pushed to
    ``ghcr.io/<owner>/<repo>/<name>:<sha7>-<arch>`` using ``skopeo copy``.
    The returned object has its ``output`` rewritten to a single
    :class:`RockArchBuild` with ``image`` set and no ``file``.

    Raises:
        OpcliError: If the rock output list is empty or has no local file.
        SubprocessError: If the skopeo push fails.
    """
    if not rock.output:
        msg = f"Rock '{rock.name}' has no build output to push to GHCR."
        raise OpcliError(msg)
    build = rock.output[0]
    if not build.file:
        msg = f"Rock '{rock.name}' has no local file to push to GHCR."
        raise OpcliError(msg)

    rock_path = Path(build.file)
    if not rock_path.is_absolute():
        rock_path = (root / rock_path).resolve()
    if not rock_path.exists():
        msg = f"Rock file not found: {rock_path}"
        raise OpcliError(msg)

    image_ref = f"ghcr.io/{ci.owner}/{ci.repo}/{rock.name}:{ci.sha}-{build.arch}"
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
        output=[RockOutput(arch=build.arch, image=image_ref)],
    )


def _to_ci_charm(charm: GeneratedCharm, ci: _CIContext) -> GeneratedCharm:
    """Return a copy of *charm* with CI artifact-reference output.

    The artifact name includes the architecture so parallel multi-arch builds
    produce distinct artifact names (e.g. ``built-charm-my-charm-amd64``).
    """
    arch = charm.output[0].arch if charm.output else _current_arch()
    return GeneratedCharm(
        name=charm.name,
        **{"charmcraft-yaml": charm.charmcraft_yaml},
        output=[
            CharmOutput.model_validate(
                {
                    "arch": arch,
                    "artifact": f"built-charm-{charm.name}-{arch}",
                    "run-id": ci.run_id,
                }
            )
        ],
        resources=charm.resources,
    )


def _to_ci_snap(snap: GeneratedSnap, ci: _CIContext) -> GeneratedSnap:
    """Return a copy of *snap* with CI artifact-reference output.

    The artifact name includes the architecture so parallel multi-arch builds
    produce distinct artifact names (e.g. ``built-snap-my-snap-amd64``).
    """
    arch = snap.output[0].arch if snap.output else _current_arch()
    return GeneratedSnap(
        name=snap.name,
        **{"snapcraft-yaml": snap.snapcraft_yaml},
        output=[
            SnapOutput.model_validate(
                {
                    "arch": arch,
                    "artifact": f"built-snap-{snap.name}-{arch}",
                    "run-id": ci.run_id,
                }
            )
        ],
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
# e.g. aproxy_ubuntu-22.04-amd64.charm → distro=ubuntu, version=22.04, arch=amd64
_CHARM_FILENAME_RE = re.compile(
    r"^.+_(?P<distro>[a-z]+)-(?P<version>\d+\.\d+)-(?P<arch>[^.]+)\.charm$"
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


def _parse_arch_from_charm_path(path: str) -> str | None:
    """Return the arch (e.g. ``amd64``) parsed from a charm filename.

    Returns ``None`` if the filename does not follow the expected convention.
    """
    filename = Path(path).name
    m = _CHARM_FILENAME_RE.match(filename)
    return m.group("arch") if m else None


# Pattern: {name}_{version}_{arch}.snap  e.g. my-snap_1.0_amd64.snap
_SNAP_FILENAME_RE = re.compile(r"^.+_[^_]+_(?P<arch>[^.]+)\.snap$")


def _parse_arch_from_snap_path(path: str) -> str | None:
    """Return the arch (e.g. ``amd64``) parsed from a snap filename.

    Returns ``None`` if the filename does not follow the expected convention.
    """
    filename = Path(path).name
    m = _SNAP_FILENAME_RE.match(filename)
    return m.group("arch") if m else None


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
        output=[RockOutput(arch=_current_arch(), file=output_file)],
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
    arch = _current_arch()
    charm_outputs = [
        CharmOutput(
            arch=arch,
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
        output=charm_outputs,
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
        output=[SnapOutput(arch=_current_arch(), file=output_file)],
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


def artifacts_matrix(root: Path) -> dict[str, list[dict[str, object]]]:
    """Read ``artifacts.yaml`` and return a GitHub Actions matrix dict.

    The returned dict has a single key ``"include"`` whose value is a list of
    entries — one per (artifact, arch) combination — each with ``"name"``,
    ``"type"``, ``"arch"``, and ``"runner"`` keys.  ``runner`` is a list of
    GitHub runner label strings.

    Rocks come first, then charms, then snaps.  Within each kind, entries are
    ordered by artifact declaration order, then by ``builds`` order.

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
    include: list[dict[str, object]] = []
    for rock in plan.rocks:
        for build in rock.builds:
            include.append(
                {
                    "name": rock.name,
                    "type": "rock",
                    "arch": build.arch,
                    "runner": json.dumps(build.runner or ["ubuntu-latest"]),
                }
            )
    for charm in plan.charms:
        for build in charm.builds:
            include.append(
                {
                    "name": charm.name,
                    "type": "charm",
                    "arch": build.arch,
                    "runner": json.dumps(build.runner or ["ubuntu-latest"]),
                }
            )
    for snap in plan.snaps:
        for build in snap.builds:
            include.append(
                {
                    "name": snap.name,
                    "type": "snap",
                    "arch": build.arch,
                    "runner": json.dumps(build.runner or ["ubuntu-latest"]),
                }
            )

    return {"include": include}


def _output_key(
    build: RockOutput | CharmOutput | SnapOutput,
) -> tuple[object, ...]:
    """Return a hashable key that uniquely identifies a build output entry.

    For :class:`CharmOutput` (flat format), multiple entries per arch are valid
    (different bases/paths), so the key includes ``path`` and ``artifact``.
    For :class:`RockOutput` and :class:`SnapOutput`, ``arch`` alone is the
    unique key since there is one entry per arch.
    """
    if isinstance(build, CharmOutput):
        return (build.arch, build.path, build.artifact)
    return (build.arch,)


def _merge_artifact_outputs[T: (GeneratedRock, GeneratedCharm, GeneratedSnap)](
    items: list[T],
    kind: str,
) -> list[T]:
    """Merge artifacts with the same name by combining their output lists.

    In a multi-arch CI build, each arch produces a separate partial file for
    the same artifact but with a different arch entry in ``output``.  This
    function groups them by name and concatenates the ``output`` lists so that
    the collected file holds all arches for each artifact.

    For rocks and snaps, raises :class:`ConfigurationError` if the same
    ``(name, arch)`` pair appears in more than one partial (genuine conflict).
    For charms (flat format), raises if the same ``(arch, path, artifact)``
    tuple appears in more than one partial.
    """
    merged: dict[str, T] = {}
    for item in items:
        if item.name not in merged:
            merged[item.name] = item
        else:
            existing = merged[item.name]
            existing_keys = {_output_key(b) for b in existing.output}
            for build in item.output:
                key = _output_key(build)
                if key in existing_keys:
                    msg = (
                        f"Duplicate {kind} '{item.name}' output {key!r} across "
                        "collected partials. Each (artifact, arch) must appear in "
                        "exactly one partial file."
                    )
                    raise ConfigurationError(msg)
            existing.output.extend(item.output)
    return list(merged.values())


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

    # Merge same-named artifacts: combine their output lists.
    # Reject duplicate (name, arch) combinations across partials.
    merged_rocks = _merge_artifact_outputs(all_rocks, "rock")
    merged_charms = _merge_artifact_outputs(all_charms, "charm")
    merged_snaps = _merge_artifact_outputs(all_snaps, "snap")

    # Validate that every rock referenced by a charm resource is present.
    rocks_by_name: dict[str, GeneratedRock] = {r.name: r for r in merged_rocks}
    for charm in merged_charms:
        for res_name, res in (charm.resources or {}).items():
            if res.rock and res.rock not in rocks_by_name:
                msg = (
                    f"Charm '{charm.name}' resource '{res_name}' references rock "
                    f"'{res.rock}' which was not found in the collected partials. "
                    f"Ensure the rock build job partial is included."
                )
                raise ConfigurationError(msg)

    generated = ArtifactsGenerated(
        rocks=merged_rocks,
        charms=merged_charms,
        snaps=merged_snaps,
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


def _find_local_file(
    root: Path, name: str, extension: str, arch: str | None = None
) -> str | None:
    """Find a single ``name_*.{extension}`` file under *root*.

    When *arch* is given and the filename follows the expected naming
    convention, only files whose parsed arch matches are considered.

    Returns the relative path (``./...``) on success, or ``None`` if no file
    is found.  Logs a warning when multiple matches exist and picks the first.
    """
    pattern = str(root / "**" / f"{name}_*.{extension}")
    matches = sorted(globmod.glob(pattern, recursive=True))
    if arch is not None and extension == "snap":
        matches = [m for m in matches if _parse_arch_from_snap_path(m) in (arch, None)]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple .%s files found for '%s'; using %s.",
            extension,
            name,
            matches[0],
        )
    return "./" + str(Path(matches[0]).relative_to(root))


def _find_charm_files_in_dir(
    search_dir: Path, root: Path, arch: str | None = None
) -> list[tuple[str, str | None]]:
    """Find all ``.charm`` files under *search_dir*.

    Returns (root-relative path, base) pairs.  Unlike
    :func:`_find_local_charm_files`, this function does not filter by charm
    name — it returns every ``.charm`` file found.  This is the correct
    approach when searching an artifact-specific subdirectory
    (e.g. ``root/built-charm-my-charm-amd64/``) because the internal charm
    name baked into the filename may differ from the opcli artifact name.

    When *arch* is given, only files whose parsed arch matches (or whose arch
    cannot be determined) are returned.  Paths are returned relative to *root*.
    """
    pattern = str(search_dir / "**" / "*.charm")
    matches = sorted(globmod.glob(pattern, recursive=True))
    if arch is not None:
        matches = [m for m in matches if _parse_arch_from_charm_path(m) in (arch, None)]
    return [
        (
            "./" + str(Path(m).relative_to(root)),
            _parse_base_from_charm_path(m),
        )
        for m in matches
    ]


def _find_snap_file_in_dir(
    search_dir: Path, root: Path, arch: str | None = None
) -> str | None:
    """Find a single ``.snap`` file under *search_dir*.

    Like :func:`_find_charm_files_in_dir`, this searches by extension only —
    not by snap name — so it works correctly when the artifact directory name
    differs from the internal snap name.  Returns a path relative to *root*.
    """
    pattern = str(search_dir / "**" / "*.snap")
    matches = sorted(globmod.glob(pattern, recursive=True))
    if arch is not None:
        matches = [m for m in matches if _parse_arch_from_snap_path(m) in (arch, None)]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple .snap files found in '%s'; using %s.",
            search_dir,
            matches[0],
        )
    return "./" + str(Path(matches[0]).relative_to(root))


def _find_local_charm_files(
    root: Path, name: str, arch: str | None = None
) -> list[tuple[str, str | None]]:
    """Find all ``name_*.charm`` files under *root* and return (path, base) pairs.

    When *arch* is given, only files whose parsed arch matches (or whose arch
    cannot be determined) are returned.  Returns an empty list when no files
    are found.  Multiple matches are expected for multi-base charms — each
    file gets its base parsed from the filename.
    """
    pattern = str(root / "**" / f"{name}_*.charm")
    matches = sorted(globmod.glob(pattern, recursive=True))
    if arch is not None:
        matches = [m for m in matches if _parse_arch_from_charm_path(m) in (arch, None)]
    return [
        (
            "./" + str(Path(m).relative_to(root)),
            _parse_base_from_charm_path(m),
        )
        for m in matches
    ]


def _localize_charm(
    charm: GeneratedCharm,
    root: Path,
    missing: list[str],
) -> int:
    """Localize CI-only charm entries by replacing them with local file entries.

    Returns the number of arch-groups that were localized.
    """
    indices_to_replace: list[int] = []
    new_entries: list[CharmOutput] = []
    localized = 0

    for idx, build in enumerate(charm.output):
        if build.path or not build.artifact:
            continue
        artifact_dir = root / build.artifact
        if artifact_dir.is_dir():
            charm_files = _find_charm_files_in_dir(artifact_dir, root, build.arch)
        else:
            charm_files = _find_local_charm_files(root, charm.name, build.arch)
        if not charm_files:
            missing.append(f"{charm.name} ({build.arch})")
            logger.error(
                "No .charm file found for charm '%s' arch '%s'.",
                charm.name,
                build.arch,
            )
            continue
        indices_to_replace.append(idx)
        for path, base in charm_files:
            new_entries.append(
                CharmOutput.model_validate(
                    {
                        "arch": build.arch,
                        "path": path,
                        "base": base,
                        "artifact": build.artifact,
                        "run-id": build.run_id,
                    }
                )
            )
        logger.info(
            "Localised charm '%s' (%s) → %d file(s).",
            charm.name,
            build.arch,
            len(charm_files),
        )
        localized += 1

    if indices_to_replace:
        replace_set = set(indices_to_replace)
        charm.output = [
            b for i, b in enumerate(charm.output) if i not in replace_set
        ] + new_entries

    return localized


def artifacts_localize(root: Path) -> int:
    """Update ``artifacts-generated.yaml`` with local artifact file paths.

    In CI, charm and snap outputs are recorded as ``artifact + run-id``
    references.  Before running integration tests, the workflow downloads
    the artifacts to the working directory.  This command scans the project
    tree for ``.charm`` / ``.snap`` files and rewrites
    ``artifacts-generated.yaml`` so that each :class:`CharmOutput` with only
    a CI artifact reference gets a ``path`` entry pointing to the discovered
    local file, and each :class:`SnapOutput` gets a ``file`` entry.

    Returns the total number of arch-builds that were localised.

    Raises:
        ConfigurationError: If ``artifacts-generated.yaml`` is not found or
            if any artifact with a CI reference has no matching local file.
    """
    gen_path = root / _ARTIFACTS_GENERATED_YAML
    if not gen_path.exists():
        msg = f"{_ARTIFACTS_GENERATED_YAML} not found."
        raise ConfigurationError(msg)

    generated = load_artifacts_generated(gen_path)

    updated = 0
    missing: list[str] = []

    for charm in generated.charms:
        updated += _localize_charm(charm, root, missing)

    for snap in generated.snaps:
        for snap_build in snap.output:
            if snap_build.file or not snap_build.artifact:
                continue
            artifact_dir = root / snap_build.artifact
            if artifact_dir.is_dir():
                rel = _find_snap_file_in_dir(artifact_dir, root, snap_build.arch)
            else:
                rel = _find_local_file(root, snap.name, "snap", snap_build.arch)
            if rel is None:
                missing.append(f"{snap.name} ({snap_build.arch})")
                logger.error(
                    "No .snap file found for snap '%s' arch '%s'.",
                    snap.name,
                    snap_build.arch,
                )
                continue
            snap_build.file = rel
            logger.info(
                "Localised snap '%s' (%s) → %s", snap.name, snap_build.arch, rel
            )
            updated += 1

    if missing:
        msg = (
            f"Could not find downloaded artifact files for: {', '.join(missing)}. "
            "Ensure artifacts were downloaded before running localize."
        )
        raise ConfigurationError(msg)

    if updated:
        dump_artifacts_generated(generated, gen_path)
        logger.info("Updated %s with %d localised artifact(s).", gen_path, updated)

    return updated


# ---------------------------------------------------------------------------
# artifacts fetch
# ---------------------------------------------------------------------------

_GITHUB_URL_RE = re.compile(r"github\.com[:/](.+?)(?:\.git)?/?$")


def _infer_repo_from_git(root: Path) -> str:
    """Return ``owner/repo`` inferred from the git remote of *root*.

    Raises:
        ConfigurationError: If the git remote URL cannot be parsed.
    """
    try:
        result = run_command(["git", "remote", "get-url", "origin"], cwd=str(root))
    except Exception as exc:
        msg = (
            "Could not read git remote 'origin'. Use --repo to specify the repository."
        )
        raise ConfigurationError(msg) from exc

    url = result.stdout.strip()
    m = _GITHUB_URL_RE.search(url)
    if not m:
        msg = (
            f"Could not parse a GitHub 'owner/repo' from remote URL {url!r}. "
            "Use --repo to specify the repository."
        )
        raise ConfigurationError(msg)
    return m.group(1)


_WAIT_MAX_ATTEMPTS = 60
_WAIT_SLEEP_SECONDS = 30
# Keywords in gh CLI stderr that indicate a hard auth/permission failure
# rather than "artifact not yet available" — retrying is pointless for these.
_AUTH_ERROR_KEYWORDS = (
    "authentication",
    "credentials",
    "unauthorized",
    "token",
    "403",
    "401",
)

# Keywords that indicate the destination file already exists; we delete it and retry.
_FILE_EXISTS_KEYWORDS = ("file exists",)


def _gh_download(cmd: list[str], cwd: str, dest: Path | None = None) -> None:
    """Run ``gh run download``, raising :class:`SubprocessError` on failure.

    If ``gh`` reports that the destination file already exists (older ``gh``
    versions lack ``--clobber``), the file is deleted and the download is
    retried once.
    """
    try:
        run_command(cmd, cwd=cwd)
    except SubprocessError as exc:
        if dest is not None and any(
            kw in exc.stderr.lower() for kw in _FILE_EXISTS_KEYWORDS
        ):
            dest.unlink(missing_ok=True)
            run_command(cmd, cwd=cwd)
        else:
            raise


def _gh_download_with_wait(
    cmd: list[str], cwd: str, run_id: str, dest: Path | None = None
) -> None:
    """Run ``gh run download``, retrying until the artifact appears.

    Retries up to :data:`_WAIT_MAX_ATTEMPTS` times with
    :data:`_WAIT_SLEEP_SECONDS` between each attempt.  Fails immediately if
    the error looks like an authentication/permission problem or a
    deterministic failure (e.g. "file exists", handled via delete-and-retry).

    Args:
        cmd: Full ``gh run download ...`` command list.
        cwd: Working directory for the subprocess.
        run_id: Run ID used only for log messages.
        dest: Path of the expected output file.  When provided and ``gh``
            reports "file exists", the file is deleted and the download is
            retried once before giving up.

    Raises:
        ConfigurationError: On auth/permission errors or when the timeout
            is exceeded (includes the last error message).
        SubprocessError: Propagated for unexpected non-retryable failures.
    """
    last_exc: SubprocessError | None = None
    for attempt in range(1, _WAIT_MAX_ATTEMPTS + 1):
        try:
            run_command(cmd, cwd=cwd)
            return
        except SubprocessError as exc:
            stderr_lower = exc.stderr.lower()
            if any(kw in stderr_lower for kw in _AUTH_ERROR_KEYWORDS):
                msg = (
                    f"Authentication/permission error downloading artifact "
                    f"from run {run_id!r}. Check GH_TOKEN and repository "
                    f"permissions.\n{exc.stderr.strip()}"
                )
                raise ConfigurationError(msg) from exc
            if dest is not None and any(
                kw in stderr_lower for kw in _FILE_EXISTS_KEYWORDS
            ):
                dest.unlink(missing_ok=True)
                run_command(cmd, cwd=cwd)
                return
            last_exc = exc
            logger.info(
                "Artifact not yet available (attempt %d/%d): %s — retrying in %ds...",
                attempt,
                _WAIT_MAX_ATTEMPTS,
                exc.stderr.strip(),
                _WAIT_SLEEP_SECONDS,
            )
            time.sleep(_WAIT_SLEEP_SECONDS)

    last_msg = last_exc.stderr.strip() if last_exc else ""
    msg = (
        f"Timed out waiting for artifacts-generated artifact from run "
        f"{run_id!r} after {_WAIT_MAX_ATTEMPTS * _WAIT_SLEEP_SECONDS}s. "
        f"Last error: {last_msg}"
    )
    raise ConfigurationError(msg)


def artifacts_fetch(
    root: Path,
    run_id: str,
    repo: str | None = None,
    *,
    wait: bool = False,
) -> Path:
    """Download a CI run's artifacts and prepare for local testing.

    Steps:
    1. Infer ``owner/repo`` from the local git remote if *repo* is not given.
    2. Download ``artifacts-generated.yaml`` from the named GitHub Actions
       artifact.  When *wait* is ``True``, retries up to
       :data:`_WAIT_MAX_ATTEMPTS` times until the artifact appears (useful
       when the test job starts before the build completes).
    3. For every charm/snap that carries a CI artifact reference, download the
       corresponding artifact archive.
    4. Call :func:`artifacts_localize` to rewrite ``artifacts-generated.yaml``
       with the local ``.charm`` / ``.snap`` file paths.

    Rock artifacts are OCI images on GHCR and need no download — their
    ``output.image`` reference is already usable locally.

    Args:
        root: Working directory; all artifacts are downloaded here.
        run_id: GitHub Actions workflow run ID.
        repo: GitHub repository in ``owner/name`` format.  Inferred from the
            local git remote when ``None``.
        wait: When ``True``, retry the initial ``artifacts-generated``
            download until it succeeds.  Fails immediately on
            authentication/permission errors.

    Returns:
        Path to the updated ``artifacts-generated.yaml``.

    Raises:
        ConfigurationError: If the repo cannot be inferred, the yaml is
            missing after download, or the wait timeout is exceeded.
        SubprocessError: If ``gh run download`` fails non-transiently.
    """
    if repo is None:
        repo = _infer_repo_from_git(root)

    generated_cmd = [
        "gh",
        "run",
        "download",
        run_id,
        "--repo",
        repo,
        "--name",
        "artifacts-generated",
        "--dir",
        str(root),
    ]
    gen_path = root / _ARTIFACTS_GENERATED_YAML
    if wait:
        _gh_download_with_wait(generated_cmd, str(root), run_id, dest=gen_path)
    else:
        _gh_download(generated_cmd, str(root), dest=gen_path)

    if not gen_path.exists():
        msg = (
            f"{_ARTIFACTS_GENERATED_YAML} not found after download. "
            "Ensure the CI run completed its build phase."
        )
        raise ConfigurationError(msg)

    generated = load_artifacts_generated(gen_path)

    # Download each charm / snap artifact archive (deduplicated)
    seen_artifacts: set[str] = set()
    for charm in generated.charms:
        for build in charm.output:
            if build.artifact:
                seen_artifacts.add(build.artifact)
    for snap in generated.snaps:
        for snap_build in snap.output:
            if snap_build.artifact:
                seen_artifacts.add(snap_build.artifact)

    for name in sorted(seen_artifacts):
        artifact_dir = root / name
        artifact_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading artifact '%s' into '%s'...", name, artifact_dir)
        run_command(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                repo,
                "--name",
                name,
                "--dir",
                str(artifact_dir),
            ],
            cwd=str(root),
        )
        logger.info("Downloaded artifact '%s'.", name)

    # Rewrite artifact refs to local file paths
    artifacts_localize(root)
    return gen_path
