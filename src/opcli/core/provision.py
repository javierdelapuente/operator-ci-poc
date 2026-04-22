"""Core logic for ``opcli provision run`` and ``opcli provision load``.

``run`` invokes concierge to provision the test environment.

``load`` reads ``artifacts-generated.yaml`` and pushes locally-built rock
OCI images into a container image registry so that Juju / MicroK8s can
pull them during integration tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

from opcli.core.exceptions import ConfigurationError
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import load_artifacts_generated

logger = logging.getLogger(__name__)

_CONCIERGE_YAML = "concierge.yaml"
_ARTIFACTS_GENERATED_YAML = "artifacts-generated.yaml"
_DEFAULT_REGISTRY = "localhost:32000"


def provision_run(
    root: Path,
    *,
    concierge_file: str = _CONCIERGE_YAML,
) -> None:
    """Run ``sudo concierge prepare`` to provision the test environment.

    Raises:
        ConfigurationError: If the concierge file does not exist.
        SubprocessError: If concierge exits non-zero.
    """
    concierge_path = root / concierge_file
    if not concierge_path.exists():
        msg = (
            f"{concierge_file} not found. "
            "Create a concierge.yaml in the repository root."
        )
        raise ConfigurationError(msg)

    run_command(
        ["sudo", "concierge", "prepare", "-c", str(concierge_path)],
        cwd=str(root),
    )
    logger.info("Provisioning complete via %s", concierge_file)


def provision_load(
    root: Path,
    *,
    registry: str = _DEFAULT_REGISTRY,
) -> list[str]:
    """Push locally-built rock images to *registry*.

    Reads ``artifacts-generated.yaml`` and for each rock with a local
    ``file`` output, converts the ``.rock`` archive to an OCI image and
    pushes it to the target registry using ``skopeo``.

    Returns:
        List of image references that were pushed.

    Raises:
        ConfigurationError: If ``artifacts-generated.yaml`` is missing.
        SubprocessError: If a push command fails.
    """
    gen_path = root / _ARTIFACTS_GENERATED_YAML
    if not gen_path.exists():
        msg = (
            f"{_ARTIFACTS_GENERATED_YAML} not found. Run 'opcli artifacts build' first."
        )
        raise ConfigurationError(msg)

    generated = load_artifacts_generated(gen_path)
    pushed: list[str] = []

    for rock in generated.rocks:
        if not rock.output.file:
            continue

        rock_path = Path(rock.output.file)
        image_ref = f"{registry}/{rock.name}:latest"

        # rockcraft.load converts a .rock archive to a local Docker image
        run_command(
            [
                "sudo",
                "rockcraft.skopeo",
                "--insecure-policy",
                "copy",
                f"oci-archive:{rock_path}",
                f"docker-daemon:{rock.name}:latest",
            ],
            cwd=str(root),
        )

        # Push to the target registry
        run_command(
            [
                "sudo",
                "rockcraft.skopeo",
                "--insecure-policy",
                "copy",
                "--dest-tls-verify=false",
                f"docker-daemon:{rock.name}:latest",
                f"docker://{image_ref}",
            ],
            cwd=str(root),
        )

        pushed.append(image_ref)
        logger.info("Pushed %s", image_ref)

    return pushed
