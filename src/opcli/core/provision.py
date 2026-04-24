"""Core logic for ``opcli provision run``, ``opcli provision load``,
and ``opcli provision registry``.

``run`` invokes concierge to provision the test environment.

``load`` reads ``artifacts-generated.yaml`` and pushes locally-built rock
OCI images into a container image registry so that Juju / MicroK8s can
pull them during integration tests.

``registry`` deploys a local OCI registry at ``localhost:32000`` using a
Kubernetes manifest (``src/opcli/data/registry.yaml``).  The manifest works
identically on both canonical k8s and MicroK8s — it creates a
``container-registry`` namespace, a ``registry:2`` deployment, and a
NodePort Service on port 32000.  This is a local-only operation — in CI
images are served from GHCR.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

from ruamel.yaml import YAML

from opcli.core.exceptions import ConfigurationError
from opcli.core.subprocess import run_command
from opcli.core.yaml_io import dump_artifacts_generated, load_artifacts_generated

logger = logging.getLogger(__name__)

_CONCIERGE_YAML = "concierge.yaml"
_ARTIFACTS_GENERATED_YAML = "artifacts-generated.yaml"
_DEFAULT_REGISTRY = "localhost:32000"
_REGISTRY_PORT = 32000

_REGISTRY_YAML = Path(__file__).parent.parent / "data" / "registry.yaml"
_REGISTRY_DEPLOYMENT = "deployment/registry"
_REGISTRY_NAMESPACE = "container-registry"


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

        if rock.output.image == image_ref:
            logger.info("Already loaded %s, skipping", image_ref)
            continue

        # Push directly from .rock archive to registry in one step — no Docker
        # daemon needed (avoids failures in MicroK8s-only environments).
        run_command(
            [
                "sudo",
                "rockcraft.skopeo",
                "--insecure-policy",
                "copy",
                "--dest-tls-verify=false",
                f"oci-archive:{rock_path}",
                f"docker://{image_ref}",
            ],
            cwd=str(root),
        )

        rock.output.image = image_ref
        for charm in generated.charms:
            for res in (charm.resources or {}).values():
                if res.rock == rock.name:
                    res.image = image_ref

        pushed.append(image_ref)
        logger.info("Pushed %s", image_ref)

    if pushed:
        dump_artifacts_generated(generated, gen_path)

    return pushed


def _is_port_open(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """Return ``True`` if a TCP connection to *host*:*port* succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def provision_registry(
    root: Path,
    *,
    concierge_file: str = _CONCIERGE_YAML,
) -> str:
    """Deploy a local OCI registry at ``localhost:32000``.

    Reads *concierge_file* to detect whether a k8s or MicroK8s provider is
    present, then applies ``src/opcli/data/registry.yaml`` via ``kubectl``.
    The same manifest works on both canonical k8s and MicroK8s.

    Returns:
        ``"deployed"``       — the registry was just provisioned.
        ``"already_running"``— a service is already listening on port 32000;
                               nothing was changed.
        ``"skipped"``        — no k8s provider is configured; nothing to do.

    Raises:
        ConfigurationError: If both microk8s and k8s providers are configured
            simultaneously.
        SubprocessError: If the underlying kubectl command fails.
    """
    concierge_path = root / concierge_file
    if not concierge_path.exists():
        logger.info("No %s found, skipping registry setup.", concierge_file)
        return "skipped"

    # Skip if there are no rocks to push — the registry is only needed to serve
    # locally-built rock images.
    gen_path = root / _ARTIFACTS_GENERATED_YAML
    if gen_path.exists():
        generated = load_artifacts_generated(gen_path)
        if not generated.rocks:
            logger.info(
                "No rocks in %s, skipping registry setup.", _ARTIFACTS_GENERATED_YAML
            )
            return "skipped"

    # Quick TCP probe — skip if something is already listening.
    if _is_port_open("localhost", _REGISTRY_PORT):
        logger.info("Registry already running at localhost:%d.", _REGISTRY_PORT)
        return "already_running"

    yaml = YAML()
    with open(concierge_path) as fh:
        data = yaml.load(fh)

    providers_raw = data.get("providers", {}) if isinstance(data, dict) else {}
    providers: dict[str, object] = (
        providers_raw if isinstance(providers_raw, dict) else {}
    )

    def _provider_enabled(name: str) -> bool:
        entry = providers.get(name)
        if not isinstance(entry, dict):
            return False
        # A provider listed under providers: is enabled by default;
        # an explicit enable: false can opt it out.
        return bool(entry.get("enable", True))

    microk8s_on = _provider_enabled("microk8s")
    k8s_on = _provider_enabled("k8s")

    if microk8s_on and k8s_on:
        msg = (
            "Both 'microk8s' and 'k8s' providers are enabled in "
            f"{concierge_file}. Only one k8s provider is supported at a time."
        )
        raise ConfigurationError(msg)

    if not microk8s_on and not k8s_on:
        logger.info(
            "No k8s provider enabled in %s, skipping registry setup.",
            concierge_file,
        )
        return "skipped"

    # Use the provider-specific kubectl — MicroK8s and canonical k8s both
    # bundle their own kubectl rather than relying on a separate install.
    kubectl = ["microk8s", "kubectl"] if microk8s_on else ["k8s", "kubectl"]

    # Wait for at least one node to be Ready before deploying — freshly
    # bootstrapped clusters (e.g. in nested LXD) can take a while.
    run_command(
        [*kubectl, "wait", "--for=condition=Ready", "node", "--all", "--timeout=300s"]
    )
    run_command([*kubectl, "apply", "-f", "-"], stdin=_REGISTRY_YAML.read_text())
    run_command(
        [
            *kubectl,
            "rollout",
            "status",
            _REGISTRY_DEPLOYMENT,
            "-n",
            _REGISTRY_NAMESPACE,
            "--timeout=300s",
        ]
    )

    return "deployed"
