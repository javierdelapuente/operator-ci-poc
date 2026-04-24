# Implementation divergences from ISD277

This document records where the current `opcli` implementation intentionally
or practically diverges from the [ISD277 specification](ISD277-redesign.md).
It is kept as a living document and should be updated when the spec or the
implementation evolves.

---

## 1. `opcli pytest` command family redesigned

**Spec:** Two commands — `opcli pytest run` (runs tox) and
`opcli pytest args` (prints assembled pytest flags).

**Implementation:** A single `opcli pytest expand` command that prints the
full, shell-quoted tox invocation:

```
tox -e integration -- --charm-file=./mycharm.charm --myrock-image=./myrock.rock
```

`opcli pytest run` and `opcli pytest args` do not exist. To run tests, pipe
the output to `eval`:

```bash
eval "$(opcli pytest expand -- -k test_charm)"
```

**Rationale:** Printing rather than executing keeps opcli out of the
subprocess wrapper for tox, avoids duplicating tox's output-streaming
complexity, and makes the exact command trivially inspectable and debuggable.

---

## 2. `task.yaml` runs tests as the `ubuntu` user

**Spec:** The example `task.yaml` execute block is:

```yaml
execute: |
    $( opcli pytest run -- -k $MODULE )
```

**Implementation:** The generated `task.yaml` captures the command from
`opcli pytest expand` and runs it under `runuser -l ubuntu`:

```yaml
execute: |
    loginctl enable-linger ubuntu
    cd "${SPREAD_PATH}"
    PYTEST_CMD=$(opcli pytest expand -- -k "$MODULE") || exit 1
    runuser -l ubuntu -c "cd \"${SPREAD_PATH}\" && $PYTEST_CMD"
```

**Rationale:** Spread rsyncs the project directory as `root`, so the spread
task runs as root by default. Tests and tox need write access under the
project tree (`.tox/`, etc.) and must run as a normal user. The `ubuntu` user
is the standard non-root user available in the LXD VM and GitHub runner.

---

## 3. Argument forwarding to spread requires `--`

**Spec examples:**

```
opcli spread run -list
opcli spread run local:ubuntu-26.04:tests/integration/run:test_charm
```

**Implementation:** Typer parses its own flags before forwarding, so all
spread arguments must follow a `--` separator:

```bash
opcli spread run -- -list
opcli spread run -- local:ubuntu-24.04:tests/integration/run:test_charm
opcli spread run -- -v local:ubuntu-24.04:tests/integration/run:test_charm
```

Tokens after `--` are passed verbatim and in order to the spread subprocess.

---

## 4. `opcli tutorial expand` implemented as a main command

**Spec:** The tutorial support is described only in the "Further Information"
section as a tentative future extension, with a sketch of what the command
could look like.

**Implementation:** `opcli tutorial expand <file>` is a fully implemented
first-class command. It extracts shell commands from a tutorial file and
prints a shell script suitable for `eval` in a spread task:

```bash
eval "$(opcli tutorial expand docs/tutorial.md)"
```

Supported file formats: `.md`, `.markdown`, `.rst`, `.rest`.

A `tutorial-test` virtual backend is also supported in `spread expand`/`run`
(complementary to `integration-test`). However, `opcli spread init` only
generates `tests/integration/run/task.yaml`; the `tests/tutorial/run/task.yaml`
is **not** auto-generated and must be created manually if needed.

---

## 5. CI-format `artifacts-generated.yaml` is only partially consumed

**Spec:** The CI variant of `artifacts-generated.yaml` contains
`image: ghcr.io/...` for rocks and `artifact: + run-id:` for charms/snaps.
The spec implies opcli participates in producing these (build collection step).

**Implementation:**

- The `ArtifactsGenerated` model (v2) correctly parses CI-format files.
- `opcli pytest expand` emits `--<resource-name>=<image>` for charm resources
  whose embedded `image:` field is set (resolved at build time from the rock's
  CI output).
- `opcli pytest expand` does **not** emit `--charm-file=` flags for charms
  whose output is `artifact: + run-id:` (CI-built charms); those are skipped.
- `opcli artifacts build` only produces local `output.file` paths; there is no
  `opcli artifacts collect` (or equivalent) command that produces the CI format.

**Rationale:** opcli owns the local side of the contract. CI artifact
collection (waiting for parallel build jobs, downloading GitHub artifacts,
tagging GHCR images) is GitHub workflow orchestration and belongs in the
workflow YAML, not in opcli.

---

## 6. `artifacts-generated.yaml` output paths are repo-relative

**Spec:** Does not explicitly specify whether `output.file` paths are absolute
or relative.

**Implementation:** `opcli artifacts build` always writes `output.file` as a
path relative to the repository root (e.g. `./mycharm/mycharm_amd64.charm`).
Building an artifact whose output lands outside the repository root is an
error.

**Rationale:** Spread rsyncs the repository into a VM at a different absolute
path than on the host machine. Absolute paths in `artifacts-generated.yaml`
would be wrong inside the VM; relative paths remain valid after rsync.

---

## 7. `--snap` filter flag on `opcli artifacts build`

**Spec:** Lists `--charm <name>` and `--rock <name>` as the filter flags for
`opcli artifacts build`.

**Implementation:** Also supports `--snap <name>` for filtering snap builds,
consistent with the snap support in `artifacts.yaml`.
