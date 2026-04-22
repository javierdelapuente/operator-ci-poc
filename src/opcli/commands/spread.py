"""CLI commands for spread-based test execution."""

import typer

app = typer.Typer(
    help="Generate, expand, and run spread-based integration tests.",
    no_args_is_help=True,
)


@app.command()
def init(
    *,
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing spread.yaml."
    ),
) -> None:
    """Generate spread.yaml and tests/run/task.yaml."""
    raise NotImplementedError("Implement in core/spread.py")


@app.command(
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    },
)
def run(ctx: typer.Context) -> None:
    """Expand virtual backend and run spread.

    Extra args after -- are forwarded to spread.
    """
    _extra_args = ctx.args
    raise NotImplementedError("Implement in core/spread.py")


@app.command()
def expand() -> None:
    """Print the fully expanded spread.yaml to stdout."""
    raise NotImplementedError("Implement in core/spread.py — expand logic")
