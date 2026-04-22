"""CLI commands for pytest/tox integration test execution."""

from pathlib import Path

import typer

from opcli.core.pytest_args import assemble_pytest_args, run_pytest

app = typer.Typer(
    help="Assemble pytest flags from build output and run integration tests.",
    no_args_is_help=True,
)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(
    ctx: typer.Context,
    *,
    tox_env: str = typer.Option("integration", "-e", help="Tox environment name."),
) -> None:
    """Run integration tests via tox. Extra args after -- are forwarded to pytest."""
    run_pytest(Path.cwd(), tox_env=tox_env, extra_args=ctx.args or None)


@app.command()
def args() -> None:
    """Print assembled tox/pytest flags from artifacts-generated.yaml."""
    flags = assemble_pytest_args(Path.cwd())
    if flags:
        typer.echo(" ".join(flags))
    else:
        typer.echo("# No flags assembled (no charms/resources found)")
