"""Core logic for ``opcli install spread`` and ``opcli install tox``.

These commands install tool dependencies needed by the spread test
environment.  They are designed to be called from the spread ``prepare:``
script after opcli itself has been bootstrapped.
"""

from __future__ import annotations

import shutil

from opcli.core.subprocess import run_command


def install_spread() -> None:
    """Install spread if not already on PATH.

    Installs Go via snap and builds spread from source.
    """
    if shutil.which("spread"):
        return
    run_command(["snap", "install", "go", "--classic"])
    run_command(
        ["go", "install", "github.com/canonical/spread/cmd/spread@latest"],
    )
    run_command(["ln", "-sf", "/root/go/bin/spread", "/usr/local/bin/spread"])


def install_tox() -> None:
    """Install tox with tox-uv via uv tool.

    Uses /usr/local/bin as the tool bin directory so that tox is
    available system-wide.
    """
    run_command(
        ["uv", "tool", "install", "tox", "--with", "tox-uv", "--quiet"],
        env={
            "UV_TOOL_BIN_DIR": "/usr/local/bin",
            "UV_TOOL_DIR": "/usr/local/share/uv-tools",
        },
    )
