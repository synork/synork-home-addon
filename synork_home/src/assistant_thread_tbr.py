"""Synork Home — Assistant Thread TBR Runner.

Runs on Assistant Edition devices that have a Thread radio detected.
Manages the local OTBR instance to join the Hub's Thread network as
a secondary Border Router.

Lifecycle:
  1. Receive ThreadCredentialsOffer from Hub via satellite WebSocket
  2. Configure local OTBR with the operational dataset
  3. OTBR joins the Thread network
  4. Periodically report status back to Hub via ThreadStatusUpdate

The actual Thread protocol work is done by OTBR — this module orchestrates
the configuration and monitoring.

Created in Session 18: Thread/Matter Mesh Extension via Assistants.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from shared.protocol import (
    ThreadCredentialsAck,
    ThreadCredentialsOffer,
    ThreadOperationalDataset,
    ThreadStatusUpdate,
    ThreadTBRStatus,
)

logger = logging.getLogger("synork.assistant_thread_tbr")

# OTBR REST API on the Assistant (same default port)
_OTBR_API_BASE = "http://localhost:8081"

# Status report interval (seconds)
_STATUS_REPORT_INTERVAL = 60.0

# Persistent credential storage
_CREDENTIALS_PATH = Path("/data/config/thread_credentials.json")


class AssistantThreadTBR:
    """Manages the Thread Border Router on an Assistant device.

    Lifecycle:
        start() → receives credentials → joins network → reports status → stop()

    The TBR only activates when:
      1. A Thread radio is detected on the device
      2. The Hub sends a ThreadCredentialsOffer after pairing
    """

    def __init__(
        self,
        satellite_id: str,
        assistant_client: Any,  # AssistantClient
        mock_mode: bool = False,
    ) -> None:
        self._satellite_id = satellite_id
        self._client = assistant_client
        self._mock_mode = mock_mode
        self._running = False

        # State
        self._dataset: Optional[ThreadOperationalDataset] = None
        self._status = ThreadTBRStatus.OFFLINE
        self._joined = False
        self._join_time: Optional[float] = None
        self._connected_devices: list[str] = []
        self._error_message: Optional[str] = None

        # Background tasks
        self._status_task: Optional[asyncio.Task] = None

    # -- Properties -------------------------------------------------------- #

    @property
    def status(self) -> ThreadTBRStatus:
        return self._status

    @property
    def joined(self) -> bool:
        return self._joined

    # -- Lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        """Start the TBR runner.

        Attempts to restore previously-received credentials. If credentials
        exist, immediately tries to join the Thread network. Otherwise, waits
        for a ThreadCredentialsOffer from the Hub.
        """
        self._running = True

        # Try to restore saved credentials
        saved = self._load_credentials()
        if saved:
            logger.info("Restored Thread credentials — attempting to rejoin network")
            self._dataset = saved
            await self._join_network()

        # Register handler for credential offers from Hub
        self._client.on("thread_credentials_offer", self._handle_credentials_offer)

        logger.info("Assistant Thread TBR runner started (mock=%s)", self._mock_mode)

    async def stop(self) -> None:
        """Stop the TBR runner."""
        self._running = False
        self._joined = False
        self._status = ThreadTBRStatus.OFFLINE

        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass

        logger.info("Assistant Thread TBR runner stopped")

    # -- Credential handling ----------------------------------------------- #

    async def _handle_credentials_offer(self, msg: Any) -> None:
        """Handle ThreadCredentialsOffer from the Hub."""
        if not isinstance(msg, ThreadCredentialsOffer):
            return

        logger.info(
            "Received Thread credentials: network=%s, channel=%d",
            msg.dataset.network_name,
            msg.dataset.channel,
        )

        # Validate we can run OTBR
        otbr_available = await self._check_otbr_available()

        # Send ack
        ack = ThreadCredentialsAck(
            satellite_id=self._satellite_id,
            accepted=otbr_available,
            reason=None if otbr_available else "otbr_not_available",
        )

        try:
            await self._client.send(ack)
        except Exception as exc:
            logger.error("Failed to send Thread credentials ack: %s", exc)
            return

        if not otbr_available:
            logger.warning("OTBR not available — cannot join Thread network")
            return

        # Store credentials and join
        self._dataset = msg.dataset
        self._save_credentials(msg.dataset)
        await self._join_network()

    async def _check_otbr_available(self) -> bool:
        """Check if the local OTBR instance is reachable."""
        if self._mock_mode:
            return True

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_OTBR_API_BASE}/node/state",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    # -- Network joining --------------------------------------------------- #

    async def _join_network(self) -> None:
        """Configure local OTBR with the dataset and join the Thread network."""
        if not self._dataset:
            return

        self._status = ThreadTBRStatus.JOINING
        logger.info("Joining Thread network '%s' on channel %d...",
                     self._dataset.network_name, self._dataset.channel)

        success = await self._apply_dataset(self._dataset)

        if success:
            self._status = ThreadTBRStatus.ONLINE
            self._joined = True
            self._join_time = asyncio.get_running_loop().time()
            logger.info("Successfully joined Thread network as secondary TBR")

            # Start periodic status reporting
            if self._status_task:
                self._status_task.cancel()
            self._status_task = asyncio.create_task(self._status_report_loop())
        else:
            self._status = ThreadTBRStatus.ERROR
            self._error_message = "Failed to apply dataset to OTBR"
            logger.error("Failed to join Thread network")

    async def _apply_dataset(self, dataset: ThreadOperationalDataset) -> bool:
        """Apply the operational dataset to the local OTBR via REST API."""
        if self._mock_mode:
            # Simulate a brief join delay
            await asyncio.sleep(0.5)
            logger.info("[MOCK] Applied Thread dataset to OTBR")
            return True

        try:
            import aiohttp

            # OTBR REST API expects the dataset in a specific format
            payload = {
                "NetworkName": dataset.network_name,
                "Channel": dataset.channel,
                "PanId": dataset.pan_id,
                "ExtPanId": dataset.extended_pan_id,
                "NetworkKey": dataset.network_key,
                "MeshLocalPrefix": dataset.mesh_local_prefix,
            }
            if dataset.security_policy is not None:
                payload["SecurityPolicy"] = dataset.security_policy

            async with aiohttp.ClientSession() as session:
                # Set the active dataset
                async with session.put(
                    f"{_OTBR_API_BASE}/node/dataset/active",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 201):
                        body = await resp.text()
                        logger.error("OTBR dataset PUT failed: %d %s", resp.status, body)
                        return False

                # Enable the Thread interface
                async with session.put(
                    f"{_OTBR_API_BASE}/node/state",
                    json={"State": "enable"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 201):
                        body = await resp.text()
                        logger.error("OTBR state enable failed: %d %s", resp.status, body)
                        return False

            return True
        except Exception as exc:
            logger.error("Failed to apply dataset to OTBR: %s", exc)
            return False

    # -- Status reporting -------------------------------------------------- #

    async def _status_report_loop(self) -> None:
        """Periodically send ThreadStatusUpdate to the Hub."""
        while self._running and self._joined:
            await asyncio.sleep(_STATUS_REPORT_INTERVAL)
            try:
                await self._send_status_update()
            except Exception as exc:
                logger.debug("Status report error: %s", exc)

    async def _send_status_update(self) -> None:
        """Send current TBR status to the Hub."""
        devices = await self._get_connected_devices()
        uptime = 0.0
        if self._join_time:
            uptime = asyncio.get_running_loop().time() - self._join_time

        update = ThreadStatusUpdate(
            satellite_id=self._satellite_id,
            tbr_status=self._status,
            connected_device_count=len(devices),
            connected_device_ids=devices,
            signal_dbm=await self._get_signal_strength(),
            uptime_seconds=uptime,
            error_message=self._error_message,
        )

        try:
            await self._client.send(update)
        except Exception as exc:
            logger.debug("Failed to send Thread status update: %s", exc)

    async def _get_connected_devices(self) -> list[str]:
        """Get Thread device EUI-64s visible to this TBR."""
        if self._mock_mode:
            return ["aabb00112233eeff"]

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_OTBR_API_BASE}/node/rloc",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    return [d.get("eui64", "") for d in data if d.get("eui64")]
        except Exception:
            return []

    async def _get_signal_strength(self) -> Optional[int]:
        """Get average signal strength from OTBR (mock returns -65 dBm)."""
        if self._mock_mode:
            return -65
        # Real implementation would query OTBR's diagnostic TLV data
        return None

    # -- Credential persistence -------------------------------------------- #

    def _save_credentials(self, dataset: ThreadOperationalDataset) -> None:
        """Persist Thread credentials locally (encrypted at rest in v2)."""
        try:
            _CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "network_name": dataset.network_name,
                "channel": dataset.channel,
                "pan_id": dataset.pan_id,
                "extended_pan_id": dataset.extended_pan_id,
                "network_key": dataset.network_key,
                "mesh_local_prefix": dataset.mesh_local_prefix,
                "security_policy": dataset.security_policy,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            _CREDENTIALS_PATH.write_text(json.dumps(data))
            logger.info("Thread credentials saved to %s", _CREDENTIALS_PATH)
        except Exception as exc:
            logger.error("Failed to save Thread credentials: %s", exc)

    def _load_credentials(self) -> Optional[ThreadOperationalDataset]:
        """Load previously-saved Thread credentials."""
        if not _CREDENTIALS_PATH.exists():
            return None

        try:
            data = json.loads(_CREDENTIALS_PATH.read_text())
            return ThreadOperationalDataset(
                network_name=data["network_name"],
                channel=data["channel"],
                pan_id=data["pan_id"],
                extended_pan_id=data["extended_pan_id"],
                network_key=data["network_key"],
                mesh_local_prefix=data["mesh_local_prefix"],
                security_policy=data.get("security_policy"),
            )
        except Exception as exc:
            logger.warning("Failed to load saved Thread credentials: %s", exc)
            return None
