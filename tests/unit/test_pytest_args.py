"""Tests for ``opcli pytest expand``."""

from __future__ import annotations

import logging
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
    files:
    - path: ./mycharm_ubuntu-22.04-amd64.charm
      base: ubuntu@22.04
  resources:
    myrock-image:
      type: oci-image
      rock: myrock
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
"""

_GENERATED_NO_RESOURCES = """\
version: 1
charms:
- name: simple
  charmcraft-yaml: charmcraft.yaml
  output:
    files:
    - path: ./simple_ubuntu-22.04-amd64.charm
      base: ubuntu@22.04
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
            "  output:\n    files:\n    - path: ./c.charm\n      base: ubuntu@22.04\n",
        )
        with pytest.raises(Exception, match=_V1_ERROR_MATCH):
            assemble_pytest_args(tmp_path)

    def test_local_charm_with_embedded_rock_resource(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_LOCAL)

        args = assemble_pytest_args(tmp_path)

        assert "--charm-file=./mycharm_ubuntu-22.04-amd64.charm" in args
        assert "--myrock-image=./rock_dir/myrock.rock" in args

    def test_ci_scenario_only_generated_file(self, tmp_path: Path) -> None:
        """pytest expand works with only artifacts-generated.yaml (no repo checkout)."""
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_CI)
        # Intentionally no artifacts.yaml present

        args = assemble_pytest_args(tmp_path)

        # CI charm has artifact output, not file — no --charm-file
        assert not any(a.startswith("--charm-file=") for a in args)
        # Rock image ref comes from iterating the rocks list directly
        assert "--myrock-image=ghcr.io/canonical/myrock:abc123" in args

    def test_ci_charm_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CI-format charm (artifact output only) emits a warning, no --charm-file."""
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_CI)

        with caplog.at_level(logging.WARNING, logger="opcli.core.pytest_args"):
            args = assemble_pytest_args(tmp_path)

        assert not any(a.startswith("--charm-file=") for a in args)
        assert any("charm-mycharm" in msg for msg in caplog.messages), (
            "expected warning mentioning the artifact name"
        )

    def test_charm_without_resources(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_NO_RESOURCES)

        args = assemble_pytest_args(tmp_path)

        assert args == ["--charm-file=./simple_ubuntu-22.04-amd64.charm"]

    def test_multi_base_charm_emits_multiple_charm_file_flags(
        self, tmp_path: Path
    ) -> None:
        """Multi-base charm produces one --charm-file per output file."""
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\ncharms:\n- name: aproxy\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    files:\n"
            "    - path: ./aproxy_ubuntu-20.04-amd64.charm\n      base: ubuntu@20.04\n"
            "    - path: ./aproxy_ubuntu-22.04-amd64.charm\n      base: ubuntu@22.04\n"
            "    - path: ./aproxy_ubuntu-24.04-amd64.charm\n      base: ubuntu@24.04\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert "--charm-file=./aproxy_ubuntu-20.04-amd64.charm" in args
        assert "--charm-file=./aproxy_ubuntu-22.04-amd64.charm" in args
        assert "--charm-file=./aproxy_ubuntu-24.04-amd64.charm" in args
        assert args.count("--charm-file=./aproxy_ubuntu-22.04-amd64.charm") == 1

    def test_empty_generated(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

        args = assemble_pytest_args(tmp_path)
        assert args == []

    def test_unresolved_resource_produces_no_flag(self, tmp_path: Path) -> None:
        """Resource with no file or image (rock not built) emits no flag."""
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\ncharms:\n- name: c\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    files:\n    - path: ./c_ubuntu-22.04-amd64.charm\n"
            "      base: ubuntu@22.04\n"
            "  resources:\n    img:\n      type: oci-image\n      rock: myrock\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert args == ["--charm-file=./c_ubuntu-22.04-amd64.charm"]
        assert not any(a.startswith("--img=") for a in args)

    def test_image_takes_priority_over_file_when_both_set(self, tmp_path: Path) -> None:
        """After provision load, image ref is preferred over local file path."""
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "  output:\n    file: ./rock_dir/myrock.rock\n"
            "    image: localhost:32000/myrock:latest\n"
            "charms:\n- name: c\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    files:\n    - path: ./c_ubuntu-22.04-amd64.charm\n"
            "      base: ubuntu@22.04\n"
            "  resources:\n    myrock-image:\n      type: oci-image\n"
            "      rock: myrock\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert "--myrock-image=localhost:32000/myrock:latest" in args
        assert not any("myrock.rock" in a for a in args)

    def test_rock_name_used_for_flag_not_resource_name(self, tmp_path: Path) -> None:
        """Flag uses rock name, not resource name — matches operator-workflows.

        When the resource name (e.g. ``app-image``) differs from the rock name
        (e.g. ``expressjs-app``), the generated flag must be
        ``--expressjs-app-image=...``, not ``--app-image=...``.
        """
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\n"
            "rocks:\n- name: expressjs-app\n  rockcraft-yaml: rockcraft.yaml\n"
            "  output:\n    file: ./expressjs-app_1.0_amd64.rock\n"
            "charms:\n- name: expressjs-k8s\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    files:\n"
            "    - path: ./expressjs-k8s_ubuntu-22.04-amd64.charm\n"
            "      base: ubuntu@22.04\n"
            "  resources:\n    app-image:\n      type: oci-image\n"
            "      rock: expressjs-app\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert "--expressjs-app-image=./expressjs-app_1.0_amd64.rock" in args
        assert not any(a.startswith("--app-image=") for a in args)

    def test_rock_without_resource_link_emits_image_flag(self, tmp_path: Path) -> None:
        """Rock with no charm resource link still generates --{rock-name}-image flag.

        This is the core operator-workflows behaviour: image flags come from
        iterating rocks directly, no explicit rock: annotation required.
        """
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\n"
            "rocks:\n"
            "- name: expressjs-app\n  rockcraft-yaml: rockcraft.yaml\n"
            "  output:\n    file: ./expressjs-app_1.0_amd64.rock\n"
            "- name: fastapi-app\n  rockcraft-yaml: fastapi/rockcraft.yaml\n"
            "  output:\n    file: ./fastapi-app_1.0_amd64.rock\n"
            "charms:\n- name: my-charm\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    files:\n"
            "    - path: ./my-charm_ubuntu-22.04-amd64.charm\n"
            "      base: ubuntu@22.04\n",
        )

        args = assemble_pytest_args(tmp_path)

        assert "--expressjs-app-image=./expressjs-app_1.0_amd64.rock" in args
        assert "--fastapi-app-image=./fastapi-app_1.0_amd64.rock" in args

    def test_resource_without_rock_link_produces_no_flag(self, tmp_path: Path) -> None:
        """Resources not linked to a rock (no rock: field) produce no image flag."""
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 1\ncharms:\n- name: mycharm\n"
            "  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n    files:\n"
            "    - path: ./mycharm_ubuntu-22.04-amd64.charm\n"
            "      base: ubuntu@22.04\n"
            "  resources:\n    standalone-image:\n      type: oci-image\n",
        )

        args = assemble_pytest_args(tmp_path)

        # Only charm-file; no image flag for a resource with no rock backing
        assert args == ["--charm-file=./mycharm_ubuntu-22.04-amd64.charm"]


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
        assert "--charm-file=./mycharm_ubuntu-22.04-amd64.charm" in argv

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
        assert "--charm-file=./mycharm_ubuntu-22.04-amd64.charm" in tail
        assert "-v" in tail
        assert "-k" in tail
        assert "test_charm" in tail

    def test_missing_generated_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            assemble_tox_argv(tmp_path)
