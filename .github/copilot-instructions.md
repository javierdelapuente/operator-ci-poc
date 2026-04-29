# opcli — Copilot Instructions

## 1. Spec & Fundamentals

### Authoritative specification

**`docs/ISD277-redesign.md`** is the single source of truth for `opcli` behavior. Always read it before implementing any feature. Do not invent commands, flags, or YAML fields not described there.

### What opcli is (and is not)

`opcli` is a **local-first CLI tool** that generates config files, builds artifacts, provisions test environments, and runs tests. It works identically on a developer machine and inside a CI job.

**`opcli` owns:** file-based contracts, local artifact discovery, subprocess execution (charmcraft/rockcraft/snapcraft/spread/tox/concierge), and YAML transforms.

**`opcli` does NOT own:** GitHub workflow orchestration, matrix job coordination, artifact upload/download between jobs, runner selection, or any GitHub API calls. Those concerns belong to the GitHub workflow YAML that _calls_ opcli commands.

### The `CI` environment variable

This is the single most important branching point in the tool:

| `CI` value | Backend expansion | Artifact resolution | Provisioning |
|---|---|---|---|
| **unset / falsy** | `local:` — provisions an LXD VM | Local file paths (`output.file`) | concierge runs inside the VM |
| **truthy** | `ci:` — runs on current machine | GitHub artifacts / GHCR images | concierge runs on the runner; concierge.yaml is patched with CI-specific overrides first |

All `opcli spread run`, `opcli spread expand`, and `opcli provision run` must respect this variable.

### Technical stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+ with strict typing |
| Packaging | `uv` — `pyproject.toml` is the single config source for all tools |
| CLI framework | `Typer` |
| Data validation | `Pydantic V2` |
| Linting / formatting | `Ruff` (rules include `I` for imports and `PL` for Pylint) |
| Type checking | `Mypy` |
| Testing | `Pytest` + `pytest-mock` + `syrupy` (snapshot testing for CLI output) |
| YAML round-trip | `ruamel.yaml` (preserves comments in user-edited files) |

### Build, lint, and test commands

```bash
# Linting and type checking
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/

# Full test suite
pytest

# Single test
pytest tests/path/to/test_file.py::test_function_name -v

# Dependency management
uv sync           # install/update from lock file
uv add <package>  # add a dependency
```

---

## 2. Data Model Rules

### YAML files: modeled vs. flexible

Not all YAML files should be modeled the same way:

| File | Ownership | Pydantic model? | Notes |
|---|---|---|---|
| `artifacts.yaml` | opcli-generated, user-editable | **Yes** — full model in `src/opcli/models/` | Strongly validated at load time |
| `artifacts-generated.yaml` | opcli-generated, machine-consumed | **Yes** — full model in `src/opcli/models/` | Strongly validated at load time |
| `spread.yaml` | User-owned, opcli-expanded | **No** — use `ruamel.yaml` dict | Preserve unknown keys, comments, custom backends. opcli only reads/transforms the `integration-test:` virtual backend. |
| `concierge.yaml` | User-owned, opcli-patched in CI | **No** — use `ruamel.yaml` dict | Additive merge for CI patches. Preserve everything else. |
| `tests/integration/run/task.yaml` | opcli-generated, user-editable | **No** — use `ruamel.yaml` dict | Simple template, not worth a model. |

**Critical rule:** When `opcli spread expand` or `opcli spread run` transforms `spread.yaml`, it must **never rewrite the original file**. It produces a transformed copy (in memory or a temp file) and passes that to spread.

### Pydantic conventions

- **YAML-facing models** (`artifacts.yaml`, `artifacts-generated.yaml`): use default lax mode (`model_config = ConfigDict()`). This allows YAML's type inference (e.g., `version: 1` as int) to coerce cleanly.
- **Internal-only models**: use `model_config = ConfigDict(strict=True)`.
- No `Any` type anywhere. Use specific types or `object` if truly dynamic.

### `artifacts.yaml` schema

Each artifact points to its craft YAML file rather than a source directory. An optional `pack-dir` sets the working directory for the build tool.

```yaml
version: 1
rocks:
  - name: indico
    rockcraft-yaml: indico_rock/rockcraft.yaml
  - name: indico-nginx
    rockcraft-yaml: nginx_rock/rockcraft.yaml
snaps:
  - name: my-snap
    snapcraft-yaml: snap/snapcraft.yaml
charms:
  - name: indico
    charmcraft-yaml: charmcraft.yaml
    resources:
      indico-image:
        type: oci-image
        rock: indico
      indico-nginx-image:
        type: oci-image
        rock: indico-nginx
```

All three artifact lists (`rocks`, `charms`, `snaps`) are optional and default to empty.

### `artifacts-generated.yaml` schema

Each entry carries the same craft YAML path as `artifacts.yaml`. Charm output uses a `files` list because `charmcraft pack` may produce one `.charm` file per declared base.

**Local** (file paths):
```yaml
version: 1
rocks:
  - name: indico
    rockcraft-yaml: indico_rock/rockcraft.yaml
    output:
      file: ./indico_rock/indico_1.0_amd64.rock
snaps:
  - name: my-snap
    snapcraft-yaml: snap/snapcraft.yaml
    output:
      file: ./snap/my-snap_1.0_amd64.snap
charms:
  - name: indico
    charmcraft-yaml: charmcraft.yaml
    output:
      files:
        - path: ./indico_ubuntu-22.04-amd64.charm
          base: ubuntu@22.04
        - path: ./indico_ubuntu-24.04-amd64.charm
          base: ubuntu@24.04
    resources:
      indico-image:
        type: oci-image
        rock: indico
```

**CI** (GitHub artifacts / GHCR images):
```yaml
version: 1
rocks:
  - name: indico
    rockcraft-yaml: indico_rock/rockcraft.yaml
    output:
      image: ghcr.io/canonical/indico:abc1234-22.04
charms:
  - name: indico
    charmcraft-yaml: charmcraft.yaml
    output:
      artifact: charm-indico
      run-id: "1234567890"
    resources:
      indico-image:
        type: oci-image
        rock: indico
```

**Modeling guidance:** Rocks/snaps use `ArtifactOutput` (optional fields `file`, `image`, `artifact`+`run-id`). Charms use `CharmArtifactOutput` with a `files` list (each entry has `path` and optional `base`) for local builds, or `artifact`+`run-id` for CI.

### `spread.yaml`

Uses a **virtual backend** called `integration-test:` that is not real spread syntax. `opcli spread run/expand` replaces it with the real `local:` or `ci:` backend (determined by the `CI` env var). Users may add any other spread-native backends, suites, or environment variables — opcli must preserve them all.

### `concierge.yaml`

Standard [concierge](https://github.com/canonical/concierge) config. In CI, `opcli provision run` patches it **additively** (merge new keys into the loaded YAML dict) to inject Docker/GHCR mirror credentials and registry config, then writes the patched version back before running `sudo concierge prepare`.

---

## 3. Command-Family Invariants

### Architecture

```
src/opcli/
  commands/     # Typer CLI layer only — no business logic
  core/         # All business logic
  models/       # Pydantic V2 models (only for artifacts.yaml + artifacts-generated.yaml)
tests/
  unit/
  integration/
```

**Key rule:** `commands/` is presentation only. It calls into `core/`. Never put business logic directly in a Typer command callback.

### `opcli artifacts init`

- Walks directories recursively from the repository root.
- Discovery markers: `charmcraft.yaml` → charm, `rockcraft.yaml` → rock, `snapcraft.yaml` → snap.
- The `source` field is set to the directory containing the marker file.
- Inspects `charmcraft.yaml` to extract resource declarations and link them to discovered rocks.
- **Non-destructive:** refuses to overwrite an existing `artifacts.yaml` unless `--force` is passed.

### `opcli artifacts build`

- Reads `artifacts.yaml`.
- For each artifact, runs the appropriate build tool in the artifact's `source` directory:
  - charm → `charmcraft pack`
  - rock → `rockcraft pack`
  - snap → `snapcraft pack`
- Extracts the produced file path from the build tool's output.
- Writes `artifacts-generated.yaml` with the `output.file` fields populated.
- Supports `--charm <name>` and `--rock <name>` flags to build a subset. These flags can be repeated.

### `opcli provision run`

- Runs `sudo concierge prepare -c concierge.yaml`.
- In CI (`CI` env var set): patches `concierge.yaml` with CI-specific overrides before running concierge.

### `opcli provision load`

- Loads OCI image artifacts (rocks) into a local image registry.
- Default registry: `localhost:32000`. Override with `-r <registry>`.

### `opcli spread init`

- Discovers integration test modules and generates `spread.yaml` + `tests/integration/run/task.yaml`.
- **Non-destructive:** refuses to overwrite existing files unless `--force` is passed.

### `opcli spread run`

- Reads `spread.yaml`, expands the `integration-test:` virtual backend into `local:` or `ci:` (based on `CI` env var), writes the expanded YAML to a **temp file** (never overwrites the original).
- Invokes `spread` as a subprocess with the temp file.
- **Argument forwarding:** all tokens after `--` are forwarded **verbatim and in order** to the spread subprocess. `opcli` must not reinterpret, normalize, or swallow spread selectors or flags.
  ```bash
  opcli spread run -- -list
  opcli spread run -- local:ubuntu-26.04:tests/integration/run:test_charm
  opcli spread run -- -v local:ubuntu-26.04:tests/integration/run:test_charm
  ```

### `opcli spread expand`

- Same expansion logic as `spread run`, but prints the fully expanded `spread.yaml` to stdout without running spread. Useful for debugging.

### `opcli pytest expand`

- Reads `artifacts-generated.yaml` and prints the full assembled tox command to stdout without running anything.
- Flag assembly rules (matching current operator-workflows conventions):
  - For each charm: `--charm-file <path-or-artifact-ref>`
  - For each OCI resource: `--<resource-name> <image-ref-or-local-path>`
- Extra arguments after `--` are forwarded into the printed command.
  ```bash
  opcli pytest expand -- -k test_charm
  # prints: tox -e integration -- --charm-file ./indico.charm ... -k test_charm
  ```

---

## 4. Implementation Constraints

### Subprocess safety

All calls to external binaries (`spread`, `juju`, `tox`, `lxc`, `concierge`, `charmcraft`, `rockcraft`, `snapcraft`) must go through a **single central wrapper** in `core/` that:
- Captures `stdout` and `stderr`
- Enforces configurable timeouts
- Raises typed custom exceptions (see error hierarchy below)

Never call `subprocess.run` or `subprocess.Popen` directly outside this wrapper.

### Error hierarchy

```
OpcliError (base)
├── SubprocessError    — external command failed (includes exit code, stderr)
├── ValidationError    — YAML schema validation failed
├── DiscoveryError     — artifact discovery found nothing or conflicting markers
└── ConfigurationError — missing/invalid config file
```

All Typer command callbacks catch `OpcliError` and produce user-friendly output (no raw tracebacks in normal usage).

### Type safety

- No `Any`. Use `typing.Annotated` for all Typer CLI parameters.
- All function signatures have full type annotations.
- `Mypy` must pass with no errors.

### Testing

- **TDD order:** write unit tests before implementation for non-trivial features.
- **Mock boundary:** unit tests mock at the subprocess wrapper level. They must never execute real `lxc`, `juju`, `spread`, `concierge`, `charmcraft`, `rockcraft`, or `snapcraft` commands.
- **Snapshot testing:** use `syrupy` for CLI output assertions — this catches unintended output format changes.

### Project layout

```
pyproject.toml          # single config: project metadata, ruff, mypy, pytest
src/opcli/
  __init__.py
  __main__.py           # entry point: `python -m opcli`
  app.py                # top-level Typer app, registers sub-commands
  commands/
    __init__.py
    artifacts.py
    provision.py
    spread.py
    pytest_cmd.py       # "pytest" is a reserved name, use pytest_cmd
  core/
    __init__.py
    artifacts.py        # discovery + build logic
    provision.py        # concierge wrapper + CI patching
    spread.py           # virtual backend expansion
    pytest_args.py      # flag assembly from artifacts-generated.yaml
    subprocess.py       # central subprocess wrapper
    exceptions.py       # OpcliError hierarchy
  models/
    __init__.py
    artifacts.py        # artifacts.yaml model
    artifacts_generated.py  # artifacts-generated.yaml model
tests/
  unit/
  integration/
  conftest.py
```
