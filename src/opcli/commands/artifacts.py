"""CLI commands for artifact discovery and building."""

from pathlib import Path

import typer

from opcli.core.artifacts import artifacts_build, artifacts_init

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
    path = artifacts_init(Path.cwd(), force=force)
    typer.echo(f"Wrote {path}")


@app.command()
def build(
    *,
    charm: list[str] = typer.Option(
        [], "--charm", help="Build only this charm. Repeatable."
    ),
    rock: list[str] = typer.Option(
        [], "--rock", help="Build only this rock. Repeatable."
    ),
    snap: list[str] = typer.Option(
        [], "--snap", help="Build only this snap. Repeatable."
    ),
) -> None:
    """Build artifacts and produce artifacts-generated.yaml."""
    path = artifacts_build(
        Path.cwd(),
        charm_names=charm or None,
        rock_names=rock or None,
        snap_names=snap or None,
    )
    typer.echo(f"Wrote {path}")
