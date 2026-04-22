"""Tests for ``opcli artifacts init`` and ``opcli artifacts build``."""

from __future__ import annotations

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
            "version: 1\ncharms:\n- name: mycharm\n  source: .\n",
        )
        # Simulate charmcraft pack producing a .charm file
        _write(tmp_path / "mycharm_amd64.charm", "fake charm")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            result = artifacts_build(tmp_path)

        mock_run.assert_called_once()
        assert "charmcraft" in mock_run.call_args[0][0]
        gen = load_artifacts_generated(result)
        assert len(gen.charms) == 1
        assert gen.charms[0].name == "mycharm"
        assert gen.charms[0].output.file is not None
        assert gen.charms[0].output.file.endswith(".charm")

    def test_build_single_rock(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n  source: rock_dir\n",
        )
        rock_dir = tmp_path / "rock_dir"
        rock_dir.mkdir()
        _write(rock_dir / "myrock_1.0_amd64.rock", "fake rock")

        with patch("opcli.core.artifacts.run_command") as mock_run:
            result = artifacts_build(tmp_path)

        mock_run.assert_called_once()
        assert "rockcraft" in mock_run.call_args[0][0]
        gen = load_artifacts_generated(result)
        assert len(gen.rocks) == 1
        assert gen.rocks[0].output.file is not None

    def test_build_single_snap(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nsnaps:\n- name: mysnap\n  source: snap_dir\n",
        )
        snap_dir = tmp_path / "snap_dir"
        snap_dir.mkdir()
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
            "- name: charm-a\n  source: a\n"
            "- name: charm-b\n  source: b\n",
        )
        (tmp_path / "a").mkdir()
        _write(tmp_path / "a" / "charm-a.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path, charm_names=["charm-a"])

        gen = load_artifacts_generated(result)
        assert len(gen.charms) == 1
        assert gen.charms[0].name == "charm-a"

    def test_unknown_charm_name_raises(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: real\n  source: .\n",
        )
        with pytest.raises(ConfigurationError, match="Unknown charm"):
            artifacts_build(tmp_path, charm_names=["nonexistent"])

    def test_no_output_file_raises(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\nrocks:\n- name: myrock\n  source: rock_dir\n",
        )
        (tmp_path / "rock_dir").mkdir()

        with (
            patch("opcli.core.artifacts.run_command"),
            pytest.raises(OpcliError, match=r"No \*.rock found"),
        ):
            artifacts_build(tmp_path)

    def test_build_monorepo(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  source: rock_dir\n"
            "charms:\n- name: mycharm\n  source: .\n",
        )
        (tmp_path / "rock_dir").mkdir()
        _write(tmp_path / "rock_dir" / "myrock.rock", "fake")
        _write(tmp_path / "mycharm.charm", "fake")

        with patch("opcli.core.artifacts.run_command"):
            result = artifacts_build(tmp_path)

        gen = load_artifacts_generated(result)
        assert len(gen.rocks) == 1
        assert len(gen.charms) == 1
