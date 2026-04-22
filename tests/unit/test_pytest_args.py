"""Tests for ``opcli pytest args`` and ``opcli pytest run``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError
from opcli.core.pytest_args import assemble_pytest_args, run_pytest


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


_PLAN_WITH_RESOURCES = """\
version: 1
rocks:
- name: myrock
  source: rock_dir
charms:
- name: mycharm
  source: .
  resources:
    myrock-image:
      type: oci-image
      rock: myrock
"""

_GENERATED_LOCAL = """\
version: 1
rocks:
- name: myrock
  source: rock_dir
  output:
    file: ./rock_dir/myrock.rock
charms:
- name: mycharm
  source: .
  output:
    file: ./mycharm.charm
"""

_GENERATED_CI = """\
version: 1
rocks:
- name: myrock
  source: rock_dir
  output:
    image: ghcr.io/canonical/myrock:abc123
charms:
- name: mycharm
  source: .
  output:
    artifact: charm-mycharm
    run-id: "999"
"""


class TestAssemblePytestArgs:
    """Tests for assemble_pytest_args()."""

    def test_missing_generated_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            assemble_pytest_args(tmp_path)

    def test_local_charm_with_rock_resource(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.yaml", _PLAN_WITH_RESOURCES)
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_LOCAL)

        args = assemble_pytest_args(tmp_path)

        assert "--charm-file" in args
        assert "./mycharm.charm" in args
        assert "--myrock-image" in args
        assert "./rock_dir/myrock.rock" in args

    def test_ci_charm_with_image_resource(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.yaml", _PLAN_WITH_RESOURCES)
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_CI)

        args = assemble_pytest_args(tmp_path)

        # CI charm has artifact output, not file — no --charm-file
        assert "--charm-file" not in args
        # Rock has image output
        assert "--myrock-image" in args
        assert "ghcr.io/canonical/myrock:abc123" in args

    def test_no_plan_still_produces_charm_file(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_LOCAL)
        # No artifacts.yaml → no resource linking, but charm-file still works

        args = assemble_pytest_args(tmp_path)

        assert "--charm-file" in args
        assert "--myrock-image" not in args

    def test_charm_without_resources(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.yaml",
            "version: 1\ncharms:\n- name: simple\n  source: .\n",
        )
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\ncharms:\n- name: simple\n  source: .\n"
            "  output:\n    file: ./simple.charm\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert args == ["--charm-file", "./simple.charm"]

    def test_empty_generated(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        args = assemble_pytest_args(tmp_path)
        assert args == []


class TestRunPytest:
    """Tests for run_pytest()."""

    def test_runs_tox_with_assembled_args(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.yaml", _PLAN_WITH_RESOURCES)
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_LOCAL)

        with patch("opcli.core.pytest_args.run_command") as mock_run:
            run_pytest(tmp_path)

        cmd = mock_run.call_args[0][0]
        assert cmd[:4] == ["tox", "-e", "integration", "--"]
        assert "--charm-file" in cmd
        assert "./mycharm.charm" in cmd

    def test_custom_tox_env(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        with patch("opcli.core.pytest_args.run_command") as mock_run:
            run_pytest(tmp_path, tox_env="e2e")

        cmd = mock_run.call_args[0][0]
        assert cmd[2] == "e2e"

    def test_extra_args_forwarded(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        with patch("opcli.core.pytest_args.run_command") as mock_run:
            run_pytest(tmp_path, extra_args=["-k", "test_foo", "-v"])

        cmd = mock_run.call_args[0][0]
        assert "-k" in cmd
        assert "test_foo" in cmd
        assert "-v" in cmd

    def test_missing_generated_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            run_pytest(tmp_path)
