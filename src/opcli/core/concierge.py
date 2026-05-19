"""Core logic for ``opcli concierge prepare``.

Installs the concierge snap and runs ``concierge prepare`` to provision
the test environment.  No-op if no ``concierge.yaml`` is found.
"""

from __future__ import annotations

import logging
from pathlib import Path

from opcli.core.subprocess import run_command

logger = logging.getLogger(__name__)

_CONCIERGE_YAML = "concierge.yaml"


def concierge_prepare(
    root: Path,
    *,
    concierge_file: str = _CONCIERGE_YAML,
) -> bool:
    """Install concierge and run ``concierge prepare``.

    Returns True if concierge was run, False if skipped (no config file).
    """
    concierge_path = root / concierge_file
    if not concierge_path.exists():
        logger.info("No %s found — skipping concierge.", concierge_file)
        return False

    run_command(["snap", "install", "concierge", "--classic"], check=False)
    run_command(
        ["concierge", "prepare", "-c", str(concierge_path)],
        cwd=str(root),
    )
    logger.info("Concierge provisioning complete via %s", concierge_file)
    return True
