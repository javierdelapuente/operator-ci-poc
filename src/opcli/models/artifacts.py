"""Pydantic models for artifacts.yaml.

This schema declares the charms, rocks, and snaps in a repository, and
the links between charms and their OCI-image resources (rocks).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class ArtifactResource(BaseModel):
    """A resource declared by a charm (e.g. an OCI image backed by a rock)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["oci-image"]
    rock: str | None = None


class CharmArtifact(BaseModel):
    """A charm declared in artifacts.yaml."""

    model_config = ConfigDict(extra="forbid")

    name: str
    source: str
    resources: dict[str, ArtifactResource] = {}


class RockArtifact(BaseModel):
    """A rock declared in artifacts.yaml."""

    model_config = ConfigDict(extra="forbid")

    name: str
    source: str


class SnapArtifact(BaseModel):
    """A snap declared in artifacts.yaml."""

    model_config = ConfigDict(extra="forbid")

    name: str
    source: str


class ArtifactsPlan(BaseModel):
    """Top-level schema for ``artifacts.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
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
