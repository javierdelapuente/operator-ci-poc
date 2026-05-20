"""CLI commands for environment provisioning."""

from pathlib import Path

import typer

from opcli.core.provision import provision_load, provision_prepare, provision_registry

app = typer.Typer(
    help="Provision test environments with concierge.",
    no_args_is_help=True,
)

_CONCIERGE_YAML = "concierge.yaml"


@app.command()
def prepare(
    *,
    concierge_file: str = typer.Option(
        _CONCIERGE_YAML,
        "-c",
        "--concierge",
        help="Path to concierge.yaml (relative to the project root).",
    ),
) -> None:
    """Run concierge prepare to provision the test environment."""
    provision_prepare(Path.cwd(), concierge_file=concierge_file)
    typer.echo("Provisioning complete.")


@app.command()
def load(
    *,
    registry: str = typer.Option(
        "localhost:32000", "-r", "--registry", help="Target image registry."
    ),
) -> None:
    """Load OCI image artifacts into a local image registry."""
    pushed = provision_load(Path.cwd(), registry=registry)
    if pushed:
        for ref in pushed:
            typer.echo(f"Pushed {ref}")
    else:
        typer.echo("No rock images to load.")


@app.command()
def registry() -> None:
    """Deploy a local OCI registry at localhost:32000 for k8s/MicroK8s.

    Auto-detects the active k8s provider and deploys the registry.
    No-op if the registry is already running or no k8s tooling is found.
    """
    status = provision_registry(Path.cwd())
    match status:
        case "deployed":
            typer.echo("Registry deployed at localhost:32000.")
        case "already_running":
            typer.echo("Registry already running at localhost:32000.")
        case _:
            typer.echo("No k8s provider found — skipping registry setup.")
