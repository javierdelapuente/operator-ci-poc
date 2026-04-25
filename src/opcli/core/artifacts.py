"""Core logic for ``opcli artifacts init`` and ``opcli artifacts build``.

``init`` discovers artifacts and writes ``artifacts.yaml``.
``build`` reads the plan, invokes pack tools, and writes
``artifacts-generated.yaml``.
"""

from __future__ import annotations

import glob as globmod
import logging
import os
from pathlib import Path

from opcli.core.discovery import discover_artifacts
from opcli.core.exceptions import ConfigurationError, OpcliError
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import (
    dump_artifacts_generated,
    dump_artifacts_plan,
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
        run_command([*_PACK_COMMANDS["rock"]], cwd=str(pack_dir))
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
    new_output = _pick_new_output(before, after, "charm", pack_dir)
    output_file = _relative_to_root(new_output, root)

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
        output=ArtifactOutput(file=output_file),
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


def _filter_by_name[T: (RockArtifact, CharmArtifact, SnapArtifact)](
    items: list[T],
    names: list[str] | None,
    kind: str,
) -> list[T]:
    """Return items filtered by *names*, or all if *names* is None/empty."""
    if not names:
        return items
    name_set = set(names)
    available = {item.name for item in items}
    unknown = name_set - available
    if unknown:
        msg = f"Unknown {kind}(s): {', '.join(sorted(unknown))}"
        raise ConfigurationError(msg)
    return [item for item in items if item.name in name_set]
