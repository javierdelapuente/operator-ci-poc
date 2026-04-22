"""Smoke tests for the opcli scaffold."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opcli.app import app
from opcli.core.exceptions import (
    ConfigurationError,
    DiscoveryError,
    OpcliError,
    SubprocessError,
    ValidationError,
)
from opcli.core.subprocess import SubprocessResult, run_command

runner = CliRunner()


class TestCLIEntryPoint:
    """Verify the Typer app is wired correctly."""

    def test_help_exits_zero(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "opcli" in result.output.lower()

    def test_artifacts_help(self) -> None:
        result = runner.invoke(app, ["artifacts", "--help"])
        assert result.exit_code == 0
        assert "artifacts" in result.output.lower()

    def test_provision_help(self) -> None:
        result = runner.invoke(app, ["provision", "--help"])
        assert result.exit_code == 0

    def test_spread_help(self) -> None:
        result = runner.invoke(app, ["spread", "--help"])
        assert result.exit_code == 0

    def test_pytest_help(self) -> None:
        result = runner.invoke(app, ["pytest", "--help"])
        assert result.exit_code == 0


class TestExceptionHierarchy:
    """Verify exception types are catchable as OpcliError."""

    def test_subprocess_error_is_opcli_error(self) -> None:
        err = SubprocessError(cmd=["false"], returncode=1, stderr="fail")
        assert isinstance(err, OpcliError)
        assert err.returncode == 1
        assert err.stderr == "fail"
        assert "false" in str(err)

    @pytest.mark.parametrize(
        "exc_cls",
        [
            SubprocessError,
            ValidationError,
            DiscoveryError,
            ConfigurationError,
        ],
    )
    def test_all_exceptions_inherit_from_base(self, exc_cls: type) -> None:
        assert issubclass(exc_cls, OpcliError)


class TestSubprocessWrapper:
    """Verify the subprocess wrapper mocking pattern works."""

    def test_run_command_success(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "ok\n"
            mock_run.return_value.stderr = ""
            mock_run.return_value.returncode = 0

            result = run_command(["echo", "hello"])

            assert isinstance(result, SubprocessResult)
            assert result.stdout == "ok\n"
            assert result.returncode == 0
            mock_run.assert_called_once()

    def test_run_command_failure_raises(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "not found"
            mock_run.return_value.returncode = 127

            with pytest.raises(SubprocessError, match="not found"):
                run_command(["bad-cmd"])

    def test_run_command_no_check(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "warn"
            mock_run.return_value.returncode = 1

            result = run_command(["cmd"], check=False)
            assert result.returncode == 1
