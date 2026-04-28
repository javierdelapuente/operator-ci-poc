"""Tests for ``opcli artifacts init``, ``build``, ``matrix``, and ``collect``."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from opcli.core.artifacts import (
    artifacts_build,
    artifacts_collect,
    artifacts_init,
    artifacts_localize,
    artifacts_matrix,
)
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

    def test_charm_filter_does_not_build_rocks(self, tmp_path: Path) -> None:
        """--charm only must not build rocks (each matrix job builds one artifact)."""
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "charms:\n- name: mycharm\n  charmcraft-yaml: charm_dir/charmcraft.yaml\n",
        )
        (tmp_path / "charm_dir").mkdir()
        _write(tmp_path / "charm_dir" / "charmcraft.yaml", "name: mycharm\n")
        _write(tmp_path / "charm_dir" / "mycharm.charm", "fake")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            artifacts_build(tmp_path, charm_names=["mycharm"])

        # Only charmcraft should have been called — rockcraft must not be invoked.
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "rockcraft" not in cmd[0], f"rockcraft unexpectedly invoked: {cmd}"

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

    def test_resource_only_carries_rock_link(self, tmp_path: Path) -> None:
        """Resource referencing a rock only stores type + rock; no file/image."""
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
        assert res.type == "oci-image"
        assert res.rock == "nonexistent-rock"

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


class TestArtifactsMatrix:
    """Tests for artifacts_matrix()."""

    def test_returns_include_list_for_all_types(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "snaps:\n- name: my-snap\n  snapcraft-yaml: snap/snapcraft.yaml\n",
        )

        result = artifacts_matrix(tmp_path)

        assert result == {
            "include": [
                {"name": "my-rock", "type": "rock"},
                {"name": "my-charm", "type": "charm"},
                {"name": "my-snap", "type": "snap"},
            ]
        }

    def test_only_charms_no_rocks_no_snaps(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n",
        )

        result = artifacts_matrix(tmp_path)

        assert result == {"include": [{"name": "my-charm", "type": "charm"}]}

    def test_empty_artifacts_yaml_returns_empty_include(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.yaml", "version: 1\n")

        result = artifacts_matrix(tmp_path)

        assert result == {"include": []}

    def test_missing_artifacts_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match=r"artifacts\.yaml"):
            artifacts_matrix(tmp_path)

    def test_result_is_json_serializable(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n",
        )

        result = artifacts_matrix(tmp_path)

        serialized = json.dumps(result)
        assert json.loads(serialized) == result


class TestArtifactsCollect:
    """Tests for artifacts_collect()."""

    def _partial(
        self,
        tmp_path: Path,
        name: str,
        content: str,
    ) -> Path:
        p = tmp_path / name / "artifacts-generated.yaml"
        _write(p, content)
        return p

    def test_merges_rock_and_charm_partials(self, tmp_path: Path) -> None:
        rock_partial = self._partial(
            tmp_path,
            "rock-job",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  output:\n    file: ./my-rock_1.0_amd64.rock\n",
        )
        charm_partial = self._partial(
            tmp_path,
            "charm-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    files:\n"
            "    - path: ./my-charm_ubuntu-24.04-amd64.charm\n"
            "      base: ubuntu@24.04\n",
        )

        dest = tmp_path / "artifacts-generated.yaml"
        artifacts_collect(tmp_path, [rock_partial, charm_partial])

        gen = load_artifacts_generated(dest)
        assert len(gen.rocks) == 1
        assert len(gen.charms) == 1
        assert gen.rocks[0].name == "my-rock"
        assert gen.charms[0].name == "my-charm"

    def test_fills_charm_resource_from_merged_rock(self, tmp_path: Path) -> None:
        """Collect validates rock reference; image lives on rock, not resource."""
        rock_partial = self._partial(
            tmp_path,
            "rock-job",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  output:\n    file: ./my-rock_1.0_amd64.rock\n",
        )
        charm_partial = self._partial(
            tmp_path,
            "charm-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    files:\n"
            "    - path: ./my-charm_ubuntu-24.04-amd64.charm\n"
            "      base: ubuntu@24.04\n"
            "  resources:\n"
            "    my-rock-image:\n"
            "      type: oci-image\n"
            "      rock: my-rock\n",
        )

        artifacts_collect(tmp_path, [rock_partial, charm_partial])

        gen = load_artifacts_generated(tmp_path / "artifacts-generated.yaml")
        # Image lives on the rock, not on the resource
        assert gen.rocks[0].output.file == "./my-rock_1.0_amd64.rock"
        resource = gen.charms[0].resources["my-rock-image"]  # type: ignore[index]
        assert resource.rock == "my-rock"

    def test_merges_multiple_rocks(self, tmp_path: Path) -> None:
        rock1 = self._partial(
            tmp_path,
            "rock1-job",
            "version: 1\n"
            "rocks:\n- name: rock-a\n  rockcraft-yaml: rock-a/rockcraft.yaml\n"
            "  output:\n    file: ./rock-a_1.0_amd64.rock\n",
        )
        rock2 = self._partial(
            tmp_path,
            "rock2-job",
            "version: 1\n"
            "rocks:\n- name: rock-b\n  rockcraft-yaml: rock-b/rockcraft.yaml\n"
            "  output:\n    file: ./rock-b_1.0_amd64.rock\n",
        )

        artifacts_collect(tmp_path, [rock1, rock2])

        gen = load_artifacts_generated(tmp_path / "artifacts-generated.yaml")
        expected_count = 2
        assert len(gen.rocks) == expected_count
        names = {r.name for r in gen.rocks}
        assert names == {"rock-a", "rock-b"}

    def test_no_partials_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="partial"):
            artifacts_collect(tmp_path, [])

    def test_missing_partial_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            artifacts_collect(tmp_path, [tmp_path / "nonexistent.yaml"])

    def test_missing_rock_partial_raises(self, tmp_path: Path) -> None:
        """Charm references a rock that has no corresponding partial — must fail."""
        charm_partial = self._partial(
            tmp_path,
            "charm-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    files:\n"
            "    - path: ./my-charm_ubuntu-24.04-amd64.charm\n"
            "      base: ubuntu@24.04\n"
            "  resources:\n"
            "    missing-rock-image:\n"
            "      type: oci-image\n"
            "      rock: missing-rock\n",
        )

        with pytest.raises(ConfigurationError, match="missing-rock"):
            artifacts_collect(tmp_path, [charm_partial])

    def test_duplicate_rock_names_across_partials_raises(self, tmp_path: Path) -> None:
        """Two partials with the same rock name must be rejected."""
        rock1 = self._partial(
            tmp_path,
            "rock-job-1",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  output:\n    file: ./my-rock_1.0_amd64.rock\n",
        )
        rock2 = self._partial(
            tmp_path,
            "rock-job-2",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  output:\n    file: ./my-rock_2.0_amd64.rock\n",
        )

        with pytest.raises(ConfigurationError, match="my-rock"):
            artifacts_collect(tmp_path, [rock1, rock2])


class TestArtifactsBuildCIMode:
    """Tests for artifacts_build() GitHub Actions CI output format."""

    _CI_ENV: ClassVar[dict[str, str]] = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ID": "9876543210",
        "GITHUB_REPOSITORY_OWNER": "MyOrg",
        "GITHUB_REPOSITORY": "MyOrg/my-repo",
        "GITHUB_SHA": "abc1234def5678",
    }

    def test_rock_build_pushes_to_ghcr_and_writes_image_ref(
        self, tmp_path: Path
    ) -> None:
        """In CI, rock output should be a GHCR image ref, not a local file."""
        _write(tmp_path / "rockcraft.yaml", "name: my-rock\n")
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n",
        )
        rock_file = tmp_path / "my-rock_1.0_amd64.rock"
        rock_file.write_bytes(b"fake rock")

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, self._CI_ENV, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: rock_file.touch()
            result = artifacts_build(tmp_path, rock_names=["my-rock"])

        gen = load_artifacts_generated(result)
        assert len(gen.rocks) == 1
        rock_out = gen.rocks[0].output
        assert rock_out.file is None
        assert rock_out.image == "ghcr.io/myorg/my-repo/my-rock:abc1234"

        # Verify skopeo was called to push to GHCR
        skopeo_calls = [c for c in mock_run.call_args_list if "skopeo" in str(c)]
        assert len(skopeo_calls) == 1
        skopeo_args = skopeo_calls[0][0][0]
        assert "skopeo" in skopeo_args
        assert any("ghcr.io/myorg/my-repo/my-rock:abc1234" in a for a in skopeo_args)

    def test_charm_build_writes_artifact_ref(self, tmp_path: Path) -> None:
        """In CI, charm output should be a GitHub artifact reference."""
        _write(tmp_path / "charmcraft.yaml", "name: my-charm\n")
        _write(
            tmp_path / "artifacts.yaml",
            (
                "version: 1\ncharms:\n- name: my-charm\n"
                "  charmcraft-yaml: charmcraft.yaml\n"
            ),
        )
        charm_file = tmp_path / "my-charm_ubuntu-24.04-amd64.charm"

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, self._CI_ENV, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: charm_file.touch()
            result = artifacts_build(tmp_path, charm_names=["my-charm"])

        gen = load_artifacts_generated(result)
        assert len(gen.charms) == 1
        charm_out = gen.charms[0].output
        assert charm_out.files == []
        assert charm_out.artifact == "built-charm-my-charm"
        assert charm_out.run_id == "9876543210"

    def test_snap_build_writes_artifact_ref(self, tmp_path: Path) -> None:
        """In CI, snap output should be a GitHub artifact reference."""
        _write(tmp_path / "snap" / "snapcraft.yaml", "name: my-snap\n")
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nsnaps:\n- name: my-snap\n"
            "  snapcraft-yaml: snap/snapcraft.yaml\n",
        )
        snap_file = tmp_path / "snap" / "my-snap_1.0_amd64.snap"

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, self._CI_ENV, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: snap_file.touch()
            result = artifacts_build(tmp_path, snap_names=["my-snap"])

        gen = load_artifacts_generated(result)
        assert len(gen.snaps) == 1
        snap_out = gen.snaps[0].output
        assert snap_out.file is None
        assert snap_out.artifact == "built-snap-my-snap"
        assert snap_out.run_id == "9876543210"

    def test_local_build_unchanged_when_no_github_actions(self, tmp_path: Path) -> None:
        """Without GITHUB_ACTIONS=true, build produces local file refs."""
        _write(tmp_path / "charmcraft.yaml", "name: my-charm\n")
        _write(
            tmp_path / "artifacts.yaml",
            (
                "version: 1\ncharms:\n- name: my-charm\n"
                "  charmcraft-yaml: charmcraft.yaml\n"
            ),
        )
        charm_file = tmp_path / "my-charm_ubuntu-24.04-amd64.charm"

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, {"GITHUB_ACTIONS": ""}, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: charm_file.touch()
            result = artifacts_build(tmp_path, charm_names=["my-charm"])

        gen = load_artifacts_generated(result)
        charm_out = gen.charms[0].output
        assert charm_out.artifact is None
        assert len(charm_out.files) == 1
        assert "my-charm_ubuntu-24.04-amd64.charm" in charm_out.files[0].path

    def test_ci_missing_env_vars_raises(self, tmp_path: Path) -> None:
        """GITHUB_ACTIONS=true with missing env vars raises ConfigurationError."""
        _write(tmp_path / "charmcraft.yaml", "name: my-charm\n")
        _write(
            tmp_path / "artifacts.yaml",
            (
                "version: 1\ncharms:\n- name: my-charm\n"
                "  charmcraft-yaml: charmcraft.yaml\n"
            ),
        )
        charm_file = tmp_path / "my-charm_ubuntu-24.04-amd64.charm"

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(
                os.environ,
                {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_RUN_ID": "",
                    "GITHUB_REPOSITORY_OWNER": "",
                    "GITHUB_REPOSITORY": "",
                    "GITHUB_SHA": "",
                },
                clear=False,
            ),
            pytest.raises(ConfigurationError, match="GITHUB_RUN_ID"),
        ):
            mock_run.side_effect = lambda cmd, **_: charm_file.touch()
            artifacts_build(tmp_path, charm_names=["my-charm"])

    def test_owner_is_lowercased(self, tmp_path: Path) -> None:
        """GITHUB_REPOSITORY_OWNER is lowercased in the image ref."""
        _write(tmp_path / "rockcraft.yaml", "name: my-rock\n")
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n",
        )
        rock_file = tmp_path / "my-rock_1.0_amd64.rock"
        rock_file.write_bytes(b"fake rock")

        with (
            patch("opcli.core.artifacts.run_command") as mock_run,
            patch.dict(os.environ, self._CI_ENV, clear=False),
        ):
            mock_run.side_effect = lambda cmd, **_: rock_file.touch()
            result = artifacts_build(tmp_path, rock_names=["my-rock"])

        gen = load_artifacts_generated(result)
        assert gen.rocks[0].output.image is not None
        assert "MyOrg" not in gen.rocks[0].output.image
        assert "myorg" in gen.rocks[0].output.image


class TestArtifactsCollectCIMode:
    """Tests for artifacts_collect() with CI-format (image/artifact) partials."""

    def _partial(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name / "artifacts-generated.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def test_collect_fills_charm_resource_image_from_ghcr_rock(
        self, tmp_path: Path
    ) -> None:
        """Collect merges partials; rock GHCR image lives on rock, not the resource."""
        rock_partial = self._partial(
            tmp_path,
            "rock-job",
            "version: 1\n"
            "rocks:\n- name: my-rock\n  rockcraft-yaml: rockcraft.yaml\n"
            "  output:\n    image: ghcr.io/myorg/my-repo/my-rock:abc1234\n",
        )
        charm_partial = self._partial(
            tmp_path,
            "charm-job",
            "version: 1\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    artifact: built-charm-my-charm\n    run-id: '9876543210'\n"
            "  resources:\n"
            "    my-rock-image:\n"
            "      type: oci-image\n"
            "      rock: my-rock\n",
        )

        artifacts_collect(tmp_path, [rock_partial, charm_partial])

        gen = load_artifacts_generated(tmp_path / "artifacts-generated.yaml")
        assert gen.rocks[0].output.image == "ghcr.io/myorg/my-repo/my-rock:abc1234"
        resource = gen.charms[0].resources["my-rock-image"]  # type: ignore[index]
        # Resource carries the rock reference; image resolved from rock.output.image
        assert resource.rock == "my-rock"
        # Charm itself still has artifact ref
        assert gen.charms[0].output.artifact == "built-charm-my-charm"
        assert gen.charms[0].output.run_id == "9876543210"


class TestArtifactsLocalize:
    """Tests for artifacts_localize()."""

    _GENERATED_CI = (
        "version: 1\n"
        "charms:\n"
        "- name: my-charm\n"
        "  charmcraft-yaml: charmcraft.yaml\n"
        "  output:\n"
        "    artifact: built-charm-my-charm\n"
        "    run-id: '9876543210'\n"
    )

    def test_localises_charm_from_downloaded_file(self, tmp_path: Path) -> None:
        """Finds .charm file and updates output.files."""

        _write(tmp_path / "artifacts-generated.yaml", self._GENERATED_CI)
        charm_file = tmp_path / "my-charm_ubuntu-24.04-amd64.charm"
        charm_file.write_bytes(b"")

        count = artifacts_localize(tmp_path)

        assert count == 1
        gen = load_artifacts_generated(tmp_path / "artifacts-generated.yaml")
        assert gen.charms[0].output.files is not None
        assert len(gen.charms[0].output.files) == 1
        path = gen.charms[0].output.files[0].path
        assert path.endswith(".charm")
        assert path.startswith("./"), f"Expected relative path, got: {path}"
        assert "/home/" not in path, f"Expected no absolute home path, got: {path}"

    def test_skips_charm_already_with_local_files(self, tmp_path: Path) -> None:
        """Does not overwrite charms that already have output.files."""

        generated = (
            "version: 1\n"
            "charms:\n"
            "- name: my-charm\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n"
            "    files:\n"
            "    - path: ./my-charm_ubuntu-24.04-amd64.charm\n"
        )
        _write(tmp_path / "artifacts-generated.yaml", generated)
        charm_file = tmp_path / "my-charm_new.charm"
        charm_file.write_bytes(b"")

        count = artifacts_localize(tmp_path)

        assert count == 0

    def test_warns_when_no_charm_file_found(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Logs a warning when no .charm file matches the charm name."""

        _write(tmp_path / "artifacts-generated.yaml", self._GENERATED_CI)

        with caplog.at_level(logging.WARNING):
            count = artifacts_localize(tmp_path)

        assert count == 0
        assert any("No .charm file found" in r.message for r in caplog.records)

    def test_missing_generated_yaml_raises(self, tmp_path: Path) -> None:
        """Raises ConfigurationError when artifacts-generated.yaml is missing."""

        with pytest.raises(ConfigurationError):
            artifacts_localize(tmp_path)

    def test_skips_charm_without_artifact_ref(self, tmp_path: Path) -> None:
        """Skips charms that have no CI artifact ref."""

        generated = (
            "version: 1\n"
            "charms:\n"
            "- name: my-charm\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n"
            "    files:\n"
            "    - path: ./my-charm_ubuntu-24.04-amd64.charm\n"
        )
        _write(tmp_path / "artifacts-generated.yaml", generated)
        # Create a second charm file — should not be picked up since charm
        # already has output.files
        (tmp_path / "my-charm_new.charm").write_bytes(b"")

        count = artifacts_localize(tmp_path)

        assert count == 0

    def test_does_not_match_charm_with_longer_prefix_name(self, tmp_path: Path) -> None:
        """Does not pick up 'my-charm-k8s_*.charm' when localising 'my-charm'."""

        _write(tmp_path / "artifacts-generated.yaml", self._GENERATED_CI)
        # Only the longer-prefix file exists — pattern must NOT match it
        (tmp_path / "my-charm-k8s_ubuntu-24.04-amd64.charm").write_bytes(b"")

        count = artifacts_localize(tmp_path)

        assert count == 0
