"""Tests for ``opcli provision run``, ``opcli provision load``,
and ``opcli provision registry``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError
from opcli.core.provision import provision_load, provision_registry, provision_run
from opcli.core.yaml_io import load_artifacts_generated


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

_GENERATED_WITH_ROCKS_AND_RESOURCES = """\
version: 2
rocks:
- name: myrock
  source: rock_dir
  output:
    file: ./rock_dir/myrock.rock
charms:
- name: mycharm
  source: .
  output:
    file: ./mycharm.charm
  resources:
    myrock-image:
      type: oci-image
      rock: myrock
      file: ./rock_dir/myrock.rock
    other-res:
      type: oci-image
      rock: otherrock
      image: ghcr.io/canonical/otherrock:abc
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

    def test_updates_artifacts_generated_with_image_ref(self, tmp_path: Path) -> None:
        """After pushing, rock.output.image is set and file is preserved."""
        _write(tmp_path / "artifacts-generated.yaml", _GENERATED_WITH_ROCKS)

        with patch("opcli.core.provision.run_command"):
            provision_load(tmp_path)

        updated = load_artifacts_generated(tmp_path / "artifacts-generated.yaml")
        myrock = next(r for r in updated.rocks if r.name == "myrock")
        assert myrock.output.image == "localhost:32000/myrock:latest"
        assert myrock.output.file == "./rock_dir/myrock.rock"

    def test_updates_charm_resources_for_pushed_rock(self, tmp_path: Path) -> None:
        """Charm resources referencing a pushed rock get image ref set."""
        _write(
            tmp_path / "artifacts-generated.yaml",
            _GENERATED_WITH_ROCKS_AND_RESOURCES,
        )

        with patch("opcli.core.provision.run_command"):
            provision_load(tmp_path)

        updated = load_artifacts_generated(tmp_path / "artifacts-generated.yaml")
        charm = updated.charms[0]
        myrock_res = charm.resources["myrock-image"]  # type: ignore[index]
        assert myrock_res.image == "localhost:32000/myrock:latest"
        assert myrock_res.file == "./rock_dir/myrock.rock"
        # Resource referencing a different rock is not touched
        other_res = charm.resources["other-res"]  # type: ignore[index]
        assert other_res.image == "ghcr.io/canonical/otherrock:abc"

    def test_idempotent_skips_already_loaded_rock(self, tmp_path: Path) -> None:
        """Rock with image already set to the target ref is skipped."""
        _write(
            tmp_path / "artifacts-generated.yaml",
            "version: 2\n"
            "rocks:\n- name: myrock\n  source: rock_dir\n"
            "  output:\n    file: ./rock_dir/myrock.rock\n"
            "    image: localhost:32000/myrock:latest\n",
        )

        with patch("opcli.core.provision.run_command") as mock_run:
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_no_writeback_when_nothing_pushed(self, tmp_path: Path) -> None:
        """artifacts-generated.yaml is not written when no rocks are pushed."""
        _write(tmp_path / "artifacts-generated.yaml", "version: 2\n")
        mtime_before = (tmp_path / "artifacts-generated.yaml").stat().st_mtime

        with patch("opcli.core.provision.run_command"):
            provision_load(tmp_path)

        mtime_after = (tmp_path / "artifacts-generated.yaml").stat().st_mtime
        assert mtime_before == mtime_after


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

    def test_malformed_providers_field_skips_gracefully(self, tmp_path: Path) -> None:
        """Non-dict providers field should not crash."""
        _write(tmp_path / "concierge.yaml", "providers: not-a-dict\n")
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "skipped"
        mock_run.assert_not_called()
