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

import logging
import os
import posixpath
import tempfile
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from opcli.core.exceptions import ConfigurationError
from opcli.core.subprocess import run_command

logger = logging.getLogger(__name__)

_yaml = YAML()
_yaml.default_flow_style = False

_SPREAD_YAML = "spread.yaml"
_TASK_YAML_REL = "tests/integration/run/task.yaml"
_TUTORIAL_TASK_YAML_REL = "tests/tutorial/run/task.yaml"
_VIRTUAL_BACKEND = "integration-test"
_TUTORIAL_BACKEND = "tutorial-test"


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
        "SUDO_USER": "",
        "SUDO_UID": "",
        "LANG": "C.UTF-8",
        "LANGUAGE": "en",
        "CONCIERGE": '$(HOST: echo "${CONCIERGE:-concierge.yaml}")',
    }

    # Suite environment: MODULE variants (scoped to this suite)
    suite_env: dict[str, str] = {}
    if modules:
        for mod in modules:
            suite_env[f"MODULE/{mod}"] = mod
    else:
        suite_env["MODULE/tests"] = "tests"

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


_TASK_YAML_CONTENT = """\
summary: integration tests

execute: |
    $( opcli pytest run -- -k $MODULE )
"""

_TUTORIAL_TASK_YAML_CONTENT = """\
summary: tutorial test

execute: |
    eval "$(opcli tutorial expand "$TUTORIAL")"
"""


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
instance_name=$(lxc ls --format csv --columns n4 \\
  | awk -F, -v addr="$SPREAD_SYSTEM_ADDRESS" \\
    '{split($2,a," "); if(a[1]==addr) {print $1; exit}}')
if [ -n "$instance_name" ]; then
  lxc delete --force "$instance_name"
fi
"""

_LOCAL_PREPARE = """\
if [ -f "$CONCIERGE" ]; then
  sudo concierge prepare -c "$CONCIERGE"
fi
if [ -f artifacts-generated.yaml ]; then
  opcli provision load
fi
"""

_CI_PREPARE = """\
if [ -f "$CONCIERGE" ]; then
  sudo concierge prepare -c "$CONCIERGE"
fi
"""

# Tutorial backend: install pip then opcli from the GitHub repo main branch so
# that ``opcli tutorial expand`` is available inside the VM.
_TUTORIAL_LOCAL_PREPARE = """\
sudo apt-get install -y python3-pip --quiet
pip install git+https://github.com/javierdelapuente/operator-ci-poc@main --quiet
"""


# Map each virtual backend name to:
#   (concrete_local, concrete_ci, local_prepare, ci_prepare)
# The CI prepare for tutorial-test is empty — workflows are expected to
# install opcli before invoking spread.
_BACKEND_CONFIGS: dict[str, tuple[str, str, str, str]] = {
    _VIRTUAL_BACKEND: ("local", "ci", _LOCAL_PREPARE, _CI_PREPARE),
    _TUTORIAL_BACKEND: ("local-tutorial", "ci-tutorial", _TUTORIAL_LOCAL_PREPARE, ""),
}


def _is_ci() -> bool:
    """Return True when running inside CI (truthy ``CI`` env var)."""
    return bool(os.environ.get("CI"))


def _inject_username(
    systems: list[object],
    username: str,
) -> list[object]:
    """Deep-merge ``username`` into each system entry.

    Handles both scalar entries (``"ubuntu-24.04"``) and mapping entries
    (``{"ubuntu-24.04": {"runner": [...]}}``) without dropping user-defined
    fields.
    """
    result: list[object] = []
    for entry in systems:
        if isinstance(entry, str):
            result.append({entry: {"username": username}})
        elif isinstance(entry, dict):
            merged: dict[str, object] = {}
            for name, props in entry.items():
                if isinstance(props, dict):
                    new_props = dict(props)
                    new_props.setdefault("username", username)
                    merged[name] = new_props
                elif props is None:
                    merged[name] = {"username": username}
                else:
                    merged[name] = props
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

    if use_ci:
        backend_def["allocate"] = "ADDRESS localhost"
        if ci_prepare:
            backend_def["prepare"] = ci_prepare
    else:
        backend_def["allocate"] = _LOCAL_ALLOCATE
        backend_def["discard"] = _LOCAL_DISCARD
        if local_prepare:
            backend_def["prepare"] = local_prepare

        systems = backend_def.get("systems")
        if isinstance(systems, list):
            backend_def["systems"] = _inject_username(systems, "ubuntu")

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
    _yaml.dump(expanded, buf)
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

    A temporary directory is created **inside** *root* containing the
    expanded ``spread.yaml`` with ``reroot: ..`` so that ``spread``
    discovers the config in the temp dir and resolves the project tree
    from the parent.  The original ``spread.yaml`` is never modified.

    Raises:
        ConfigurationError: If ``spread.yaml`` is missing or malformed.
        SubprocessError: If spread exits non-zero.
    """
    expanded = _expand(root, ci=ci)
    expanded["reroot"] = _compose_reroot(expanded.get("reroot"))

    with tempfile.TemporaryDirectory(
        prefix=".opcli-spread-",
        dir=root,
    ) as temp_dir:
        temp_dir_path = Path(temp_dir)
        spread_file = temp_dir_path / _SPREAD_YAML
        with spread_file.open("w") as fh:
            _yaml.dump(expanded, fh)

        cmd = ["spread"]
        if extra_args:
            cmd.extend(extra_args)
        run_command(cmd, cwd=str(temp_dir_path))
