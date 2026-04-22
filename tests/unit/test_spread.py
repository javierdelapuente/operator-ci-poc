"""Tests for ``opcli spread init``, ``expand``, and ``run``."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from opcli.core.exceptions import ConfigurationError, SubprocessError
from opcli.core.spread import spread_expand, spread_init, spread_run

_yaml = YAML()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


_MINIMAL_SPREAD = """\
project: test-project
backends:
  integration-test:
    systems:
      - ubuntu-24.04
environment:
  MODULE/test_charm: test_charm
suites:
  tests/: {}
"""


class TestSpreadInit:
    """Tests for spread_init()."""

    def test_generates_files(self, tmp_path: Path) -> None:
        spread_path, task_path = spread_init(tmp_path)

        assert spread_path.exists()
        assert task_path.exists()
        assert task_path == tmp_path / "tests" / "run" / "task.yaml"

        content = spread_path.read_text()
        assert "integration-test" in content

        task_content = task_path.read_text()
        assert "opcli pytest run" in task_content

    def test_discovers_test_modules(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "tests" / "integration"
        test_dir.mkdir(parents=True)
        (test_dir / "test_charm.py").write_text("")
        (test_dir / "test_actions.py").write_text("")
        (test_dir / "conftest.py").write_text("")  # not a test module

        spread_path, _ = spread_init(tmp_path)

        content = spread_path.read_text()
        assert "MODULE/test_charm" in content
        assert "MODULE/test_actions" in content
        assert "conftest" not in content

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", "existing\n")

        with pytest.raises(ConfigurationError, match="already exists"):
            spread_init(tmp_path)

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", "old\n")
        _write(tmp_path / "tests" / "run" / "task.yaml", "old\n")

        spread_path, task_path = spread_init(tmp_path, force=True)
        assert "integration-test" in spread_path.read_text()
        assert "opcli pytest run" in task_path.read_text()

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
        assert "concierge" in local["prepare"]
        assert "opcli provision load" in local["prepare"]
        assert local["systems"] == ["ubuntu-24.04"]

    def test_expand_ci(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=True)
        parsed = _yaml.load(StringIO(result))

        assert "integration-test" not in result
        ci = parsed["backends"]["ci"]
        assert ci["type"] == "adhoc"
        assert ci["allocate"] == "ADDRESS localhost"
        assert "concierge" in ci["prepare"]
        assert "discard" not in ci
        assert ci["systems"] == ["ubuntu-24.04"]

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
  tests/: {}
"""
        _write(tmp_path / "spread.yaml", spread_with_extras)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        local = parsed["backends"]["local"]

        assert local["environment"] == {"EXTRA_VAR": "hello"}
        assert "extra setup" in local["prepare-each"]
        assert local["kill-timeout"] == "30m"
        assert local["systems"] == ["ubuntu-24.04"]
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

    def test_no_virtual_backend_raises(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch.dict("os.environ", {"CI": "true"}):
            result = spread_expand(tmp_path)
        parsed = _yaml.load(StringIO(result))
        assert "ci" in parsed["backends"]
        assert parsed["backends"]["ci"]["allocate"] == "ADDRESS localhost"

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


class TestSpreadRun:
    """Tests for spread_run()."""

    def test_runs_spread_from_temp_dir(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        captured_cwd: list[str] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> None:
            captured_cwd.append(str(kwargs.get("cwd", "")))

        with patch("opcli.core.spread.run_command", side_effect=capture_cmd):
            spread_run(tmp_path, ci=False)

        assert len(captured_cwd) == 1
        cwd = Path(captured_cwd[0])
        # Temp dir must be inside the project root
        assert cwd.parent == tmp_path
        assert cwd.name.startswith(".opcli-spread-")

    def test_no_fake_spread_flag(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch("opcli.core.spread.run_command") as mock_run:
            spread_run(tmp_path, ci=False)

        cmd = mock_run.call_args[0][0]
        assert cmd == ["spread"]
        # No -spread= flag should ever appear
        assert not any(arg.startswith("-spread=") for arg in cmd)

    def test_temp_dir_contains_spread_yaml_with_reroot(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        written_yaml: list[dict[str, object]] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> None:
            cwd = Path(str(kwargs.get("cwd", "")))
            spread_file = cwd / "spread.yaml"
            assert spread_file.exists()
            with spread_file.open() as fh:
                written_yaml.append(_yaml.load(fh))

        with patch("opcli.core.spread.run_command", side_effect=capture_cmd):
            spread_run(tmp_path, ci=False)

        assert len(written_yaml) == 1
        assert written_yaml[0]["reroot"] == ".."

    def test_extra_args_forwarded(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        with patch("opcli.core.spread.run_command") as mock_run:
            spread_run(
                tmp_path,
                extra_args=["-v", "local:ubuntu-24.04:tests/run:test_charm"],
                ci=False,
            )

        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "spread",
            "-v",
            "local:ubuntu-24.04:tests/run:test_charm",
        ]

    def test_temp_dir_cleaned_up_on_success(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        captured_cwd: list[str] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> None:
            captured_cwd.append(str(kwargs.get("cwd", "")))

        with patch("opcli.core.spread.run_command", side_effect=capture_cmd):
            spread_run(tmp_path, ci=False)

        assert not Path(captured_cwd[0]).exists()

    def test_temp_dir_cleaned_up_on_failure(self, tmp_path: Path) -> None:
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        captured_cwd: list[str] = []

        def failing_cmd(cmd: list[str], **kwargs: object) -> None:
            captured_cwd.append(str(kwargs.get("cwd", "")))
            raise SubprocessError(cmd=cmd, returncode=1, stderr="spread failed")

        with (
            patch("opcli.core.spread.run_command", side_effect=failing_cmd),
            pytest.raises(SubprocessError),
        ):
            spread_run(tmp_path, ci=False)

        assert not Path(captured_cwd[0]).exists()

    def test_preserves_existing_reroot(self, tmp_path: Path) -> None:
        spread_with_reroot = _MINIMAL_SPREAD + "reroot: custom/path\n"
        _write(tmp_path / "spread.yaml", spread_with_reroot)

        written_yaml: list[dict[str, object]] = []

        def capture_cmd(cmd: list[str], **kwargs: object) -> None:
            cwd = Path(str(kwargs.get("cwd", "")))
            with (cwd / "spread.yaml").open() as fh:
                written_yaml.append(_yaml.load(fh))

        with patch("opcli.core.spread.run_command", side_effect=capture_cmd):
            spread_run(tmp_path, ci=False)

        # ../custom/path normalised
        assert written_yaml[0]["reroot"] == "../custom/path"

    def test_non_string_reroot_raises(self, tmp_path: Path) -> None:
        spread_with_bad_reroot = _MINIMAL_SPREAD + "reroot: 42\n"
        _write(tmp_path / "spread.yaml", spread_with_bad_reroot)

        with pytest.raises(ConfigurationError, match="must be a string"):
            spread_run(tmp_path, ci=False)

    def test_absolute_reroot_raises(self, tmp_path: Path) -> None:
        spread_with_abs_reroot = _MINIMAL_SPREAD + "reroot: /absolute/path\n"
        _write(tmp_path / "spread.yaml", spread_with_abs_reroot)

        with pytest.raises(ConfigurationError, match="must be a relative path"):
            spread_run(tmp_path, ci=False)

    def test_expand_output_has_no_reroot(self, tmp_path: Path) -> None:
        """spread_expand() for display should not include reroot."""
        _write(tmp_path / "spread.yaml", _MINIMAL_SPREAD)

        result = spread_expand(tmp_path, ci=False)
        parsed = _yaml.load(StringIO(result))
        assert "reroot" not in parsed

    def test_missing_spread_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            spread_run(tmp_path)
