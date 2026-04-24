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
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  output:
    file: ./rock_dir/myrock.rock
- name: otherrock
  rockcraft-yaml: other/rockcraft.yaml
  output:
    image: ghcr.io/canonical/otherrock:abc
charms:
- name: mycharm
  charmcraft-yaml: charmcraft.yaml
  output:
    file: ./mycharm.charm
"""

_GENERATED_WITH_ROCKS_AND_RESOURCES = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  output:
    file: ./rock_dir/myrock.rock
charms:
- name: mycharm
  charmcraft-yaml: charmcraft.yaml
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
            "version: 1\n"
            "rocks:\n- name: r1\n  rockcraft-yaml: rd/rockcraft.yaml\n"
            "  output:\n    image: ghcr.io/r1:v1\n",
        )

        with patch("opcli.core.provision.run_command") as mock_run:
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_empty_generated_returns_empty(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")

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
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "  output:\n    file: ./rock_dir/myrock.rock\n"
            "    image: localhost:32000/myrock:latest\n",
        )

        with patch("opcli.core.provision.run_command") as mock_run:
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_no_writeback_when_nothing_pushed(self, tmp_path: Path) -> None:
        """artifacts-generated.yaml is not written when no rocks are pushed."""
        _write(tmp_path / "artifacts-generated.yaml", "version: 1\n")
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

# Provider listed without explicit enable: key — should be treated as enabled.
_CONCIERGE_MICROK8S_NO_ENABLE = """\
providers:
  microk8s:
    channel: 1.34-strict/stable
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

_CONCIERGE_MICROK8S_DISABLED = """\
providers:
  microk8s:
    enable: false
    channel: 1.34-strict/stable
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

    def test_skipped_when_provider_explicitly_disabled(self, tmp_path: Path) -> None:
        """enable: false in concierge.yaml opts the provider out."""
        _write(tmp_path / "concierge.yaml", _CONCIERGE_MICROK8S_DISABLED)
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

    def test_microk8s_provider_applies_manifest(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge.yaml", _CONCIERGE_MICROK8S)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        # Three calls: microk8s kubectl wait + apply + rollout status
        assert mock_run.call_count == 3  # noqa: PLR2004
        assert mock_run.call_args_list[0][0][0][:3] == ["microk8s", "kubectl", "wait"]
        apply_call = mock_run.call_args_list[1]
        assert apply_call[0][0] == ["microk8s", "kubectl", "apply", "-f", "-"]
        assert apply_call[1]["stdin"]  # manifest content passed via stdin
        assert mock_run.call_args_list[2][0][0][:3] == [
            "microk8s",
            "kubectl",
            "rollout",
        ]

    def test_provider_enabled_without_explicit_enable_key(self, tmp_path: Path) -> None:
        """Provider listed without enable: key should be treated as enabled."""
        _write(tmp_path / "concierge.yaml", _CONCIERGE_MICROK8S_NO_ENABLE)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        mock_run.assert_called()

    def test_k8s_provider_applies_manifest_and_waits(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge.yaml", _CONCIERGE_K8S)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        # Three calls: k8s kubectl wait + apply + rollout status
        assert mock_run.call_count == 3  # noqa: PLR2004
        wait_cmd = mock_run.call_args_list[0][0][0]
        assert wait_cmd[:3] == ["k8s", "kubectl", "wait"]
        assert "--for=condition=Ready" in wait_cmd
        apply_call = mock_run.call_args_list[1]
        assert apply_call[0][0] == ["k8s", "kubectl", "apply", "-f", "-"]
        assert apply_call[1]["stdin"]  # manifest content passed via stdin
        rollout_cmd = mock_run.call_args_list[2][0][0]
        assert rollout_cmd[:3] == ["k8s", "kubectl", "rollout"]
        assert "status" in rollout_cmd
        assert "deployment/registry" in rollout_cmd
        assert "container-registry" in rollout_cmd

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
        mock_run.assert_called()

    def test_k8s_manifest_contains_registry_image(self, tmp_path: Path) -> None:
        """Verify the registry.yaml manifest references registry:2 on NodePort 32000."""
        _write(tmp_path / "concierge.yaml", _CONCIERGE_K8S)
        applied_stdin: list[str] = []

        def capture_apply(cmd: list[str], **kwargs: object) -> object:
            if "apply" in cmd:
                stdin_content = kwargs.get("stdin")
                if isinstance(stdin_content, str):
                    applied_stdin.append(stdin_content)
            return None

        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.run_command", side_effect=capture_apply),
        ):
            provision_registry(tmp_path)

        assert applied_stdin, "apply was not called with stdin"
        content = applied_stdin[0]
        assert "registry:2" in content
        assert "nodePort: 32000" in content
        assert "container-registry" in content

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
