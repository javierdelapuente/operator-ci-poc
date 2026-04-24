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
    PYTEST_CMD=$(opcli pytest expand -e "${TOX_ENV:-integration}" -- -k "$MODULE") || exit 1
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

---

## 8. System entry resource fields (`cpu`, `memory`, `disk`) — opcli extension

**Spec:** System entries in the virtual `integration-test` backend may carry a
`runner:` list for GitHub Actions runner label selection (used in CI expansion):

```yaml
backends:
  integration-test:
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]
```

**Implementation:** opcli extends this to also support `cpu`, `memory`, and
`disk` integer fields for controlling LXD VM resources in local expansion:

```yaml
      - ubuntu-24.04:
          runner: [self-hosted, noble]   # CI: runner label selection
          cpu: 4                         # local: LXD VM vCPUs
          memory: 8                      # local: LXD VM RAM (GiB)
          disk: 20                       # local: LXD VM disk (GiB)
```

These fields are stripped before the YAML is passed to spread — they are
opcli-only metadata. During local expansion, they are injected into the
`allocate` script as per-system `case` arms using `${VAR:-N}` semantics
so that explicit env-var overrides still take precedence. During CI expansion,
`runner` is kept (real spread field) while `cpu`/`memory`/`disk` are stripped.

**Rationale:** Declaring VM size alongside the system name keeps all
per-system configuration in one place, avoids managing separate env vars, and
makes the resource intent visible in version control without requiring changes
to any other file.

---

## 9. `opcli provision registry` — local OCI registry management

**Spec:** Does not describe local OCI registry provisioning. The spec mentions
that images are stored in GHCR in CI, but leaves local registry setup as an
exercise for the user/environment.

**Implementation:** `opcli provision registry` is added as a new subcommand
under `opcli provision`. It reads `concierge.yaml` to detect the active k8s
provider and deploys a local OCI registry at `localhost:32000`:

- **MicroK8s provider**: calls `microk8s enable registry` (which deploys the
  registry pod and configures containerd to trust `localhost:32000`).
- **Canonical k8s provider**: applies an embedded `registry:2`
  Deployment + NodePort 32000 Service manifest via `kubectl apply`, then waits
  for the deployment to roll out via `kubectl rollout status`.
- **Neither**: no-op (skipped with a log message).
- **Already running** (TCP port 32000 open): no-op (skipped, nothing changed).

The local `prepare` script generated by `opcli spread init` automatically
calls `opcli provision registry -c "$CONCIERGE"` after `concierge prepare`,
so the step is transparent to users in the spread workflow.

**Known limitation (canonical k8s):** For workloads to pull images from
`localhost:32000`, containerd must be configured to treat it as an insecure
registry. For MicroK8s this is handled automatically by the addon; for
canonical k8s it may require additional manual configuration.

**Rationale:** Without an explicit registry setup step, users with canonical
k8s would need to push to a public registry or configure one manually.
Deploying a registry pod via `kubectl apply` gives a uniform `localhost:32000`
endpoint that matches the default `opcli provision load` target.

---

## 10. `opcli artifacts init` supports the legacy charmcraft split format

**Spec:** Describes discovery of charms via `charmcraft.yaml` and extraction
of `name` and `resources` from that file, without mentioning alternate layouts.

**Implementation:** Many real-world charms use the **legacy split format**
where `charmcraft.yaml` contains only build configuration and `metadata.yaml`
(alongside it) holds the charm's `name`, `summary`, `description`, and
`resources`. This is a supported and documented charmcraft pattern; it is also
required by charms that use `ops.testing.Harness` without migrating to the
unified format.

`opcli artifacts init` handles this transparently:

- If `name` is absent from `charmcraft.yaml`, it falls back to `metadata.yaml`
  in the same directory.
- If `resources` is absent from `charmcraft.yaml`, it falls back to
  `metadata.yaml` in the same directory.
- When `name` is present in `charmcraft.yaml` (unified format), it is used
  directly and `metadata.yaml` is ignored — no behaviour change for unified
  repos.
- The fallback only applies to `charmcraft.yaml`; `rockcraft.yaml` and
  `snapcraft.yaml` always carry their own `name`.

**Rationale:** Treating the split format as an error would block `opcli`
adoption in repositories that cannot yet migrate to the unified format (e.g.
because their unit tests depend on `Harness` reading `metadata.yaml`).
