"""CLI commands for artifact discovery and building."""

import json
from pathlib import Path
from typing import Annotated

import typer

from opcli.core.artifacts import (
    artifacts_build,
    artifacts_collect,
    artifacts_fetch,
    artifacts_init,
    artifacts_localize,
    artifacts_matrix,
)

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


@app.command()
def matrix() -> None:
    """Print the GitHub Actions build matrix as JSON.

    Reads artifacts.yaml and outputs a JSON object with an ``include`` key
    suitable for use as a GitHub Actions ``strategy.matrix`` value.
    """
    result = artifacts_matrix(Path.cwd())
    typer.echo(json.dumps(result))


@app.command()
def collect(
    partials: Annotated[
        list[Path],
        typer.Argument(help="Partial artifacts-generated.yaml files to merge."),
    ],
) -> None:
    """Merge partial artifacts-generated.yaml files into one.

    Downloads from each parallel CI build job produce a partial
    artifacts-generated.yaml.  This command merges them and re-fills charm
    resource references from the merged rock outputs.
    """
    path = artifacts_collect(Path.cwd(), partials)
    typer.echo(f"Wrote {path}")


@app.command()
def fetch(
    *,
    run_id: Annotated[
        str,
        typer.Option("--run-id", help="GitHub Actions workflow run ID."),
    ],
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="GitHub repository in 'owner/name' format. "
            "Defaults to the current git remote.",
        ),
    ] = None,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait/--no-wait",
            help="Retry until artifacts-generated appears (use when the build "
            "job may still be running).",
        ),
    ] = False,
) -> None:
    """Download artifacts from a CI run and prepare for local testing.

    Downloads artifacts-generated.yaml, then downloads all charm/snap artifact
    archives. Rock artifacts are GHCR images and require no download.
    Finally rewrites artifacts-generated.yaml with local file paths so that
    ``opcli pytest run`` and ``opcli spread run`` work without a local build.
    """
    path = artifacts_fetch(Path.cwd(), run_id=run_id, repo=repo, wait=wait)
    typer.echo(f"Fetched artifacts and updated {path}")


@app.command()
def localize() -> None:
    """Update artifacts-generated.yaml with downloaded local charm file paths.

    In CI, charm outputs are CI artifact references (artifact + run-id).
    After the workflow downloads the built charm files, run this command to
    rewrite artifacts-generated.yaml so each charm points to the local
    ``.charm`` file instead of the CI reference.
    """
    updated = artifacts_localize(Path.cwd())
    if updated:
        typer.echo(f"Localised {updated} charm(s) in artifacts-generated.yaml.")
    else:
        typer.echo(
            "No CI artifact references found; artifacts-generated.yaml unchanged."
        )
