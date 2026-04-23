"""Core logic for ``opcli artifacts init`` and ``opcli artifacts build``.

``init`` discovers artifacts and writes ``artifacts.yaml``.
``build`` reads the plan, invokes pack tools, and writes
``artifacts-generated.yaml``.
"""

from __future__ import annotations

import glob as globmod
import logging
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


def _find_output_file(source_dir: Path, kind: str, root: Path) -> str:
    """Glob for the built artifact in *source_dir*, returning a relative path."""
    pattern = _OUTPUT_GLOBS[kind]
    matches = sorted(globmod.glob(str(source_dir / pattern)))
    if not matches:
        msg = f"No {pattern} found in {source_dir} after pack"
        raise OpcliError(msg)
    if len(matches) > 1:
        logger.warning(
            "Multiple %s files in %s; using %s",
            pattern,
            source_dir,
            matches[0],
        )
    return str(Path(matches[0]).relative_to(root))


def _build_rock(rock: RockArtifact, root: Path) -> GeneratedRock:
    source_dir = root / rock.source
    run_command([*_PACK_COMMANDS["rock"]], cwd=str(source_dir))
    output_file = _find_output_file(source_dir, "rock", root)
    return GeneratedRock(
        name=rock.name,
        source=rock.source,
        output=ArtifactOutput(file=output_file),
    )


def _build_charm(charm: CharmArtifact, root: Path) -> GeneratedCharm:
    source_dir = root / charm.source
    run_command([*_PACK_COMMANDS["charm"]], cwd=str(source_dir))
    output_file = _find_output_file(source_dir, "charm", root)
    return GeneratedCharm(
        name=charm.name,
        source=charm.source,
        output=ArtifactOutput(file=output_file),
    )


def _build_snap(snap: SnapArtifact, root: Path) -> GeneratedSnap:
    source_dir = root / snap.source
    run_command([*_PACK_COMMANDS["snap"]], cwd=str(source_dir))
    output_file = _find_output_file(source_dir, "snap", root)
    return GeneratedSnap(
        name=snap.name,
        source=snap.source,
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
    gen_charms = [_build_charm(c, root) for c in charms_to_build]
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
