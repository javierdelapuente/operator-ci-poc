"""Core logic for ``opcli spread init``, ``expand``, and ``run``.

``init`` discovers integration test modules and generates ``spread.yaml``
plus ``tests/run/task.yaml``.

``expand`` reads ``spread.yaml``, replaces the virtual ``integration-test``
backend with a concrete ``local:`` or ``ci:`` backend, and returns the
expanded YAML.  The original file is **never** modified.

``run`` expands to a temporary file and invokes ``spread`` as a subprocess.
"""

from __future__ import annotations

import logging
import os
import tempfile
from copy import deepcopy
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML

from opcli.core.exceptions import ConfigurationError
from opcli.core.subprocess import run_command

logger = logging.getLogger(__name__)

_yaml = YAML()
_yaml.default_flow_style = False

_SPREAD_YAML = "spread.yaml"
_TASK_YAML_REL = "tests/run/task.yaml"
_VIRTUAL_BACKEND = "integration-test"


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
    data: dict[str, object] = {
        "project": project_name,
        "backends": {
            _VIRTUAL_BACKEND: {
                "systems": ["ubuntu-24.04"],
            },
        },
    }

    env: dict[str, str] = {
        "CONCIERGE": '$(HOST: echo "${CONCIERGE:-concierge.yaml}")',
    }
    if modules:
        for mod in modules:
            env[f"MODULE/{mod}"] = mod
    else:
        env["MODULE/tests"] = "tests"
    data["environment"] = env

    data["suites"] = {
        "tests/": {},
    }

    _yaml.dump(data, buf)
    return buf.getvalue()


_TASK_YAML_CONTENT = """\
summary: integration tests

execute: |
    $( opcli pytest run -- -k $MODULE )
"""


def spread_init(root: Path, *, force: bool = False) -> tuple[Path, Path]:
    """Generate ``spread.yaml`` and ``tests/run/task.yaml``.

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

_LOCAL_PREPARE = """\
#!/bin/bash
set -eu
sudo concierge prepare -c "$CONCIERGE"
opcli provision load
"""

_CI_PREPARE = """\
#!/bin/bash
set -eu
sudo concierge prepare -c "$CONCIERGE"
"""


def _is_ci() -> bool:
    """Return True when running inside CI (truthy ``CI`` env var)."""
    return bool(os.environ.get("CI"))


def _expand_backend(
    spread_data: dict[str, object],
    *,
    ci: bool | None = None,
) -> dict[str, object]:
    """Replace the virtual backend with a concrete one.

    Args:
        spread_data: Parsed spread.yaml (mutated in place on a deep copy).
        ci: Force CI mode if True, local if False, auto-detect if None.

    Returns:
        New dict with the backend replaced.
    """
    data = deepcopy(spread_data)
    backends = data.get("backends")
    if not isinstance(backends, dict):
        msg = "spread.yaml has no 'backends' section"
        raise ConfigurationError(msg)

    virtual = backends.pop(_VIRTUAL_BACKEND, None)
    if virtual is None:
        msg = (
            f"spread.yaml has no '{_VIRTUAL_BACKEND}' virtual backend. "
            "Nothing to expand."
        )
        raise ConfigurationError(msg)

    use_ci = ci if ci is not None else _is_ci()

    if use_ci:
        backend_def: dict[str, object] = {
            "type": "adhoc",
            "allocate": "ADDRESS localhost",
            "prepare": _CI_PREPARE,
        }
    else:
        backend_def = {
            "prepare": _LOCAL_PREPARE,
        }

    if isinstance(virtual, dict):
        systems = virtual.get("systems")
        if systems:
            backend_def["systems"] = systems

    backend_name = "ci" if use_ci else "local"
    backends[backend_name] = backend_def
    data["backends"] = backends
    return data


def spread_expand(
    root: Path,
    *,
    ci: bool | None = None,
) -> str:
    """Read ``spread.yaml`` and return the expanded content as a string.

    Raises:
        ConfigurationError: If ``spread.yaml`` is missing or malformed.
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

    expanded = _expand_backend(data, ci=ci)
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

    The expanded file is written to a temporary location — the original
    ``spread.yaml`` is never modified.

    Raises:
        ConfigurationError: If ``spread.yaml`` is missing or malformed.
        SubprocessError: If spread exits non-zero.
    """
    expanded_content = spread_expand(root, ci=ci)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix="spread-expanded-",
        delete=False,
    ) as tmp:
        tmp.write(expanded_content)
        tmp_path = tmp.name

    try:
        cmd = ["spread", f"-spread={tmp_path}"]
        if extra_args:
            cmd.extend(extra_args)
        run_command(cmd, cwd=str(root))
    finally:
        Path(tmp_path).unlink(missing_ok=True)
