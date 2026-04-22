"""Smoke tests for the opcli scaffold."""

import io
import subprocess as sp
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

    def test_subprocess_error_formats_with_shlex(self) -> None:
        err = SubprocessError(cmd=["echo", "hello world"], returncode=1, stderr="fail")
        assert "echo 'hello world'" in str(err)

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

    # --- Captured mode (stream=False) ---

    def test_captured_success(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "ok\n"
            mock_run.return_value.stderr = ""
            mock_run.return_value.returncode = 0

            result = run_command(["echo", "hello"], stream=False)

            assert isinstance(result, SubprocessResult)
            assert result.stdout == "ok\n"
            assert result.returncode == 0
            mock_run.assert_called_once()

    def test_captured_failure_raises(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "not found"
            mock_run.return_value.returncode = 127

            with pytest.raises(SubprocessError, match="not found"):
                run_command(["bad-cmd"], stream=False)

    def test_captured_no_check(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "warn"
            mock_run.return_value.returncode = 1

            result = run_command(["cmd"], check=False, stream=False)
            assert result.returncode == 1

    def test_captured_file_not_found(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("No such file")

            with pytest.raises(SubprocessError) as exc_info:
                run_command(["nonexistent-binary"], stream=False)

            assert exc_info.value.returncode == 127  # noqa: PLR2004
            assert "No such file" in exc_info.value.stderr

    def test_captured_timeout(self) -> None:
        with patch("opcli.core.subprocess.subprocess.run") as mock_run:
            mock_run.side_effect = sp.TimeoutExpired(cmd=["slow"], timeout=5)

            with pytest.raises(SubprocessError, match="timed out"):
                run_command(["slow"], timeout=5, stream=False)

    # --- Streaming mode (stream=True, the default) ---

    def test_streaming_success(self) -> None:
        with patch("opcli.core.subprocess.subprocess.Popen") as mock_popen:
            proc = mock_popen.return_value
            proc.stdout = io.StringIO("line1\nline2\n")
            proc.stderr = io.StringIO("")
            proc.returncode = 0
            proc.wait.return_value = 0

            result = run_command(["build", "thing"])

            assert result.stdout == "line1\nline2\n"
            assert result.returncode == 0

    def test_streaming_failure_raises(self) -> None:
        with patch("opcli.core.subprocess.subprocess.Popen") as mock_popen:
            proc = mock_popen.return_value
            proc.stdout = io.StringIO("")
            proc.stderr = io.StringIO("error output\n")
            proc.returncode = 1
            proc.wait.return_value = 1

            with pytest.raises(SubprocessError, match="error output"):
                run_command(["bad-build"])

    def test_streaming_file_not_found(self) -> None:
        with patch("opcli.core.subprocess.subprocess.Popen") as mock_popen:
            mock_popen.side_effect = FileNotFoundError("No such file")

            with pytest.raises(SubprocessError) as exc_info:
                run_command(["missing-tool"])

            assert exc_info.value.returncode == 127  # noqa: PLR2004
