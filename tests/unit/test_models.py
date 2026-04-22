"""Tests for Pydantic models (artifacts.yaml and artifacts-generated.yaml)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from opcli.models.artifacts import (
    ArtifactResource,
    ArtifactsPlan,
    CharmArtifact,
    RockArtifact,
)
from opcli.models.artifacts_generated import (
    ArtifactOutput,
    ArtifactsGenerated,
    GeneratedCharm,
    GeneratedRock,
    GeneratedSnap,
)


class TestArtifactsPlan:
    """Validation tests for artifacts.yaml models."""

    def test_minimal_valid(self) -> None:
        plan = ArtifactsPlan()
        assert plan.version == 1
        assert plan.rocks == []
        assert plan.charms == []
        assert plan.snaps == []

    def test_full_example(self) -> None:
        plan = ArtifactsPlan(
            rocks=[
                RockArtifact(name="indico", source="indico_rock"),
                RockArtifact(name="indico-nginx", source="nginx_rock"),
            ],
            charms=[
                CharmArtifact(
                    name="indico",
                    source=".",
                    resources={
                        "indico-image": ArtifactResource(
                            type="oci-image", rock="indico"
                        ),
                    },
                ),
            ],
        )
        assert len(plan.rocks) == 2  # noqa: PLR2004
        assert len(plan.charms) == 1
        assert plan.charms[0].resources["indico-image"].rock == "indico"

    def test_duplicate_rock_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate rock"):
            ArtifactsPlan(
                rocks=[
                    RockArtifact(name="myrock", source="a"),
                    RockArtifact(name="myrock", source="b"),
                ]
            )

    def test_duplicate_charm_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate charm"):
            ArtifactsPlan(
                charms=[
                    CharmArtifact(name="mycharm", source="a"),
                    CharmArtifact(name="mycharm", source="b"),
                ]
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            ArtifactsPlan.model_validate({"version": 1, "unknown_key": "val"})

    def test_wrong_version_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactsPlan.model_validate({"version": 2})

    def test_resource_wrong_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactResource(type="file")  # type: ignore[arg-type]

    def test_from_yaml_dict(self) -> None:
        """Validate a dict that mimics YAML load output."""
        data = {
            "version": 1,
            "rocks": [{"name": "myrock", "source": "rock_dir"}],
            "charms": [
                {
                    "name": "mycharm",
                    "source": ".",
                    "resources": {
                        "img": {"type": "oci-image", "rock": "myrock"},
                    },
                }
            ],
            "snaps": [{"name": "mysnap", "source": "snap_dir"}],
        }
        plan = ArtifactsPlan.model_validate(data)
        assert plan.rocks[0].name == "myrock"
        assert plan.charms[0].resources["img"].rock == "myrock"
        assert plan.snaps[0].name == "mysnap"


class TestArtifactsGenerated:
    """Validation tests for artifacts-generated.yaml models."""

    def test_local_output(self) -> None:
        out = ArtifactOutput(file="./myrock.rock")
        assert out.file == "./myrock.rock"
        assert out.image is None

    def test_ci_output_with_image(self) -> None:
        out = ArtifactOutput(image="ghcr.io/canonical/indico:abc1234")
        assert out.image is not None

    def test_ci_output_artifact_requires_run_id(self) -> None:
        with pytest.raises(ValidationError, match="run-id is required"):
            ArtifactOutput(artifact="charm-indico")

    def test_ci_output_artifact_with_run_id(self) -> None:
        out = ArtifactOutput(artifact="charm-indico", run_id="123456")
        assert out.artifact == "charm-indico"
        assert out.run_id == "123456"

    def test_empty_output_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one"):
            ArtifactOutput()

    def test_run_id_alias_from_yaml(self) -> None:
        """YAML uses ``run-id`` (hyphen), Python uses ``run_id``."""
        data = {"artifact": "charm-x", "run-id": "999"}
        out = ArtifactOutput.model_validate(data)
        assert out.run_id == "999"

    def test_run_id_serialized_with_hyphen(self) -> None:
        out = ArtifactOutput(artifact="charm-x", run_id="999")
        dumped = out.model_dump(by_alias=True, exclude_none=True)
        assert "run-id" in dumped
        assert "run_id" not in dumped

    def test_full_generated_example(self) -> None:
        gen = ArtifactsGenerated(
            rocks=[
                GeneratedRock(
                    name="indico",
                    source="indico_rock",
                    output=ArtifactOutput(file="./indico.rock"),
                )
            ],
            charms=[
                GeneratedCharm(
                    name="indico",
                    source=".",
                    output=ArtifactOutput(file="./indico.charm"),
                )
            ],
        )
        assert gen.rocks[0].output.file == "./indico.rock"
        assert gen.charms[0].output.file == "./indico.charm"

    def test_generated_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            ArtifactsGenerated.model_validate({"version": 1, "junk": True})

    def test_snap_generated(self) -> None:
        gen = ArtifactsGenerated(
            snaps=[
                GeneratedSnap(
                    name="mysnap",
                    source="snap_dir",
                    output=ArtifactOutput(file="./mysnap.snap"),
                )
            ],
        )
        assert gen.snaps[0].name == "mysnap"
