"""Synork Home — Assistant Provisioner (first-boot OOBE for Assistant Edition).

Handles the Assistant Edition's simplified first-boot flow:
  1. Probe hardware — confirm mic + speaker present
  2. Scan local network via mDNS for _synork-hub._tcp services
  3. If no Hubs: enter WiFi AP mode for network configuration, then rescan
  4. If 1 Hub found: auto-select, wait for user confirmation via app
  5. If multiple Hubs: user picks one via app
  6. Run pairing handshake with chosen Hub
  7. Persist Hub identity locally for reconnection on every boot

Much simpler than Hub Edition OOBE — no Synork account sign-in, no
household setup. Just WiFi config + Hub selection + pairing.

Created in Session 16: Assistant Edition Build Target & Hub-Assistant Pairing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser

logger = logging.getLogger("synork.assistant_provisioner")

_HUB_SERVICE_TYPE = "_synork-hub._tcp.local."
_SCAN_TIMEOUT = 15.0  # seconds to scan for Hubs
_SCAN_RETRY_INTERVAL = 30.0


@dataclass
class DiscoveredHub:
    """A Hub discovered via mDNS on the local network."""
    device_id: str
    hostname: str
    ip_address: str
    port: int
    paired: bool
    version: str
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def ws_url(self) -> str:
        """WebSocket URL for satellite connection."""
        return f"ws://{self.ip_address}:{self.port}"


class AssistantProvisioner:
    """Handles first-boot discovery and pairing for Assistant Edition.

    The provisioner runs once at first boot (before the AssistantClient
    can connect, since there's no Hub URL yet). After successful pairing,
    it writes the config and exits — subsequent boots use AssistantClient
    directly with the persisted config.
    """

    def __init__(
        self,
        satellite_id: str,
        capabilities: list[str],
        platform: str = "unknown",
        mock_mode: bool = False,
    ) -> None:
        self.satellite_id = satellite_id
        self.capabilities = capabilities
        self.platform = platform
        self._mock_mode = mock_mode

        # Discovered Hubs
        self._discovered_hubs: dict[str, DiscoveredHub] = {}

    async def run_oobe(self) -> Optional[DiscoveredHub]:
        """Run the Assistant OOBE flow.

        Returns the chosen Hub on success, None if OOBE was aborted.
        """
        logger.info("═══════════════════════════════════════════════════")
        logger.info("  Synork Home Assistant Edition — First Boot")
        logger.info("  Satellite ID: %s", self.satellite_id)
        logger.info("═══════════════════════════════════════════════════")

        # Step 1: Validate audio hardware
        if not await self._validate_audio():
            logger.error("Audio hardware not detected — Assistant Edition requires mic + speaker")
            return None

        # Step 2: Scan for Hubs
        hubs = await self._scan_for_hubs()

        if not hubs:
            logger.info("No Hubs found on local network")
            # TODO: Enter WiFi AP mode, let user configure WiFi, rescan
            # For now, retry scan periodically
            for attempt in range(10):
                logger.info(
                    "Retrying Hub scan in %.0fs (attempt %d/10)...",
                    _SCAN_RETRY_INTERVAL, attempt + 1,
                )
                await asyncio.sleep(_SCAN_RETRY_INTERVAL)
                hubs = await self._scan_for_hubs()
                if hubs:
                    break

        if not hubs:
            logger.error("No Hubs found after retries. Check network configuration.")
            return None

        # Step 3: Select Hub
        if len(hubs) == 1:
            chosen = hubs[0]
            logger.info(
                "Found 1 Hub: %s at %s:%d — auto-selecting",
                chosen.device_id, chosen.ip_address, chosen.port,
            )
        else:
            # Multiple Hubs — for now, pick the first one
            # In production, user selects via the Synork app
            chosen = hubs[0]
            logger.info(
                "Found %d Hubs — selecting first: %s at %s:%d",
                len(hubs), chosen.device_id, chosen.ip_address, chosen.port,
            )
            for hub in hubs:
                logger.info("  - %s at %s:%d", hub.device_id, hub.ip_address, hub.port)

        return chosen

    async def _validate_audio(self) -> bool:
        """Check that mic and speaker hardware are present."""
        if self._mock_mode:
            logger.info("Mock mode — assuming audio hardware present")
            return True

        has_mic = "microphone" in self.capabilities
        has_speaker = "audio_output" in self.capabilities

        if not has_mic:
            logger.error("No microphone detected — Assistant Edition requires a microphone")
        if not has_speaker:
            logger.error("No audio output detected — Assistant Edition requires a speaker")

        return has_mic and has_speaker

    async def _scan_for_hubs(self) -> list[DiscoveredHub]:
        """Scan the local network for _synork-hub._tcp mDNS services."""
        if self._mock_mode:
            return self._mock_hub_scan()

        logger.info("Scanning for Synork Hubs on local network...")
        discovered: dict[str, DiscoveredHub] = {}

        zc = Zeroconf(ip_version=IPVersion.V4Only)

        class HubListener:
            def add_service(self_, zc_ref: Zeroconf, type_: str, name: str) -> None:
                info = zc_ref.get_service_info(type_, name)
                if not info:
                    return

                addresses = info.parsed_scoped_addresses()
                if not addresses:
                    return

                props = {}
                if info.properties:
                    for k, v in info.properties.items():
                        key = k.decode() if isinstance(k, bytes) else k
                        val = v.decode() if isinstance(v, bytes) else str(v)
                        props[key] = val

                device_id = props.get("device_id", "unknown")
                hub = DiscoveredHub(
                    device_id=device_id,
                    hostname=info.server or name,
                    ip_address=addresses[0],
                    port=info.port or 8765,
                    paired=props.get("paired", "false") == "true",
                    version=props.get("version", "unknown"),
                    properties=props,
                )
                discovered[device_id] = hub
                logger.info("Discovered Hub: %s at %s:%d", device_id, hub.ip_address, hub.port)

            def remove_service(self_, zc_ref: Zeroconf, type_: str, name: str) -> None:
                pass

            def update_service(self_, zc_ref: Zeroconf, type_: str, name: str) -> None:
                pass

        browser = ServiceBrowser(zc, _HUB_SERVICE_TYPE, HubListener())

        # Wait for discovery
        await asyncio.sleep(_SCAN_TIMEOUT)

        browser.cancel()
        zc.close()

        self._discovered_hubs = discovered
        logger.info("Hub scan complete: found %d Hub(s)", len(discovered))
        return list(discovered.values())

    def _mock_hub_scan(self) -> list[DiscoveredHub]:
        """Return a mock Hub for testing."""
        mock_hub = DiscoveredHub(
            device_id="mock-hub-0001",
            hostname="synork-home-0001.local.",
            ip_address="127.0.0.1",
            port=8765,
            paired=True,
            version="0.1.0",
        )
        logger.info("Mock mode — returning mock Hub at %s:%d", mock_hub.ip_address, mock_hub.port)
        return [mock_hub]

    @staticmethod
    def generate_satellite_id() -> str:
        """Generate a unique satellite ID.

        Uses machine-id if available, otherwise generates a random UUID.
        """
        try:
            machine_id = open("/etc/machine-id").read().strip()
            # Derive a stable satellite ID from machine-id
            return hashlib.sha256(
                f"synork-satellite-{machine_id}".encode()
            ).hexdigest()[:16]
        except FileNotFoundError:
            return uuid.uuid4().hex[:16]
