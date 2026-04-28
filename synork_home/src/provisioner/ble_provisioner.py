"""Synork Home — BLE GATT Provisioning.

BLE GATT provisioning — v1.5 feature.
Allows mobile devices to provision the Synork Home device over Bluetooth
Low Energy using GATT characteristics for Wi-Fi credentials and device pairing.

Not implemented in v1. Stubbed with clear interface for v1.5.
"""

from __future__ import annotations

from typing import Any


class BLEProvisioner:
    """BLE GATT server for device provisioning.

    v1.5 feature — not implemented. All methods raise NotImplementedError.
    """

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id

    async def start_advertising(self) -> None:
        """Start BLE advertising for provisioning discovery."""
        raise NotImplementedError("BLE provisioning is a v1.5 feature — not yet implemented")

    async def stop_advertising(self) -> None:
        """Stop BLE advertising."""
        raise NotImplementedError("BLE provisioning is a v1.5 feature — not yet implemented")

    async def handle_connection(self, connection: Any) -> None:
        """Handle an incoming BLE GATT connection for provisioning."""
        raise NotImplementedError("BLE provisioning is a v1.5 feature — not yet implemented")
