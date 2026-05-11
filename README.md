# opcli

CLI tool for operator development workflows — build, test, and provision Charms, Rocks, and Snaps.

`opcli` replaces the monolithic `operator-workflows` approach with a modular, local-first model based on explicit build plans (`artifacts.yaml`), stable build output (`artifacts-generated.yaml`), and spread-based test execution.

See [docs/ISD277-redesign.md](docs/ISD277-redesign.md) for the full specification.

## Installation

`opcli` is installed directly from this repository (not published to PyPI).

### With uv (recommended)

```bash
uv tool install git+https://github.com/javierdelapuente/operator-ci-poc.git
```

Or from a local clone:

```bash
git clone https://github.com/javierdelapuente/operator-ci-poc.git
cd operator-ci-poc
uv tool install .
```

### With pip

```bash
pip install git+https://github.com/javierdelapuente/operator-ci-poc.git
```

Or from a local clone:

```bash
git clone https://github.com/javierdelapuente/operator-ci-poc.git
cd operator-ci-poc
pip install .
```

### Verify

```bash
opcli --help
```

## Quick start

### Local testing with spread

```bash
# Discover charms/rocks/snaps and generate artifacts.yaml
opcli artifacts init

# Build all declared artifacts → produces artifacts-generated.yaml
opcli artifacts build

# Generate spread.yaml and tests/integration/run/task.yaml
opcli spread init

# Preview the expanded spread configuration
opcli spread expand

# Run integration tests via spread (local LXD backend)
opcli spread run
# Or target a specific test:
opcli spread run -- integration-test-local:ubuntu-24.04:tests/integration/run:test_charm
```

### Local testing without spread

```bash
opcli artifacts init
opcli artifacts build

# Provision the environment with concierge
opcli provision run

# Deploy a local OCI registry (if k8s or MicroK8s is enabled in concierge.yaml)
opcli provision registry

# Load rock images into the local registry
opcli provision load

# Run integration tests via tox (opcli pytest expand prints the full command)
eval "$(opcli pytest expand -- -k test_charm)"
```

### Inspect the assembled tox command

```bash
# Print the full tox command that would run the integration tests
opcli pytest expand
# Example output: tox -e integration -- --charm-file=./mycharm.charm --myrock-image=./myrock.rock

# With extra pytest arguments forwarded:
opcli pytest expand -- -k test_charm
```

## Commands

### `opcli artifacts`

| Command | Description |
|---|---|
| `opcli artifacts init` | Discover charms/rocks/snaps and generate `artifacts.yaml`. Use `--force` to overwrite. |
| `opcli artifacts build` | Build all artifacts and produce `artifacts-generated.yaml`. Filter with `--charm <name>`, `--rock <name>`, `--snap <name>`. |
| `opcli artifacts matrix` | Read `artifacts.yaml` and print a JSON build matrix for GitHub Actions (one entry per artifact/arch build target pair). |
| `opcli artifacts collect <partial>...` | Merge partial `artifacts-generated.yaml` files from parallel build jobs into a single output file. |
| `opcli artifacts fetch` | Download `artifacts-generated.yaml` and all charm/snap archives from a CI run, then rewrite paths to local files so `opcli pytest expand` and `opcli spread run` work without a local build. Rock artifacts are GHCR images and need no download. Use `--run-id`, `--repo`, and `--wait` (retries until the artifact appears). |
| `opcli artifacts localize` | Rewrite `artifacts-generated.yaml` to replace CI artifact references with local `.charm`/`.snap` file paths after charm and snap archives have been manually downloaded. (Prefer `opcli artifacts fetch` for the full workflow.) |

### `opcli provision`

| Command | Description |
|---|---|
| `opcli provision run` | Run `concierge prepare` to provision the test environment. |
| `opcli provision load` | Push locally-built rock images to a container registry and update each arch build entry's `image` field in `artifacts-generated.yaml`. Use `-r` to set registry (default: `localhost:32000`). Images are tagged `{registry}/{name}:{arch}` (e.g. `localhost:32000/my-rock:amd64`). |
| `opcli provision registry` | Deploy a local OCI registry at `localhost:32000`. Reads `concierge.yaml` to detect whether MicroK8s or canonical k8s is enabled and applies the same embedded `registry:2` manifest via the provider's own `kubectl`. No-op if the registry is already running, if no k8s provider is configured, or if there are no rocks to push. Raises an error if both providers are enabled simultaneously. Use `-c` to specify a custom concierge file path. |

### `opcli spread`

| Command | Description |
|---|---|
| `opcli spread init` | Discover integration tests and generate `spread.yaml` + `tests/integration/run/task.yaml`. Use `--force` to overwrite. |
| `opcli spread expand` | Print the fully expanded `spread.yaml` to stdout. |
| `opcli spread run` | Expand the virtual backend and run spread. Extra args after `--` are forwarded verbatim to spread (e.g. `opcli spread run -- -list`). |
| `opcli spread tasks` | Generate the GitHub Actions CI matrix as JSON. Calls `spread -list` on the CI-mode expanded `spread.yaml` and emits one entry per spread task with `name`, `selector`, `runs-on`, and `arch` fields. |

### `opcli pytest`

| Command | Description |
|---|---|
| `opcli pytest expand` | Print the full `tox -e integration -- <flags>` command assembled from `artifacts-generated.yaml`. Use `-e` to change the tox environment. Extra args after `--` are forwarded into the printed command. Pipe to `eval` to execute. |

The tox environment defaults to `integration`. To use a different environment, either pass `-e` directly:

```bash
opcli pytest expand -e charms-integration -- -k test_charm
```

Or, when using spread, set `TOX_ENV` in the suite environment of `spread.yaml` (generated by `opcli spread init`):

```yaml
suites:
  tests/integration/:
    environment:
      TOX_ENV: charms-integration   # overrides the default "integration"
      MODULE/test_charm: test_charm
```

Different suites can each declare their own `TOX_ENV` value.

### `opcli tutorial`

| Command | Description |
|---|---|
| `opcli tutorial expand <file>` | Extract and print shell commands from a tutorial file (`.md`, `.rst`). Output is a shell script suitable for `eval` in a spread task. |

## Key files

| File | Purpose |
|---|---|
| `artifacts.yaml` | Declares charms, rocks, snaps and their resource links. Auto-generated or hand-edited. |
| `artifacts-generated.yaml` | Build output with artifact paths/refs. Generated by `opcli artifacts build`. |
| `concierge.yaml` | Declarative environment provisioning (Juju, MicroK8s, LXD, etc.). |
| `spread.yaml` | Spread configuration with a virtual `integration-test` backend expanded by opcli. |
| `tests/integration/run/task.yaml` | Spread task that runs integration tests via `opcli pytest expand`. |

## `artifacts.yaml` schema

Each artifact entry uses an explicit path to its craft YAML file rather than a directory.
An optional `builds:` list declares which architectures (and runners) to build for — it
defaults to `[{arch: amd64}]` when omitted:

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
    pack-dir: .        # run snapcraft pack from the repo root
```

The `runner` field in each `builds:` entry is a YAML list of GitHub Actions runner
labels (e.g. `[ubuntu-latest]` or `[self-hosted, arm64]`). It is used by
`opcli artifacts matrix` and the reusable build workflow to select the correct
runner for each arch. If omitted, it defaults to `[ubuntu-latest]`.

When `opcli artifacts matrix` serialises the matrix for GitHub Actions it
JSON-encodes the runner list into a string so that
`${{ fromJSON(matrix.runner) }}` produces the correct runner label array at
job execution time.

### `pack-dir` (charms, rocks, and snaps)

By default `opcli artifacts build` runs the pack tool from the directory that contains
the craft YAML file. Set `pack-dir` to run from a different directory. This is required
for Go monorepos where `go.mod` lives at the repository root but `rockcraft.yaml` lives
in a subdirectory:

```yaml
rocks:
  - name: my-go-rock
    rockcraft-yaml: rocks/my-go-rock/rockcraft.yaml
    pack-dir: .    # rockcraft pack runs from the repo root where go.mod lives
```

When `pack-dir` differs from the directory containing the craft YAML, opcli creates a
temporary symlink `<pack-dir>/rockcraft.yaml → <rockcraft-yaml>` before running
`rockcraft pack`, then removes it afterwards. If a real (non-symlink) file already
exists at that path with **different content**, the build fails with an error. If a
real file exists with identical byte content it is accepted and no symlink is created.

## `artifacts-generated.yaml` schema — local format

`opcli artifacts build` produces `artifacts-generated.yaml`. The `output:` field
is a flat list where each entry carries `arch` alongside its file info. Rocks and
snaps produce one entry per arch. Charms produce **one entry per produced `.charm`
file** — when `charmcraft pack` builds multiple bases (e.g. ubuntu@22.04 and
ubuntu@24.04), each file becomes a separate entry in the list:

```yaml
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: rocks/my-rock/rockcraft.yaml
    output:
      - arch: amd64
        file: ./rocks/my-rock/my-rock_1.0_amd64.rock
charms:
  - name: aproxy
    charmcraft-yaml: charmcraft.yaml
    output:
      - arch: amd64
        path: ./aproxy_ubuntu-20.04-amd64.charm
        base: ubuntu@20.04
      - arch: amd64
        path: ./aproxy_ubuntu-22.04-amd64.charm
        base: ubuntu@22.04
      - arch: amd64
        path: ./aproxy_ubuntu-24.04-amd64.charm
        base: ubuntu@24.04
```

`opcli pytest expand` emits one `--charm-file=<path>` flag per entry matching the current machine's arch.

System entries under the virtual `integration-test` (or `tutorial`) backend accept opcli-specific fields alongside standard spread fields:

```yaml
backends:
  integration-test:
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]   # GitHub Actions runner labels (CI only)
          arch: arm64                    # explicit arch override (CI only, optional)
          cpu: 4                         # LXD VM vCPUs (local only, default 4)
          memory: 8                      # LXD VM RAM in GiB (local only, default 8)
          disk: 20                       # LXD VM disk in GiB (local only, default 20)
```

**How they are handled:**

| Field | Local backend | CI backend |
|---|---|---|
| `runner` | Stripped | Read by `opcli spread tasks` to populate `runs-on` in the matrix, then stripped |
| `arch` | Stripped | Read by `opcli spread tasks` to populate `arch` in the matrix (derived from runner labels when absent), then stripped |
| `cpu` / `memory` / `disk` | Used in LXD `lxc launch --vm` arguments, then stripped | Stripped (not applicable to cloud runners) |

Resource values are injected as per-system defaults in the allocate script using `${CPU:-N}` semantics so that an explicit environment variable override (e.g. `CPU=2 opcli spread run`) still takes precedence.

## CI vs local

Two environment variables govern how opcli behaves in different environments:

| Env var | Controls | Local | CI |
|---|---|---|---|
| `CI` | Spread backend expansion | `integration-test-local:` (LXD VM) | `integration-test-ci:` (current machine) |
| `GITHUB_ACTIONS` | Artifact build output format | Local file paths (`output.file`) | GHCR images + GitHub artifact refs |

When `GITHUB_ACTIONS=true` (set automatically by GitHub Actions runners),
`opcli artifacts build` switches to CI format:

- **Rocks**: pushed to GHCR via skopeo; each arch build entry gets `output[*].image: ghcr.io/<owner>/<repo>/<rock>:<sha7>-<arch>`
- **Charms / Snaps**: each arch build entry gets `output[*].artifact: built-<type>-<name>-<arch>` + `output[*].run-id: <GITHUB_RUN_ID>`

## `artifacts-generated.yaml` — CI vs local format

### Local format

```yaml
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: rocks/my-rock/rockcraft.yaml
    output:
      - arch: amd64
        file: ./rocks/my-rock/my-rock_1.0_amd64.rock
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft.yaml
    output:
      - arch: amd64
        path: ./my-charm_ubuntu-24.04-amd64.charm
        base: ubuntu@24.04
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock
```

### CI format (after `opcli artifacts collect`)

```yaml
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: rocks/my-rock/rockcraft.yaml
    output:
      - arch: amd64
        image: ghcr.io/myorg/my-repo/my-rock:abc1234-amd64
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft.yaml
    output:
      - arch: amd64
        artifact: built-charm-my-charm-amd64
        run-id: "1234567890"
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock
```

Rock images live exclusively under `rocks[].output.image`. Charm resources
carry only a `rock:` link — the image ref is never duplicated onto the resource.
`opcli pytest expand` resolves rock flags by iterating `rocks`, not by reading
resource fields.

## GitHub Actions reusable workflow

The repository ships a reusable workflow that builds all charms, rocks, and
snaps in parallel and publishes a merged `artifacts-generated.yaml` as a
GitHub artifact.

### Calling from an operator repository

```yaml
jobs:
  build:
    uses: javierdelapuente/operator-ci-poc/.github/workflows/build-artifacts.yml@main
    permissions:
      contents: read
      packages: write   # required for GHCR rock pushes
      actions: read     # required for get-workflow-version-action
    with:
      working-directory: .  # directory containing artifacts.yaml (default: .)
```

Pinning to a SHA or tag (e.g. `@abc1234`, `@v1.2`) automatically installs the
matching `opcli` version — the SHA is resolved via
`canonical/get-workflow-version-action`, so no separate version input is needed.

### Workflow jobs

| Job | What it does |
|---|---|
| **build-matrix** | Runs `opcli artifacts matrix` to generate the GitHub Actions matrix |
| **build** (parallel) | Builds each artifact; pushes rocks to GHCR; uploads partial `artifacts-generated.yaml` |
| **collect** | Merges all partials via `opcli artifacts collect`; uploads final `artifacts-generated` artifact |

## Local OCI registry

When running integration tests locally with k8s or MicroK8s, rock images need to be available in a registry that the cluster can pull from.

`opcli provision registry` deploys a local OCI registry at `localhost:32000` by inspecting `concierge.yaml`:

- **MicroK8s or canonical k8s**: applies an embedded `registry:2` Deployment + NodePort 32000 Service manifest via the provider's own `kubectl` (`microk8s kubectl` or `k8s kubectl`). The same manifest is used for both providers.
- **Neither enabled**: logs a message and skips — no action taken.
- **Already running**: detects an existing listener on port 32000 and skips without modifying anything.
- **No rocks in `artifacts-generated.yaml`**: skips — the registry is only needed to serve locally-built rock images.
- **Both providers enabled simultaneously**: raises an error.

When using `opcli spread run`, the local prepare script automatically calls `opcli provision registry -c "$CONCIERGE"` after `concierge prepare`, so no manual step is needed in the spread workflow.

> **Note (canonical k8s):** For workloads to pull images from `localhost:32000`, containerd must treat it as an insecure registry. This may require additional manual configuration for canonical k8s.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/javierdelapuente/operator-ci-poc.git
cd operator-ci-poc
uv sync

# Lint
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Type check
uv run mypy src/

# Test
uv run pytest tests/ -v
```

## License

See [LICENSE](LICENSE).
