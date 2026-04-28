"""Core logic for ``opcli spread init``, ``expand``, and ``run``.

``init`` discovers integration test modules and generates ``spread.yaml``
plus ``tests/integration/run/task.yaml``.

``expand`` reads ``spread.yaml``, replaces the virtual ``integration-test``
backend with a concrete ``local:`` or ``ci:`` backend, and returns the
expanded YAML.  The original file is **never** modified.

``run`` creates a temporary directory inside the project root containing
the expanded ``spread.yaml`` with ``reroot: ..`` and runs ``spread`` from
that directory.  Spread discovers ``spread.yaml`` in the temp dir and
uses ``reroot`` to locate the actual project tree one level up.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import tempfile
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from opcli.core.exceptions import ConfigurationError, ValidationError
from opcli.core.subprocess import run_command

logger = logging.getLogger(__name__)

_yaml = YAML()
_yaml.default_flow_style = False

_SPREAD_YAML = "spread.yaml"
_TASK_YAML_REL = "tests/integration/run/task.yaml"
_TUTORIAL_TASK_YAML_REL = "tests/tutorial/run/task.yaml"
_VIRTUAL_BACKEND = "integration-test"
_TUTORIAL_BACKEND = "tutorial-test"


def _literalize(obj: Any) -> Any:
    """Recursively convert multiline strings to ``LiteralScalarString``.

    This ensures ruamel.yaml serialises shell scripts with the ``|`` block
    style rather than as inline escaped strings.
    """
    if isinstance(obj, str) and "\n" in obj:
        return LiteralScalarString(obj)
    if isinstance(obj, dict):
        return {k: _literalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_literalize(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
#  spread init
# ---------------------------------------------------------------------------


def _discover_test_modules(root: Path) -> list[str]:
    """Find ``test_*.py`` files under ``tests/integration/``."""
    integration_dir = root / "tests" / "integration"
    if not integration_dir.is_dir():
        return []
    modules = sorted(p.stem for p in integration_dir.glob("test_*.py") if p.is_file())
    return modules


def _generate_spread_yaml(
    project_name: str,
    modules: list[str],
) -> str:
    """Build the default ``spread.yaml`` content."""
    buf = StringIO()

    # Root environment: project-wide vars (CONCIERGE, standard vars)
    root_env: dict[str, str] = {
        # Default to "ubuntu" (local LXD VM user); CI backends override this
        # with $(HOST: id -un) so concierge and runuser target the right user.
        "SUDO_USER": "ubuntu",
        "LANG": "C.UTF-8",
        "LANGUAGE": "en",
        "CONCIERGE": '$(HOST: echo "${CONCIERGE:-concierge.yaml}")',
        # Defaults to "main"; override on the host with OPCLI_GIT_REF=<branch>
        # before running spread to install opcli from a specific branch.
        "OPCLI_GIT_REF": '$(HOST: echo "${OPCLI_GIT_REF:-main}")',
    }

    # Suite environment: MODULE variants + TOX_ENV (scoped to this suite)
    suite_env: dict[str, str] = {}
    if modules:
        for mod in modules:
            suite_env[f"MODULE/{mod}"] = mod
    else:
        suite_env["MODULE/tests"] = "tests"
    suite_env["TOX_ENV"] = ""

    data: dict[str, object] = {
        "project": project_name,
        "path": "/home/ubuntu/proj",
        "kill-timeout": "60m",
        "backends": {
            _VIRTUAL_BACKEND: {
                "systems": ["ubuntu-24.04"],
            },
        },
        "environment": root_env,
        "exclude": [".git", ".tox", ".venv", ".*_cache"],
        "suites": {
            "tests/integration/": {
                "summary": "integration tests",
                "backends": [_VIRTUAL_BACKEND],
                "environment": suite_env,
            },
        },
    }

    _yaml.dump(data, buf)
    return buf.getvalue()


_TASK_YAML_CONTENT = (
    "summary: integration tests\n"
    "\n"
    "execute: |\n"
    '    cd "${SPREAD_PATH}"\n'
    '    PYTEST_CMD=$(opcli pytest expand -e "${TOX_ENV:-integration}"'
    ' -- -k "$MODULE") || exit 1\n'
    '    runuser -l "${SUDO_USER}" -c'
    ' "cd \\"${SPREAD_PATH}\\" && $PYTEST_CMD"\n'
)

_TUTORIAL_TASK_YAML_CONTENT = (
    "summary: tutorial test\n"
    "\n"
    "execute: |\n"
    '    runuser -l "${SUDO_USER}" -s /bin/bash -c \'set -ex; . <(opcli tutorial expand -- "$1")\' _ "${SPREAD_PATH}${TUTORIAL}"\n'
)


def spread_init(root: Path, *, force: bool = False) -> tuple[Path, Path]:
    """Generate ``spread.yaml`` and ``tests/integration/run/task.yaml``.

    Returns:
        Tuple of (spread.yaml path, task.yaml path).

    Raises:
        ConfigurationError: If files exist and *force* is ``False``.
    """
    spread_path = root / _SPREAD_YAML
    task_path = root / _TASK_YAML_REL

    if not force:
        for p in (spread_path, task_path):
            if p.exists():
                msg = f"{p.name} already exists. Use --force to overwrite."
                raise ConfigurationError(msg)

    project_name = root.resolve().name
    modules = _discover_test_modules(root)

    spread_content = _generate_spread_yaml(project_name, modules)
    spread_path.write_text(spread_content)
    logger.info("Wrote %s", spread_path)

    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(_TASK_YAML_CONTENT)
    logger.info("Wrote %s", task_path)

    return spread_path, task_path


# ---------------------------------------------------------------------------
#  spread expand
# ---------------------------------------------------------------------------

# -- Inline shell scripts for adhoc backends --------------------------------
#
# Spread prepends ``set -eu`` and defines helper functions (``ADDRESS``,
# ``FATAL``, ``ERROR``) in every script it runs.  Scripts must call
# ``ADDRESS <ip>`` to tell spread where to SSH.
#
# The local allocate script mirrors craft-application's .extension but is
# fully self-contained (no external script file).

_LOCAL_ALLOCATE = """\
DISTRO=$(echo "$SPREAD_SYSTEM" | cut -d- -f1)
SERIES=$(echo "$SPREAD_SYSTEM" | cut -d- -f2)
VM_NAME="spread-${DISTRO}-${SERIES}-$$-${RANDOM}"
VM_NAME=$(echo "$VM_NAME" | tr . -)

DISK="${DISK:-20}"
CPU="${CPU:-4}"
MEM="${MEM:-8}"

CLOUD_CONFIG=$(mktemp)
cat > "$CLOUD_CONFIG" <<'ENDCLOUD'
#cloud-config
ssh_pwauth: true
users:
  - name: ubuntu
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
ENDCLOUD

CLEANUP_VM=true
cleanup() {
  rm -f "$CLOUD_CONFIG" 2>/dev/null || true
  if [ "$CLEANUP_VM" = true ] && [ -n "${VM_NAME:-}" ]; then
    lxc delete --force "${VM_NAME}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

lxc launch --vm \\
  "${DISTRO}:${SERIES}" \\
  "${VM_NAME}" \\
  --config "user.user-data=$(cat "$CLOUD_CONFIG")" \\
  --config "limits.cpu=${CPU}" \\
  --config "limits.memory=${MEM}GiB" \\
  --device "root,size=${DISK}GiB" >&2

# Wait for LXD agent to be ready inside the VM
while ! lxc exec "${VM_NAME}" -- true &>/dev/null; do sleep 0.5; done

# Wait for cloud-init and snap seeding
lxc exec "${VM_NAME}" -- cloud-init status --wait >&2
lxc exec "${VM_NAME}" -- snap wait system seed.loaded >&2

# Set ubuntu user password (using lxc exec to avoid YAML escaping issues)
lxc exec "${VM_NAME}" -- bash -c "echo ubuntu:${SPREAD_PASSWORD} | chpasswd"

# Enable SSH password authentication
lxc exec "${VM_NAME}" -- bash -c \\
  'if [ -d /etc/ssh/sshd_config.d ]; then
     printf "PasswordAuthentication yes\\n" \\
       > /etc/ssh/sshd_config.d/00-spread.conf
   fi'
lxc exec "${VM_NAME}" -- sed -i \\
  's/^\\s*#\\?\\s*PasswordAuthentication\\>.*/PasswordAuthentication yes/' \\
  /etc/ssh/sshd_config
lxc exec "${VM_NAME}" -- killall -HUP sshd 2>/dev/null || true

# Get and report the VM's IPv4 address
while true; do
  RAW_ADDR=$(lxc ls --format csv --columns 4 "name=${VM_NAME}" | head -1)
  ADDR=$(echo "$RAW_ADDR" | awk '{print $1}')
  if [ -n "$ADDR" ]; then
    CLEANUP_VM=false
    ADDRESS "$ADDR"
    break
  fi
  sleep 0.5
done
"""

_LOCAL_DISCARD = """\
instance_name=$(lxc ls --format json \
  | jq -r --arg a "$SPREAD_SYSTEM_ADDRESS" \
    '.[] | select(any(
      .state.network[]?.addresses[]?; .address == $a
    )) | .name' | head -1)
if [ -n "$instance_name" ]; then
  lxc delete --force "$instance_name"
fi
"""

_LOCAL_PREPARE = """\
loginctl enable-linger ubuntu
sudo snap install astral-uv --classic || true
export UV_TOOL_BIN_DIR=/usr/local/bin
if grep -q 'name = "opcli"' "${SPREAD_PATH}/pyproject.toml" 2>/dev/null; then
  uv tool install "${SPREAD_PATH}" --quiet
else
  uv tool install \
      "git+https://github.com/javierdelapuente/operator-ci-poc@${OPCLI_GIT_REF:-main}" \
      --quiet
fi
if ! command -v spread >/dev/null 2>&1; then
  sudo snap install go --classic
  go install github.com/canonical/spread/cmd/spread@latest
  sudo ln -sf ~/go/bin/spread /usr/local/bin/spread
fi
runuser -l ubuntu -c "UV_TOOL_BIN_DIR=/usr/local/bin uv tool install tox --with tox-uv --quiet"
if [ -f "$CONCIERGE" ]; then
  sudo snap install concierge --classic || true
  concierge prepare -c "$CONCIERGE"
  runuser -l ubuntu -c \
    "cd \\"${SPREAD_PATH}\\" && opcli provision registry -c \\"$CONCIERGE\\""
fi
if [ -f "${SPREAD_PATH}/artifacts-generated.yaml" ] && \
    curl -sf --max-time 5 http://localhost:32000/v2/ > /dev/null 2>&1; then
  opcli provision load
fi
chown -R ubuntu:ubuntu "${SPREAD_PATH}"
"""

_CI_PREPARE = """\
loginctl enable-linger ubuntu
snap install astral-uv --classic || true
export UV_TOOL_BIN_DIR=/usr/local/bin
if [ -n "${GITHUB_WORKSPACE:-}" ] && grep -q 'name = "opcli"' "${GITHUB_WORKSPACE}/pyproject.toml" 2>/dev/null; then
  uv tool install "${GITHUB_WORKSPACE}" --quiet
elif grep -q 'name = "opcli"' "${SPREAD_PATH}/pyproject.toml" 2>/dev/null; then
  uv tool install "${SPREAD_PATH}" --quiet
else
  uv tool install \
      "git+https://github.com/javierdelapuente/operator-ci-poc@${OPCLI_GIT_REF:-main}" \
      --quiet
fi
if ! command -v spread >/dev/null 2>&1; then
  snap install go --classic
  go install github.com/canonical/spread/cmd/spread@latest
  ln -sf ~/go/bin/spread /usr/local/bin/spread
fi
runuser -l ubuntu -c "UV_TOOL_BIN_DIR=/usr/local/bin uv tool install tox --with tox-uv --quiet"
if [ -f "$CONCIERGE" ]; then
  snap install concierge --classic || true
  concierge prepare -c "$CONCIERGE"
fi
if [ -n "${GITHUB_RUN_ID:-}" ]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "Error: gh CLI is required for CI artifact download but was not found" >&2
    exit 1
  fi
  export GH_TOKEN="${GITHUB_TOKEN}"
  cd "${SPREAD_PATH}" && opcli artifacts fetch \
    --run-id "${GITHUB_RUN_ID}" \
    --repo "${GITHUB_REPOSITORY}" \
    --wait
fi
chown -R ubuntu:ubuntu "${SPREAD_PATH}"
"""

_CI_ALLOCATE = """\
id ubuntu &>/dev/null || sudo useradd -m -s /bin/bash ubuntu
sudo sed -i 's/^[[:space:]]*#\\?[[:space:]]*\\(PermitRootLogin\\|PasswordAuthentication\\).*/\\1 yes/' \
    /etc/ssh/sshd_config
if [ -d /etc/ssh/sshd_config.d ]; then
  printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' | \
      sudo tee /etc/ssh/sshd_config.d/00-spread.conf > /dev/null
fi
sudo systemctl restart ssh
echo "root:${SPREAD_PASSWORD}" | sudo chpasswd

ADDRESS localhost
"""

# Tutorial backend: install uv then opcli so that ``opcli tutorial expand``
# is available inside the VM.
_TUTORIAL_LOCAL_PREPARE = """\
sudo snap install astral-uv --classic || true
export UV_TOOL_BIN_DIR=/usr/local/bin
if grep -q 'name = "opcli"' "${SPREAD_PATH}/pyproject.toml" 2>/dev/null; then
  uv tool install "${SPREAD_PATH}" --quiet
else
  uv tool install \
      "git+https://github.com/javierdelapuente/operator-ci-poc@${OPCLI_GIT_REF:-main}" \
      --quiet
fi
"""


# Map each virtual backend name to:
#   (concrete_local, concrete_ci, local_prepare, ci_prepare)
# The CI prepare for tutorial-test is empty — workflows are expected to
# install opcli before invoking spread.
_BACKEND_CONFIGS: dict[str, tuple[str, str, str, str]] = {
    _VIRTUAL_BACKEND: ("local", "ci", _LOCAL_PREPARE, _CI_PREPARE),
    _TUTORIAL_BACKEND: ("local-tutorial", "ci-tutorial", _TUTORIAL_LOCAL_PREPARE, ""),
}

# Keys in system entries that are opcli-specific and must be stripped before
# passing to spread.  ``runner`` is a GitHub Actions runner-label field only
# meaningful to the CI backend; resource fields are used by the local allocate
# script and have no meaning in spread's own backend model.
_LOCAL_STRIP_KEYS: frozenset[str] = frozenset({"cpu", "memory", "disk", "runner"})
_CI_STRIP_KEYS: frozenset[str] = frozenset({"cpu", "memory", "disk", "runner"})

# Names of resource fields and their corresponding shell variable names.
_RESOURCE_FIELDS: dict[str, str] = {"cpu": "CPU", "memory": "MEM", "disk": "DISK"}


def _is_ci() -> bool:
    """Return True when running inside CI (truthy ``CI`` env var)."""
    return bool(os.environ.get("CI"))


def _extract_system_resources(
    systems: list[object],
) -> dict[str, dict[str, int]]:
    """Return ``{system_name: {cpu/memory/disk: value}}`` from system entries.

    Only positive integer values are accepted.

    Raises:
        ValidationError: If a resource value is not a positive integer.
    """
    result: dict[str, dict[str, int]] = {}
    for entry in systems:
        if not isinstance(entry, dict):
            continue
        for name, props in entry.items():
            if not isinstance(props, dict):
                continue
            res: dict[str, int] = {}
            for field in _RESOURCE_FIELDS:
                val = props.get(field)
                if val is None:
                    continue
                if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                    msg = (
                        f"System '{name}': '{field}' must be a positive integer, "
                        f"got {val!r}"
                    )
                    raise ValidationError(msg)
                res[field] = val
            if res:
                result[name] = res
    return result


def _make_resource_preamble(resources: dict[str, dict[str, int]]) -> str:
    """Return a bash ``case`` snippet that sets CPU/MEM/DISK per ``$SPREAD_SYSTEM``.

    Each arm uses ``${VAR:-N}`` so that an explicit env-var override still wins.
    Returns an empty string when *resources* is empty.
    """
    if not resources:
        return ""
    lines = ['case "$SPREAD_SYSTEM" in\n']
    for sys_name, res in resources.items():
        parts = [
            f'{shell_var}="${{{shell_var}:-{res[field]}}}"'
            for field, shell_var in _RESOURCE_FIELDS.items()
            if field in res
        ]
        if parts:
            # Quote the pattern to prevent shell glob expansion (e.g. ubuntu-*)
            lines.append(f'  "{sys_name}") {"; ".join(parts)} ;;\n')
    lines.append("esac\n\n")
    return "".join(lines)


def _transform_system_props(
    name: str,
    props: object,
    *,
    strip_keys: frozenset[str],
    inject_username: str | None,
) -> object:
    """Return the transformed props for a single system name→props pair."""
    if isinstance(props, dict):
        new_props = {k: v for k, v in props.items() if k not in strip_keys}
        if inject_username:
            new_props.setdefault("username", inject_username)
        return new_props if new_props else None
    if props is None:
        if inject_username:
            return {"username": inject_username}
        return None
    return props


def _transform_systems(
    systems: list[object],
    *,
    strip_keys: frozenset[str],
    inject_username: str | None = None,
) -> list[object]:
    """Strip opcli-specific keys from system entries and optionally inject ``username``.

    For each system entry:
    - Plain strings: converted to a dict if username injection is needed.
    - Dict entries: ``strip_keys`` are removed from the props mapping; ``username``
      is set via ``setdefault`` if *inject_username* is given.
    - If all props are removed and no username is injected, collapses back to a
      plain string (avoids ``{"ubuntu-24.04": {}}`` noise in the output).
    """
    result: list[object] = []
    for entry in systems:
        if isinstance(entry, str):
            if inject_username:
                result.append({entry: {"username": inject_username}})
            else:
                result.append(entry)
        elif isinstance(entry, dict):
            merged: dict[str, object] = {
                name: _transform_system_props(
                    name, props, strip_keys=strip_keys, inject_username=inject_username
                )
                for name, props in entry.items()
            }
            # If a single-key mapping collapsed to {name: None}, emit plain string
            if (
                len(merged) == 1
                and next(iter(merged.values())) is None
                and not inject_username
            ):
                result.append(next(iter(merged)))
            else:
                result.append(merged)
        else:
            result.append(entry)
    return result


def _build_concrete_backend(
    virtual: object,
    *,
    use_ci: bool,
    local_prepare: str,
    ci_prepare: str,
) -> dict[str, object]:
    """Return a concrete adhoc backend dict built from a virtual backend entry."""
    backend_def: dict[str, object] = (
        deepcopy(virtual) if isinstance(virtual, dict) else {}
    )
    backend_def["type"] = "adhoc"

    systems = backend_def.get("systems")

    if use_ci:
        backend_def["allocate"] = _CI_ALLOCATE
        if ci_prepare:
            backend_def["prepare"] = ci_prepare
        # GitHub Actions vars are only needed for the CI backend so that
        # _CI_PREPARE can authenticate and download build artifacts via gh.
        # Scoping them here keeps the root spread.yaml clean for local runs.
        backend_def["environment"] = {
            "GITHUB_TOKEN": '$(HOST: echo "${GITHUB_TOKEN:-}")',
            "GITHUB_RUN_ID": '$(HOST: echo "${GITHUB_RUN_ID:-}")',
            "GITHUB_REPOSITORY": '$(HOST: echo "${GITHUB_REPOSITORY:-}")',
            "GITHUB_WORKSPACE": '$(HOST: echo "${GITHUB_WORKSPACE:-}")',
        }
        if isinstance(systems, list):
            backend_def["systems"] = _transform_systems(
                systems, strip_keys=_CI_STRIP_KEYS, inject_username="root"
            )
    else:
        # Extract per-system resource overrides before stripping the fields.
        resources: dict[str, dict[str, int]] = {}
        if isinstance(systems, list):
            resources = _extract_system_resources(systems)

        preamble = _make_resource_preamble(resources)
        backend_def["allocate"] = preamble + _LOCAL_ALLOCATE
        backend_def["discard"] = _LOCAL_DISCARD
        if local_prepare:
            backend_def["prepare"] = local_prepare

        if isinstance(systems, list):
            backend_def["systems"] = _transform_systems(
                systems, strip_keys=_LOCAL_STRIP_KEYS, inject_username="ubuntu"
            )

    return backend_def


def _replace_suite_backend_name(
    data: dict[str, object],
    virtual_name: str,
    concrete_name: str,
) -> None:
    """Replace *virtual_name* with *concrete_name* in all suite ``backends:`` lists."""
    suites = data.get("suites")
    if not isinstance(suites, dict):
        return
    for suite_cfg in suites.values():
        if isinstance(suite_cfg, dict):
            suite_backends = suite_cfg.get("backends")
            if isinstance(suite_backends, list):
                suite_cfg["backends"] = [
                    concrete_name if b == virtual_name else b for b in suite_backends
                ]


def _expand_backend(
    spread_data: dict[str, object],
    *,
    ci: bool | None = None,
) -> dict[str, object]:
    """Replace all known virtual backends with concrete ones.

    Recognises ``integration-test`` and ``tutorial-test`` virtual backends.
    Each is removed from the YAML and replaced with its concrete counterpart
    (``local`` / ``ci`` for integration, ``local-tutorial`` / ``ci-tutorial``
    for tutorials).  All user-defined fields (``systems``, ``environment``,
    ``prepare-each``, ``kill-timeout``, etc.) are preserved.

    Suite-level ``backends:`` lists are also updated so that any reference to
    a virtual backend name is replaced with the corresponding concrete name.
    This prevents suites from accidentally running on the wrong backend when
    both virtual backends are declared in the same ``spread.yaml``.

    Args:
        spread_data: Parsed spread.yaml (mutated in place on a deep copy).
        ci: Force CI mode if True, local if False, auto-detect if None.

    Returns:
        New dict with the backends replaced.

    Raises:
        ConfigurationError: If no known virtual backend is found.
    """
    data = deepcopy(spread_data)
    backends = data.get("backends")
    if not isinstance(backends, dict):
        msg = "spread.yaml has no 'backends' section"
        raise ConfigurationError(msg)

    use_ci = ci if ci is not None else _is_ci()
    found_any = False

    for virtual_name, (
        concrete_local,
        concrete_ci,
        local_prepare,
        ci_prepare,
    ) in _BACKEND_CONFIGS.items():
        virtual = backends.pop(virtual_name, None)
        if virtual is None:
            continue
        found_any = True

        concrete_name = concrete_ci if use_ci else concrete_local
        backends[concrete_name] = _build_concrete_backend(
            virtual,
            use_ci=use_ci,
            local_prepare=local_prepare,
            ci_prepare=ci_prepare,
        )
        _replace_suite_backend_name(data, virtual_name, concrete_name)

    if not found_any:
        known = ", ".join(f"'{n}'" for n in _BACKEND_CONFIGS)
        msg = (
            f"spread.yaml contains no known virtual backend ({known}). "
            "Nothing to expand."
        )
        raise ConfigurationError(msg)

    data["backends"] = backends
    return data


def _load_spread_yaml(root: Path) -> dict[str, Any]:
    """Load and validate ``spread.yaml`` from *root*.

    Returns:
        Parsed YAML mapping.

    Raises:
        ConfigurationError: If the file is missing or not a YAML mapping.
    """
    spread_path = root / _SPREAD_YAML
    if not spread_path.exists():
        msg = f"{_SPREAD_YAML} not found. Run 'opcli spread init' first."
        raise ConfigurationError(msg)

    with spread_path.open() as fh:
        data = _yaml.load(fh)

    if not isinstance(data, dict):
        msg = f"{_SPREAD_YAML} does not contain a YAML mapping"
        raise ConfigurationError(msg)

    return data


def _expand(root: Path, *, ci: bool | None = None) -> dict[str, Any]:
    """Load ``spread.yaml``, expand its virtual backend, return the dict."""
    data = _load_spread_yaml(root)
    return _expand_backend(data, ci=ci)


def _compose_reroot(existing_reroot: object | None) -> str:
    """Return a ``reroot`` value that accounts for the temp sub-directory.

    The expanded ``spread.yaml`` lives one directory below the project root,
    so we need ``..`` to point back.  If the user already specified a
    ``reroot`` in their original ``spread.yaml``, we compose ``../`` with
    that existing value (normalised).

    Raises:
        ConfigurationError: If *existing_reroot* is not a string or is absolute.
    """
    if existing_reroot is None:
        return ".."

    if not isinstance(existing_reroot, str):
        msg = (
            "'reroot' in spread.yaml must be a string, "
            f"got {type(existing_reroot).__name__}"
        )
        raise ConfigurationError(msg)

    if posixpath.isabs(existing_reroot):
        msg = (
            f"'reroot' in spread.yaml must be a relative path, got '{existing_reroot}'"
        )
        raise ConfigurationError(msg)

    return posixpath.normpath(posixpath.join("..", existing_reroot))


def spread_expand(
    root: Path,
    *,
    ci: bool | None = None,
) -> str:
    """Read ``spread.yaml`` and return the expanded content as a string.

    The output is for display / debugging; it does **not** include the
    ``reroot`` field that ``spread_run`` injects.

    Raises:
        ConfigurationError: If ``spread.yaml`` is missing or malformed.
    """
    expanded = _expand(root, ci=ci)
    buf = StringIO()
    _yaml.dump(_literalize(expanded), buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
#  spread run
# ---------------------------------------------------------------------------


def spread_run(
    root: Path,
    *,
    extra_args: list[str] | None = None,
    ci: bool | None = None,
) -> None:
    """Expand ``spread.yaml`` and run ``spread``.

    The expanded YAML is written to a temporary subdirectory inside *root*
    with a ``reroot: ..`` field so spread resolves the project tree from the
    parent directory.  Spread is invoked from that subdirectory; the original
    ``spread.yaml`` is never modified.

    Raises:
        ConfigurationError: If ``spread.yaml`` is missing or malformed.
        SubprocessError: If spread exits non-zero.
    """
    expanded = _expand(root, ci=ci)
    expanded["reroot"] = _compose_reroot(expanded.get("reroot"))

    with tempfile.TemporaryDirectory(prefix=".spread-run-", dir=root) as tmp_dir:
        tmp_yaml = Path(tmp_dir) / _SPREAD_YAML
        with tmp_yaml.open("w") as fh:
            _yaml.dump(_literalize(expanded), fh)

        cmd = ["spread"]
        if extra_args:
            cmd.extend(extra_args)
        run_command(cmd, cwd=tmp_dir, interactive=True)


# ---------------------------------------------------------------------------
#  spread tasks — enumerate CI test selectors for GitHub Actions matrix
# ---------------------------------------------------------------------------

_DEFAULT_RUNNER = "ubuntu-latest"


def _runner_by_system(raw: dict[str, object]) -> dict[str, str]:
    """Extract {system_name: runner_label_json} from the raw spread.yaml.

    The runner value is always JSON-encoded so that the workflow can use
    ``fromJSON(matrix.runs-on)`` for both plain strings and runner arrays.
    """
    result: dict[str, str] = {}
    raw_backends = raw.get("backends") or {}
    if not isinstance(raw_backends, dict):
        return result
    for backend_cfg in raw_backends.values():
        if not isinstance(backend_cfg, dict):
            continue
        for system_entry in backend_cfg.get("systems") or []:
            if isinstance(system_entry, str):
                result.setdefault(system_entry, json.dumps(_DEFAULT_RUNNER))
            elif isinstance(system_entry, dict):
                for sys_name, sys_props in system_entry.items():
                    raw_runner: object = _DEFAULT_RUNNER
                    if isinstance(sys_props, dict):
                        raw_runner = sys_props.get("runner", _DEFAULT_RUNNER)
                    result.setdefault(sys_name, json.dumps(raw_runner))
    return result


def _task_dirs(root: Path, suite_dir: str) -> list[str]:
    """Return sorted task directory names (dirs with task.yaml) inside *suite_dir*."""
    suite_abs = root / suite_dir
    if not suite_abs.is_dir():
        return []
    return sorted(
        d.name for d in suite_abs.iterdir() if d.is_dir() and (d / "task.yaml").exists()
    )


def _system_entry_name(entry: object) -> str | None:
    """Extract the system name from a spread systems-list entry."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        key = next(iter(entry))
        return str(key)
    return None


def spread_tasks(root: Path) -> list[dict[str, str]]:
    """Return all CI spread task selectors as a list of GitHub Actions matrix entries.

    Reads the raw ``spread.yaml`` to extract per-system ``runner`` labels
    (GitHub Actions runner selectors), then expands with CI mode to derive
    the concrete backend name and system names.

    Each entry has:
    - ``name``: display name (variant name, or task directory if no variant)
    - ``selector``: full spread selector (``backend:system:task/path:variant``)
    - ``runs-on``: GitHub Actions runner label (from ``runner:`` system field,
      or ``ubuntu-latest`` if absent)

    Only enumerates ``MODULE/*`` variants.  For non-standard suite structures
    use ``spread -list`` directly.

    Raises:
        ConfigurationError: If ``spread.yaml`` is missing or malformed.
    """
    raw = _load_spread_yaml(root)
    expanded = _expand_backend(raw, ci=True)

    runner_map = _runner_by_system(raw)
    raw_suites = expanded.get("suites") or {}
    raw_all_backends = expanded.get("backends") or {}
    suites: dict[str, object] = raw_suites if isinstance(raw_suites, dict) else {}
    all_backends: dict[str, object] = (
        raw_all_backends if isinstance(raw_all_backends, dict) else {}
    )

    entries: list[dict[str, str]] = []
    for suite_path, suite_cfg in suites.items():
        if not isinstance(suite_cfg, dict):
            continue
        suite_dir = suite_path.rstrip("/")

        suite_backend_names: list[str] = []
        raw_suite_backends = suite_cfg.get("backends")
        if isinstance(raw_suite_backends, list):
            suite_backend_names = [str(b) for b in raw_suite_backends]
        else:
            suite_backend_names = list(all_backends.keys())

        env = suite_cfg.get("environment") or {}
        variants: list[str | None] = [
            str(v) for k, v in env.items() if str(k).startswith("MODULE/")
        ]
        if not variants:
            variants = [None]

        dirs = _task_dirs(root, suite_dir)
        if not dirs:
            continue

        entries.extend(
            _collect_suite_entries(
                suite_dir,
                dirs,
                variants,
                suite_backend_names,
                all_backends,
                runner_map,
            )
        )

    return entries


def _collect_suite_entries(  # noqa: PLR0913
    suite_dir: str,
    dirs: list[str],
    variants: list[str | None],
    backend_names: list[str],
    all_backends: dict[str, object],
    runner_map: dict[str, str],
) -> list[dict[str, str]]:
    """Return matrix entries for one suite."""
    entries: list[dict[str, str]] = []
    for backend_name in backend_names:
        backend_cfg = all_backends.get(backend_name)
        if not isinstance(backend_cfg, dict):
            continue
        for system_entry in backend_cfg.get("systems") or []:
            system_name = _system_entry_name(system_entry)
            if system_name is None:
                continue
            runner = runner_map.get(system_name, json.dumps(_DEFAULT_RUNNER))
            for task_dir in dirs:
                task_path = f"{suite_dir}/{task_dir}"
                for variant in variants:
                    if variant:
                        selector = f"{backend_name}:{system_name}:{task_path}:{variant}"
                        name = variant
                    else:
                        selector = f"{backend_name}:{system_name}:{task_path}"
                        name = task_dir
                    entries.append(
                        {"name": name, "selector": selector, "runs-on": runner}
                    )
    return entries
