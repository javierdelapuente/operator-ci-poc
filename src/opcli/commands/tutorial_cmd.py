"""``opcli tutorial`` command group."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from opcli.core.exceptions import OpcliError
from opcli.core.tutorial import expand_tutorial

app = typer.Typer(help="Tutorial testing commands.")


@app.command()
def expand(
    tutorial_file: Annotated[
        Path,
        typer.Argument(
            help="Path to the tutorial file (.md or .rst).",
            exists=True,
            readable=True,
        ),
    ],
) -> None:
    """Extract shell commands from a tutorial file and print them to stdout.

    The output is a shell script suitable for use with eval in a spread task.yaml:

        eval "$(opcli tutorial expand "$TUTORIAL")"

    Supports Markdown (.md) and reStructuredText (.rst) files.
    """
    try:
        script = expand_tutorial(tutorial_file)
        typer.echo(script)
    except OpcliError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
