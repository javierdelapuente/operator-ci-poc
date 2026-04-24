"""Pydantic models for artifacts.yaml.

This schema declares the charms, rocks, and snaps in a repository, and
the links between charms and their OCI-image resources (rocks).

Schema version history
----------------------
v1 — initial release (``source`` directory field)
v2 — ``source`` replaced by explicit ``<tool>-yaml`` file path; optional
     ``pack-dir`` added to run the build tool from a different directory
     (e.g. a Go monorepo where ``go.mod`` lives at the repo root but
     ``rockcraft.yaml`` lives in a subdirectory).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactResource(BaseModel):
    """A resource declared by a charm (e.g. an OCI image backed by a rock)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["oci-image"]
    rock: str | None = None


class CharmArtifact(BaseModel):
    """A charm declared in artifacts.yaml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    charmcraft_yaml: str = Field(alias="charmcraft-yaml")
    pack_dir: str | None = Field(default=None, alias="pack-dir")
    resources: dict[str, ArtifactResource] = {}


class RockArtifact(BaseModel):
    """A rock declared in artifacts.yaml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    rockcraft_yaml: str = Field(alias="rockcraft-yaml")
    pack_dir: str | None = Field(default=None, alias="pack-dir")


class SnapArtifact(BaseModel):
    """A snap declared in artifacts.yaml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    snapcraft_yaml: str = Field(alias="snapcraft-yaml")
    pack_dir: str | None = Field(default=None, alias="pack-dir")


class ArtifactsPlan(BaseModel):
    """Top-level schema for ``artifacts.yaml`` (schema version 2)."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    rocks: list[RockArtifact] = []
    charms: list[CharmArtifact] = []
    snaps: list[SnapArtifact] = []

    @model_validator(mode="after")
    def _unique_names(self) -> ArtifactsPlan:
        """Ensure no duplicate names within each artifact kind."""
        checks: list[tuple[str, list[str]]] = [
            ("rock", [r.name for r in self.rocks]),
            ("charm", [c.name for c in self.charms]),
            ("snap", [s.name for s in self.snaps]),
        ]
        for kind, names in checks:
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                msg = f"Duplicate {kind} name(s): {', '.join(sorted(dupes))}"
                raise ValueError(msg)
        return self
