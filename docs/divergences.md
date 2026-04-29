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
`loginctl enable-linger ubuntu` in the execute block ensures ubuntu's systemd
user manager is running before `runuser -l ubuntu` starts the tox session.
(The same call appears in `_CI_PREPARE` before concierge for a related but
distinct reason — see divergence 21.)

---

## 3. Argument forwarding to spread requires `--`

**Spec examples:**

```
opcli spread run -list
opcli spread run -- integration-test-local:ubuntu-26.04:tests/integration/run:test_charm
```

**Implementation:** Typer parses its own flags before forwarding, so all
spread arguments must follow a `--` separator:

```bash
opcli spread run -- -list
opcli spread run -- integration-test-local:ubuntu-24.04:tests/integration/run:test_charm
opcli spread run -- -v integration-test-local:ubuntu-24.04:tests/integration/run:test_charm
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

## 5. `opcli artifacts build` produces CI-format output when run in GitHub Actions

**Spec:** Describes `output.file` paths for all artifact types. The spec
mentions GHCR image refs and GitHub artifact refs in the CI-format YAML but
does not explicitly specify how they are produced.

**Implementation:** When `GITHUB_ACTIONS=true`, `opcli artifacts build`:

- **Rocks**: pushes the built `.rock` image to GHCR via `skopeo` and writes
  `output.image: ghcr.io/<owner-lowercased>/<repo>/<rock>:<sha7>` (no `output.file`).
- **Charms / Snaps**: writes `output.artifact: built-<type>-<name>` and
  `output.run-id: <GITHUB_RUN_ID>` (no `output.files`).

A companion command `opcli artifacts collect` (also not in the spec — see below)
merges the per-artifact partial files from parallel build jobs.

`opcli pytest expand` emits rock image flags from `rocks[].output.image` for
CI-format files. It logs a warning and skips `--charm-file=` for charms whose
output is `artifact: + run-id:` (CI-built charms), because charm download
belongs in the test workflow, not inside opcli.

**Rationale:** Detecting `GITHUB_ACTIONS=true` rather than generic `CI` is
necessary because CI-format output requires GitHub-specific environment
variables (`GITHUB_RUN_ID`, `GITHUB_REPOSITORY_OWNER`, `GITHUB_REPOSITORY`,
`GITHUB_SHA`, `GITHUB_TOKEN`) that are only available in GitHub Actions.

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

---

## 11. `artifacts.yaml` uses explicit yaml-file paths instead of source directories

**Spec:** The `artifacts.yaml` schema uses a `source` field containing the
**directory** that contains the craft YAML file (e.g. `source: rocks/my-rock`).

**Implementation:** The `source` field is replaced by an explicit
path to the craft YAML file:

```yaml
version: 1
rocks:
  - name: my-rock
    rockcraft-yaml: rocks/my-rock/rockcraft.yaml
charms:
  - name: my-charm
    charmcraft-yaml: charmcraft.yaml
snaps:
  - name: my-snap
    snapcraft-yaml: snap/snapcraft.yaml
    pack-dir: .
```

An optional `pack-dir` field is added to all artifact types. When set, the
build tool (`rockcraft pack`, `charmcraft pack`, `snapcraft pack`) is invoked
from `pack-dir` instead of from the directory containing the craft YAML. For
rocks, opcli creates a temporary `rockcraft.yaml` symlink in `pack-dir` before
running the build and removes it afterwards.

**Rationale:** Go monorepos place `go.mod` at the repository root but
`rockcraft.yaml` in a subdirectory. Rockcraft's managed LXC container does not
follow symlinks, so the go-framework extension fails to find `go.mod` unless
`rockcraft pack` runs from the repo root. The `pack-dir` field enables this
without requiring a manual workaround. The explicit yaml-file path (rather than
directory) makes the configuration unambiguous and consistent across all
artifact types.

---

## 12. Multi-base charm output — `output.files` list instead of single `output.file`

**Spec:** The `artifacts-generated.yaml` schema shows a single `output.file`
path for each charm entry:

```yaml
charms:
  - name: indico
    output:
      file: ./indico_ubuntu-22.04-amd64.charm
```

**Implementation:** Charms use an `output.files` list of `{path, base}` objects
because `charmcraft pack` produces **one `.charm` file per declared base** in a
single invocation (e.g. ubuntu@20.04, ubuntu@22.04, ubuntu@24.04 with the same
architecture each produce a separate file):

```yaml
charms:
  - name: aproxy
    charmcraft-yaml: charmcraft.yaml
    output:
      files:
        - path: ./aproxy_ubuntu-20.04-amd64.charm
          base: ubuntu@20.04
        - path: ./aproxy_ubuntu-22.04-amd64.charm
          base: ubuntu@22.04
        - path: ./aproxy_ubuntu-24.04-amd64.charm
          base: ubuntu@24.04
```

The `base` field is parsed best-effort from the filename
(`{name}_{distro}-{version}-{arch}.charm` → `{distro}@{version}`); it is
`null` when the filename does not follow this convention.

`opcli pytest expand` emits one `--charm-file=<path>` flag per entry in
`output.files`.

For CI-built charms, the `artifact` and `run-id` fields remain on
`CharmArtifactOutput` alongside `files` (which is empty in that case).

**Rationale:** Rocks and snaps always produce exactly one file per
architecture so their schema is unchanged. Only charms need the list because
multi-base (same-arch) builds are a common pattern in the Canonical operator
ecosystem.

---

## 13. `opcli provision load` writes back rock image refs to `artifacts-generated.yaml`

**Spec:** `opcli provision load` loads rock images into the local registry.
The spec does not describe any modification of `artifacts-generated.yaml`.

**Implementation:** After successfully pushing each rock image,
`opcli provision load` updates the corresponding `rock.output.image` field in
`artifacts-generated.yaml` with the pushed image reference (e.g.
`localhost:32000/myrock:latest`).

This means that after running `opcli provision load`, `opcli pytest expand`
automatically emits `--<resource-name>=localhost:32000/myrock:latest` (the
live registry reference) for any charm resource linked to that rock, because
pytest-args resolves image refs by looking up the rock — not by reading a
separate field on the resource.

**Rationale:** Without the writeback, users would have to manually update
`artifacts-generated.yaml` after each `provision load`. Writing back makes the
`provision load → pytest expand` pipeline seamless.

---

## 14. `ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS=1` always passed to rockcraft

**Spec:** `opcli artifacts build` runs `rockcraft pack` for each declared rock.
The spec does not mention environment variable configuration for the build tools.

**Implementation:** `opcli artifacts build` always sets
`ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS=1` in the environment when invoking
`rockcraft pack`, regardless of whether the rock actually uses experimental
extensions.

**Rationale:** Many operator-adjacent rocks use extensions (e.g.
`go-framework`, `django-framework`) that are still flagged experimental in some
rockcraft versions. Omitting the variable causes an immediate build failure with
a confusing error. Since the variable is harmless when the rock does not use
experimental extensions, always setting it avoids this footgun without any
downside.

---

## 15. `opcli artifacts matrix` and `opcli artifacts collect` are new commands

**Spec:** Does not describe these commands.

**Implementation:** Two new commands support the parallel CI build pattern:

- **`opcli artifacts matrix`** reads `artifacts.yaml` and prints a JSON object
  suitable for use as a GitHub Actions `strategy.matrix`. Each artifact
  (rocks, then charms, then snaps) becomes one entry with `name` and `type`:
  ```json
  {"include": [{"name": "my-rock", "type": "rock"}, {"name": "my-charm", "type": "charm"}]}
  ```
  This drives the parallel `build` job in the reusable `build-artifacts.yml` workflow.

- **`opcli artifacts collect <partial1> <partial2> ...`** reads multiple partial
  `artifacts-generated.yaml` files (one per parallel build job) and merges them
  into a single `artifacts-generated.yaml`. It validates that:
  - No artifact name appears in more than one partial.
  - Every rock referenced by a charm resource is present in the collected set.

**Rationale:** Each parallel build job produces its own partial
`artifacts-generated.yaml`. The collect step merges them into the single file
that downstream workflows consume. The matrix step decouples the artifact list
from the workflow YAML — adding a new charm or rock to `artifacts.yaml`
automatically adds a build job without touching the workflow file.

---

## 16. `GITHUB_ACTIONS` (not `CI`) controls artifact-build CI mode

**Spec:** Uses `CI` as the environment variable that switches between local
and CI behaviour throughout.

**Implementation:** Two different env vars are used, each for its appropriate scope:

| Env var | Controls | Why |
|---|---|---|
| `CI` | Spread backend expansion (`integration-test-local:` vs `integration-test-ci:`) | Generic CI detection; correct for spread, which runs on any CI |
| `GITHUB_ACTIONS` | `artifacts build` CI-format output | Must be GitHub Actions because CI format requires `GITHUB_RUN_ID`, `GITHUB_REPOSITORY_OWNER`, `GITHUB_REPOSITORY`, `GITHUB_SHA`, `GITHUB_TOKEN` — all GitHub-specific |

`GITHUB_ACTIONS` is set to `"true"` automatically by GitHub Actions runners and
is not set in other CI environments or locally. This makes CI-format artifact
output strictly GitHub-specific while keeping spread backend expansion
environment-agnostic.

---

## 17. Rock images are not duplicated on charm resources

**Spec:** The CI-format `artifacts-generated.yaml` example in the spec shows
`image:` fields on both `rocks[].output` and `charms[].resources[]`.

**Implementation:** The `GeneratedResource` model has no `image` field. Rock
images live exclusively on `rocks[].output.image`. Charm resources only carry
`type: oci-image` and `rock: <rock-name>`.

```yaml
# Implemented schema (single source of truth)
rocks:
  - name: my-rock
    output:
      image: ghcr.io/owner/repo/my-rock:abc1234   # ← image lives here only
charms:
  - name: my-charm
    resources:
      my-rock-image:
        type: oci-image
        rock: my-rock                              # ← link only, no image field
```

Any consumer that needs the image for `my-rock-image` looks up `my-rock` in
the `rocks` list and reads `output.image` from there.

**Affected consumers:**
- `opcli pytest expand` — already iterates `rocks` for image flags; charm resource
  entries for rock-backed resources are skipped (the rock flag covers them).
- `opcli provision load` — updates only `rock.output.image`; no charm resource
  fields to update.

**Rationale:** Duplicating the image ref creates two sources of truth that can
diverge (e.g. after `provision load` updates the rock image ref). Single source
eliminates the consistency problem entirely.

---

## 18. Reusable GitHub Actions build workflow

**Spec:** Describes the CI pipeline at a high level but does not provide a
concrete GitHub Actions workflow.

**Implementation:** A reusable workflow `.github/workflows/build-artifacts.yml`
is provided that external operator repositories can call directly:

```yaml
jobs:
  build:
    uses: javierdelapuente/operator-ci-poc/.github/workflows/build-artifacts.yml@main
    with:
      working-directory: .
    permissions:
      contents: read
      packages: write    # required for GHCR rock pushes
```

The workflow implements three jobs:

1. **build-matrix** — runs `opcli artifacts matrix` to generate the GitHub
   Actions matrix (one entry per rock/charm/snap).
2. **build** (parallel matrix) — for each artifact, installs the appropriate
   tool, runs `opcli artifacts build --<type> <name>`, pushes rocks to GHCR,
   and uploads a partial `artifacts-generated.yaml`.
3. **collect** — downloads all partials and runs `opcli artifacts collect` to
   produce the final merged `artifacts-generated.yaml` artifact.

**opcli self-pinning:** `opcli` is installed using the ref extracted from
`github.workflow_ref` (the ref portion of
`org/repo/.github/workflows/file.yml@ref`). This means calling the workflow
at `@main`, `@v1.2`, or `@abc1234` automatically installs the matching opcli
version — no separate version input needed. The `opcli-ref` input overrides
this for cases where the auto-derived ref is not directly fetchable (e.g. a
pull-request test-merge commit).

---

## 19. `opcli artifacts localize` — new command for CI artifact discovery

**Spec:** Does not describe this command.

**Implementation:** `opcli artifacts localize` scans the current directory
tree for built artifact files (`.charm`, `.rock`, `.snap`) and writes their
relative paths back into `artifacts-generated.yaml` in-place.

This is used in the CI `Test Integration` workflow after downloading
charm artifacts from GitHub Actions. The downloaded files land flat in the
working directory (e.g. `./k8s-charm_ubuntu-24.04-amd64.charm`). Running
`opcli artifacts localize` discovers those files and populates
`output.files[].path` with the correct repo-relative paths (`./k8s-charm_ubuntu-24.04-amd64.charm`).
Downstream, `opcli pytest expand` reads these paths and passes them as
`--charm-file=` arguments to pytest.

**Path format:** All paths written by `localize` (and by `artifacts build`)
are `./`-prefixed paths relative to the repository root. This is required
because spread delivers the project directory to `SPREAD_PATH=/home/ubuntu/proj`
inside the VM/runner; absolute host paths (e.g. `/home/runner/work/...`) would
be unreachable after delivery.

---

## 20. `opcli spread tasks` — new command for GitHub Actions test matrix

**Spec:** Does not describe this command.

**Implementation:** `opcli spread tasks` reads `spread.yaml` (without expanding
it), extracts all `MODULE/*` variants from the suites, and prints a JSON object
suitable for use as a GitHub Actions `strategy.matrix`:

```json
{"include": [
  {"name": "test_charm", "selector": "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm", "runs-on": ["self-hosted", "noble"]},
  {"name": "test_actions", "selector": "integration-test-ci:ubuntu-24.04:tests/integration/run:test_actions", "runs-on": ["self-hosted", "noble"]}
]}
```

Each entry includes:
- `name` — the variant name (used as the job display name).
- `selector` — the full spread selector passed to `spread run`.
- `runs-on` — the GitHub Actions runner labels, taken from the system's
  `runner:` field in the virtual `integration-test` backend, or
  `ubuntu-latest` if absent.

This command drives the test matrix in the reusable `integration-test.yml`
workflow, so adding a new test module to `spread.yaml` automatically adds a
CI job without touching the workflow file.

---

## 21. `loginctl enable-linger ubuntu` in CI prepare — snap cgroup fix

**Spec:** Does not describe the internals of the CI `prepare` script.

**Implementation:** The `_CI_PREPARE` script (generated by `opcli spread expand`
for the `integration-test-ci:` backend) calls `loginctl enable-linger ubuntu` **before** running
`concierge prepare`. This is required to avoid a snap-confine failure:

- Concierge is a snap. In CI it runs as `root` via spread's SSH session.
- Concierge internally calls `sudo -u ubuntu juju`, and `juju` is also a snap.
- When juju's snap-confine launches inside concierge's snap cgroup scope
  (`snap.concierge.concierge-<UUID>.scope`) it fails with:
  `snap.concierge.concierge-<UUID>.scope is not a snap cgroup for tag snap.juju.juju`
- `loginctl enable-linger ubuntu` starts ubuntu's systemd user manager so
  snap-confine can create juju's own cgroup scope under ubuntu's user slice
  instead of inheriting concierge's scope.

**Rationale:** This is a snap confinement implementation detail with no spec
equivalent. It is fully transparent to users — the fix lives entirely in the
generated prepare script.

---

## 22. opcli installed to `/usr/local/bin` via `UV_TOOL_BIN_DIR` in CI prepare

**Spec:** Does not describe how opcli is installed inside the spread VM/runner.

**Implementation:** In `_CI_PREPARE`, opcli is installed with:

```bash
export UV_TOOL_BIN_DIR=/usr/local/bin
uv tool install "git+https://github.com/...@${OPCLI_GIT_REF:-main}" --quiet
```

Setting `UV_TOOL_BIN_DIR=/usr/local/bin` puts the `opcli` wrapper at
`/usr/local/bin/opcli`, which is in `PATH` for all users including `root`'s
non-login SSH sessions (i.e. the spread SSH session). Without this, uv places
the wrapper in `~root/.local/bin`, which is typically not in `PATH` for the
spread-controlled SSH connection.

Similarly, `tox` is installed for the `ubuntu` user with:

```bash
runuser -l ubuntu -c "UV_TOOL_BIN_DIR=/usr/local/bin uv tool install tox --with tox-uv --quiet"
```

This ensures `tox` is at `/usr/local/bin/tox`, reachable in the ubuntu login
shell used by `runuser -l ubuntu` in the EXECUTE script.

## 23. Concrete backend names derived from virtual name

**Spec:** Describes the backends as `local:` (for local/LXD) and `ci:` (for GitHub Actions runners).

**Implementation:** Virtual backends are identified by a `type:` field (mirroring how spread itself uses `type: lxd`, `type: adhoc`, etc.). The recognised virtual types are `integration-test` and `tutorial`. The backend name is user-defined; the concrete name is derived by appending `-local` or `-ci`:

```yaml
backends:
  integration-test:       # user-defined name
    type: integration-test  # virtual type recognized by opcli
    systems:
      - ubuntu-24.04

  integration-test-arm:   # second backend, same type, different systems
    type: integration-test
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, arm64]

  tutorial-test:
    type: tutorial
    systems:
      - ubuntu-24.04
```

The `type:` field is consumed by opcli and replaced with `type: adhoc` in the expanded YAML. Concrete names produced:

- `integration-test` → `integration-test-local` / `integration-test-ci`
- `integration-test-arm` → `integration-test-arm-local` / `integration-test-arm-ci`
- `tutorial-test` → `tutorial-test-local` / `tutorial-test-ci`

The spec names the tutorial type `tutorial-test`; the implementation uses `tutorial`.

This means spread selectors use the derived name, e.g.:

```bash
opcli spread run -- integration-test-local:ubuntu-24.04:tests/integration/run:test_charm
opcli spread run -- integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm
```

And `opcli spread tasks` produces entries like:

```json
{"name": "test_charm", "selector": "integration-test-ci:ubuntu-24.04:tests/integration/run:test_charm", "runs-on": ["self-hosted", "noble"]}
```

The naming convention makes the origin of each backend immediately visible in selector strings and spread output, and avoids clashing with user-defined backends named `local` or `ci`.
