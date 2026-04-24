"""Core logic for ``opcli pytest expand``.

Reads ``artifacts-generated.yaml`` and assembles the flags that tox/pytest
need to locate built charms and their OCI-image resources.

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
from opcli.core.yaml_io import load_artifacts_generated, load_artifacts_plan
from opcli.models.artifacts import ArtifactsPlan
from opcli.models.artifacts_generated import ArtifactsGenerated

logger = logging.getLogger(__name__)

_ARTIFACTS_YAML = "artifacts.yaml"
_ARTIFACTS_GENERATED_YAML = "artifacts-generated.yaml"


def _resolve_resource_value(
    resource_rock: str | None,
    generated: ArtifactsGenerated,
) -> str | None:
    """Find the output value for a rock linked to a charm resource."""
    if resource_rock is None:
        return None
    for rock in generated.rocks:
        if rock.name == resource_rock:
            return rock.output.file or rock.output.image
    return None


def assemble_pytest_args(
    root: Path,
) -> list[str]:
    """Build the list of pytest flags from the generated artifacts.

    Returns:
        A list of CLI flags like
        ``["--charm-file=path.charm", "--img=path.rock"]``.

    Raises:
        ConfigurationError: If required YAML files are missing.
    """
    gen_path = root / _ARTIFACTS_GENERATED_YAML
    if not gen_path.exists():
        msg = (
            f"{_ARTIFACTS_GENERATED_YAML} not found. Run 'opcli artifacts build' first."
        )
        raise ConfigurationError(msg)

    generated = load_artifacts_generated(gen_path)

    # Load the plan to get resource definitions (rock links).
    plan_path = root / _ARTIFACTS_YAML
    plan: ArtifactsPlan | None = None
    if plan_path.exists():
        plan = load_artifacts_plan(plan_path)

    args: list[str] = []

    for charm in generated.charms:
        if charm.output.file:
            args.append(f"--charm-file={charm.output.file}")

        # Resolve resource flags from the plan's resource definitions.
        if plan is None:
            continue
        plan_charm = next((c for c in plan.charms if c.name == charm.name), None)
        if plan_charm is None:
            continue
        for res_name, res_def in plan_charm.resources.items():
            value = _resolve_resource_value(res_def.rock, generated)
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
