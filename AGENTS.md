# AGENTS.md

Instructions for AI coding agents working on this repository.

---

## Project overview

`opcli` — local-first CLI for Canonical operator developers to build charms/rocks/snaps, manage test environments, and run integration tests.

- **Spec:** [`docs/ISD277-redesign.md`](docs/ISD277-redesign.md) — read before implementing new features.
- **Divergences:** [`docs/divergences.md`](docs/divergences.md) — where implementation differs from spec.
- **opcli owns:** file-based contracts, artifact discovery, subprocess execution, YAML transforms.
- **opcli does NOT own:** GitHub workflow orchestration, artifact upload/download, runner selection, GitHub API calls.

---

## Quick-start

```bash
uv sync                                    # install deps
uv run opcli --help                        # run the tool
uv run ruff check src/ tests/              # lint
uv run ruff format --check src/ tests/     # format check
uv run mypy src/                           # type check
uv run pytest tests/unit/                  # unit tests
```

Never use `pip install`. All dependency management goes through `uv` and `pyproject.toml`.

---

## Architecture rules

### Repository layout

```
src/opcli/
  commands/    # CLI layer ONLY — parses args, calls core/. No business logic.
  core/        # All business logic lives here.
  models/      # Pydantic V2 models (artifacts.yaml, artifacts.build.yaml)
  data/        # Bundled static files (e.g. registry.yaml manifest)
tests/
  unit/        # Fast tests — mock external processes
  integration/ # Requires LXD/spread — skip-guarded with @pytest.mark.integration
docs/          # Spec + divergences
```

### Key constraints

1. **`commands/` is presentation only.** Never put logic in Typer callbacks. Tests validate `core/` directly.
2. **Subprocess rule.** All external binary calls go through `core/subprocess.py:run_command`. This is the mock boundary in tests.
3. **Never overwrite `spread.yaml`.** Always produce a transformed copy in a temp file.
4. **Avoid `Any`.** Prefer specific types; `mypy --strict` must pass. Legacy `Any` in YAML-handling helpers is tolerated but should not spread.

---

## Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+, strict typing |
| Packaging | `uv` |
| CLI | `Typer` |
| Data models | `Pydantic V2` |
| Lint/format | `Ruff` (rules: `E F W I UP B SIM PL RUF`) |
| YAML (user files) | `ruamel.yaml` (preserves comments) |
| Testing | `pytest` + `pytest-mock` + `syrupy` |

---

## CI detection

| Variable | Controls | Where checked |
|---|---|---|
| `CI` | Spread backend expansion (`-local` vs `-ci`) | `core/spread.py` |
| `GITHUB_ACTIONS=true` | CI-format artifact output (GHCR + artifact refs) | `core/artifacts.py` |

---

## Data model: Pydantic vs ruamel.yaml

| File | Approach |
|---|---|
| `artifacts.yaml`, `artifacts.build.yaml` | **Pydantic V2** — validated at load |
| `spread.yaml`, `concierge.yaml`, `task.yaml` | **ruamel.yaml dict** — preserve comments/unknown keys |

Pydantic conventions:
- YAML-facing models: lax mode. Internal-only: `strict=True`.
- Field aliases: `alias=` + `populate_by_name=True` (e.g. `charmcraft-yaml` → `charmcraft_yaml`).

---

## Build tool invariants

These encode hard-won correctness lessons — do not violate.

1. **Symlinks for non-standard filenames.** When `artifacts.yaml` points to e.g. `charmcraft-my-charm.yaml`, a temp symlink is created in `pack_dir`. If a real file with *different* content exists at that path → `ConfigurationError`. Cleanup checks `.is_symlink()` not `.exists()`.

2. **Output attribution.** `attributed: set[str]` tracks claimed output paths across builds. Prevents two artifacts in a shared pack-dir from claiming the same file.

3. **`after - before` for output detection.** Never use `sorted(after)` alone. The set difference identifies files produced by *this* specific build invocation.

4. **CI artifact download.** `artifacts_fetch` downloads to `root/{artifact-name}/` subdirectories to prevent filename collisions.

---

## Error hierarchy

```
OpcliError (base)
├── SubprocessError    — external command failed
├── ValidationError    — YAML schema validation failed
├── DiscoveryError     — discovery found nothing / conflicts
└── ConfigurationError — missing or invalid config
```

All Typer callbacks catch `OpcliError` and emit user-friendly messages. No raw tracebacks.

---

## Testing conventions

- **TDD:** write unit tests before implementation for non-trivial features.
- **Mock boundary:** mock at `run_command`. Never run real charmcraft/rockcraft/spread in unit tests.
- **Snapshot testing:** `syrupy` for CLI output assertions.
- **`pre_existing_before/after` pattern:** simulate build tool output by writing files inside the `fake_run` side-effect, not before it.

---

## Git workflow

**Never push to `main`.** Always: branch → PR → CI green → squash merge.

```bash
git checkout -b fix/my-fix
# make changes
git push --set-upstream origin fix/my-fix
gh pr create --title "..." --body "..."
gh pr checks <number> --watch   # WAIT for green (CI + Test Integration workflows)
gh pr merge <number> --squash
```

**CI must be green before merging. No exceptions.**

All commits must include:
```
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```
