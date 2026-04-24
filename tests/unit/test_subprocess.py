"""Tests for the central subprocess wrapper."""

from __future__ import annotations

import pytest

from opcli.core.exceptions import SubprocessError
from opcli.core.subprocess import run_command


class TestStdinCaptured:
    """stdin forwarding in captured (non-streaming) mode."""

    def test_stdin_piped_to_process(self) -> None:
        result = run_command(
            ["cat"],
            stream=False,
            stdin="hello from stdin\n",
        )
        assert result.stdout == "hello from stdin\n"
        assert result.returncode == 0

    def test_stdin_none_is_harmless(self) -> None:
        result = run_command(["echo", "ok"], stream=False, stdin=None)
        assert "ok" in result.stdout
        assert result.returncode == 0


class TestStdinStreaming:
    """stdin forwarding in streaming (real-time) mode."""

    def test_stdin_piped_to_process(self) -> None:
        result = run_command(
            ["cat"],
            stream=True,
            stdin="streamed input\n",
        )
        assert result.stdout == "streamed input\n"
        assert result.returncode == 0

    def test_stdin_none_is_harmless(self) -> None:
        result = run_command(["echo", "ok"], stream=True, stdin=None)
        assert "ok" in result.stdout
        assert result.returncode == 0

    def test_broken_pipe_does_not_raise_from_writer_thread(self) -> None:
        """Commands that close stdin early must not crash the wrapper."""
        # `true` exits immediately without reading stdin — triggers BrokenPipeError.
        result = run_command(["true"], stream=True, stdin="ignored input")
        assert result.returncode == 0

    def test_failed_command_raises_subprocess_error(self) -> None:
        with pytest.raises(SubprocessError):
            run_command(["false"], stream=True, stdin="some input")


class TestInteractiveMutualExclusion:
    """interactive and stdin are mutually exclusive."""

    def test_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            run_command(["echo", "hi"], interactive=True, stdin="hello")
