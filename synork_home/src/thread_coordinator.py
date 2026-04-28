"""Synork Home — Thread Network Coordinator (Hub-side).

Manages the household's Thread network from the Hub:
  - Reads the Hub's OTBR operational dataset via HA's OTBR REST API
  - Maintains the registry of active Thread Border Routers (Hub + Assistants)
  - Distributes Thread credentials to newly-paired Thread-capable Assistants
  - Periodically polls TBR status from connected Assistants
  - Handles network re-keying by redistributing new credentials to all TBRs

The Hub is always the primary TBR. Assistants are secondary TBRs that receive
credentials from the Hub and join the same Thread network.

OTBR handles the actual Thread protocol — this module orchestrates
configuration distribution only.

Created in Session 18: Thread/Matter Mesh Extension via Assistants.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from shared.protocol import (
    ThreadCommissioningRequest,
    ThreadCredentialsAck,
    ThreadCredentialsOffer,
    ThreadOperationalDataset,
    ThreadStatusUpdate,
    ThreadTBRStatus,
)

logger = logging.getLogger("synork.thread_coordinator")

# OTBR REST API default (HA runs OTBR addon on this port)
_OTBR_API_BASE = "http://localhost:8081"

# How often to poll Assistant TBR status (seconds)
_TBR_POLL_INTERVAL = 60.0


class TBRRecord:
    """Registry entry for a Thread Border Router in the household."""

    def __init__(
        self,
        tbr_id: str,
        is_primary: bool = False,
        satellite_id: Optional[str] = None,
    ) -> None:
        self.tbr_id = tbr_id
        self.is_primary = is_primary
        self.satellite_id = satellite_id  # None for Hub
        self.status = ThreadTBRStatus.OFFLINE
        self.connected_device_count = 0
        self.connected_device_ids: list[str] = []
        self.signal_dbm: Optional[int] = None
        self.uptime_seconds = 0.0
        self.last_status_at: Optional[datetime] = None
        self.credentials_sent = False
        self.credentials_acked = False
        self.error_message: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tbr_id": self.tbr_id,
            "is_primary": self.is_primary,
            "satellite_id": self.satellite_id,
            "status": self.status.value,
            "connected_device_count": self.connected_device_count,
            "connected_device_ids": self.connected_device_ids,
            "signal_dbm": self.signal_dbm,
            "uptime_seconds": self.uptime_seconds,
            "last_status_at": self.last_status_at.isoformat() if self.last_status_at else None,
            "credentials_sent": self.credentials_sent,
            "credentials_acked": self.credentials_acked,
            "error_message": self.error_message,
        }


class OTBRClient:
    """Client for the OpenThread Border Router REST API.

    In mock mode, returns plausible fake data for development/testing.
    In real mode, makes HTTP requests to the local OTBR addon.
    """

    def __init__(self, base_url: str = _OTBR_API_BASE, mock_mode: bool = False) -> None:
        self._base_url = base_url
        self._mock_mode = mock_mode

    async def get_active_dataset(self) -> Optional[ThreadOperationalDataset]:
        """Read the active operational dataset from OTBR."""
        if self._mock_mode:
            return ThreadOperationalDataset(
                network_name="SynorkThread",
                channel=15,
                pan_id="face",
                extended_pan_id="dead00beef00cafe",
                network_key="00112233445566778899aabbccddeeff",
                mesh_local_prefix="fd11:22::/64",
                security_policy=672,
                dataset_timestamp=datetime.now(timezone.utc),
            )

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/node/dataset/active",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        logger.error("OTBR dataset request failed: %d", resp.status)
                        return None
                    data = await resp.json()
                    return ThreadOperationalDataset(
                        network_name=data.get("NetworkName", ""),
                        channel=data.get("Channel", 15),
                        pan_id=data.get("PanId", ""),
                        extended_pan_id=data.get("ExtPanId", ""),
                        network_key=data.get("NetworkKey", ""),
                        mesh_local_prefix=data.get("MeshLocalPrefix", ""),
                        security_policy=data.get("SecurityPolicy"),
                        dataset_timestamp=datetime.now(timezone.utc),
                    )
        except Exception as exc:
            logger.error("Failed to read OTBR dataset: %s", exc)
            return None

    async def get_node_state(self) -> str:
        """Get the OTBR node's current state (leader/router/child/disabled)."""
        if self._mock_mode:
            return "leader"

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/node/state",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return "unknown"
                    data = await resp.json()
                    return data.get("State", "unknown")
        except Exception as exc:
            logger.error("Failed to read OTBR state: %s", exc)
            return "unknown"

    async def get_connected_devices(self) -> list[dict[str, Any]]:
        """Get list of Thread devices visible to this TBR."""
        if self._mock_mode:
            return [
                {"eui64": "0011223344556677", "rloc16": "0x0400", "mode": "rdn"},
                {"eui64": "8899aabbccddeeff", "rloc16": "0x0800", "mode": "rn"},
            ]

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/node/rloc",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return []
                    return await resp.json()
        except Exception as exc:
            logger.error("Failed to read OTBR connected devices: %s", exc)
            return []


class ThreadCoordinator:
    """Coordinates the household's Thread network from the Hub.

    Lifecycle:
        initialize() → runs until stop()

    The coordinator maintains the TBR registry and distributes credentials
    to Thread-capable Assistants through the satellite broker.
    """

    def __init__(
        self,
        hub_device_id: str,
        satellite_broker: Any,  # SatelliteBroker
        mock_mode: bool = False,
    ) -> None:
        self._hub_device_id = hub_device_id
        self._broker = satellite_broker
        self._mock_mode = mock_mode
        self._otbr = OTBRClient(mock_mode=mock_mode)
        self._running = False

        # TBR registry: tbr_id → TBRRecord
        self._tbrs: dict[str, TBRRecord] = {}

        # Cached operational dataset
        self._dataset: Optional[ThreadOperationalDataset] = None
        self._network_established_at: Optional[datetime] = None

        # Background tasks
        self._poll_task: Optional[asyncio.Task] = None

        # Pending credential ack futures: satellite_id → Future
        self._pending_acks: dict[str, asyncio.Future[bool]] = {}

    # -- Properties -------------------------------------------------------- #

    @property
    def active_tbrs(self) -> list[TBRRecord]:
        """All TBRs with ONLINE status."""
        return [t for t in self._tbrs.values() if t.status == ThreadTBRStatus.ONLINE]

    @property
    def dataset(self) -> Optional[ThreadOperationalDataset]:
        return self._dataset

    @property
    def total_thread_devices(self) -> int:
        return sum(t.connected_device_count for t in self._tbrs.values())

    # -- Lifecycle --------------------------------------------------------- #

    async def initialize(self) -> None:
        """Initialize the coordinator and register the Hub as primary TBR."""
        self._running = True

        # Read Hub's OTBR dataset
        self._dataset = await self._otbr.get_active_dataset()
        if self._dataset:
            self._network_established_at = datetime.now(timezone.utc)
            logger.info(
                "Thread network active: name=%s, channel=%d, pan_id=%s",
                self._dataset.network_name,
                self._dataset.channel,
                self._dataset.pan_id,
            )
        else:
            logger.warning("No Thread network found on Hub OTBR — Thread extension disabled")
            return

        # Register Hub as primary TBR
        hub_tbr = TBRRecord(
            tbr_id=self._hub_device_id,
            is_primary=True,
        )
        hub_tbr.status = ThreadTBRStatus.ONLINE
        hub_tbr.credentials_sent = True
        hub_tbr.credentials_acked = True
        self._tbrs[self._hub_device_id] = hub_tbr

        # Update Hub's device count from OTBR
        devices = await self._otbr.get_connected_devices()
        hub_tbr.connected_device_count = len(devices)
        hub_tbr.connected_device_ids = [d.get("eui64", "") for d in devices]
        hub_tbr.last_status_at = datetime.now(timezone.utc)

        # Register message handlers on the satellite broker
        self._broker.on("thread_credentials_ack", self._handle_credentials_ack)
        self._broker.on("thread_status_update", self._handle_status_update)
        self._broker.on("thread_commissioning_request", self._handle_commissioning_request)

        # Start periodic status polling
        self._poll_task = asyncio.create_task(self._poll_loop())

        logger.info(
            "Thread coordinator initialized: %d TBR(s), %d device(s)",
            len(self._tbrs),
            self.total_thread_devices,
        )

    async def stop(self) -> None:
        """Stop the coordinator and clean up."""
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        # Cancel any pending ack futures
        for future in self._pending_acks.values():
            if not future.done():
                future.cancel()
        self._pending_acks.clear()

        logger.info("Thread coordinator stopped")

    # -- Credential distribution ------------------------------------------- #

    async def offer_credentials_to_satellite(
        self,
        satellite_id: str,
        timeout: float = 30.0,
    ) -> bool:
        """Send Thread credentials to a Thread-capable Assistant.

        Called by the satellite broker after pairing completes for a satellite
        that reported THREAD_RADIO capability.

        Returns True if the Assistant accepted the credentials.
        """
        if not self._dataset:
            logger.warning("Cannot offer Thread credentials — no dataset available")
            return False

        # Create the offer message
        offer = ThreadCredentialsOffer(
            dataset=self._dataset,
            hub_tbr_id=self._hub_device_id,
            network_established_at=self._network_established_at or datetime.now(timezone.utc),
        )

        # Register a future for the ack
        ack_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_acks[satellite_id] = ack_future

        # Send via satellite broker
        sent = await self._broker.send_to_satellite(satellite_id, offer)
        if not sent:
            logger.warning("Failed to send Thread credentials to %s — not connected", satellite_id)
            self._pending_acks.pop(satellite_id, None)
            return False

        logger.info("Thread credentials offered to %s", satellite_id)

        # Register TBR record (pending)
        tbr = TBRRecord(
            tbr_id=satellite_id,
            is_primary=False,
            satellite_id=satellite_id,
        )
        tbr.status = ThreadTBRStatus.JOINING
        tbr.credentials_sent = True
        self._tbrs[satellite_id] = tbr

        # Wait for ack
        try:
            accepted = await asyncio.wait_for(ack_future, timeout=timeout)
            tbr.credentials_acked = accepted
            if accepted:
                logger.info("Thread credentials accepted by %s", satellite_id)
            else:
                logger.info("Thread credentials rejected by %s", satellite_id)
                tbr.status = ThreadTBRStatus.ERROR
            return accepted
        except asyncio.TimeoutError:
            logger.warning("Thread credentials ack timed out for %s", satellite_id)
            tbr.status = ThreadTBRStatus.ERROR
            tbr.error_message = "Ack timeout"
            return False
        finally:
            self._pending_acks.pop(satellite_id, None)

    # -- Message handlers -------------------------------------------------- #

    async def _handle_credentials_ack(self, satellite_id: str, msg: Any) -> None:
        """Handle ThreadCredentialsAck from an Assistant."""
        if not isinstance(msg, ThreadCredentialsAck):
            return

        future = self._pending_acks.get(msg.satellite_id)
        if future and not future.done():
            future.set_result(msg.accepted)

        if not msg.accepted:
            logger.info(
                "Satellite %s declined Thread credentials: %s",
                msg.satellite_id,
                msg.reason,
            )

    async def _handle_status_update(self, satellite_id: str, msg: Any) -> None:
        """Handle ThreadStatusUpdate from an Assistant TBR."""
        if not isinstance(msg, ThreadStatusUpdate):
            return

        tbr = self._tbrs.get(msg.satellite_id)
        if not tbr:
            logger.warning("Status update from unknown TBR: %s", msg.satellite_id)
            return

        tbr.status = msg.tbr_status
        tbr.connected_device_count = msg.connected_device_count
        tbr.connected_device_ids = msg.connected_device_ids
        tbr.signal_dbm = msg.signal_dbm
        tbr.uptime_seconds = msg.uptime_seconds
        tbr.error_message = msg.error_message
        tbr.last_status_at = datetime.now(timezone.utc)

        logger.debug(
            "TBR status from %s: %s, %d devices",
            msg.satellite_id, msg.tbr_status.value, msg.connected_device_count,
        )

    async def _handle_commissioning_request(self, satellite_id: str, msg: Any) -> None:
        """Handle ThreadCommissioningRequest — route to preferred TBR."""
        if not isinstance(msg, ThreadCommissioningRequest):
            return

        preferred = msg.preferred_tbr_id
        if preferred and preferred != self._hub_device_id:
            # Forward to the preferred satellite TBR
            sent = await self._broker.send_to_satellite(preferred, msg)
            if sent:
                logger.info("Forwarded commissioning request to TBR %s", preferred)
                return

        # Hub handles it (or preferred TBR unavailable)
        logger.info(
            "Hub handling Thread commissioning for device %s",
            msg.target_device_eui64 or "unknown",
        )
        # Actual OTBR commissioning would happen here via the OTBR API
        # Deferred to when real hardware testing is possible

    # -- Status polling ---------------------------------------------------- #

    async def _poll_loop(self) -> None:
        """Periodically update the Hub's own TBR status."""
        while self._running:
            await asyncio.sleep(_TBR_POLL_INTERVAL)
            try:
                await self._update_hub_status()
            except Exception as exc:
                logger.debug("Hub TBR status poll error: %s", exc)

    async def _update_hub_status(self) -> None:
        """Refresh Hub's TBR status from local OTBR."""
        hub_tbr = self._tbrs.get(self._hub_device_id)
        if not hub_tbr:
            return

        state = await self._otbr.get_node_state()
        if state in ("leader", "router"):
            hub_tbr.status = ThreadTBRStatus.ONLINE
        elif state == "disabled":
            hub_tbr.status = ThreadTBRStatus.OFFLINE
        else:
            hub_tbr.status = ThreadTBRStatus.ERROR
            hub_tbr.error_message = f"Unexpected OTBR state: {state}"

        devices = await self._otbr.get_connected_devices()
        hub_tbr.connected_device_count = len(devices)
        hub_tbr.connected_device_ids = [d.get("eui64", "") for d in devices]
        hub_tbr.last_status_at = datetime.now(timezone.utc)

    # -- Re-keying --------------------------------------------------------- #

    async def recommission_network(self) -> bool:
        """Re-key the Thread network and redistribute credentials to all TBRs.

        This is a destructive operation — all Thread devices will need to be
        re-commissioned after the network key changes. Use only when the
        household suspects credential compromise.

        Returns True if all active TBRs received new credentials.
        """
        logger.warning("Thread network recommissioning requested")

        # Refresh dataset from OTBR (which should have the new key)
        new_dataset = await self._otbr.get_active_dataset()
        if not new_dataset:
            logger.error("Cannot recommission — failed to read new OTBR dataset")
            return False

        self._dataset = new_dataset

        # Distribute to all secondary TBRs
        success = True
        for tbr in self._tbrs.values():
            if tbr.is_primary:
                continue
            if not tbr.satellite_id:
                continue

            accepted = await self.offer_credentials_to_satellite(tbr.satellite_id)
            if not accepted:
                logger.error("Failed to distribute new credentials to %s", tbr.satellite_id)
                success = False

        return success

    # -- Query methods ----------------------------------------------------- #

    def get_network_status(self) -> dict[str, Any]:
        """Get the overall Thread network status for REST/frontend consumption."""
        return {
            "network_active": self._dataset is not None,
            "network_name": self._dataset.network_name if self._dataset else None,
            "channel": self._dataset.channel if self._dataset else None,
            "pan_id": self._dataset.pan_id if self._dataset else None,
            "mesh_local_prefix": self._dataset.mesh_local_prefix if self._dataset else None,
            "network_established_at": (
                self._network_established_at.isoformat()
                if self._network_established_at else None
            ),
            "tbr_count": len(self._tbrs),
            "active_tbr_count": len(self.active_tbrs),
            "total_thread_devices": self.total_thread_devices,
        }

    def get_tbr_list(self) -> list[dict[str, Any]]:
        """Get list of all TBRs for REST/frontend consumption."""
        return [tbr.to_dict() for tbr in self._tbrs.values()]

    def is_satellite_tbr(self, satellite_id: str) -> bool:
        """Check if a satellite is registered as a Thread TBR."""
        tbr = self._tbrs.get(satellite_id)
        return tbr is not None and tbr.status == ThreadTBRStatus.ONLINE

    def remove_tbr(self, satellite_id: str) -> None:
        """Remove a TBR when a satellite disconnects or is unpaired."""
        if satellite_id in self._tbrs and not self._tbrs[satellite_id].is_primary:
            del self._tbrs[satellite_id]
            logger.info("Removed TBR record for satellite %s", satellite_id)
