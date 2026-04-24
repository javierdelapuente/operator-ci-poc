"""Tests for ``opcli provision run``, ``opcli provision load``,
and ``opcli provision registry``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError
from opcli.core.provision import provision_load, provision_registry, provision_run


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
        # Single skopeo call: direct oci-archive → registry
        assert mock_run.call_count == 1

    def test_custom_registry(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_WITH_ROCKS)

        with patch("opcli.core.provision.run_command") as mock_run:
            pushed = provision_load(tmp_path, registry="myregistry:5000")

        assert "myregistry:5000" in pushed[0]
        # Verify the single push command uses the custom registry
        push_cmd = mock_run.call_args_list[0][0][0]
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

        # Single call: direct oci-archive → registry (no docker-daemon step)
        assert mock_run.call_count == 1
        cmd = mock_run.call_args_list[0][0][0]
        assert "rockcraft.skopeo" in cmd
        assert any("oci-archive:" in arg for arg in cmd)
        assert any("docker://" in arg for arg in cmd)
        assert not any("docker-daemon:" in arg for arg in cmd)
        assert "--dest-tls-verify=false" in cmd


_CONCIERGE_MICROK8S = """\
providers:
  microk8s:
    enable: true
    channel: 1.31-strict/stable
"""

_CONCIERGE_K8S = """\
providers:
  k8s:
    enable: true
    channel: 1.31/stable
"""

_CONCIERGE_BOTH = """\
providers:
  microk8s:
    enable: true
  k8s:
    enable: true
"""

_CONCIERGE_NEITHER = """\
providers:
  juju:
    channel: 3.6/stable
"""


class TestProvisionRegistry:
    """Tests for provision_registry()."""

    def test_skipped_when_no_concierge_yaml(self, tmp_path: Path) -> None:
        with patch("opcli.core.provision.run_command") as mock_run:
            result = provision_registry(tmp_path)
        assert result == "skipped"
        mock_run.assert_not_called()

    def test_skipped_when_no_k8s_provider(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge.yaml", _CONCIERGE_NEITHER)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "skipped"
        mock_run.assert_not_called()

    def test_already_running_skips_deployment(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge.yaml", _CONCIERGE_MICROK8S)
        with (
            patch("opcli.core.provision._is_port_open", return_value=True),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "already_running"
        mock_run.assert_not_called()

    def test_microk8s_provider_uses_microk8s_enable(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge.yaml", _CONCIERGE_MICROK8S)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd == ["microk8s", "enable", "registry"]

    def test_k8s_provider_applies_manifest_and_waits(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge.yaml", _CONCIERGE_K8S)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        # Two calls: kubectl apply + kubectl rollout status
        call_count = 2
        assert mock_run.call_count == call_count
        apply_cmd = mock_run.call_args_list[0][0][0]
        assert apply_cmd[:3] == ["kubectl", "apply", "-f"]
        rollout_cmd = mock_run.call_args_list[1][0][0]
        assert "rollout" in rollout_cmd
        assert "status" in rollout_cmd
        assert "deployment/registry" in rollout_cmd

    def test_both_providers_raises(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge.yaml", _CONCIERGE_BOTH)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            pytest.raises(ConfigurationError, match="Both"),
        ):
            provision_registry(tmp_path)

    def test_custom_concierge_file(self, tmp_path: Path) -> None:
        _write(tmp_path / "my-concierge.yaml", _CONCIERGE_MICROK8S)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path, concierge_file="my-concierge.yaml")
        assert result == "deployed"
        mock_run.assert_called_once()

    def test_k8s_manifest_contains_registry_image(self, tmp_path: Path) -> None:
        """Verify the embedded manifest references the registry:2 image."""
        _write(tmp_path / "concierge.yaml", _CONCIERGE_K8S)
        applied_files: list[str] = []

        def capture_apply(cmd: list[str], **_kwargs: object) -> object:
            if "apply" in cmd:
                manifest_path = cmd[cmd.index("-f") + 1]
                applied_files.append(Path(manifest_path).read_text())
            return None

        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command", side_effect=capture_apply),
        ):
            provision_registry(tmp_path)

        assert applied_files, "apply was not called"
        assert "registry:2" in applied_files[0]
        assert "nodePort: 32000" in applied_files[0]
