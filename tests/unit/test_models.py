"""Tests for Pydantic models (artifacts.yaml and artifacts-generated.yaml)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from opcli.models.artifacts import (
    ArtifactResource,
    ArtifactsPlan,
    BuildTarget,
    CharmArtifact,
    RockArtifact,
)
from opcli.models.artifacts_generated import (
    ArtifactsGenerated,
    CharmArchBuild,
    CharmFile,
    GeneratedCharm,
    GeneratedRock,
    GeneratedSnap,
    RockArchBuild,
    SnapArchBuild,
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
                RockArtifact(
                    name="indico", rockcraft_yaml="indico_rock/rockcraft.yaml"
                ),
                RockArtifact(
                    name="indico-nginx", rockcraft_yaml="nginx_rock/rockcraft.yaml"
                ),
            ],
            charms=[
                CharmArtifact(
                    name="indico",
                    charmcraft_yaml="charmcraft.yaml",
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
                    RockArtifact(name="myrock", rockcraft_yaml="a/rockcraft.yaml"),
                    RockArtifact(name="myrock", rockcraft_yaml="b/rockcraft.yaml"),
                ]
            )

    def test_duplicate_charm_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate charm"):
            ArtifactsPlan(
                charms=[
                    CharmArtifact(name="mycharm", charmcraft_yaml="a/charmcraft.yaml"),
                    CharmArtifact(name="mycharm", charmcraft_yaml="b/charmcraft.yaml"),
                ]
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            ArtifactsPlan.model_validate({"version": 1, "unknown_key": "val"})

    def test_wrong_version_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactsPlan.model_validate({"version": 99})

    def test_resource_wrong_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactResource(type="file")  # type: ignore[arg-type]

    def test_from_yaml_dict(self) -> None:
        """Validate a dict that mimics YAML load output (hyphenated keys)."""
        data = {
            "version": 1,
            "rocks": [{"name": "myrock", "rockcraft-yaml": "rock_dir/rockcraft.yaml"}],
            "charms": [
                {
                    "name": "mycharm",
                    "charmcraft-yaml": "charmcraft.yaml",
                    "resources": {
                        "img": {"type": "oci-image", "rock": "myrock"},
                    },
                }
            ],
            "snaps": [
                {
                    "name": "mysnap",
                    "snapcraft-yaml": "snap/snapcraft.yaml",
                    "pack-dir": ".",
                }
            ],
        }
        plan = ArtifactsPlan.model_validate(data)
        assert plan.rocks[0].name == "myrock"
        assert plan.charms[0].resources["img"].rock == "myrock"
        assert plan.snaps[0].name == "mysnap"
        assert plan.snaps[0].pack_dir == "."

    def test_pack_dir_optional(self) -> None:
        rock = RockArtifact(name="myrock", rockcraft_yaml="rock/rockcraft.yaml")
        assert rock.pack_dir is None

    def test_pack_dir_set(self) -> None:
        rock = RockArtifact(
            name="myrock", rockcraft_yaml="rock/rockcraft.yaml", pack_dir="."
        )
        assert rock.pack_dir == "."

    def test_artifacts_yaml_serializes_with_hyphens(self) -> None:
        """dump_artifacts_plan must emit hyphenated keys, not underscored."""
        plan = ArtifactsPlan(
            rocks=[
                RockArtifact(name="r", rockcraft_yaml="r/rockcraft.yaml", pack_dir=".")
            ],
        )
        dumped = plan.model_dump(by_alias=True, exclude_none=True)
        rock = dumped["rocks"][0]
        assert "rockcraft-yaml" in rock
        assert "pack-dir" in rock
        assert "rockcraft_yaml" not in rock
        assert "pack_dir" not in rock


class TestArtifactsGenerated:
    """Validation tests for artifacts-generated.yaml models."""

    def test_local_output(self) -> None:
        out = RockArchBuild(arch="amd64", file="./myrock.rock")
        assert out.file == "./myrock.rock"
        assert out.image is None

    def test_ci_output_with_image(self) -> None:
        out = RockArchBuild(arch="amd64", image="ghcr.io/canonical/indico:abc1234")
        assert out.image is not None

    def test_ci_output_artifact_requires_run_id(self) -> None:
        with pytest.raises(ValidationError, match="run-id is required"):
            CharmArchBuild(arch="amd64", artifact="charm-indico")

    def test_ci_output_artifact_with_run_id(self) -> None:
        out = CharmArchBuild(arch="amd64", artifact="charm-indico", run_id="123456")
        assert out.artifact == "charm-indico"
        assert out.run_id == "123456"

    def test_empty_output_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must specify file or image"):
            RockArchBuild(arch="amd64")

    def test_run_id_alias_from_yaml(self) -> None:
        """YAML uses ``run-id`` (hyphen), Python uses ``run_id``."""
        data = {"arch": "amd64", "artifact": "charm-x", "run-id": "999"}
        out = CharmArchBuild.model_validate(data)
        assert out.run_id == "999"

    def test_run_id_serialized_with_hyphen(self) -> None:
        out = CharmArchBuild(arch="amd64", artifact="charm-x", run_id="999")
        dumped = out.model_dump(by_alias=True, exclude_none=True)
        assert "run-id" in dumped
        assert "run_id" not in dumped

    def test_full_generated_example(self) -> None:
        gen = ArtifactsGenerated(
            rocks=[
                GeneratedRock(
                    name="indico",
                    rockcraft_yaml="indico_rock/rockcraft.yaml",
                    output=[RockArchBuild(arch="amd64", file="./indico.rock")],
                )
            ],
            charms=[
                GeneratedCharm(
                    name="indico",
                    charmcraft_yaml="charmcraft.yaml",
                    output=[
                        CharmArchBuild(
                            arch="amd64",
                            files=[
                                CharmFile(path="./indico.charm", base="ubuntu@22.04")
                            ],
                        )
                    ],
                )
            ],
        )
        assert gen.rocks[0].output[0].file == "./indico.rock"
        assert gen.charms[0].output[0].files[0].path == "./indico.charm"

    def test_generated_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            ArtifactsGenerated.model_validate({"version": 99, "junk": True})

    def test_snap_generated(self) -> None:
        gen = ArtifactsGenerated(
            snaps=[
                GeneratedSnap(
                    name="mysnap",
                    snapcraft_yaml="snap/snapcraft.yaml",
                    output=[SnapArchBuild(arch="amd64", file="./mysnap.snap")],
                )
            ],
        )
        assert gen.snaps[0].name == "mysnap"


class TestCharmArchBuild:
    """Validation tests for CharmArchBuild and CharmFile models."""

    def test_local_single_base(self) -> None:
        out = CharmArchBuild(
            arch="amd64",
            files=[
                CharmFile(path="./aproxy_ubuntu-22.04-amd64.charm", base="ubuntu@22.04")
            ],
        )
        assert len(out.files) == 1
        assert out.files[0].base == "ubuntu@22.04"

    def test_local_multi_base(self) -> None:
        out = CharmArchBuild(
            arch="amd64",
            files=[
                CharmFile(path="./a_ubuntu-20.04-amd64.charm", base="ubuntu@20.04"),
                CharmFile(path="./a_ubuntu-22.04-amd64.charm", base="ubuntu@22.04"),
            ],
        )
        expected_count = 2
        assert len(out.files) == expected_count

    def test_ci_artifact_output(self) -> None:
        out = CharmArchBuild(arch="amd64", artifact="charm-aproxy", run_id="999")
        assert out.artifact == "charm-aproxy"
        assert out.run_id == "999"
        assert out.files == []

    def test_ci_artifact_yaml_alias(self) -> None:
        out = CharmArchBuild.model_validate(
            {"arch": "amd64", "artifact": "charm-x", "run-id": "1"}
        )
        assert out.run_id == "1"

    def test_empty_output_rejected(self) -> None:
        with pytest.raises(ValidationError, match="files or artifact"):
            CharmArchBuild(arch="amd64")

    def test_artifact_without_run_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="run-id"):
            CharmArchBuild(arch="amd64", artifact="charm-x")

    def test_base_none_allowed(self) -> None:
        """Base can be None when filename does not follow convention."""
        out = CharmArchBuild(arch="amd64", files=[CharmFile(path="./mycharm.charm")])
        assert out.files[0].base is None


class TestBuildTarget:
    """Validation tests for BuildTarget model."""

    def test_build_target_defaults_no_runner(self) -> None:
        bt = BuildTarget(arch="amd64")
        assert bt.runner is None

    def test_build_target_with_runner(self) -> None:
        bt = BuildTarget(arch="amd64", runner=["ubuntu-latest"])
        assert bt.runner == ["ubuntu-latest"]


class TestRockArchBuild:
    """Validation tests for RockArchBuild model."""

    def test_rock_local_build(self) -> None:
        build = RockArchBuild(arch="amd64", file="./my.rock")
        assert build.arch == "amd64"
        assert build.file == "./my.rock"
        assert build.image is None

    def test_rock_ci_build(self) -> None:
        build = RockArchBuild(arch="amd64", image="ghcr.io/canonical/my-rock:abc123")
        assert build.image == "ghcr.io/canonical/my-rock:abc123"
        assert build.file is None

    def test_rock_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RockArchBuild(arch="amd64")

    def test_rock_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            RockArchBuild.model_validate(
                {"arch": "amd64", "file": "./my.rock", "unknown": "x"}
            )


class TestSnapArchBuild:
    """Validation tests for SnapArchBuild model."""

    def test_snap_local_build(self) -> None:
        build = SnapArchBuild(arch="amd64", file="./my.snap")
        assert build.arch == "amd64"
        assert build.file == "./my.snap"
        assert build.artifact is None

    def test_snap_ci_build(self) -> None:
        build = SnapArchBuild(
            arch="amd64", artifact="built-snap-my-snap-amd64", run_id="123"
        )
        assert build.artifact == "built-snap-my-snap-amd64"
        assert build.run_id == "123"

    def test_snap_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SnapArchBuild(arch="amd64")

    def test_snap_artifact_without_run_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="run-id"):
            SnapArchBuild(arch="amd64", artifact="built-snap-my-snap")
