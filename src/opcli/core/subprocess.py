"""Central subprocess wrapper.

All external command execution goes through :func:`run_command` so that
tests can mock a single boundary and we get consistent timeout /
error handling everywhere.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from opcli.core.exceptions import SubprocessError

_DEFAULT_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class SubprocessResult:
    """Captured output of a finished subprocess."""

    stdout: str
    stderr: str
    returncode: int


def run_command(
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    check: bool = True,
) -> SubprocessResult:
    """Execute *cmd* and return captured output.

    Args:
        cmd: Command and arguments.
        cwd: Working directory for the subprocess.
        timeout: Maximum wall-clock seconds before the process is killed.
        check: If ``True`` (default), raise :class:`SubprocessError` on
            non-zero exit codes.

    Raises:
        SubprocessError: If the command fails and *check* is ``True``.

    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            check=False,
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
