"""CLI commands for pytest/tox integration test execution."""

import typer

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
    _extra_args = ctx.args
    raise NotImplementedError("Implement in core/pytest_args.py — run logic")


@app.command()
def args() -> None:
    """Print assembled tox/pytest flags from artifacts-generated.yaml."""
    raise NotImplementedError("Implement in core/pytest_args.py — args logic")
