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
    all_rocks: dict[str, GeneratedRock],
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
        file_val: str | None = None
        image_val: str | None = None
        if res_def.rock and res_def.rock in all_rocks:
            rock_out = all_rocks[res_def.rock].output
            file_val = rock_out.file
            image_val = rock_out.image
        resources[res_name] = GeneratedResource(
            type=res_def.type,
            rock=res_def.rock,
            file=file_val,
            image=image_val,
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
    all_rocks = {r.name: r for r in gen_rocks}
    gen_charms = [_build_charm(c, root, all_rocks) for c in charms_to_build]
    gen_snaps = [_build_snap(s, root) for s in snaps_to_build]

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

    # Re-fill charm resource refs from the merged rock outputs.
    rocks_by_name: dict[str, GeneratedRock] = {r.name: r for r in all_rocks}
    filled_charms: list[GeneratedCharm] = []
    for charm in all_charms:
        if not charm.resources:
            filled_charms.append(charm)
            continue
        filled: dict[str, GeneratedResource] = {}
        for res_name, res in charm.resources.items():
            if res.rock and res.rock in rocks_by_name:
                rock_out = rocks_by_name[res.rock].output
                filled[res_name] = GeneratedResource(
                    type=res.type,
                    rock=res.rock,
                    file=rock_out.file,
                    image=rock_out.image,
                )
            elif res.rock and res.rock not in rocks_by_name:
                msg = (
                    f"Charm '{charm.name}' resource '{res_name}' references rock "
                    f"'{res.rock}' which was not found in the collected partials. "
                    f"Ensure the rock build job partial is included."
                )
                raise ConfigurationError(msg)
            else:
                filled[res_name] = res
        filled_charms.append(
            GeneratedCharm(
                name=charm.name,
                **{"charmcraft-yaml": charm.charmcraft_yaml},
                output=charm.output,
                resources=filled,
            )
        )

    generated = ArtifactsGenerated(
        rocks=all_rocks,
        charms=filled_charms,
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
