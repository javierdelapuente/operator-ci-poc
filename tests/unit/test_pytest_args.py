"""Tests for ``opcli pytest expand``."""

from __future__ import annotations

from pathlib import Path

import pytest

from opcli.core.exceptions import ConfigurationError
from opcli.core.pytest_args import assemble_pytest_args, assemble_tox_argv


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

        assert "--charm-file=./mycharm.charm" in args
        assert "--myrock-image=./rock_dir/myrock.rock" in args

    def test_ci_charm_with_image_resource(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.yaml", _PLAN_WITH_RESOURCES)
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_CI)

        args = assemble_pytest_args(tmp_path)

        # CI charm has artifact output, not file — no --charm-file
        assert not any(a.startswith("--charm-file=") for a in args)
        # Rock has image output
        assert "--myrock-image=ghcr.io/canonical/myrock:abc123" in args

    def test_no_plan_still_produces_charm_file(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_LOCAL)
        # No artifacts.yaml → no resource linking, but charm-file still works

        args = assemble_pytest_args(tmp_path)

        assert "--charm-file=./mycharm.charm" in args
        assert not any(a.startswith("--myrock-image=") for a in args)

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

        assert args == ["--charm-file=./simple.charm"]

    def test_empty_generated(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        args = assemble_pytest_args(tmp_path)
        assert args == []


class TestAssembleToxArgv:
    """Tests for assemble_tox_argv()."""

    def test_no_flags_no_extra_omits_separator(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        argv = assemble_tox_argv(tmp_path)

        assert argv == ["tox", "-e", "integration"]
        assert "--" not in argv

    def test_assembled_flags_include_separator(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.yaml", _PLAN_WITH_RESOURCES)
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_LOCAL)

        argv = assemble_tox_argv(tmp_path)

        assert argv[:3] == ["tox", "-e", "integration"]
        assert "--" in argv
        assert "--charm-file=./mycharm.charm" in argv

    def test_extra_args_only_include_separator(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        argv = assemble_tox_argv(tmp_path, extra_args=["-k", "test_foo"])

        assert "--" in argv
        assert "-k" in argv
        assert "test_foo" in argv

    def test_custom_tox_env(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        argv = assemble_tox_argv(tmp_path, tox_env="e2e")

        assert argv[2] == "e2e"

    def test_extra_args_appended_after_assembled(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.yaml", _PLAN_WITH_RESOURCES)
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_LOCAL)

        argv = assemble_tox_argv(tmp_path, extra_args=["-v", "-k", "test_charm"])

        sep_idx = argv.index("--")
        tail = argv[sep_idx + 1 :]
        assert "--charm-file=./mycharm.charm" in tail
        assert "-v" in tail
        assert "-k" in tail
        assert "test_charm" in tail

    def test_missing_generated_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            assemble_tox_argv(tmp_path)
