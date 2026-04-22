"""Top-level Typer application — registers all command groups."""

import typer

from opcli.commands import artifacts, provision, pytest_cmd, spread, tutorial_cmd

app = typer.Typer(
    name="opcli",
    help="CLI tool for operator development workflows (Charms, Rocks, Snaps).",
    no_args_is_help=True,
)

app.add_typer(artifacts.app, name="artifacts")
app.add_typer(provision.app, name="provision")
app.add_typer(spread.app, name="spread")
app.add_typer(pytest_cmd.app, name="pytest")
app.add_typer(tutorial_cmd.app, name="tutorial")
