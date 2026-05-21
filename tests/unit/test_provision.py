"""Tests for ``opcli provision prepare``, ``opcli provision load``,
and ``opcli provision registry``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from opcli.core.exceptions import ConfigurationError
from opcli.core.provision import provision_load, provision_prepare, provision_registry
from opcli.core.yaml_io import load_artifacts_build


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


_GENERATED_WITH_ROCKS = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  output:
  - arch: amd64
    file: ./rock_dir/myrock.rock
- name: otherrock
  rockcraft-yaml: other/rockcraft.yaml
  output:
  - arch: amd64
    image: ghcr.io/canonical/otherrock:abc
charms:
- name: mycharm
  charmcraft-yaml: charmcraft.yaml
  output:
  - arch: amd64
    path: ./mycharm_ubuntu-22.04-amd64.charm
    base: ubuntu@22.04
"""

_GENERATED_WITH_ROCKS_AND_RESOURCES = """\
version: 1
rocks:
- name: myrock
  rockcraft-yaml: rock_dir/rockcraft.yaml
  output:
  - arch: amd64
    file: ./rock_dir/myrock.rock
- name: otherrock
  rockcraft-yaml: other_dir/rockcraft.yaml
  output:
  - arch: amd64
    image: ghcr.io/canonical/otherrock:abc
charms:
- name: mycharm
  charmcraft-yaml: charmcraft.yaml
  output:
  - arch: amd64
    path: ./mycharm_ubuntu-22.04-amd64.charm
    base: ubuntu@22.04
  resources:
    myrock-image:
      type: oci-image
      rock: myrock
    other-res:
      type: oci-image
      rock: otherrock
"""


class TestProvisionPrepare:
    """Tests for provision_prepare()."""

    def test_runs_concierge(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge.yaml", "providers: {}\n")

        with patch("opcli.core.provision.run_command") as mock_run:
            provision_prepare(tmp_path)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["concierge", "prepare", "-c", str(tmp_path / "concierge.yaml")]

    def test_missing_concierge_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            provision_prepare(tmp_path)

    def test_custom_concierge_file(self, tmp_path: Path) -> None:
        _write(tmp_path / "concierge_juju4.yaml", "providers: {}\n")

        with patch("opcli.core.provision.run_command") as mock_run:
            provision_prepare(tmp_path, concierge_file="concierge_juju4.yaml")

        cmd = mock_run.call_args[0][0]
        assert any("concierge_juju4.yaml" in arg for arg in cmd)


class TestProvisionLoad:
    """Tests for provision_load()."""

    def test_missing_generated_returns_empty(self, tmp_path: Path) -> None:
        result = provision_load(tmp_path)
        assert result == []

    def test_pushes_local_rocks(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_ROCKS)

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path)

        # Only myrock has a file output; otherrock has image (CI) → skipped
        assert len(pushed) == 1
        assert "myrock" in pushed[0]
        assert "localhost:32000" in pushed[0]
        # Single skopeo call: direct oci-archive → registry
        assert mock_run.call_count == 1

    def test_custom_registry(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_ROCKS)

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path, registry="myregistry:5000")

        assert "myregistry:5000" in pushed[0]
        # Verify the single push command uses the custom registry
        push_cmd = mock_run.call_args_list[0][0][0]
        assert any("myregistry:5000" in arg for arg in push_cmd)

    def test_no_local_rocks_returns_empty(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "artifacts.build.yaml",
            "version: 1\n"
            "rocks:\n- name: r1\n  rockcraft-yaml: rd/rockcraft.yaml\n"
            "  output:\n  - arch: amd64\n    image: ghcr.io/r1:v1\n",
        )

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_empty_generated_returns_empty(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.build.yaml", "version: 1\n")

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_skopeo_commands_correct(self, tmp_path: Path) -> None:
        _write(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_ROCKS)

        with (
            patch("opcli.core.provision.run_command") as mock_run,
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            provision_load(tmp_path)

        # Single call: direct oci-archive → registry (no docker-daemon step)
        assert mock_run.call_count == 1
        cmd = mock_run.call_args_list[0][0][0]
        assert "rockcraft.skopeo" in cmd
        assert any("oci-archive:" in arg for arg in cmd)
        assert any("docker://" in arg for arg in cmd)
        assert not any("docker-daemon:" in arg for arg in cmd)
        assert "--dest-tls-verify=false" in cmd

    def test_updates_artifacts_build_with_image_ref(self, tmp_path: Path) -> None:
        """After pushing, rock.output.image is set and file is preserved."""
        _write(tmp_path / "artifacts.build.yaml", _GENERATED_WITH_ROCKS)

        with (
            patch("opcli.core.provision.run_command"),
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            provision_load(tmp_path)

        updated = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        myrock = next(r for r in updated.rocks if r.name == "myrock")
        assert myrock.output[0].image == "localhost:32000/myrock:amd64"
        assert myrock.output[0].file == "./rock_dir/myrock.rock"

    def test_updates_charm_resources_for_pushed_rock(self, tmp_path: Path) -> None:
        """provision_load pushes rocks; charm resources reference via rock: field."""
        _write(
            tmp_path / "artifacts.build.yaml",
            _GENERATED_WITH_ROCKS_AND_RESOURCES,
        )

        with (
            patch("opcli.core.provision.run_command"),
            patch("opcli.core.provision._is_port_open", return_value=True),
        ):
            provision_load(tmp_path)

        updated = load_artifacts_build(tmp_path / "artifacts.build.yaml")
        # Rock output.image is updated after push
        myrock = next(r for r in updated.rocks if r.name == "myrock")
        assert myrock.output[0].image == "localhost:32000/myrock:amd64"
        # Charm resources still only carry the rock reference, not a duplicated image
        charm = updated.charms[0]
        assert charm.resources is not None
        assert charm.resources["myrock-image"].rock == "myrock"
        assert charm.resources["other-res"].rock == "otherrock"

    def test_idempotent_skips_already_loaded_rock(self, tmp_path: Path) -> None:
        """Rock with image already set to the target ref is skipped."""
        _write(
            tmp_path / "artifacts.build.yaml",
            "version: 1\n"
            "rocks:\n- name: myrock\n  rockcraft-yaml: rock_dir/rockcraft.yaml\n"
            "  output:\n  - arch: amd64\n    file: ./rock_dir/myrock.rock\n"
            "    image: localhost:32000/myrock:amd64\n",
        )

        with patch("opcli.core.provision.run_command") as mock_run:
            pushed = provision_load(tmp_path)

        assert pushed == []
        mock_run.assert_not_called()

    def test_no_writeback_when_nothing_pushed(self, tmp_path: Path) -> None:
        """artifacts.build.yaml is not written when no rocks are pushed."""
        _write(tmp_path / "artifacts.build.yaml", "version: 1\n")
        mtime_before = (tmp_path / "artifacts.build.yaml").stat().st_mtime

        with patch("opcli.core.provision.run_command"):
            provision_load(tmp_path)

        mtime_after = (tmp_path / "artifacts.build.yaml").stat().st_mtime
        assert mtime_before == mtime_after


class TestProvisionRegistry:
    """Tests for provision_registry()."""

    def _which_for(self, *providers: str):
        """Return a shutil.which side_effect that resolves only *providers*."""

        def _which(name: str) -> str | None:
            if name in providers:
                return f"/usr/bin/{name}"
            return None

        return _which

    def test_skipped_when_no_k8s_on_path(self, tmp_path: Path) -> None:
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch("opcli.core.provision.shutil.which", return_value=None),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "skipped"
        mock_run.assert_not_called()

    def test_already_running_skips_deployment(self, tmp_path: Path) -> None:
        with (
            patch("opcli.core.provision._is_port_open", return_value=True),
            patch(
                "opcli.core.provision.shutil.which",
                side_effect=self._which_for("microk8s"),
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "already_running"
        mock_run.assert_not_called()

    def test_microk8s_detected_applies_manifest(self, tmp_path: Path) -> None:
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch(
                "opcli.core.provision.shutil.which",
                side_effect=self._which_for("microk8s"),
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        assert mock_run.call_count == 3  # noqa: PLR2004
        wait_prefix = ["sudo", "microk8s", "kubectl", "wait"]
        assert mock_run.call_args_list[0][0][0][:4] == wait_prefix
        apply_call = mock_run.call_args_list[1]
        assert apply_call[0][0] == ["sudo", "microk8s", "kubectl", "apply", "-f", "-"]
        assert apply_call[1]["stdin"]  # manifest content passed via stdin
        assert mock_run.call_args_list[2][0][0][:4] == [
            "sudo",
            "microk8s",
            "kubectl",
            "rollout",
        ]

    def test_k8s_detected_applies_manifest(self, tmp_path: Path) -> None:
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch(
                "opcli.core.provision.shutil.which", side_effect=self._which_for("k8s")
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        assert mock_run.call_count == 3  # noqa: PLR2004
        wait_cmd = mock_run.call_args_list[0][0][0]
        assert wait_cmd[:4] == ["sudo", "k8s", "kubectl", "wait"]
        assert "--for=condition=Ready" in wait_cmd
        apply_call = mock_run.call_args_list[1]
        assert apply_call[0][0] == ["sudo", "k8s", "kubectl", "apply", "-f", "-"]
        assert apply_call[1]["stdin"]  # manifest content passed via stdin
        rollout_cmd = mock_run.call_args_list[2][0][0]
        assert rollout_cmd[:4] == ["sudo", "k8s", "kubectl", "rollout"]
        assert "status" in rollout_cmd
        assert "deployment/registry" in rollout_cmd
        assert "container-registry" in rollout_cmd

    def test_kubectl_fallback_when_no_provider_binary(self, tmp_path: Path) -> None:
        """Falls back to standalone kubectl if neither microk8s nor k8s found."""
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch(
                "opcli.core.provision.shutil.which",
                side_effect=self._which_for("kubectl"),
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        assert mock_run.call_count == 3  # noqa: PLR2004
        wait_cmd = mock_run.call_args_list[0][0][0]
        assert wait_cmd[:3] == ["sudo", "kubectl", "wait"]

    def test_microk8s_preferred_over_k8s(self, tmp_path: Path) -> None:
        """When both microk8s and k8s are on PATH, microk8s wins."""
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch(
                "opcli.core.provision.shutil.which",
                side_effect=self._which_for("microk8s", "k8s"),
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "deployed"
        assert mock_run.call_args_list[0][0][0][:4] == [
            "sudo",
            "microk8s",
            "kubectl",
            "wait",
        ]

    def test_skipped_when_no_rocks(self, tmp_path: Path) -> None:
        """Skip if artifacts.build.yaml exists but has no rocks."""
        content = (
            "version: 1\nrocks: []\ncharms:\n"
            "- name: c\n  charmcraft-yaml: charmcraft.yaml\n"
            "  output:\n  - arch: amd64\n"
            "    path: ./c.charm\n    base: ubuntu@22.04\n"
        )
        _write(tmp_path / "artifacts.build.yaml", content)
        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch(
                "opcli.core.provision.shutil.which",
                side_effect=self._which_for("microk8s"),
            ),
            patch("opcli.core.provision.run_command") as mock_run,
        ):
            result = provision_registry(tmp_path)
        assert result == "skipped"
        mock_run.assert_not_called()

    def test_k8s_manifest_contains_registry_image(self, tmp_path: Path) -> None:
        """Verify the registry.yaml manifest references registry:2 on NodePort 32000."""
        applied_stdin: list[str] = []

        def capture_apply(cmd: list[str], **kwargs: object) -> object:
            if "apply" in cmd:
                stdin_content = kwargs.get("stdin")
                if isinstance(stdin_content, str):
                    applied_stdin.append(stdin_content)
            return None

        with (
            patch("opcli.core.provision._is_port_open", return_value=False),
            patch(
                "opcli.core.provision.shutil.which", side_effect=self._which_for("k8s")
            ),
            patch("opcli.core.provision.run_command", side_effect=capture_apply),
        ):
            provision_registry(tmp_path)

        assert applied_stdin, "apply was not called with stdin"
        content = applied_stdin[0]
        assert "registry:2" in content
        assert "nodePort: 32000" in content
        assert "container-registry" in content
