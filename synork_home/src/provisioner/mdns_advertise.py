"""Synork Home — mDNS Advertising.

Advertises the Synork Home device on the local network via mDNS.

Hub Edition advertises TWO service types:
  - `_synork._tcp` — for the Synork mobile app to discover the device
  - `_synork-hub._tcp` — for Assistant satellites to find their Hub

Assistant Edition does NOT advertise (it scans for Hubs instead).

Uses the zeroconf library. The advertisement runs continuously so the
Synork mobile app and satellite devices can discover the device at any time.

The service TXT record includes:
  - device_id: unique device identifier
  - version: addon version
  - paired: whether the device has been paired to a household
  - edition: hub or assistant
  - satellite_port: port for satellite WebSocket connections (Hub only)
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logger = logging.getLogger("synork.provisioner.mdns")

SERVICE_TYPE = "_synork._tcp.local."
HUB_SERVICE_TYPE = "_synork-hub._tcp.local."


class MDNSAdvertiser:
    """Advertises this device via mDNS for local discovery.

    The service name is derived from the device_id to create a stable,
    recognizable name on the local network: synork-home-{last4}.

    Hub Edition registers both _synork._tcp (for app discovery) and
    _synork-hub._tcp (for satellite discovery). The hub service includes
    the satellite WebSocket port in its TXT record.
    """

    def __init__(
        self,
        device_id: str,
        port: int = 8099,
        addon_version: str = "0.1.0",
        edition: str = "hub",
        satellite_port: int = 8765,
    ) -> None:
        self.device_id = device_id
        self.port = port
        self.addon_version = addon_version
        self.edition = edition
        self.satellite_port = satellite_port
        self._zeroconf: Optional[AsyncZeroconf] = None
        self._service_info: Optional[ServiceInfo] = None
        self._hub_service_info: Optional[ServiceInfo] = None
        self._running = False

    @property
    def hostname(self) -> str:
        """The mDNS hostname for this device."""
        suffix = self.device_id[-4:] if len(self.device_id) >= 4 else self.device_id
        return f"synork-home-{suffix}"

    async def start(self, ip_address: str, paired: bool = False) -> None:
        """Begin mDNS service advertisement.

        Args:
            ip_address: The IP address to advertise.
            paired: Whether this device has been paired to a household.
        """
        self._running = True

        try:
            ip_bytes = socket.inet_aton(ip_address)
        except OSError:
            logger.error("Invalid IP address for mDNS: %s", ip_address)
            return

        properties = {
            "device_id": self.device_id,
            "version": self.addon_version,
            "paired": str(paired).lower(),
            "edition": self.edition,
        }

        service_name = f"{self.hostname}.{SERVICE_TYPE}"
        self._service_info = ServiceInfo(
            type_=SERVICE_TYPE,
            name=service_name,
            addresses=[ip_bytes],
            port=self.port,
            properties=properties,
            server=f"{self.hostname}.local.",
        )

        self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await self._zeroconf.async_register_service(self._service_info)

        logger.info(
            "mDNS advertising: %s at %s:%d (paired=%s, edition=%s)",
            service_name,
            ip_address,
            self.port,
            paired,
            self.edition,
        )

        # Hub Edition: also advertise _synork-hub._tcp for satellite discovery
        if self.edition == "hub":
            hub_properties = {
                "device_id": self.device_id,
                "version": self.addon_version,
                "paired": str(paired).lower(),
                "satellite_port": str(self.satellite_port),
            }

            hub_service_name = f"{self.hostname}.{HUB_SERVICE_TYPE}"
            self._hub_service_info = ServiceInfo(
                type_=HUB_SERVICE_TYPE,
                name=hub_service_name,
                addresses=[ip_bytes],
                port=self.satellite_port,
                properties=hub_properties,
                server=f"{self.hostname}.local.",
            )

            await self._zeroconf.async_register_service(self._hub_service_info)
            logger.info(
                "mDNS hub advertising: %s at %s:%d (for satellite discovery)",
                hub_service_name,
                ip_address,
                self.satellite_port,
            )

    async def update_paired_status(self, paired: bool) -> None:
        """Update the paired status in the mDNS TXT record."""
        if not self._service_info or not self._zeroconf:
            return

        self._service_info.properties[b"paired"] = str(paired).lower().encode()
        await self._zeroconf.async_update_service(self._service_info)

        if self._hub_service_info:
            self._hub_service_info.properties[b"paired"] = str(paired).lower().encode()
            await self._zeroconf.async_update_service(self._hub_service_info)

        logger.info("mDNS paired status updated: %s", paired)

    async def stop(self) -> None:
        """Stop mDNS advertisement and clean up."""
        self._running = False

        if self._zeroconf and self._hub_service_info:
            try:
                await self._zeroconf.async_unregister_service(self._hub_service_info)
            except Exception as exc:
                logger.warning("Error unregistering hub mDNS service: %s", exc)

        if self._zeroconf and self._service_info:
            try:
                await self._zeroconf.async_unregister_service(self._service_info)
            except Exception as exc:
                logger.warning("Error unregistering mDNS service: %s", exc)

        if self._zeroconf:
            try:
                await self._zeroconf.async_close()
            except Exception as exc:
                logger.warning("Error closing zeroconf: %s", exc)

        self._zeroconf = None
        self._service_info = None
        self._hub_service_info = None
        logger.info("mDNS advertisement stopped")
