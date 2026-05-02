# AGENTS.md

Developer guide for AI coding agents working on this repository.

---

## What this project is

`opcli` is a **local-first CLI tool** that helps Canonical operator developers build charms, rocks, and snaps; manage test environments; and run integration tests — identically on a developer laptop and inside a CI job.

The authoritative functional specification is [`docs/ISD277-redesign.md`](docs/ISD277-redesign.md). Always read it before implementing a new feature. Known divergences between the spec and the implementation are documented in [`docs/divergences.md`](docs/divergences.md).

**`opcli` owns:** file-based contracts, local artifact discovery, subprocess execution (charmcraft/rockcraft/snapcraft/spread/tox/concierge), and YAML transforms.

**`opcli` does NOT own:** GitHub workflow orchestration, matrix job coordination, artifact upload/download between jobs, runner selection, or any GitHub API calls. Those concerns belong to the GitHub workflow YAML that calls opcli commands.

---

## Quick-start

```bash
# Install dependencies (uses uv lockfile)
uv sync

# Run the tool
uv run opcli --help

# Run all checks (lint → format → types → tests)
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run pytest tests/unit/

# Auto-fix formatting
uv run ruff format src/ tests/
```

Never use `pip install`. All dependency management goes through `uv` and `pyproject.toml`.

---

## Repository layout

```
pyproject.toml                  # single config: project, ruff, mypy, pytest
src/opcli/
  app.py                        # top-level Typer app, registers sub-commands
  commands/                     # Typer CLI layer only — no business logic here
    artifacts.py
    provision.py
    spread.py
    pytest_cmd.py               # "pytest" is a reserved Python name
    tutorial_cmd.py
  core/                         # all business logic
    artifacts.py                # build, fetch, localize, collect
    discovery.py                # walk repo tree to find charmcraft/rockcraft/snapcraft yamls
    exceptions.py               # OpcliError hierarchy
    provision.py                # concierge wrapper + CI patching
    pytest_args.py              # assemble tox/pytest flags from artifacts-generated.yaml
    spread.py                   # virtual backend expansion + matrix generation
    subprocess.py               # central subprocess wrapper (mock boundary in tests)
    tutorial.py
    yaml_io.py                  # YAML load/dump helpers (ruamel.yaml)
  models/
    artifacts.py                # Pydantic V2 model for artifacts.yaml
    artifacts_generated.py      # Pydantic V2 model for artifacts-generated.yaml
  data/                         # bundled static files (templates, etc.)
tests/
  unit/                         # fast unit tests — no external processes
  integration/                  # integration tests (require LXD, spread, etc.)
  conftest.py
docs/
  ISD277-redesign.md            # authoritative spec
  divergences.md                # where implementation differs from spec
```

### Strict architecture rule

`commands/` is **presentation only**. It parses CLI arguments and calls `core/`. Never put business logic directly in a Typer command callback. Tests validate `core/` functions directly — they do not go through the CLI layer.

---

## Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+ with strict typing (`mypy --strict`) |
| Packaging | `uv` |
| CLI | `Typer` |
| Data validation | `Pydantic V2` |
| Lint / format | `Ruff` (rules: `E F W I UP B SIM PL RUF`) |
| YAML (user files) | `ruamel.yaml` — preserves comments and round-trips |
| Testing | `pytest` + `pytest-mock` + `syrupy` (snapshot testing) |

---

## CI detection

Two environment variables gate CI behaviour — they are set independently by different subsystems:

| Variable | Checked by | Meaning when set |
|---|---|---|
| `CI` | `spread.py` (`_is_ci()`) | Expand virtual backends to `{name}-ci` instead of `{name}-local` |
| `GITHUB_ACTIONS=true` | `artifacts.py` (`_get_ci_context()`) | Produce CI-format artifact references (GitHub artifact names + run-id) instead of local file paths |

In practice both are set inside a GitHub Actions job, so they agree. On a developer machine neither is set.

Every `artifacts build`, `spread run`, `spread expand`, and `provision run` must respect their respective variable.

---

## Data model overview

### Which files use Pydantic vs ruamel.yaml

| File | Ownership | Approach | Notes |
|---|---|---|---|
| `artifacts.yaml` | opcli-generated, user-editable | **Pydantic V2 model** | Strongly validated at load time |
| `artifacts-generated.yaml` | opcli-generated, machine-consumed | **Pydantic V2 model** | Strongly validated at load time |
| `spread.yaml` | User-owned, opcli-expanded | **ruamel.yaml dict** | Preserve unknown keys, comments, custom backends; opcli only transforms virtual backends |
| `concierge.yaml` | User-owned, opcli-patched in CI | **ruamel.yaml dict** | Additive merge for CI patches — preserve everything else |
| `tests/integration/run/task.yaml` | opcli-generated, user-editable | **ruamel.yaml dict** | Simple template, not worth a model |

**Critical:** when transforming `spread.yaml`, never rewrite the original file. Always produce a transformed copy in a temp file.

### `artifacts.yaml` (user-edited, Pydantic-validated)

Declares what to build. Each artifact points to its craft YAML file:

```yaml
version: 1
charms:
  - name: my-charm              # opcli alias (may differ from internal charmcraft name)
    charmcraft-yaml: charmcraft-my-charm.yaml
    pack-dir: .                 # optional: cwd for charmcraft pack
    resources:
      my-image:
        type: oci-image
        rock: my-rock
    builds:
      - arch: amd64
        runner: ["ubuntu-22.04"]
      - arch: arm64
        runner: ["ubuntu-24.04-arm"]
rocks:
  - name: my-rock
    rockcraft-yaml: my_rock/rockcraft.yaml
snaps:
  - name: my-snap
    snapcraft-yaml: snap/snapcraft.yaml
```

### `artifacts-generated.yaml` (machine-generated, Pydantic-validated)

Produced by `opcli artifacts build` (local paths) or `opcli artifacts fetch`/`localize` (CI references → local paths). The `output` field is a **flat list** — one entry per produced file:

```yaml
# Local build
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft-my-charm.yaml
    output:
      - arch: amd64
        path: ./my-charm_ubuntu-22.04-amd64.charm
        base: ubuntu@22.04
      - arch: amd64
        path: ./my-charm_ubuntu-24.04-amd64.charm
        base: ubuntu@24.04

# CI build (before localize)
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft-my-charm.yaml
    output:
      - arch: amd64
        artifact: built-charm-my-charm-amd64
        run-id: "1234567890"
```

### `spread.yaml` — virtual backends

`opcli spread` recognises a `type:` field in backend entries. Known virtual types: `integration-test`, `tutorial`. The backend **name** is user-defined. `opcli` expands each virtual backend to `{name}-local` (LXD VM) or `{name}-ci` (current runner) depending on `CI`. All other spread-native backends and keys are preserved unchanged — never rewrite the original file; always work from a temp copy.

---

## Command-family invariants

### `opcli artifacts init`

- Walks directories recursively from the repository root.
- Discovery markers: `charmcraft.yaml` → charm, `rockcraft.yaml` → rock, `snapcraft.yaml` → snap.
- Inspects `charmcraft.yaml` to extract resource declarations and link them to discovered rocks.
- **Non-destructive:** refuses to overwrite an existing `artifacts.yaml` unless `--force` is passed.

### `opcli artifacts build`

- Reads `artifacts.yaml`; for each artifact runs the appropriate pack tool in `pack-dir` (or the directory containing the craft YAML if `pack-dir` is omitted).
- Extracts the produced file paths from build tool output; writes `artifacts-generated.yaml` with `output` entries populated.
- Supports `--charm <name>` and `--rock <name>` flags (repeatable) to build a subset.

### `opcli provision run`

- Runs `sudo concierge prepare -c concierge.yaml`.
- In CI (`CI` env var set): additively patches `concierge.yaml` with CI-specific overrides (Docker/GHCR mirror credentials, registry config) before running concierge.

### `opcli provision load`

- Loads OCI image artifacts (rocks) into a local image registry.
- Default registry: `localhost:32000`. Override with `-r <registry>`.

### `opcli spread init`

- Discovers integration test modules and generates `spread.yaml` + `tests/integration/run/task.yaml`.
- **Non-destructive:** refuses to overwrite existing files unless `--force` is passed.

### `opcli spread run`

- Reads `spread.yaml`, expands all virtual backends, writes to a **temp file** (never overwrites the original), invokes `spread` with the temp file.
- All tokens after `--` are forwarded **verbatim and in order** to spread — opcli must not reinterpret or swallow spread selectors or flags.
  ```bash
  opcli spread run -- -list
  opcli spread run -- integration-test-local:ubuntu-24.04:tests/integration/run:test_charm
  ```

### `opcli spread expand`

- Same expansion logic as `spread run`, but prints the fully expanded `spread.yaml` to stdout without running spread. Useful for debugging.

### `opcli pytest expand`

- Reads `artifacts-generated.yaml` and prints the full assembled tox command to stdout (does not run it).
- For each charm: `--charm-file <path-or-artifact-ref>`; for each OCI resource: `--<resource-name> <image-or-path>`.
- Extra arguments after `--` are appended to the printed command.

---

## Subprocess rule

All calls to external binaries (`charmcraft`, `rockcraft`, `snapcraft`, `spread`, `concierge`, `juju`, `tox`) **must** go through `core/subprocess.py:run_command`. Never call `subprocess.run` or `subprocess.Popen` directly outside that module. This is the mock boundary in unit tests.

---

## Error hierarchy

```
OpcliError (base)
├── SubprocessError    — external command failed (exit code + stderr)
├── ValidationError    — YAML schema validation failed
├── DiscoveryError     — artifact discovery found nothing / conflicting markers
└── ConfigurationError — missing or invalid config file
```

All Typer command callbacks catch `OpcliError` and emit a user-friendly message. Raw tracebacks must not appear in normal usage.

---

## Build tool invariants

These invariants must be maintained; they encode hard-won correctness lessons.

### 1. `charmcraft-yaml` / `rockcraft-yaml` symlinks

Charmcraft and Rockcraft always look for `charmcraft.yaml` / `rockcraft.yaml` by that exact filename in the working directory. When `artifacts.yaml` points to a non-standard filename (e.g. `charmcraft-my-charm.yaml`), `_with_charm_symlink` / `_with_rock_symlink` creates a **temporary relative symlink** in `pack_dir` before the build and removes it in a `finally` block.

Rules:
- If `pack_dir/charmcraft.yaml` is already the right file (resolved path matches): no-op.
- If `pack_dir/charmcraft.yaml` is a real file with **identical byte content**: no-op (accepted as a valid copy).
- If `pack_dir/charmcraft.yaml` is a real file with **different content**: raise `ConfigurationError` — prevents silently building the wrong charm.
- Cleanup uses `.is_symlink()`, not `.exists()`, so a real file accidentally at that path is never deleted.

### 2. Output attribution — `attributed` set

`artifacts_build` maintains a shared `attributed: set[str]` of absolute output paths already claimed by previous builds in the same session. Each `_build_rock`, `_build_charm`, and `_build_snap` call adds its outputs to `attributed` and passes it to `_pick_new_output` / `_pick_new_charm_outputs`.

The overwrite-in-place fallback (when `after - before` is empty) raises `OpcliError` if the candidate path is already in `attributed`. This catches the case where two artifacts share a pack-dir and produce identically-named output files.

### 3. `after - before` for output detection

`_pick_new_output` (rocks, snaps) and `_pick_new_charm_outputs` (charms) both use `after - before` to identify the file(s) produced by this specific build invocation. Never use `sorted(after)` alone — this was the root cause of a cross-attribution bug where the second charm in a shared pack-dir inherited all charm files.

The overwrite-in-place fallback (`after == before`, i.e. `after - before` is empty) only applies when a rebuild produces the same filenames.

### 4. CI artifact download — per-artifact subdirectories

`artifacts_fetch` downloads each charm/snap artifact to `root/{artifact-name}/` (e.g. `root/built-charm-my-charm-amd64/`) rather than a flat `root/`. This prevents filename collisions when two charms have the same internal charmcraft name (and thus the same packed filename).

`_localize_charm` and the snap localize path search the artifact's own subdirectory for any `.charm`/`.snap` file by extension (not by opcli alias), because the packed filename reflects the internal craft name — which may differ from the opcli alias.

---

## Git workflow

**Never push directly to `main`.** Always:

1. Create a feature branch
2. Open a PR
3. Wait for CI to pass (both `CI` and `Test Integration` workflows)
4. Run a code review with a sub-agent when making non-trivial changes
5. Squash-merge

```bash
git checkout -b fix/my-fix
# ... make changes ...
git push --set-upstream origin fix/my-fix
gh pr create --title "..." --body "..."
# wait for CI
gh pr merge <number> --squash
git checkout main && git pull
```

When creating git commits, always include the trailer:

```
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

---

## Testing conventions

- **TDD order**: write unit tests before implementation for non-trivial features.
- **Mock boundary**: unit tests mock at `opcli.core.artifacts.run_command` (or `opcli.core.subprocess.run_command`). Never execute real `charmcraft`, `rockcraft`, `snapcraft`, `spread`, `concierge`, or `juju` in unit tests.
- **Snapshot testing**: use `syrupy` for CLI output assertions to catch unintended format changes.
- **Integration tests** (`@pytest.mark.integration`) require real LXD and are not run in the standard unit test suite.
- The `pre_existing_before / after` pattern: when testing build output detection, simulate charmcraft's behaviour by writing files inside the `fake_run` side-effect, not before it.

---

## Pydantic conventions

- **YAML-facing models** (`artifacts.yaml`, `artifacts-generated.yaml`): use default lax mode (`ConfigDict()`). YAML's native int/str coercion works.
- **Internal-only models**: use `ConfigDict(strict=True)`.
- No `Any` type anywhere. Use specific types or `object` if truly dynamic.
- Field aliases use `alias=` (hyphenated YAML keys map to underscore Python names, e.g. `charmcraft-yaml` → `charmcraft_yaml`). Use `populate_by_name=True` so both forms work.

---

## Type safety

- No `Any` anywhere in the codebase. Use specific types or `object` if truly dynamic.
- All function signatures have full type annotations.
- Use `typing.Annotated` for all Typer CLI parameters.
- `mypy --strict` must pass with zero errors before merging.

---

## YAML handling

- `ruamel.yaml` is used for all user-owned files (`spread.yaml`, `concierge.yaml`, `task.yaml`) to preserve comments and unknown keys.
- Pydantic models are used for `artifacts.yaml` and `artifacts-generated.yaml` only.
- When transforming `spread.yaml` (spread expand / run), **never overwrite the original file**. Always write to a temp file and pass that to the spread subprocess.

---

## Reusable GitHub workflows

Three workflows in `.github/workflows/` are designed to be called from operator repositories:

| Workflow | Purpose |
|---|---|
| `build-artifacts.yml` | Generates build matrix, builds all artifacts in parallel, merges into a single `artifacts-generated.yaml` artifact |
| `integration-test.yml` | Downloads built artifacts, generates spread task matrix, runs integration tests |
| `test-integration.yml` | Lighter integration test runner (no spread) |

These workflows install `opcli` at the exact commit SHA they were called with (via `canonical/get-workflow-version-action`), so downstream repos automatically get the version of opcli pinned to the workflow ref they reference.
