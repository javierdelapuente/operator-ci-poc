"""Tests for ``opcli spread init``, ``expand``, and ``run``."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from opcli.core.exceptions import ConfigurationError, SubprocessError, ValidationError
from opcli.core.spread import (
    _runner_by_system,
    spread_expand,
    spread_init,
    spread_run,
    spread_tasks,
)

_yaml = YAML()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


_MINIMAL_SPREAD = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04
environment:
  CONCIERGE: concierge.yaml
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
"""


class TestSpreadInit:
    """Tests for spread_init()."""

    def test_generates_files(self, tmp_path: Path) -> None:
        spread_path, task_path = spread_init(tmp_path)

        assert spread_path.exists()
        assert task_path.exists()
        assert task_path == tmp_path / "tests" / "integration" / "run" / "task.yaml"

        content = spread_path.read_text()
        assert "integration-test" in content

        task_content = task_path.read_text()
        assert 'opcli pytest expand -e "${TOX_ENV:-integration}"' in task_content

    def test_generates_required_fields(self, tmp_path: Path) -> None:
        spread_path, _ = spread_init(tmp_path)

        parsed = _yaml.load(StringIO(spread_path.read_text()))
        assert parsed["path"] == "/home/ubuntu/proj"
        assert parsed["kill-timeout"] == "60m"
        assert "summary" in parsed["suites"]["tests/integration/"]

    def test_generates_exclude_list(self, tmp_path: Path) -> None:
        spread_path, _ = spread_init(tmp_path)

        parsed = _yaml.load(StringIO(spread_path.read_text()))
        exclude = parsed["exclude"]
        assert ".git" in exclude
        assert ".tox" in exclude
        assert ".venv" in exclude
        assert ".*_cache" in exclude

    def test_generates_standard_env_vars(self, tmp_path: Path) -> None:
        spread_path, _ = spread_init(tmp_path)

        parsed = _yaml.load(StringIO(spread_path.read_text()))
        env = parsed["environment"]
        assert env["SUDO_USER"] == "ubuntu"
        assert "SUDO_UID" not in env
        assert env["LANG"] == "C.UTF-8"
        assert env["LANGUAGE"] == "en"
        assert "CONCIERGE" in env
        # GitHub Actions vars belong only in the expanded CI backend, not root
        assert "GITHUB_TOKEN" not in env
        assert "GITHUB_RUN_ID" not in env
        assert "GITHUB_REPOSITORY" not in env
        # MODULE variants belong in the suite, not the root environment
        assert not any(k.startswith("MODULE") for k in env)
        assert "TOX_ENV" not in env

    def test_module_vars_in_suite_environment(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "tests" / "integration"
        test_dir.mkdir(parents=True)
        (test_dir / "test_charm.py").write_text("")
        (test_dir / "test_actions.py").write_text("")

        spread_path, _ = spread_init(tmp_path)

        parsed = _yaml.load(StringIO(spread_path.read_text()))
        suite_env = parsed["suites"]["tests/integration/"]["environment"]
        assert suite_env["MODULE/test_charm"] == "test_charm"
        assert suite_env["MODULE/test_actions"] == "test_actions"
        assert suite_env["TOX_ENV"] == ""
        # Also not in root environment
        assert "MODULE/test_charm" not in parsed["environment"]
        assert "TOX_ENV" not in parsed["environment"]

    def test_discovers_test_modules(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "tests" / "integration"
        test_dir.mkdir(parents=True)
        (test_dir / "test_charm.py").write_text("")
        (test_dir / "test_actions.py").write_text("")
        (test_dir / "conftest.py").write_text("")  # not a test module

        spread_path, _ = spread_init(tmp_path)

        parsed = _yaml.load(StringIO(spread_path.read_text()))
        suite_env = parsed["suites"]["tests/integration/"]["environment"]
        assert "MODULE/test_charm" in suite_env
        assert "MODULE/test_actions" in suite_env
        assert not any("conftest" in k for k in suite_env)

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", "existing\n")

        with pytest.raises(ConfigurationError, match="already exists"):
            spread_init(tmp_path)

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", "old\n")
        _write(tmp_path / "tests" / "integration" / "run" / "task.yaml", "old\n")

        spread_path, task_path = spread_init(tmp_path, force=True)
        assert "integration-test" in spread_path.read_text()
        tox_env_flag = 'opcli pytest expand -e "${TOX_ENV:-integration}"'
        assert tox_env_flag in task_path.read_text()

    def test_project_name_from_directory(self, tmp_path: Path) -> None:
        spread_path, _ = spread_init(tmp_path)
        content = spread_path.read_text()
        assert f"project: {tmp_path.resolve().name}" in content


class TestSpreadExpand:
    """Tests for spread_expand()."""

    def test_missing_spread_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            spread_expand(tmp_path)

    def test_expand_local(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))

        assert "integration-test" not in result
        local = parsed["backends"]["local"]
        assert local["type"] == "adhoc"
        assert "lxc launch --vm" in local["allocate"]
        assert "SPREAD_PASSWORD" in local["allocate"]
        assert "lxc delete --force" in local["discard"]
        prepare = local["prepare"]
        assert "concierge prepare" in prepare
        assert "opcli provision registry" in prepare
        assert '[ -f "$CONCIERGE" ]' in prepare
        assert "opcli provision load" in prepare

        # Systems should have username: ubuntu injected
        systems = local["systems"]
        assert len(systems) == 1
        assert "ubuntu-24.04" in systems[0]
        assert systems[0]["ubuntu-24.04"]["username"] == "ubuntu"

    def test_expand_ci(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=True)
        parsed = _yaml.load(StringIO(result))

        assert "integration-test" not in result
        ci = parsed["backends"]["ci"]
        assert ci["type"] == "adhoc"
        assert "ADDRESS localhost" in ci["allocate"]
        assert "chpasswd" in ci["allocate"]
        assert "PasswordAuthentication yes" in ci["allocate"]
        assert "password" not in ci
        assert "concierge" in ci["prepare"]
        # concierge runs as root but loginctl enable-linger ubuntu ensures
        # ubuntu's systemd session is active so snap cgroups work correctly
        assert 'runuser -l ubuntu -c "concierge prepare' not in ci["prepare"]
        assert "tox" in ci["prepare"]
        assert "opcli" in ci["prepare"]
        assert "SPREAD_PATH" in ci["prepare"]
        assert "chown" in ci["prepare"]
        # tox is installed for the ubuntu user via runuser with explicit bin dir
        assert "runuser" in ci["prepare"]
        assert "runuser -l ubuntu" in ci["prepare"]
        assert "UV_TOOL_BIN_DIR=/usr/local/bin uv tool install tox" in ci["prepare"]
        assert "loginctl enable-linger ubuntu" in ci["prepare"]
        assert "UV_TOOL_BIN_DIR=/usr/local/bin" in ci["prepare"]
        # CI prepare waits for and downloads build artifacts via gh CLI
        assert "gh run download" in ci["prepare"]
        assert "artifacts-generated" in ci["prepare"]
        assert "built-charm-*" in ci["prepare"]
        assert "GH_TOKEN" in ci["prepare"]
        assert "GITHUB_RUN_ID" in ci["prepare"]
        assert "opcli artifacts localize" in ci["prepare"]
        assert "command -v gh" in ci["prepare"]
        # CI backend has GitHub Actions vars scoped to it for artifact download
        assert "environment" in ci
        ci_env = ci["environment"]
        assert "GITHUB_TOKEN" in ci_env
        assert "GITHUB_RUN_ID" in ci_env
        assert "GITHUB_REPOSITORY" in ci_env
        # CI backend does NOT override SUDO_USER; ubuntu is created in allocate
        assert "SUDO_USER" not in ci_env
        assert "useradd" in ci["allocate"]
        assert "pipx install" not in ci["prepare"]
        assert "discard" not in ci
        # CI injects username: root per-system for SSH access
        systems = ci["systems"]
        assert len(systems) == 1
        assert isinstance(systems[0], dict)
        assert systems[0]["ubuntu-24.04"]["username"] == "root"

    def test_preserves_other_sections(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)

        assert "project: test-project" in result
        assert "MODULE/test_charm" in result
        assert "suites:" in result

    def test_preserves_systems(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)

        assert "ubuntu-24.04" in result

    def test_preserves_user_defined_backend_fields(self, tmp_path: Path) -> None:
        """User fields in the virtual backend survive expansion."""
        spread_with_extras = """\
project: test-project
backends:
  integration-test:
    systems:
      - ubuntu-24.04
    environment:
      EXTRA_VAR: hello
    prepare-each: |
      echo extra setup
    kill-timeout: 30m
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/: {}
"""
        _write(tmp_path / "spread.yaml", spread_with_extras)
        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        local = parsed["backends"]["local"]

        assert local["environment"] == {"EXTRA_VAR": "hello"}
        assert "extra setup" in local["prepare-each"]
        assert local["kill-timeout"] == "30m"
        # Systems get username injected for local backend
        assert local["systems"] == [{"ubuntu-24.04": {"username": "ubuntu"}}]
        # opcli fields are set
        assert local["type"] == "adhoc"
        assert "lxc launch --vm" in local["allocate"]

    def test_local_allocate_has_cleanup_trap(self, tmp_path: Path) -> None:
        """The local allocate script must clean up the VM on failure."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        allocate = parsed["backends"]["local"]["allocate"]

        assert "CLEANUP_VM=true" in allocate
        assert "trap cleanup EXIT" in allocate
        assert "CLEANUP_VM=false" in allocate

    def test_local_allocate_waits_for_agent(self, tmp_path: Path) -> None:
        """The local allocate script waits for LXD agent before cloud-init."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        allocate = parsed["backends"]["local"]["allocate"]

        # Agent readiness must come before cloud-init
        agent_pos = allocate.index('lxc exec "${VM_NAME}" -- true')
        cloudinit_pos = allocate.index("cloud-init status --wait")
        assert agent_pos < cloudinit_pos

    def test_auto_detects_ci_env_var(self, tmp_path: Path) -> None:
        """CI env var toggles between ci/local backend expansion."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch.dict("os.environ", {"CI": "true"}):
            result = spread_expand(tmp_path)
        parsed = _yaml.load(StringIO(result))
        assert "ci" in parsed["backends"]
        assert "ADDRESS localhost" in parsed["backends"]["ci"]["allocate"]

        with patch.dict("os.environ", {"CI": ""}, clear=False):
            result = spread_expand(tmp_path)
        parsed = _yaml.load(StringIO(result))
        assert "local" in parsed["backends"]
        assert "lxc launch --vm" in parsed["backends"]["local"]["allocate"]

    def test_expanded_is_valid_yaml(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)

        parsed = _yaml.load(StringIO(result))
        assert isinstance(parsed, dict)
        assert "backends" in parsed

    def test_local_allocate_uses_ubuntu_user(self, tmp_path: Path) -> None:
        """The allocate script sets up ubuntu user, not root."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        allocate = parsed["backends"]["local"]["allocate"]

        assert "echo ubuntu:${SPREAD_PASSWORD}" in allocate
        assert "PermitRootLogin" not in allocate
        assert "PasswordAuthentication yes" in allocate

    def test_local_prepare_conditional(self, tmp_path: Path) -> None:
        """Prepare script gates concierge and provision load on file existence."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        prepare = parsed["backends"]["local"]["prepare"]

        assert '[ -f "$CONCIERGE" ]' in prepare
        assert "[ -f artifacts-generated.yaml ]" in prepare

    def test_ci_prepare_conditional(self, tmp_path: Path) -> None:
        """CI prepare gates concierge on file existence."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=True)
        parsed = _yaml.load(StringIO(result))
        prepare = parsed["backends"]["ci"]["prepare"]

        assert '[ -f "$CONCIERGE" ]' in prepare
        assert "tox" in prepare
        assert "SPREAD_PATH" in prepare
        assert "pipx install" not in prepare

    def test_local_username_injection_mapping_systems(self, tmp_path: Path) -> None:
        """Username injection deep-merges; runner is stripped; native fields kept."""
        spread_with_mapping = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]
          workers: 2
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        _write(tmp_path / "spread.yaml", spread_with_mapping)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        systems = parsed["backends"]["local"]["systems"]

        assert len(systems) == 1
        sys_def = systems[0]["ubuntu-24.04"]
        assert sys_def["username"] == "ubuntu"
        # runner is CI-only; stripped from local expansion
        assert "runner" not in sys_def
        # spread-native fields like workers survive
        _EXPECTED_WORKERS = 2
        assert sys_def["workers"] == _EXPECTED_WORKERS

    def test_local_username_preserves_user_set_username(self, tmp_path: Path) -> None:
        """If user already set a username, it is not overridden."""
        spread_with_user = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04:
          username: custom-user
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        _write(tmp_path / "spread.yaml", spread_with_user)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        systems = parsed["backends"]["local"]["systems"]

        assert systems[0]["ubuntu-24.04"]["username"] == "custom-user"


class TestSystemResourceFields:
    """Tests for cpu/memory/disk/runner handling in virtual backend system entries."""

    _SPREAD_WITH_RESOURCES = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04:
          cpu: 2
          memory: 4
          disk: 30
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
"""

    def test_resources_appear_in_local_allocate(self, tmp_path: Path) -> None:
        """cpu/memory/disk from system entry appear as case-arm in local allocate."""
        _write(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        allocate = parsed["backends"]["local"]["allocate"]

        assert "ubuntu-24.04" in allocate
        assert 'CPU="${CPU:-2}"' in allocate
        assert 'MEM="${MEM:-4}"' in allocate
        assert 'DISK="${DISK:-30}"' in allocate

    def test_resources_stripped_from_local_systems(self, tmp_path: Path) -> None:
        """cpu/memory/disk are removed from system entries in local expansion."""
        _write(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        sys_def = parsed["backends"]["local"]["systems"][0]["ubuntu-24.04"]

        assert "cpu" not in sys_def
        assert "memory" not in sys_def
        assert "disk" not in sys_def
        assert sys_def["username"] == "ubuntu"

    def test_resources_stripped_from_ci_systems(self, tmp_path: Path) -> None:
        """cpu/memory/disk are removed from system entries in CI expansion."""
        _write(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=True)
        parsed = _yaml.load(StringIO(result))
        # After stripping resource keys, only username: ubuntu remains
        systems = parsed["backends"]["ci"]["systems"]
        assert len(systems) == 1
        assert isinstance(systems[0], dict)
        sys_props = systems[0]["ubuntu-24.04"]
        assert "cpu" not in sys_props
        assert "memory" not in sys_props
        assert "disk" not in sys_props
        assert sys_props.get("username") == "root"

    def test_runner_stripped_from_local_systems(self, tmp_path: Path) -> None:
        """runner label is stripped from local system entries (CI-only field)."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        _write(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        sys_def = parsed["backends"]["local"]["systems"][0]["ubuntu-24.04"]

        assert "runner" not in sys_def
        assert sys_def["username"] == "ubuntu"

    def test_runner_stripped_from_ci_systems(self, tmp_path: Path) -> None:
        """runner label is stripped from CI system entries (GitHub Actions only)."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        _write(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=True)
        parsed = _yaml.load(StringIO(result))
        systems = parsed["backends"]["ci"]["systems"]

        assert len(systems) == 1
        sys_def = systems[0]["ubuntu-24.04"]
        assert "runner" not in sys_def
        assert sys_def.get("username") == "root"

    def test_multiple_systems_with_different_resources(self, tmp_path: Path) -> None:
        """Each system gets its own case arm in the allocate preamble."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-22.04:
          cpu: 2
          memory: 4
          disk: 20
      - ubuntu-24.04:
          cpu: 8
          memory: 16
          disk: 50
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        _write(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        allocate = parsed["backends"]["local"]["allocate"]

        assert "ubuntu-22.04" in allocate
        assert "ubuntu-24.04" in allocate
        assert 'CPU="${CPU:-2}"' in allocate
        assert 'CPU="${CPU:-8}"' in allocate

    def test_env_var_overrides_system_resource(self, tmp_path: Path) -> None:
        """Per-system case arms use :- so explicit env vars still win."""
        _write(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        allocate = parsed["backends"]["local"]["allocate"]

        # Each arm must use ${VAR:-value} not bare assignment
        assert 'CPU="${CPU:-' in allocate
        assert 'MEM="${MEM:-' in allocate
        assert 'DISK="${DISK:-' in allocate

    def test_invalid_resource_value_raises(self, tmp_path: Path) -> None:
        """Non-positive-integer resource value raises ValidationError at expand time."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04:
          cpu: -1
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        _write(tmp_path / "spread.yaml", spread)

        with pytest.raises(ValidationError, match="positive integer"):
            spread_expand(tmp_path, ci=False)

    def test_no_resources_no_preamble(self, tmp_path: Path) -> None:
        """When no resources are declared, no case statement is prepended."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        allocate = parsed["backends"]["local"]["allocate"]

        assert "case" not in allocate
        # Fallback defaults still present
        assert 'DISK="${DISK:-20}"' in allocate

    def test_boolean_resource_value_raises(self, tmp_path: Path) -> None:
        """Boolean values must be rejected (bool is a subclass of int in Python)."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04:
          cpu: true
environment:
  MODULE/test_charm: test_charm
suites:
  tests/integration/:
    summary: integration tests
"""
        _write(tmp_path / "spread.yaml", spread)

        with pytest.raises(ValidationError, match="positive integer"):
            spread_expand(tmp_path, ci=False)

    def test_case_pattern_is_quoted(self, tmp_path: Path) -> None:
        """Case arm patterns must be quoted to prevent shell glob expansion."""
        _write(tmp_path / "spread.yaml", self._SPREAD_WITH_RESOURCES)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        allocate = parsed["backends"]["local"]["allocate"]

        # Pattern must be quoted: "ubuntu-24.04") not ubuntu-24.04)
        assert '"ubuntu-24.04")' in allocate


class TestSpreadRun:
    def test_runs_spread_from_temp_subdir(self, tmp_path: Path) -> None:
        """spread is invoked from a temp subdirectory inside the project root."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        captured_cwd: list[str] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> None:
            captured_cwd.append(str(kwargs.get("cwd", "")))

        with patch("opcli.core.spread.run_command", side_effect=capture_cmd):
            spread_run(tmp_path, ci=False)

        assert len(captured_cwd) == 1
        cwd = Path(captured_cwd[0])
        # Must be inside the project root, not the root itself
        assert cwd.parent == tmp_path
        assert cwd != tmp_path

    def test_uses_spread_binary(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch("opcli.core.spread.run_command") as mock_run:
            spread_run(tmp_path, ci=False)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "spread"
        assert not any(arg.startswith("-spread=") for arg in cmd)

    def test_spread_yaml_in_temp_subdir_has_reroot(self, tmp_path: Path) -> None:
        """The temp spread.yaml must contain reroot pointing to the project root."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        captured_yaml: list[dict[str, object]] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> None:
            cwd = kwargs.get("cwd", "")
            tmp_yaml = Path(str(cwd)) / "spread.yaml"
            with tmp_yaml.open() as fh:
                captured_yaml.append(_yaml.load(fh))

        with patch("opcli.core.spread.run_command", side_effect=capture_cmd):
            spread_run(tmp_path, ci=False)

        assert len(captured_yaml) == 1
        written = captured_yaml[0]
        assert "local" in written["backends"]
        assert "integration-test" not in written["backends"]
        assert written.get("reroot") == ".."

    def test_original_spread_yaml_never_modified(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)
        original_content = (tmp_path / "spread.yaml").read_text()

        with patch("opcli.core.spread.run_command"):
            spread_run(tmp_path, ci=False)

        assert (tmp_path / "spread.yaml").read_text() == original_content

    def test_original_spread_yaml_not_modified_on_failure(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)
        original_content = (tmp_path / "spread.yaml").read_text()

        def failing_cmd(cmd: list[str], **kwargs: object) -> None:
            raise SubprocessError(cmd=cmd, returncode=1, stderr="spread failed")

        with (
            patch("opcli.core.spread.run_command", side_effect=failing_cmd),
            pytest.raises(SubprocessError),
        ):
            spread_run(tmp_path, ci=False)

        assert (tmp_path / "spread.yaml").read_text() == original_content

    def test_temp_dir_cleaned_up_on_success(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch("opcli.core.spread.run_command"):
            spread_run(tmp_path, ci=False)

        leftover = list(tmp_path.glob(".spread-run-*"))
        assert leftover == []

    def test_extra_args_forwarded(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        _SELECTOR = "local:ubuntu-24.04:tests/integration/run:test_charm"
        with patch("opcli.core.spread.run_command") as mock_run:
            spread_run(
                tmp_path,
                extra_args=["-v", _SELECTOR],
                ci=False,
            )

        cmd = mock_run.call_args[0][0]
        assert cmd == ["spread", "-v", _SELECTOR]

    def test_expand_output_has_no_reroot(self, tmp_path: Path) -> None:
        """spread_expand() for display should not include reroot."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        assert "reroot" not in parsed

    def test_missing_spread_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            spread_run(tmp_path)


_MINIMAL_SPREAD_WITH_BOTH_BACKENDS = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04
  tutorial-test:
    systems:
      - ubuntu-24.04
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
  tests/tutorial/:
    summary: tutorial tests
    backends:
      - tutorial-test
    environment:
      TUTORIAL/tutorial1: docs/tutorial1.rst
"""


class TestTutorialBackend:
    """Tests for tutorial-test virtual backend expansion."""

    def test_expand_tutorial_local(self, tmp_path: Path) -> None:
        """tutorial-test expands to local-tutorial with minimal prepare."""
        spread = """\
project: test-project
path: /home/ubuntu/proj
backends:
  tutorial-test:
    systems:
      - ubuntu-24.04
suites:
  tests/tutorial/: {}
"""
        _write(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))

        assert "tutorial-test" not in result
        backend = parsed["backends"]["local-tutorial"]
        assert backend["type"] == "adhoc"
        assert "lxc launch --vm" in backend["allocate"]
        assert "lxc delete" in backend["discard"]
        assert "pipx install" in backend["prepare"]
        assert "astral.sh" not in backend["prepare"]
        assert "operator-ci-poc" in backend["prepare"]
        # No concierge/provision in tutorial prepare
        assert "concierge" not in backend["prepare"]
        assert "opcli provision load" not in backend["prepare"]

    def test_expand_tutorial_ci(self, tmp_path: Path) -> None:
        """tutorial-test CI expansion has no prepare."""
        spread = """\
project: test-project
backends:
  tutorial-test:
    systems:
      - ubuntu-24.04
suites:
  tests/tutorial/: {}
"""
        _write(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=True)
        parsed = _yaml.load(StringIO(result))

        backend = parsed["backends"]["ci-tutorial"]
        assert backend["type"] == "adhoc"
        assert "ADDRESS localhost" in backend["allocate"]
        assert "chpasswd" in backend["allocate"]
        assert "prepare" not in backend

    def test_tutorial_username_injected_local(self, tmp_path: Path) -> None:
        """Ubuntu user is injected into tutorial backend systems for local."""
        spread = """\
project: test-project
backends:
  tutorial-test:
    systems:
      - ubuntu-24.04
suites:
  tests/tutorial/: {}
"""
        _write(tmp_path / "spread.yaml", spread)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        systems = parsed["backends"]["local-tutorial"]["systems"]
        assert systems[0]["ubuntu-24.04"]["username"] == "ubuntu"

    def test_suite_backend_scoping_replaces_virtual_names(self, tmp_path: Path) -> None:
        """Suite backends lists are updated from virtual to concrete names."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD_WITH_BOTH_BACKENDS)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))

        assert "integration-test" not in result
        assert "tutorial-test" not in result
        assert parsed["suites"]["tests/integration/"]["backends"] == ["local"]
        assert parsed["suites"]["tests/tutorial/"]["backends"] == ["local-tutorial"]

    def test_both_backends_coexist(self, tmp_path: Path) -> None:
        """Both virtual backends can be expanded from the same spread.yaml."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD_WITH_BOTH_BACKENDS)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))

        assert "local" in parsed["backends"]
        assert "local-tutorial" in parsed["backends"]

    def test_no_known_virtual_backend_raises(self, tmp_path: Path) -> None:
        """Raises ConfigurationError when no known virtual backend is found."""
        spread = """\
project: test-project
backends:
  custom-backend:
    type: adhoc
suites:
  tests/: {}
"""
        _write(tmp_path / "spread.yaml", spread)

        with pytest.raises(ConfigurationError, match="no known virtual backend"):
            spread_expand(tmp_path)

    def test_generated_suite_has_backends_key(self, tmp_path: Path) -> None:
        """spread_init generates suite with backends: [integration-test] for scoping."""
        _write(tmp_path / "tests" / "integration" / "test_charm.py", "")
        spread_path, _ = spread_init(tmp_path)

        parsed = _yaml.load(StringIO(spread_path.read_text()))
        suite = parsed["suites"]["tests/integration/"]
        assert "backends" in suite
        assert "integration-test" in suite["backends"]

    def test_generated_suite_backends_replaced_after_expand(
        self, tmp_path: Path
    ) -> None:
        """After expansion, suite backends reference the concrete backend name."""
        _, _ = spread_init(tmp_path)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))

        suite = parsed["suites"]["tests/integration/"]
        assert "integration-test" not in suite.get("backends", [])
        assert "local" in suite["backends"]


# ---------------------------------------------------------------------------
#  Tests for _runner_by_system and spread_tasks
# ---------------------------------------------------------------------------

_SPREAD_WITH_RUNNER = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-22.04:
          runner: ubuntu-22.04-runner
      - ubuntu-24.04:
          runner: [self-hosted, ubuntu-24.04]
environment:
  CONCIERGE: concierge.yaml
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
      MODULE/test_other: test_other
"""

_SPREAD_NO_RUNNER = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04
environment:
  CONCIERGE: concierge.yaml
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
    environment:
      MODULE/test_charm: test_charm
"""


class TestRunnerBySystem:
    """Tests for _runner_by_system()."""

    def test_string_runner_label(self) -> None:
        """Runner string label is JSON-encoded in result."""
        raw = _yaml.load(StringIO(_SPREAD_WITH_RUNNER))
        result = _runner_by_system(raw)
        assert result["ubuntu-22.04"] == '"ubuntu-22.04-runner"'

    def test_list_runner_label(self) -> None:
        """Runner list is JSON-encoded when system uses a list."""
        raw = _yaml.load(StringIO(_SPREAD_WITH_RUNNER))
        result = _runner_by_system(raw)
        assert result["ubuntu-24.04"] == json.dumps(["self-hosted", "ubuntu-24.04"])

    def test_no_runner_defaults_to_ubuntu_latest(self) -> None:
        """Systems without runner: default to JSON-encoded ubuntu-latest."""
        raw = _yaml.load(StringIO(_SPREAD_NO_RUNNER))
        result = _runner_by_system(raw)
        assert result.get("ubuntu-24.04") == '"ubuntu-latest"'

    def test_empty_backends(self) -> None:
        """No backends → empty result."""
        result = _runner_by_system({})
        assert result == {}


class TestSpreadTasks:
    """Tests for spread_tasks()."""

    def _make_task_dir(self, root: Path, path: str) -> None:
        task_path = root / path / "task.yaml"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text("summary: test task\n")

    def test_returns_selectors_for_each_variant(self, tmp_path: Path) -> None:
        """Returns one entry per (system, task_dir, variant) combination."""
        _write(tmp_path / "spread.yaml", _SPREAD_WITH_RUNNER)
        self._make_task_dir(tmp_path, "tests/integration/run")

        entries = spread_tasks(tmp_path)

        names = [e["name"] for e in entries]
        assert "test_charm" in names
        assert "test_other" in names

    def test_selector_format(self, tmp_path: Path) -> None:
        """Selector includes backend:system:suite/task:variant."""
        _write(tmp_path / "spread.yaml", _SPREAD_NO_RUNNER)
        self._make_task_dir(tmp_path, "tests/integration/run")

        entries = spread_tasks(tmp_path)

        assert len(entries) == 1
        entry = entries[0]
        assert entry["selector"].startswith("ci:")
        assert "ubuntu-24.04" in entry["selector"]
        assert ":test_charm" in entry["selector"]

    def test_runs_on_from_runner_field(self, tmp_path: Path) -> None:
        """runs-on matches the system's runner: label (JSON-encoded)."""
        _write(tmp_path / "spread.yaml", _SPREAD_WITH_RUNNER)
        self._make_task_dir(tmp_path, "tests/integration/run")

        entries = spread_tasks(tmp_path)

        ubuntu_22_entries = [e for e in entries if "ubuntu-22.04" in e["selector"]]
        assert all(e["runs-on"] == '"ubuntu-22.04-runner"' for e in ubuntu_22_entries)

    def test_no_variants_uses_task_dir_name(self, tmp_path: Path) -> None:
        """When no MODULE/ variants, name is the task directory name."""
        spread_no_variants = """\
project: test-project
path: /home/ubuntu/proj
backends:
  integration-test:
    systems:
      - ubuntu-24.04
environment:
  CONCIERGE: concierge.yaml
suites:
  tests/integration/:
    summary: integration tests
    backends:
      - integration-test
"""
        _write(tmp_path / "spread.yaml", spread_no_variants)
        self._make_task_dir(tmp_path, "tests/integration/run")

        entries = spread_tasks(tmp_path)

        assert len(entries) == 1
        assert entries[0]["name"] == "run"

    def test_missing_spread_yaml_raises(self, tmp_path: Path) -> None:
        """Raises ConfigurationError when spread.yaml is missing."""
        with pytest.raises(ConfigurationError):
            spread_tasks(tmp_path)

    def test_ci_backend_has_username_root(self, tmp_path: Path) -> None:
        """Expanded CI backend sets username: root per system for SSH."""
        _write(tmp_path / "spread.yaml", _SPREAD_NO_RUNNER)

        result = spread_expand(tmp_path, ci=True)
        parsed = _yaml.load(StringIO(result))

        ci_backend = parsed["backends"].get("ci")
        assert ci_backend is not None
        systems = ci_backend.get("systems", [])
        assert len(systems) > 0
        # username is set per-system entry
        for system_entry in systems:
            if isinstance(system_entry, dict):
                for _sys_name, sys_props in system_entry.items():
                    assert isinstance(sys_props, dict)
                    assert sys_props.get("username") == "root"

    def test_ci_backend_strips_runner_field(self, tmp_path: Path) -> None:
        """Expanded CI backend does not contain runner: key in systems."""
        _write(tmp_path / "spread.yaml", _SPREAD_WITH_RUNNER)

        result = spread_expand(tmp_path, ci=True)
        parsed = _yaml.load(StringIO(result))

        ci_backend = parsed["backends"].get("ci")
        assert ci_backend is not None
        for system_entry in ci_backend.get("systems", []):
            if isinstance(system_entry, dict):
                for _sys_name, sys_props in system_entry.items():
                    if isinstance(sys_props, dict):
                        assert "runner" not in sys_props
