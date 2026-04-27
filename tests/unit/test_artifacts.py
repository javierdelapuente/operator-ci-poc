"""Tests for ``opcli artifacts init`` and ``opcli artifacts build``."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.artifacts import artifacts_build, artifacts_init
from opcli.core.exceptions import ConfigurationError, OpcliError
from opcli.core.yaml_io import load_artifacts_generated, load_artifacts_plan


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestArtifactsInit:
    """Tests for artifacts_init()."""

    def test_generates_artifacts_yaml(self, tmp_path: Path) -> None:
        _write(tmp_path / "charmcraft.yaml", "name: mycharm\ntype: charm\n")
        _write(tmp_path / "rock_dir" / "rockcraft.yaml", "name: myrock\n")

        result = artifacts_init(tmp_path)

        assert result == tmp_path / "artifacts.yaml"
        assert result.exists()
        plan = load_artifacts_plan(result)
        assert len(plan.charms) == 1
        assert len(plan.rocks) == 1
        assert plan.charms[0].name == "mycharm"
        assert plan.rocks[0].name == "myrock"

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.yaml", "version: 1\n")

        with pytest.raises(ConfigurationError, match="already exists"):
            artifacts_init(tmp_path)

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.yaml", "version: 1\n")
        _write(tmp_path / "charmcraft.yaml", "name: new-charm\ntype: charm\n")

        result = artifacts_init(tmp_path, force=True)

        plan = load_artifacts_plan(result)
        assert plan.charms[0].name == "new-charm"

    def test_empty_repo(self, tmp_path: Path) -> None:
        result = artifacts_init(tmp_path)
        plan = load_artifacts_plan(result)
        assert plan.charms == []
        assert plan.rocks == []
        assert plan.snaps == []


class TestArtifactsBuild:
    """Tests for artifacts_build()."""

    def test_missing_artifacts_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            artifacts_build(tmp_path)

    def test_build_single_charm(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n"
            "  charmcraft-yaml: charmcraft.yaml\n",
        )
        _write(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        # Simulate charmcraft pack producing a .charm file
        _write(tmp_path / "mycharm_amd64.charm", "fake charm")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            result = artifacts_build(tmp_path)

        mock_run.assert_called_once()
        assert "charmcraft" in mock_run.call_args[0][0]
        gen = load_artifacts_generated(result)
        assert len(gen.charms) == 1
        assert gen.charms[0].name == "mycharm"
        assert len(gen.charms[0].output.files) == 1
        assert gen.charms[0].output.files[0].path.startswith("./")
        assert gen.charms[0].output.files[0].path.endswith(".charm")

    def test_build_multi_base_charm(self, tmp_path: Path) -> None:
        """Multi-base charm: all produced files appear in output.files."""
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: aproxy\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        _write(tmp_path / "charmcraft.yaml", "name: aproxy\n")
        # Simulate charmcraft pack producing three .charm files (one per base)
        _write(tmp_path / "aproxy_ubuntu-20.04-amd64.charm", "fake")
        _write(tmp_path / "aproxy_ubuntu-22.04-amd64.charm", "fake")
        _write(tmp_path / "aproxy_ubuntu-24.04-amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_generated(result)
        files = gen.charms[0].output.files
        expected_count = 3
        assert len(files) == expected_count
        paths = {f.path for f in files}
        assert "./aproxy_ubuntu-20.04-amd64.charm" in paths
        assert "./aproxy_ubuntu-22.04-amd64.charm" in paths
        assert "./aproxy_ubuntu-24.04-amd64.charm" in paths
        bases = {f.base for f in files}
        assert "ubuntu@20.04" in bases
        assert "ubuntu@22.04" in bases
        assert "ubuntu@24.04" in bases

    def test_build_multi_base_charm_incremental(self, tmp_path: Path) -> None:
        """Adding a new base: pre-existing files + new file all appear in output.

        charmcraft pack always rebuilds all declared bases, so after adding a
        base we must return all files in the output directory, not just new ones.
        """
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: aproxy\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        _write(tmp_path / "charmcraft.yaml", "name: aproxy\n")
        # Pre-existing file from a previous single-base build
        _write(tmp_path / "aproxy_ubuntu-20.04-amd64.charm", "old")
        # charmcraft pack rebuilds ubuntu-20.04 AND produces ubuntu-22.04
        # (simulated: file already existed before, pack just overwrites)
        _write(tmp_path / "aproxy_ubuntu-22.04-amd64.charm", "new")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_generated(result)
        files = gen.charms[0].output.files
        expected_count = 2
        assert len(files) == expected_count
        paths = {f.path for f in files}
        assert "./aproxy_ubuntu-20.04-amd64.charm" in paths
        assert "./aproxy_ubuntu-22.04-amd64.charm" in paths

    def test_build_single_rock(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n"
            "  rockcraft-yaml: rock_dir/rockcraft.yaml\n",
        )
        rock_dir = tmp_path / "rock_dir"
        rock_dir.mkdir()
        _write(rock_dir / "rockcraft.yaml", "name: myrock\n")
        _write(rock_dir / "myrock_1.0_amd64.rock", "fake rock")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            result = artifacts_build(tmp_path)

        mock_run.assert_called_once()
        assert "rockcraft" in mock_run.call_args[0][0]
        gen = load_artifacts_generated(result)
        assert len(gen.rocks) == 1
        assert gen.rocks[0].output.file is not None
        assert gen.rocks[0].output.file.startswith("./")

    def test_build_rock_sets_experimental_extensions_env(self, tmp_path: Path) -> None:
        """rockcraft pack must always pass ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS."""
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n"
            "  rockcraft-yaml: rock_dir/rockcraft.yaml\n",
        )
        rock_dir = tmp_path / "rock_dir"
        rock_dir.mkdir()
        _write(rock_dir / "rockcraft.yaml", "name: myrock\n")
        _write(rock_dir / "myrock_1.0_amd64.rock", "fake rock")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            artifacts_build(tmp_path)

        env_kwarg = mock_run.call_args.kwargs.get("env")
        assert env_kwarg is not None
        assert env_kwarg.get("ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS") == "1"

    def test_build_single_snap(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nsnaps:\n- name: mysnap\n"
            "  snapcraft-yaml: snap_dir/snapcraft.yaml\n",
        )
        snap_dir = tmp_path / "snap_dir"
        snap_dir.mkdir()
        _write(snap_dir / "snapcraft.yaml", "name: mysnap\n")
        _write(snap_dir / "mysnap_1.0_amd64.snap", "fake snap")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            result = artifacts_build(tmp_path)

        mock_run.assert_called_once()
        assert "snapcraft" in mock_run.call_args[0][0]
        gen = load_artifacts_generated(result)
        assert len(gen.snaps) == 1

    def test_build_filtered_by_charm_name(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n"
            "- name: charm-a\n  charmcraft-yaml: a/charmcraft.yaml\n"
            "- name: charm-b\n  charmcraft-yaml: b/charmcraft.yaml\n",
        )
        (tmp_path / "a").mkdir()
        _write(tmp_path / "a" / "charmcraft.yaml", "name: charm-a\n")
        _write(tmp_path / "a" / "charm-a.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path, charm_names=["charm-a"])

        gen = load_artifacts_generated(result)
        assert len(gen.charms) == 1
        assert gen.charms[0].name == "charm-a"

    def test_unknown_charm_name_raises(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: real\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        with pytest.raises(ConfigurationError, match="Unknown charm"):
            artifacts_build(tmp_path, charm_names=["nonexistent"])

    def test_no_output_file_raises(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n"
            "  rockcraft-yaml: rock_dir/rockcraft.yaml\n",
        )
        rock_dir = tmp_path / "rock_dir"
        rock_dir.mkdir()
        _write(rock_dir / "rockcraft.yaml", "name: myrock\n")

        with (
            patch("opcli.core.artifacts.run_command"),
            pytest.raises(OpcliError, match=r"No \*.rock found"),
        ):
            artifacts_build(tmp_path)

    def test_build_monorepo(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "charms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n",
        )
        (tmp_path / "rock_dir").mkdir()
        _write(tmp_path / "rock_dir" / "rockcraft.yaml", "name: myrock\n")
        _write(tmp_path / "rock_dir" / "myrock.rock", "fake")
        _write(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        _write(tmp_path / "mycharm.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_generated(result)
        assert len(gen.rocks) == 1
        assert len(gen.charms) == 1

    def test_build_propagates_resources_to_generated(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "charms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  resources:\n"
            "    myrock-image:\n"
            "      type: oci-image\n"
            "      rock: myrock\n",
        )
        (tmp_path / "rock_dir").mkdir()
        _write(tmp_path / "rock_dir" / "rockcraft.yaml", "name: myrock\n")
        _write(tmp_path / "rock_dir" / "myrock_1.0_amd64.rock", "fake")
        _write(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        _write(tmp_path / "mycharm_amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_generated(result)
        charm = gen.charms[0]
        assert charm.resources is not None
        assert "myrock-image" in charm.resources
        res = charm.resources["myrock-image"]
        assert res.type == "oci-image"
        assert res.rock == "myrock"
        assert res.file is not None
        assert res.file.startswith("./")
        assert "myrock_1.0_amd64.rock" in res.file

    def test_resource_unresolved_when_rock_not_built(self, tmp_path: Path) -> None:
        """Resource referencing a rock not in artifacts.yaml has unresolved output."""
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "charms:\n- name: mycharm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  resources:\n"
            "    myrock-image:\n"
            "      type: oci-image\n"
            "      rock: nonexistent-rock\n",
        )
        _write(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        _write(tmp_path / "mycharm_amd64.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_generated(result)
        charm = gen.charms[0]
        assert charm.resources is not None
        res = charm.resources["myrock-image"]
        assert res.file is None
        assert res.image is None

    def test_invalid_generated_fields_rejected(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\ncharms:\n- name: c\n  source: .\n"
            "  output:\n    file: ./c.charm\n",
        )
        with pytest.raises(Exception, match="validation error"):
            load_artifacts_generated(tmp_path / "artifacts-generated.yaml")

    def test_build_rock_with_pack_dir_creates_symlink(self, tmp_path: Path) -> None:
        """pack-dir: a temporary rockcraft.yaml symlink is created and removed."""
        rock_subdir = tmp_path / "rocks" / "myrock"
        rock_subdir.mkdir(parents=True)
        _write(rock_subdir / "rockcraft.yaml", "name: myrock\n")
        # The .rock output lands in pack_dir (repo root), not rock_subdir
        _write(tmp_path / "myrock_1.0_amd64.rock", "fake rock")

        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n"
            "  rockcraft-yaml: rocks/myrock/rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        created_symlinks: list[Path] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            # Verify symlink exists during the build
            symlink = tmp_path / "rockcraft.yaml"
            assert symlink.is_symlink(), "symlink should exist while pack runs"
            created_symlinks.append(symlink)

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            result = artifacts_build(tmp_path)

        # Symlink must be removed after build
        assert not (tmp_path / "rockcraft.yaml").exists()
        gen = load_artifacts_generated(result)
        assert gen.rocks[0].output.file is not None

    def test_build_rock_pack_dir_real_file_raises(self, tmp_path: Path) -> None:
        """A real rockcraft.yaml at pack-dir raises ConfigurationError."""
        rock_subdir = tmp_path / "rocks" / "myrock"
        rock_subdir.mkdir(parents=True)
        _write(rock_subdir / "rockcraft.yaml", "name: myrock\n")
        # Real file at the pack-dir location (not a symlink)
        _write(tmp_path / "rockcraft.yaml", "name: other\n")

        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n"
            "  rockcraft-yaml: rocks/myrock/rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        with (
            patch("opcli.core.artifacts.run_command"),
            pytest.raises(ConfigurationError, match="regular file already exists"),
        ):
            artifacts_build(tmp_path)

    def test_build_rock_pack_dir_existing_symlink_replaced(
        self, tmp_path: Path
    ) -> None:
        """An existing symlink at pack-dir is replaced without error."""
        rock_subdir = tmp_path / "rocks" / "myrock"
        rock_subdir.mkdir(parents=True)
        _write(rock_subdir / "rockcraft.yaml", "name: myrock\n")
        _write(tmp_path / "myrock_1.0_amd64.rock", "fake rock")

        # Pre-existing symlink pointing somewhere else
        existing_symlink = tmp_path / "rockcraft.yaml"
        existing_symlink.symlink_to("/dev/null")

        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n"
            "  rockcraft-yaml: rocks/myrock/rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        # Symlink removed after build
        assert not existing_symlink.exists()
        gen = load_artifacts_generated(result)
        assert gen.rocks[0].output.file is not None

    def test_build_rock_nonstandard_yaml_name_creates_symlink(
        self, tmp_path: Path
    ) -> None:
        """Non-standard yaml name (e.g. planner-rockcraft.yaml) always gets a symlink.

        Even when pack-dir == dirname(yaml), rockcraft needs a file named
        'rockcraft.yaml'. A non-standard name must be symlinked.
        """
        _write(tmp_path / "planner-rockcraft.yaml", "name: planner\n")
        _write(tmp_path / "planner_1.0_amd64.rock", "fake rock")

        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: planner\n"
            "  rockcraft-yaml: planner-rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        symlink_seen: list[bool] = []
        symlink_target: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            symlink = tmp_path / "rockcraft.yaml"
            symlink_seen.append(symlink.is_symlink())
            if symlink.is_symlink():
                symlink_target.append(str(os.readlink(symlink)))

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            result = artifacts_build(tmp_path)

        assert symlink_seen == [True], "symlink must exist while pack runs"
        assert symlink_target == ["planner-rockcraft.yaml"], (
            "symlink target must be relative"
        )
        assert not (tmp_path / "rockcraft.yaml").exists(), "symlink removed after build"
        gen = load_artifacts_generated(result)
        assert gen.rocks[0].output.file is not None

    def test_build_rock_standard_yaml_name_no_symlink(self, tmp_path: Path) -> None:
        """When yaml is already named rockcraft.yaml in pack-dir, no symlink needed."""
        _write(tmp_path / "rockcraft.yaml", "name: myrock\n")
        _write(tmp_path / "myrock_1.0_amd64.rock", "fake rock")

        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n"
            "  rockcraft-yaml: rockcraft.yaml\n"
            "  pack-dir: .\n",
        )

        symlink_created: list[bool] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            symlink = tmp_path / "rockcraft.yaml"
            symlink_created.append(symlink.is_symlink())

        with patch("opcli.core.artifacts.run_command", side_effect=fake_run):
            artifacts_build(tmp_path)

        assert symlink_created == [False], "no symlink should be created"

    def test_build_missing_yaml_raises(self, tmp_path: Path) -> None:
        """Missing yaml file raises ConfigurationError before running pack."""
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n"
            "  rockcraft-yaml: nonexistent/rockcraft.yaml\n",
        )

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            pytest.raises(ConfigurationError, match="rockcraft-yaml not found"),
        ):
            artifacts_build(tmp_path)

        mock_run.assert_not_called()

    def test_build_missing_pack_dir_raises(self, tmp_path: Path) -> None:
        """Missing pack-dir raises ConfigurationError before running pack."""
        _write(tmp_path / "rockcraft.yaml", "name: myrock\n")
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n"
            "  rockcraft-yaml: rockcraft.yaml\n"
            "  pack-dir: nonexistent-dir\n",
        )

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            pytest.raises(ConfigurationError, match="pack-dir not found"),
        ):
            artifacts_build(tmp_path)

        mock_run.assert_not_called()

    def test_pick_new_charm_output_overwrite_in_place_multi(
        self, tmp_path: Path
    ) -> None:
        """Overwrite-in-place with multiple pre-existing charm files returns all.

        This is the multi-base scenario: charmcraft pack rebuilds the same set of
        files in-place (no new files appear). All pre-existing files are returned
        since they were all just rebuilt.
        """
        _write(tmp_path / "charmcraft.yaml", "name: mycharm\n")
        _write(tmp_path / "mycharm_ubuntu-22.04-amd64.charm", "old1")
        _write(tmp_path / "mycharm_ubuntu-24.04-amd64.charm", "old2")

        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: mycharm\n"
            "  charmcraft-yaml: charmcraft.yaml\n",
        )

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_generated(result)
        files = gen.charms[0].output.files
        expected_count = 2
        assert len(files) == expected_count
        paths = {f.path for f in files}
        assert "./mycharm_ubuntu-22.04-amd64.charm" in paths
        assert "./mycharm_ubuntu-24.04-amd64.charm" in paths
