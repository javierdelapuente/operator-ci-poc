# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration test: k8s-charm deploys with OCI resource and reaches active/idle.

Although named 'k8s-charm', this charm deploys to a standard machine (LXD)
model for simplicity.  The OCI image resource is provided to exercise the full
'opcli pytest expand' flag assembly (--k8s-rock-image=...) without requiring a
Kubernetes substrate.
"""

from __future__ import annotations

import pytest
from pytest_operator.plugin import OpsTest


@pytest.mark.asyncio
async def test_k8s_charm_active(
    ops_test: OpsTest,
    k8s_charm_file: str,
    k8s_rock_image: str,
) -> None:
    """Deploy k8s-charm with its OCI resource and assert active/idle within 5 minutes."""
    app = await ops_test.model.deploy(
        k8s_charm_file,
        application_name="k8s-charm",
        resources={"k8s-rock-image": k8s_rock_image},
    )
    await ops_test.model.wait_for_idle(
        apps=[app.name],
        status="active",
        timeout=300,
    )
    assert app.units[0].workload_status == "active"
