"""CLI commands for environment provisioning."""

from pathlib import Path

import typer

from opcli.core.provision import provision_load, provision_run

app = typer.Typer(
    help="Provision test environments with concierge.",
    no_args_is_help=True,
)


@app.command()
def run() -> None:
    """Run concierge prepare to provision the test environment."""
    provision_run(Path.cwd())
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
