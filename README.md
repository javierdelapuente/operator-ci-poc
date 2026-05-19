# opcli

A **local-first CLI tool** for Canonical operator developers to build charms, rocks, and snaps; manage test environments; and run integration tests — identically on a developer laptop and inside a CI job.

`opcli` replaces the monolithic [`operator-workflows`](https://github.com/canonical/operator-workflows) approach with a modular pipeline based on explicit build plans (`artifacts.yaml`), stable build output (`artifacts.build.yaml`), and [spread](https://github.com/canonical/spread)-based test execution.

## Documentation

| Document | Purpose |
|---|---|
| [docs/ISD277-redesign.md](docs/ISD277-redesign.md) | Authoritative functional specification |
| [docs/divergences.md](docs/divergences.md) | Where implementation differs from the spec |
| [AGENTS.md](AGENTS.md) | Developer guide for AI coding agents |

## Installation

```bash
# With uv (recommended)
uv tool install git+https://github.com/javierdelapuente/operator-ci-poc.git

# Or from a local clone
git clone https://github.com/javierdelapuente/operator-ci-poc.git
cd operator-ci-poc && uv tool install .

# Verify
opcli --help
```

## Quick start

### Local testing with spread

```bash
opcli artifacts init     # discover charms/rocks/snaps → artifacts.yaml
opcli artifacts build    # build all → artifacts.build.yaml
opcli spread init        # generate spread.yaml + task.yaml
opcli spread expand      # preview expanded spread config
opcli spread run         # run integration tests (LXD backend)

# Target a specific test:
opcli spread run -- integration-test-local:ubuntu-24.04:tests/integration/run:test_charm
```

### Local testing without spread

```bash
opcli artifacts init
opcli artifacts build
opcli provision run          # provision with concierge
opcli provision registry     # deploy local OCI registry (if k8s enabled)
opcli provision load         # push rocks to registry
eval "$(opcli pytest expand -- -k test_charm)"   # run tests via tox
```

## Commands

### `opcli artifacts`

| Command | Description |
|---|---|
| `init` | Discover charms/rocks/snaps and generate `artifacts.yaml`. `--force` to overwrite. |
| `build` | Build artifacts → `artifacts.build.yaml`. Filter: `--charm`, `--rock`, `--snap`. |
| `matrix` | Print JSON build matrix for GitHub Actions. |
| `collect <partial>...` | Merge partial `artifacts.build.yaml` from parallel jobs. |
| `fetch` | Download CI artifacts and rewrite to local paths. `--run-id`, `--repo`, `--wait`. |
| `localize` | Rewrite CI artifact refs to local paths (after manual download). |

### `opcli provision`

| Command | Description |
|---|---|
| `run` | Run `concierge prepare` to provision the test environment. |
| `load` | Push rock images to registry, update `artifacts.build.yaml`. `-r` for registry. |
| `registry` | Deploy local OCI registry at `localhost:32000`. `-c` for concierge path. |

### `opcli spread`

| Command | Description |
|---|---|
| `init` | Generate `spread.yaml` + `tests/integration/run/task.yaml`. `--force` to overwrite. |
| `expand` | Print fully expanded `spread.yaml` to stdout. |
| `run` | Expand virtual backend and run spread. Args after `--` forwarded verbatim. |
| `tasks` | Print CI matrix JSON (one entry per spread task). |

### `opcli pytest`

| Command | Description |
|---|---|
| `expand` | Print full `tox -e integration -- <flags>` command. `-e` for env, `--` forwards args. |

### `opcli tutorial`

| Command | Description |
|---|---|
| `expand <file>` | Extract shell commands from a tutorial (`.md`/`.rst`) for `eval`. |

## `artifacts.yaml` schema

```yaml
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: rocks/my-rock/rockcraft.yaml
    builds:
      - arch: amd64
      - arch: arm64
        runner: [self-hosted, arm64]
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft.yaml
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock
snaps:
  - name: my-snap
    snapcraft-yaml: snap/snapcraft.yaml
    pack-dir: .
```

Key fields:
- **`*-yaml`**: explicit path to the craft YAML file (not a directory).
- **`pack-dir`**: working directory for the build tool (defaults to the YAML's parent dir).
- **`builds[].runner`**: GitHub Actions runner labels (used by `opcli artifacts matrix`; defaults to `["ubuntu-latest"]` at matrix generation time when omitted).

## CI vs local

| Env var | Controls | Local | CI |
|---|---|---|---|
| `CI` | Spread backend expansion | `*-local` (LXD VM) | `*-ci` (current runner) |
| `GITHUB_ACTIONS` | Artifact output format | Local file paths | GHCR images + artifact refs |

## GitHub Actions reusable workflows

Two reusable workflows are available for operator repositories:

| Workflow | Purpose |
|---|---|
| `build-artifacts.yml` | Build matrix generation, parallel artifact builds, merged `artifacts.build.yaml` |
| `integration-test.yml` | Download artifacts, generate spread task matrix, run integration tests |

Example usage:

```yaml
jobs:
  build:
    uses: javierdelapuente/operator-ci-poc/.github/workflows/build-artifacts.yml@main
    permissions:
      contents: read
      packages: write
      actions: read
    with:
      working-directory: .
```

Pinning to a SHA or tag automatically installs the matching `opcli` version via `canonical/get-workflow-version-action`.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                    # install deps
uv run opcli --help                        # run the tool
uv run ruff check src/ tests/              # lint
uv run ruff format --check src/ tests/     # format check
uv run mypy src/                           # type check
uv run pytest tests/unit/                  # unit tests
```

## License

See [LICENSE](LICENSE).
