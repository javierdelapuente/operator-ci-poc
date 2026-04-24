"""Tests for ``opcli provision run`` and ``opcli provision load``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError
from opcli.core.provision import provision_load, provision_run


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


_GENERATED_WITH_ROCKS = """\
version: 2
rocks:
- name: myrock
  source: rock_dir
  output:
    file: ./rock_dir/myrock.rock
- name: otherrock
  source: other
  output:
    image: ghcr.io/canonical/otherrock:abc
charms:
- name: mycharm
  source: .
  output:
    file: ./mycharm.charm
"""


class TestProvisionRun:
    """Tests for provision_run()."""

    def test_runs_concierge(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge.yaml", "providers: {}\n")

        with patch("opcli.core.provision.run_command") as mock_run:
            provision_run(tmp_path)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "concierge" in cmd
        assert "prepare" in cmd
        assert any("concierge.yaml" in arg for arg in cmd)

    def test_missing_concierge_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            provision_run(tmp_path)

    def test_custom_concierge_file(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge_juju4.yaml", "providers: {}\n")

        with patch("opcli.core.provision.run_command") as mock_run:
            provision_run(tmp_path, concierge_file="concierge_juju4.yaml")

        cmd = mock_run.call_args[0][0]
        assert any("concierge_juju4.yaml" in arg for arg in cmd)


class TestProvisionLoad:
    """Tests for provision_load()."""

    def test_missing_generated_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            provision_load(tmp_path)

    def test_pushes_local_rocks(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_WITH_ROCKS)

        with patch("opcli.core.provision.run_command") as mock_run:
            pushed = provision_load(tmp_path)

        # Only myrock has a file output; otherrock has image (CI) → skipped
        assert len(pushed) == 1
        assert "myrock" in pushed[0]
        assert "localhost:32000" in pushed[0]
        # Two skopeo calls: copy to docker-daemon, then push to registry
        assert mock_run.call_count == 2  # noqa: PLR2004

    def test_custom_registry(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_WITH_ROCKS)

        with patch("opcli.core.provision.run_command") as mock_run:
            pushed = provision_load(tmp_path, registry="myregistry:5000")

        assert "myregistry:5000" in pushed[0]
        # Verify the push command uses the custom registry
        push_cmd = mock_run.call_args_list[1][0][0]
        assert any("myregistry:5000" in arg for arg in push_cmd)

    def test_no_local_rocks_returns_empty(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 2\n"
            "rocks:\n- name: r1\n  source: rd\n"
            "  output:\n    image: ghcr.io/r1:v1\n",
        )

        with patch("opcli.core.provision.run_command") as mock_run:
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_empty_generated_returns_empty(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 2\n")

        with patch("opcli.core.provision.run_command") as mock_run:
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_skopeo_commands_correct(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_WITH_ROCKS)

        with patch("opcli.core.provision.run_command") as mock_run:
            provision_load(tmp_path)

        # First call: copy rock archive to docker daemon
        copy_cmd = mock_run.call_args_list[0][0][0]
        assert "rockcraft.skopeo" in copy_cmd
        assert any("oci-archive:" in arg for arg in copy_cmd)
        assert any("docker-daemon:" in arg for arg in copy_cmd)

        # Second call: push from docker daemon to registry
        push_cmd = mock_run.call_args_list[1][0][0]
        assert "rockcraft.skopeo" in push_cmd
        assert any("docker://" in arg for arg in push_cmd)
