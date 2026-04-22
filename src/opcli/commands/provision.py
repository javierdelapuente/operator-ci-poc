"""CLI commands for environment provisioning."""

import typer

app = typer.Typer(
    help="Provision test environments with concierge.",
    no_args_is_help=True,
)


@app.command()
def run() -> None:
    """Run concierge prepare to provision the test environment."""
    raise NotImplementedError("Implement in core/provision.py")


@app.command()
def load(
    *,
    registry: str = typer.Option(
        "localhost:32000", "-r", "--registry", help="Target image registry."
    ),
) -> None:
    """Load OCI image artifacts into a local image registry."""
    raise NotImplementedError("Implement in core/provision.py — load logic")
