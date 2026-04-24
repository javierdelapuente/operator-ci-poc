"""Central subprocess wrapper.

All external command execution goes through :func:`run_command` so that
tests can mock a single boundary and we get consistent timeout /
error handling everywhere.

Every invocation prints the command and working directory so that
failures can be reproduced manually by copy-pasting from the output.
"""

from __future__ import annotations

import io
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass

import typer

from opcli.core.exceptions import SubprocessError

_DEFAULT_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class SubprocessResult:
    """Captured output of a finished subprocess."""

    stdout: str
    stderr: str
    returncode: int


def _stream_pipe(
    pipe: io.TextIOWrapper,
    buf: list[str],
    dest: io.TextIOWrapper,
) -> None:
    """Read *pipe* line-by-line, echo to *dest*, accumulate in *buf*."""
    for line in pipe:
        buf.append(line)
        dest.write(line)
        dest.flush()


def _write_stdin(pipe: io.TextIOWrapper, data: str) -> None:
    """Write *data* to *pipe* and close it.

    ``BrokenPipeError`` and ``ValueError`` are silently swallowed — they
    mean the subprocess closed its stdin early, which is normal for commands
    that consume all their input before exiting.
    """
    try:
        pipe.write(data)
        pipe.close()
    except (BrokenPipeError, ValueError):
        pass


def run_command(  # noqa: PLR0913
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    check: bool = True,
    stream: bool = True,
    interactive: bool = False,
    stdin: str | None = None,
) -> SubprocessResult:
    """Execute *cmd* and return captured output.

    Args:
        cmd: Command and arguments.
        cwd: Working directory for the subprocess.
        timeout: Maximum wall-clock seconds before the process is killed.
            Ignored when *interactive* is ``True``.
        check: If ``True`` (default), raise :class:`SubprocessError` on
            non-zero exit codes.
        stream: If ``True`` (default), echo stdout/stderr to the terminal
            in real time while still capturing them. If ``False``, buffer
            all output silently (useful for commands whose stdout is
            consumed programmatically, like ``opcli pytest args``).
        interactive: If ``True``, inherit the parent's stdin/stdout/stderr
            so the subprocess has full TTY access. Required for commands
            like ``spread -shell``. Output is not captured in this mode.
            Cannot be combined with *stdin*.
        stdin: Optional string to feed to the subprocess's standard input.
            Useful for commands that read from stdin (e.g. ``kubectl apply
            -f -``). Cannot be combined with *interactive*.

    Raises:
        SubprocessError: If the command fails and *check* is ``True``.
        ValueError: If both *interactive* and *stdin* are provided.

    """
    if interactive and stdin is not None:
        msg = "'interactive' and 'stdin' are mutually exclusive"
        raise ValueError(msg)
    if interactive:
        return _run_interactive(cmd, cwd=cwd, check=check)
    if stream:
        return _run_streaming(cmd, cwd=cwd, timeout=timeout, check=check, stdin=stdin)
    return _run_captured(cmd, cwd=cwd, timeout=timeout, check=check, stdin=stdin)


def _log_command(cmd: list[str], cwd: str | None) -> None:
    """Print the command and working directory for reproducibility."""
    typer.echo(f"$ {shlex.join(cmd)}")
    if cwd:
        typer.echo(f"  cwd: {cwd}")


def _run_interactive(
    cmd: list[str],
    *,
    cwd: str | None = None,
    check: bool = True,
) -> SubprocessResult:
    """Run *cmd* with inherited stdin/stdout/stderr for full TTY access."""
    _log_command(cmd, cwd)
    try:
        proc = subprocess.run(cmd, cwd=cwd, check=False)
    except OSError as exc:
        raise SubprocessError(
            cmd=cmd,
            returncode=127 if isinstance(exc, FileNotFoundError) else -1,
            stderr=str(exc),
        ) from exc

    result = SubprocessResult(stdout="", stderr="", returncode=proc.returncode)

    if check and result.returncode != 0:
        raise SubprocessError(
            cmd=cmd,
            returncode=result.returncode,
            stderr="(output not captured in interactive mode)",
        )

    return result


def _run_streaming(
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    check: bool = True,
    stdin: str | None = None,
) -> SubprocessResult:
    """Run *cmd* with real-time output to the terminal."""
    _log_command(cmd, cwd)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin is not None else None,
            text=True,
            cwd=cwd,
        )
    except OSError as exc:
        raise SubprocessError(
            cmd=cmd,
            returncode=127 if isinstance(exc, FileNotFoundError) else -1,
            stderr=str(exc),
        ) from exc

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    assert proc.stdout is not None
    assert proc.stderr is not None

    out_thread = threading.Thread(
        target=_stream_pipe,
        args=(proc.stdout, stdout_lines, sys.stdout),
        daemon=True,
    )
    err_thread = threading.Thread(
        target=_stream_pipe,
        args=(proc.stderr, stderr_lines, sys.stderr),
        daemon=True,
    )
    out_thread.start()
    err_thread.start()

    # Write stdin in a dedicated thread so the main thread remains free to
    # enforce the timeout and reader threads can drain stdout/stderr
    # concurrently, preventing deadlocks.
    if stdin is not None and proc.stdin is not None:
        in_thread = threading.Thread(
            target=_write_stdin,
            args=(proc.stdin, stdin),
            daemon=True,
        )
        in_thread.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        out_thread.join(timeout=2)
        err_thread.join(timeout=2)
        partial = "".join(stderr_lines)
        raise SubprocessError(
            cmd=cmd,
            returncode=-1,
            stderr=f"Command timed out after {timeout}s\n{partial}".strip(),
        ) from exc

    out_thread.join()
    err_thread.join()

    result = SubprocessResult(
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        returncode=proc.returncode,
    )

    if check and result.returncode != 0:
        raise SubprocessError(
            cmd=cmd,
            returncode=result.returncode,
            stderr=result.stderr,
        )

    return result


def _run_captured(
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    check: bool = True,
    stdin: str | None = None,
) -> SubprocessResult:
    """Run *cmd* with fully buffered output (no terminal echo)."""
    _log_command(cmd, cwd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            check=False,
            input=stdin,
        )
    except subprocess.TimeoutExpired as exc:
        partial_err = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise SubprocessError(
            cmd=cmd,
            returncode=-1,
            stderr=f"Command timed out after {timeout}s\n{partial_err}".strip(),
        ) from exc
    except OSError as exc:
        raise SubprocessError(
            cmd=cmd,
            returncode=127 if isinstance(exc, FileNotFoundError) else -1,
            stderr=str(exc),
        ) from exc

    result = SubprocessResult(
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )

    if check and result.returncode != 0:
        raise SubprocessError(
            cmd=cmd,
            returncode=result.returncode,
            stderr=result.stderr,
        )

    return result
