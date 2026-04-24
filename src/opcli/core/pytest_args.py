"""Core logic for ``opcli pytest expand``.

Reads ``artifacts-generated.yaml`` (schema v1) and assembles the flags that
tox/pytest need to locate built charms and their OCI-image resources.

``artifacts-generated.yaml`` carries resolved resource paths inside each
charm entry, so this module no longer needs to read ``artifacts.yaml``.

Convention (matching operator-workflows):
    --charm-file=<path>          for each locally built charm
    --<resource-name>=<path>     for each OCI-image resource with a local file
    --<resource-name>=<image>    for each OCI-image resource with a registry image

The ``KEY=VALUE`` form is required because some conftest.py files register
``--charm-file`` with ``nargs="+"``, which makes argparse greedy.  With the
space-separated form, subsequent ``--charm-file`` tokens would be consumed as
values of the first flag instead of starting a new flag.
"""

from __future__ import annotations

import logging
from pathlib import Path

from opcli.core.exceptions import ConfigurationError
from opcli.core.yaml_io import load_artifacts_generated

logger = logging.getLogger(__name__)

_ARTIFACTS_GENERATED_YAML = "artifacts-generated.yaml"


def assemble_pytest_args(
    root: Path,
) -> list[str]:
    """Build the list of pytest flags from the generated artifacts.

    Returns:
        A list of CLI flags like
        ``["--charm-file=path.charm", "--img=path.rock"]``.

    Raises:
        ConfigurationError: If ``artifacts-generated.yaml`` is missing.
    """
    gen_path = root / _ARTIFACTS_GENERATED_YAML
    if not gen_path.exists():
        msg = (
            f"{_ARTIFACTS_GENERATED_YAML} not found. Run 'opcli artifacts build' first."
        )
        raise ConfigurationError(msg)

    generated = load_artifacts_generated(gen_path)

    args: list[str] = []

    for charm in generated.charms:
        if charm.output.file:
            args.append(f"--charm-file={charm.output.file}")
        elif charm.output.artifact:
            logger.warning(
                "Skipping --charm-file for charm '%s': output is a CI artifact "
                "(%s). Run 'opcli artifacts build' locally to get a local file.",
                charm.name,
                charm.output.artifact,
            )

        for res_name, res in (charm.resources or {}).items():
            value = res.image or res.file
            if value:
                args.append(f"--{res_name}={value}")

    return args


def assemble_tox_argv(
    root: Path,
    *,
    tox_env: str = "integration",
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build the full tox argv for running integration tests.

    Returns a list of tokens suitable for shell execution or display via
    ``shlex.join()``.  The ``--`` separator is included only when there are
    pytest flags or forwarded extra args to pass.

    Raises:
        ConfigurationError: If required YAML files are missing.
    """
    assembled = assemble_pytest_args(root)
    pytest_args = assembled + (extra_args or [])

    cmd: list[str] = ["tox", "-e", tox_env]
    if pytest_args:
        cmd += ["--", *pytest_args]
    return cmd
