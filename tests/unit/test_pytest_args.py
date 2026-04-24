"""Tests for ``opcli pytest expand``."""

from __future__ import annotations

from pathlib import Path

import pytest

from opcli.core.exceptions import ConfigurationError
from opcli.core.pytest_args import assemble_pytest_args, assemble_tox_argv

_V1_ERROR_MATCH = "validation error"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# Fixtures — all version 2 (resources embedded in charm entries)
# ---------------------------------------------------------------------------

_GENERATED_LOCAL = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  output:
    file: ./rock_dir/myrock.rock
charms:
- name: mycharm
  charmcraft-yaml: charmcraft.yaml
  output:
    file: ./mycharm.charm
  resources:
    myrock-image:
      type: oci-image
      rock: myrock
      file: ./rock_dir/myrock.rock
"""

_GENERATED_CI = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  output:
    image: ghcr.io/canonical/myrock:abc123
charms:
- name: mycharm
  charmcraft-yaml: charmcraft.yaml
  output:
    artifact: charm-mycharm
    run-id: "999"
  resources:
    myrock-image:
      type: oci-image
      rock: myrock
      image: ghcr.io/canonical/myrock:abc123
"""

_GENERATED_NO_RESOURCES = """\
version: 1
charms:
- name: simple
  charmcraft-yaml: charmcraft.yaml
  output:
    file: ./simple.charm
"""


class TestAssemblePytestArgs:
    """Tests for assemble_pytest_args()."""

    def test_missing_generated_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            assemble_pytest_args(tmp_path)

    def test_invalid_generated_fields_raises(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\ncharms:\n- name: c\n  source: .\n"
            "  output:\n    file: ./c.charm\n",
        )
        with pytest.raises(Exception, match=_V1_ERROR_MATCH):
            assemble_pytest_args(tmp_path)

    def test_local_charm_with_embedded_rock_resource(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_LOCAL)

        args = assemble_pytest_args(tmp_path)

        assert "--charm-file=./mycharm.charm" in args
        assert "--myrock-image=./rock_dir/myrock.rock" in args

    def test_ci_scenario_only_generated_file(self, tmp_path: Path) -> None:
        """pytest expand works with only artifacts-generated.yaml (no repo checkout)."""
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_CI)
        # Intentionally no artifacts.yaml present

        args = assemble_pytest_args(tmp_path)

        # CI charm has artifact output, not file — no --charm-file
        assert not any(a.startswith("--charm-file=") for a in args)
        # Rock image ref is embedded in the charm resources
        assert "--myrock-image=ghcr.io/canonical/myrock:abc123" in args

    def test_charm_without_resources(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_NO_RESOURCES)

        args = assemble_pytest_args(tmp_path)

        assert args == ["--charm-file=./simple.charm"]

    def test_empty_generated(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        args = assemble_pytest_args(tmp_path)
        assert args == []

    def test_unresolved_resource_produces_no_flag(self, tmp_path: Path) -> None:
        """Resource with no file or image (rock not built) emits no flag."""
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\ncharms:\n- name: c\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    file: ./c.charm\n"
            "  resources:\n    img:\n      type: oci-image\n      rock: myrock\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert args == ["--charm-file=./c.charm"]
        assert not any(a.startswith("--img=") for a in args)

    def test_image_takes_priority_over_file_when_both_set(self, tmp_path: Path) -> None:
        """After provision load, image ref is preferred over local file path."""
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\ncharms:\n- name: c\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    file: ./c.charm\n"
            "  resources:\n    myrock-image:\n      type: oci-image\n"
            "      rock: myrock\n"
            "      file: ./rock_dir/myrock.rock\n"
            "      image: localhost:32000/myrock:latest\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert "--myrock-image=localhost:32000/myrock:latest" in args
        assert not any("myrock.rock" in a for a in args)


class TestAssembleToxArgv:
    """Tests for assemble_tox_argv()."""

    def test_no_flags_no_extra_omits_separator(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        argv = assemble_tox_argv(tmp_path)

        assert argv == ["tox", "-e", "integration"]
        assert "--" not in argv

    def test_assembled_flags_include_separator(self, tmp_path: Path) -> None:
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
