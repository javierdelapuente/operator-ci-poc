"""CLI commands for artifact discovery and building."""

import typer

app = typer.Typer(
    help="Discover and build charms, rocks, and snaps.",
    no_args_is_help=True,
)


@app.command()
def init(
    *,
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing artifacts.yaml."
    ),
) -> None:
    """Discover artifacts and generate artifacts.yaml."""
    raise NotImplementedError("Implement in core/artifacts.py")


@app.command()
def build(
    *,
    charm: list[str] = typer.Option(
        [], "--charm", help="Build only this charm. Repeatable."
    ),
    rock: list[str] = typer.Option(
        [], "--rock", help="Build only this rock. Repeatable."
    ),
) -> None:
    """Build artifacts and produce artifacts-generated.yaml."""
    raise NotImplementedError("Implement in core/artifacts.py")
