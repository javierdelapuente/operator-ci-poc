"""Integration tests for spread local backend with real LXD VMs.

These tests require ``spread`` and ``lxc`` to be available on the host.
They launch real VMs, so they are slow (~30-60s) and marked with
``@pytest.mark.integration``.

The prepare script is conditional — it skips concierge and provision load
when the corresponding files are absent.  This lets us exercise the full
allocate → SSH → execute → discard flow without needing concierge or
opcli installed inside the VM.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from opcli.core.spread import spread_run

_HAVE_SPREAD = shutil.which("spread") is not None
_HAVE_LXC = shutil.which("lxc") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_HAVE_SPREAD and _HAVE_LXC),
        reason="spread and/or lxc not available",
    ),
]


_SPREAD_YAML = """\
project: integration-test

path: /home/ubuntu/proj

kill-timeout: 30m

backends:
  integration-test:
    systems:
      - ubuntu-24.04

environment:
  CONCIERGE: concierge.yaml
  MODULE/test_basic: test_basic

exclude:
  - .git

suites:
  tests/:
    summary: integration tests
"""

_TASK_YAML = """\
summary: basic spread integration test

execute: |
    echo "spread integration test passed"
    whoami
    pwd
"""


class TestSpreadLxdLocal:
    """End-to-end tests using the local (LXD VM) backend."""

    def test_basic_spread_run(self, tmp_path: pytest.TempPathFactory) -> None:
        """A trivial task runs successfully inside an LXD VM."""
        # Set up project structure
        spread_path = tmp_path / "spread.yaml"  # type: ignore[operator]
        spread_path.write_text(_SPREAD_YAML)

        task_dir = tmp_path / "tests" / "run"  # type: ignore[operator]
        task_dir.mkdir(parents=True)
        (task_dir / "task.yaml").write_text(_TASK_YAML)

        # Run spread with local backend
        spread_run(tmp_path, ci=False)  # type: ignore[arg-type]

    def test_no_leftover_vms(self, tmp_path: pytest.TempPathFactory) -> None:
        """VMs are cleaned up after a successful spread run."""
        spread_path = tmp_path / "spread.yaml"  # type: ignore[operator]
        spread_path.write_text(_SPREAD_YAML)

        task_dir = tmp_path / "tests" / "run"  # type: ignore[operator]
        task_dir.mkdir(parents=True)
        (task_dir / "task.yaml").write_text(_TASK_YAML)

        # Count VMs with our naming prefix before
        before = _count_spread_vms()

        spread_run(tmp_path, ci=False)  # type: ignore[arg-type]

        # After successful run, VM count should be same as before
        after = _count_spread_vms()
        assert after == before


def _count_spread_vms() -> int:
    """Count LXD instances whose names start with ``spread-``."""
    result = subprocess.run(
        ["lxc", "ls", "--format", "csv", "--columns", "n"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0
    return sum(1 for line in result.stdout.splitlines() if line.startswith("spread-"))
