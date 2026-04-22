"""Artifact discovery — walk a repository to find charms, rocks, and snaps.

Discovery looks for ``charmcraft.yaml``, ``rockcraft.yaml``, and
``snapcraft.yaml`` marker files, extracts names, and links charm OCI-image
resources to discovered rocks when the match is unambiguous.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from opcli.core.exceptions import DiscoveryError
from opcli.models.artifacts import (
    ArtifactResource,
    ArtifactsPlan,
    CharmArtifact,
    RockArtifact,
    SnapArtifact,
)

logger = logging.getLogger(__name__)

_yaml = YAML()

_PRUNE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".tox",
        "build",
        "dist",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)

_MARKER_FILES: dict[str, str] = {
    "charmcraft.yaml": "charm",
    "rockcraft.yaml": "rock",
    "snapcraft.yaml": "snap",
}


def _read_yaml_name(path: Path) -> str:
    """Read the ``name`` field from a craft YAML file."""
    with path.open() as fh:
        data = _yaml.load(fh)
    if not isinstance(data, dict):
        msg = f"{path} does not contain a YAML mapping"
        raise DiscoveryError(msg)
    name = data.get("name")
    if not isinstance(name, str) or not name:
        msg = f"{path} is missing a valid 'name' field"
        raise DiscoveryError(msg)
    return name


def _read_charm_resources(path: Path) -> dict[str, dict[str, Any]]:
    """Read the ``resources`` section from a charmcraft.yaml file."""
    with path.open() as fh:
        data = _yaml.load(fh)
    if not isinstance(data, dict):
        return {}
    resources = data.get("resources")
    if not isinstance(resources, dict):
        return {}
    return dict(resources)


def _source_relative(marker_path: Path, root: Path) -> str:
    """Return the marker file's directory as a relative path from *root*."""
    rel = marker_path.parent.relative_to(root)
    return str(rel) if str(rel) != "." else "."


def _snap_source_relative(marker_path: Path, root: Path) -> str:
    """Return the snap project root relative to *root*.

    Snapcraft supports ``snapcraft.yaml`` in the project root **or** under
    a ``snap/`` subdirectory.  When the marker lives at ``*/snap/snapcraft.yaml``
    the project root (where ``snapcraft pack`` should run) is the parent of
    ``snap/``.
    """
    if marker_path.parent.name == "snap":
        rel = marker_path.parent.parent.relative_to(root)
    else:
        rel = marker_path.parent.relative_to(root)
    return str(rel) if str(rel) != "." else "."


def _process_marker(
    path: Path,
    root: Path,
    rocks: list[RockArtifact],
    snaps: list[SnapArtifact],
    charm_raw: list[tuple[str, str, dict[str, dict[str, Any]]]],
) -> None:
    """Process a single marker file found during discovery."""
    kind = _MARKER_FILES[path.name]
    source = _source_relative(path, root)

    if kind == "rock":
        name = _read_yaml_name(path)
        rocks.append(RockArtifact(name=name, source=source))
    elif kind == "snap":
        name = _read_yaml_name(path)
        snap_source = _snap_source_relative(path, root)
        snaps.append(SnapArtifact(name=name, source=snap_source))
    elif kind == "charm":
        name = _read_yaml_name(path)
        raw_resources = _read_charm_resources(path)
        charm_raw.append((name, source, raw_resources))


def _link_charm_resources(
    charm_name: str,
    raw_resources: dict[str, dict[str, Any]],
    rock_names: set[str],
) -> dict[str, ArtifactResource]:
    """Build charm resources, auto-linking OCI images to rocks when unambiguous."""
    resources: dict[str, ArtifactResource] = {}
    for res_name, res_data in raw_resources.items():
        if not isinstance(res_data, dict):
            continue
        if res_data.get("type") != "oci-image":
            continue
        upstream = res_data.get("upstream-source", "")
        candidates = []
        if isinstance(upstream, str) and upstream in rock_names:
            candidates.append(upstream)
        if res_name in rock_names and res_name not in candidates:
            candidates.append(res_name)
        rock_ref = candidates[0] if len(candidates) == 1 else None
        if len(candidates) > 1:
            logger.warning(
                "Ambiguous rock match for resource '%s' in charm '%s'; "
                "skipping auto-link (candidates: %s)",
                res_name,
                charm_name,
                ", ".join(candidates),
            )
        resources[res_name] = ArtifactResource(type="oci-image", rock=rock_ref)
    return resources


def discover_artifacts(root: Path) -> ArtifactsPlan:
    """Walk *root* and discover charms, rocks, and snaps.

    The walk prunes common non-source directories and does not follow
    symlinked directories.  Charm resources of ``type: oci-image`` are
    linked to discovered rocks when the match is unambiguous.

    Raises:
        DiscoveryError: If a marker file cannot be read or is malformed.
    """
    root = root.resolve()
    rocks: list[RockArtifact] = []
    charms: list[CharmArtifact] = []
    snaps: list[SnapArtifact] = []

    charm_raw: list[tuple[str, str, dict[str, dict[str, Any]]]] = []

    for path in sorted(root.rglob("*")):
        if path.is_dir() and path.is_symlink():
            continue
        if path.is_dir() and path.name in _PRUNE_DIRS:
            continue
        if not path.is_file() or path.name not in _MARKER_FILES:
            continue
        if any(part in _PRUNE_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        _process_marker(path, root, rocks, snaps, charm_raw)

    rock_names = {r.name for r in rocks}
    for charm_name, charm_source, raw_resources in charm_raw:
        resources = _link_charm_resources(charm_name, raw_resources, rock_names)
        charms.append(
            CharmArtifact(name=charm_name, source=charm_source, resources=resources)
        )

    return ArtifactsPlan(rocks=rocks, charms=charms, snaps=snaps)
