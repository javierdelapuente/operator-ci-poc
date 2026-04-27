#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Trivial charm used to validate the opcli CI build pipeline.

Accepts a k8s-rock-image OCI resource but does not use it as a workload
container — this keeps the test simple while still exercising the full
resource flag assembly in opcli pytest expand.
"""

import ops


class K8sCharm(ops.CharmBase):
    """A minimal charm that immediately goes active."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.framework.observe(self.on.install, self._on_install)

    def _on_install(self, event: ops.InstallEvent) -> None:
        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":
    ops.main(K8sCharm)
