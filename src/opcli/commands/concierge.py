"""CLI commands for concierge operations."""

from pathlib import Path

import typer

from opcli.core.concierge import concierge_prepare as _concierge_prepare

app = typer.Typer(
    help="Manage concierge-based test environment provisioning.",
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
    """Install concierge and provision the test environment.

    No-op if concierge.yaml does not exist.
    """
    ran = _concierge_prepare(Path.cwd(), concierge_file=concierge_file)
    if ran:
        typer.echo("Concierge provisioning complete.")
    else:
        typer.echo("No concierge.yaml found — skipped.")
