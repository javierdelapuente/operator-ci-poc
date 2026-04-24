"""Pydantic models for artifacts-generated.yaml.

Extends the build plan with paths/references of the built artifacts.

Schema version history
----------------------
v1 — initial release (charm resources not included)
v2 — charm entries carry a ``resources`` mapping with resolved output paths,
     making ``artifacts-generated.yaml`` self-contained (no need to also read
     ``artifacts.yaml`` when assembling pytest flags).
v3 — ``source`` renamed to ``<tool>-yaml`` (explicit path to the craft YAML
     file) to align with the v2 ``artifacts.yaml`` schema change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactOutput(BaseModel):
    """Location of a built artifact — local file, OCI image, or GitHub artifact."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    file: str | None = None
    image: str | None = None
    artifact: str | None = None
    run_id: str | None = Field(default=None, alias="run-id")

    @model_validator(mode="after")
    def _at_least_one_output(self) -> ArtifactOutput:
        if not any([self.file, self.image, self.artifact]):
            msg = "ArtifactOutput must specify at least one of file, image, or artifact"
            raise ValueError(msg)
        if self.artifact and not self.run_id:
            msg = "run-id is required when artifact is set"
            raise ValueError(msg)
        return self


class GeneratedResource(BaseModel):
    """A charm resource with its resolved output path or image reference.

    The ``file`` and ``image`` fields mirror the linked rock's ``output.file``
    and ``output.image`` at build time.  Either may be ``None`` if the rock was
    not built in the same ``opcli artifacts build`` invocation.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["oci-image"]
    rock: str | None = None
    file: str | None = None
    image: str | None = None


class GeneratedRock(BaseModel):
    """A rock with its build output."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    rockcraft_yaml: str = Field(alias="rockcraft-yaml")
    output: ArtifactOutput


class GeneratedCharm(BaseModel):
    """A charm with its build output and resolved resource paths."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    charmcraft_yaml: str = Field(alias="charmcraft-yaml")
    output: ArtifactOutput
    resources: dict[str, GeneratedResource] | None = None


class GeneratedSnap(BaseModel):
    """A snap with its build output."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    snapcraft_yaml: str = Field(alias="snapcraft-yaml")
    output: ArtifactOutput


class ArtifactsGenerated(BaseModel):
    """Top-level schema for ``artifacts-generated.yaml`` (schema version 3).

    Version 3 renames ``source`` to ``<tool>-yaml`` in all artifact entries
    to align with the explicit yaml-file-path convention in ``artifacts.yaml``.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[3] = 3
    rocks: list[GeneratedRock] = []
    charms: list[GeneratedCharm] = []
    snaps: list[GeneratedSnap] = []
