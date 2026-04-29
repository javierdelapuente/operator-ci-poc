"""Pydantic models for artifacts-generated.yaml.

Extends the build plan with paths/references of the built artifacts.

Schema version: 1
- Each artifact carries an explicit path to its craft YAML file
  (``rockcraft-yaml``, ``charmcraft-yaml``, ``snapcraft-yaml``).
- Charm entries include a ``resources`` mapping with resolved output paths
  (image reference), making ``artifacts-generated.yaml`` self-contained for
  pytest flag assembly without needing to also read ``artifacts.yaml``.
- ``output`` is always a **list of per-architecture builds** (:class:`RockArchBuild`,
  :class:`CharmArchBuild`, or :class:`SnapArchBuild`), one entry per built arch.
  Local builds produce ``file`` / ``files`` entries; CI builds produce ``image``
  (rocks) or ``artifact`` + ``run-id`` (charms / snaps) entries.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CharmFile(BaseModel):
    """A single charm file with its local path and optional base annotation.

    The ``base`` field is parsed from the charmcraft output filename on a
    best-effort basis (e.g. ``aproxy_ubuntu-22.04-amd64.charm`` →
    ``ubuntu@22.04``).  It is ``None`` when the filename does not follow the
    expected ``{name}_{distro}-{version}-{arch}.charm`` convention.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    base: str | None = None


class RockArchBuild(BaseModel):
    """Rock output for a specific architecture.

    Local builds populate ``file``; CI builds populate ``image``.
    """

    model_config = ConfigDict(extra="forbid")

    arch: str
    file: str | None = None
    image: str | None = None

    @model_validator(mode="after")
    def _at_least_one_output(self) -> RockArchBuild:
        if not self.file and not self.image:
            msg = "RockArchBuild must specify file or image"
            raise ValueError(msg)
        return self


class CharmArchBuild(BaseModel):
    """Charm output for a specific architecture.

    Local builds populate ``files`` (one :class:`CharmFile` per built base).
    CI builds populate ``artifact`` and ``run-id`` instead.
    Localised CI builds (after ``artifacts localize``) populate both.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    arch: str
    files: list[CharmFile] = []
    artifact: str | None = None
    run_id: str | None = Field(default=None, alias="run-id")

    @model_validator(mode="after")
    def _at_least_one_output(self) -> CharmArchBuild:
        if not self.files and not self.artifact:
            msg = "CharmArchBuild must specify files or artifact"
            raise ValueError(msg)
        if self.artifact and not self.run_id:
            msg = "run-id is required when artifact is set"
            raise ValueError(msg)
        return self


class SnapArchBuild(BaseModel):
    """Snap output for a specific architecture.

    Local builds populate ``file``; CI builds populate ``artifact`` + ``run-id``.
    Localised CI builds (after ``artifacts localize``) populate both.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    arch: str
    file: str | None = None
    artifact: str | None = None
    run_id: str | None = Field(default=None, alias="run-id")

    @model_validator(mode="after")
    def _at_least_one_output(self) -> SnapArchBuild:
        if not self.file and not self.artifact:
            msg = "SnapArchBuild must specify file or artifact"
            raise ValueError(msg)
        if self.artifact and not self.run_id:
            msg = "run-id is required when artifact is set"
            raise ValueError(msg)
        return self


class GeneratedResource(BaseModel):
    """A charm OCI-image resource with a reference to its backing rock.

    The actual image location is resolved by looking up the linked rock in
    ``ArtifactsGenerated.rocks`` — it is not duplicated here.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["oci-image"]
    rock: str | None = None


class GeneratedRock(BaseModel):
    """A rock with its per-architecture build output."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    rockcraft_yaml: str = Field(alias="rockcraft-yaml")
    output: list[RockArchBuild]


class GeneratedCharm(BaseModel):
    """A charm with its per-architecture build output and resolved resource paths."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    charmcraft_yaml: str = Field(alias="charmcraft-yaml")
    output: list[CharmArchBuild]
    resources: dict[str, GeneratedResource] | None = None


class GeneratedSnap(BaseModel):
    """A snap with its per-architecture build output."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    snapcraft_yaml: str = Field(alias="snapcraft-yaml")
    output: list[SnapArchBuild]


class ArtifactsGenerated(BaseModel):
    """Top-level schema for ``artifacts-generated.yaml`` (schema version 1)."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    rocks: list[GeneratedRock] = []
    charms: list[GeneratedCharm] = []
    snaps: list[GeneratedSnap] = []
