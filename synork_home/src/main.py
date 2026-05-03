"""Synork Home Add-on — Main Entrypoint.

Initializes all subsystems in the correct order:
  1. Load config from CLI args (passed by run.sh from HA add-on options)
  2. Set up shared types import path
  3. Detect network → start mDNS / WiFi AP if needed
  4. Probe hardware → resolve personas → activate
  5. Connect to HA bridge
  6. Connect relay client
  7. Start assistant pipeline (if satellite persona is active)
  8. Install frontend theme/patcher
  9. Run until SIGTERM

All subsystems run as asyncio tasks on a single event loop.
Clean shutdown handles SIGTERM from HA Supervisor.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Set up shared types import path before importing anything else.
# The shared protocol/schema types live at Other Apps/synork-home/shared/
# and need to be importable as `from shared.protocol import ...`
_ADDON_DIR = Path(__file__).resolve().parent  # /app/
_SYNORK_HOME_DIR = _ADDON_DIR.parent.parent  # Other Apps/synork-home/
# In Docker, shared/ is copied to /app/shared/ by the Dockerfile
# In development, it's at ../../shared/ relative to src/
for candidate in [_ADDON_DIR, _SYNORK_HOME_DIR]:
    shared_path = candidate / "shared"
    if shared_path.exists():
        parent = str(candidate)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        break

from relay_client import RelayClient
from ha_bridge import HABridge
from provisioner.network_detect import NetworkDetector
from provisioner.mdns_advertise import MDNSAdvertiser
from provisioner.wifi_ap_fallback import WiFiAPFallback
from persona.hardware_probe import HardwareProbe
from persona.persona_resolver import PersonaResolver
from persona.personas.hub import HubPersona
from persona.personas.satellite import SatellitePersona
from persona.personas.speaker import SpeakerPersona
from persona.personas.composite import CompositePersona
from assistant.wake_word import WakeWordDetector
from assistant.vad import VADProcessor
from assistant.stt import STTEngine
from assistant.tts_router import TTSRouter
from assistant.turn_taking import TurnTakingManager
from frontend.install_frontend import FrontendInstaller
from _version import ADDON_VERSION

# Hub-side satellite management (Session 16)
from satellite_broker import SatelliteBroker
from hub_pairing import HubPairingService

# Assistant-side Hub connection (Session 16)
from assistant_client import AssistantClient
from assistant_provisioner import AssistantProvisioner

# Thread mesh extension (Session 18)
from thread_coordinator import ThreadCoordinator
from assistant_thread_tbr import AssistantThreadTBR

# Setup wizard for addon-on-existing-HA install path (Session 20)
from wizard import (
    HubConfig,
    WizardServer,
    ensure_device_id,
    load_hub_config,
)
from wizard.notifications import dismiss_setup_notification, notify_setup_needed

from shared.protocol import (
    AssistantInvoke,
    AssistantMode,
    AssistantResponse,
    DeviceRegistrySnapshot,
    EntityStatePayload,
    EntityStateQuery,
    EntityStateUpdate,
    ServiceCallRequest,
    HaRegistryMutationRequest,
    HomeKitPairingRequest,
    VoiceQueryFromAssistant,
    VoiceResponseToAssistant,
)
from shared.persona_schema import (
    HardwareCapability,
    HardwareProbeResult,
    Persona,
    PersonaConfig,
    PersonaResolution,
)

logger = logging.getLogger("synork.addon")


class AddonConfig:
    """Parsed addon configuration.

    Sources, in priority order:
      1. /data/synork/hub_config.json sidecar (Hub-edition pairing values
         persisted by the setup wizard — survives restarts; takes priority
         because the wizard's writes are newer than HA's options.json).
      2. CLI args from run.sh (sourced from HA addon options).
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.mode: str = args.mode  # "hub" or "assistant"
        self.relay_url: str = args.relay_url
        self.relay_api_url: str = args.relay_api_url
        self.device_id: str = args.device_id
        self.device_secret: str = args.device_secret
        self.language: str = args.language
        self.log_level: str = args.log_level
        self.mock_hardware: bool = args.mock_hardware.lower() == "true"
        self.local_stt: bool = args.local_stt.lower() == "true"
        self.cloud_stt: bool = args.cloud_stt.lower() == "true"
        self.wake_word: str = args.wake_word
        self.tts_provider: str = args.tts_provider
        # cartesia_api_key intentionally NOT in config — cloud TTS goes through
        # the Synork relay (Bearer-authenticated by the device session_token);
        # the relay holds the upstream provider key. Only voice_id is
        # user-selectable.
        self.cartesia_voice_id: str = getattr(args, "cartesia_voice_id", "") or ""
        self.assistant_pipeline: bool = args.assistant_pipeline.lower() == "true"
        self.frontend_patcher: bool = args.frontend_patcher.lower() == "true"
        self.satellite_port: int = int(args.satellite_port)

        # Wizard-managed sidecar overrides CLI args for hub identity + relay URLs.
        # Only applies in Hub mode — Assistant mode's pairing lives at
        # /data/synork/assistant_config.json and is owned by AssistantClient.
        self.hub_config: HubConfig = load_hub_config() or HubConfig()
        if self.is_hub:
            if self.hub_config.device_id:
                self.device_id = self.hub_config.device_id
            if self.hub_config.device_secret:
                self.device_secret = self.hub_config.device_secret
            if self.hub_config.relay_ws_url:
                self.relay_url = self.hub_config.relay_ws_url
            if self.hub_config.relay_api_url:
                self.relay_api_url = self.hub_config.relay_api_url

    @property
    def is_hub(self) -> bool:
        return self.mode == "hub"

    @property
    def is_assistant(self) -> bool:
        return self.mode == "assistant"


class SynorkAddon:
    """Main addon orchestrator — manages all subsystems.

    Lifecycle:
      start() → runs until SIGTERM → stop()

    All subsystems are started as asyncio tasks. The addon maintains
    references to all subsystems for clean shutdown and health reporting.
    """

    def __init__(self, config: AddonConfig) -> None:
        self.config = config
        self._running = False
        self._start_time = 0.0
        self._shutdown_event = asyncio.Event()

        # Subsystem instances (initialized during start)
        self._relay_client: Optional[RelayClient] = None
        self._ha_bridge: Optional[HABridge] = None
        self._network_detector: Optional[NetworkDetector] = None
        self._mdns: Optional[MDNSAdvertiser] = None
        self._wifi_ap: Optional[WiFiAPFallback] = None
        self._hardware_probe: Optional[HardwareProbe] = None
        self._persona_resolver: Optional[PersonaResolver] = None
        self._persona_resolution: Optional[PersonaResolution] = None
        self._persona_impl: Optional[Any] = None  # HubPersona | SatellitePersona | SpeakerPersona | CompositePersona
        self._pipeline: Optional[TurnTakingManager] = None
        self._frontend_installer: Optional[FrontendInstaller] = None

        # Hub-side satellite management (Session 16)
        self._satellite_broker: Optional[SatelliteBroker] = None
        self._hub_pairing: Optional[HubPairingService] = None

        # Assistant-side Hub connection (Session 16)
        self._assistant_client: Optional[AssistantClient] = None
        self._assistant_provisioner: Optional[AssistantProvisioner] = None

        # Thread mesh extension (Session 18)
        self._thread_coordinator: Optional[ThreadCoordinator] = None
        self._assistant_thread_tbr: Optional[AssistantThreadTBR] = None

        # Setup wizard for addon-on-existing-HA install (Session 20).
        # Hub mode only — Assistant edition uses the AssistantProvisioner.
        self._wizard: Optional[WizardServer] = None

        # Background tasks
        self._relay_task: Optional[asyncio.Task] = None
        self._assistant_task: Optional[asyncio.Task] = None

        # Tracks which voice slugs (stt / wake_word / tts) HA's Wyoming
        # add-ons are currently serving. When all three are up the local
        # in-process voice pipeline (which is a TODO stub) is skipped and
        # voice flows through HA Assist instead. Keyed by service name.
        self._wyoming_voice_provisioned: dict[str, bool] = {}

    async def start(self) -> None:
        """Start the addon — the main lifecycle method."""
        self._running = True
        self._start_time = time.monotonic()

        # Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_handler, sig)

        logger.info("═══════════════════════════════════════════════════")
        logger.info("  Synork Home Add-on v0.1.0 starting")
        logger.info("  Mode: %s", self.config.mode.upper())
        logger.info("  Device: %s", self.config.device_id)
        logger.info("  Language: %s", self.config.language)
        logger.info("  Mock hardware: %s", self.config.mock_hardware)
        if self.config.is_hub:
            logger.info("  Relay: %s", self.config.relay_url)
            logger.info("  Satellite port: %d", self.config.satellite_port)
        logger.info("═══════════════════════════════════════════════════")

        try:
            if self.config.is_hub:
                await self._start_hub_mode()
            else:
                await self._start_assistant_mode()

            logger.info("All subsystems started — addon is operational (%s mode)", self.config.mode)

            # Auto-update poller (channel-based git pull, see updater.py)
            try:
                import updater  # type: ignore[import-not-found]

                self._updater_task = asyncio.create_task(updater.run())
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-updater failed to start: %r", exc)

            # Wait for shutdown signal
            await self._shutdown_event.wait()

        except Exception as exc:
            logger.error("Addon startup failed: %s", exc, exc_info=True)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Gracefully shut down all subsystems in reverse order."""
        logger.info("Shutting down...")
        self._running = False

        # Stop assistant pipeline
        if self._pipeline:
            try:
                await self._pipeline.stop()
            except Exception as exc:
                logger.warning("Pipeline shutdown error: %s", exc)

        # Stop Thread coordinator (Hub mode, Session 18)
        if self._thread_coordinator:
            try:
                await self._thread_coordinator.stop()
            except Exception as exc:
                logger.warning("Thread coordinator shutdown error: %s", exc)

        # Stop Thread TBR (Assistant mode, Session 18)
        if self._assistant_thread_tbr:
            try:
                await self._assistant_thread_tbr.stop()
            except Exception as exc:
                logger.warning("Thread TBR shutdown error: %s", exc)

        # Stop satellite broker (Hub mode)
        if self._satellite_broker:
            try:
                await self._satellite_broker.stop()
            except Exception as exc:
                logger.warning("Satellite broker shutdown error: %s", exc)

        # Close hub pairing DB (Hub mode)
        if self._hub_pairing:
            try:
                await self._hub_pairing.close()
            except Exception as exc:
                logger.warning("Hub pairing shutdown error: %s", exc)

        # Disconnect assistant client (Assistant mode)
        if self._assistant_client:
            try:
                await self._assistant_client.disconnect()
            except Exception as exc:
                logger.warning("Assistant client disconnect error: %s", exc)

        if self._assistant_task:
            self._assistant_task.cancel()
            try:
                await self._assistant_task
            except asyncio.CancelledError:
                pass

        # Disconnect relay
        if self._relay_client:
            try:
                await self._relay_client.disconnect()
            except Exception as exc:
                logger.warning("Relay disconnect error: %s", exc)

        if self._relay_task:
            self._relay_task.cancel()
            try:
                await self._relay_task
            except asyncio.CancelledError:
                pass

        # Disconnect HA bridge
        if self._ha_bridge:
            try:
                await self._ha_bridge.disconnect()
            except Exception as exc:
                logger.warning("HA bridge disconnect error: %s", exc)

        # Stop persona
        if self._persona_impl:
            try:
                await self._persona_impl.stop()
            except Exception as exc:
                logger.warning("Persona shutdown error: %s", exc)

        # Stop provisioning
        if self._mdns:
            try:
                await self._mdns.stop()
            except Exception as exc:
                logger.warning("mDNS shutdown error: %s", exc)

        if self._wifi_ap:
            try:
                await self._wifi_ap.stop_ap()
            except Exception as exc:
                logger.warning("WiFi AP shutdown error: %s", exc)

        # Stop wizard server last — keeping it alive through other phases'
        # teardown means a user who hits the panel during shutdown still
        # gets a coherent response instead of a connection-refused.
        if self._wizard:
            try:
                await self._wizard.stop()
            except Exception as exc:
                logger.warning("Wizard shutdown error: %s", exc)

        uptime = time.monotonic() - self._start_time
        logger.info("Addon shutdown complete (uptime: %.0fs)", uptime)

    def _signal_handler(self, sig: signal.Signals) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        logger.info("Received signal %s — initiating shutdown", sig.name)
        self._shutdown_event.set()

    # ── Mode startup paths (Session 16) ────────────────────────────────

    async def _start_hub_mode(self) -> None:
        """Hub Edition startup: full stack + satellite broker."""
        # Phase 0 (Session 20): Setup wizard on ingress port 8099.
        # Starts before everything else so the user can reach the wizard
        # immediately after the addon container is running, even before
        # network detection / persona resolution complete.
        await self._setup_wizard()

        # Phase 1: Network detection and provisioning
        await self._setup_network()

        # Phase 2: Hardware detection and persona activation
        await self._setup_personas()

        # Phase 3: Connect to HA bridge
        await self._setup_ha_bridge()

        # Phase 4: Connect to Synork relay
        await self._setup_relay()

        # Phase 5: Start assistant pipeline (if satellite persona)
        await self._setup_assistant_pipeline()

        # Phase 6: Install frontend
        await self._setup_frontend()

        # Phase 7 (Session 16): Start satellite broker for Assistant connections
        await self._setup_satellite_broker()

        # Phase 8 (Session 18): Start Thread coordinator for mesh extension
        await self._setup_thread_coordinator()

    async def _start_assistant_mode(self) -> None:
        """Assistant Edition startup: audio pipeline + Hub connection."""
        # Phase 1: Network detection (mDNS scan, no relay)
        await self._setup_network()

        # Phase 2: Hardware detection (must have mic + speaker)
        await self._setup_personas()

        # Phase 3: HA bridge (Assistant still runs on HA OS, but limited use)
        # Skip for now — Assistants don't control HA directly

        # Phase 4: Connect to Hub (replaces relay connection)
        await self._setup_hub_connection()

        # Phase 5: Start assistant pipeline (voice is the primary function)
        await self._setup_assistant_pipeline()

        # Phase 6 (Session 18): Start Thread TBR if Thread radio detected
        await self._setup_assistant_thread_tbr()

        # Skip Phase 7: No frontend installation on Assistant Edition

    # ── Phase 0 (Hub): Setup wizard (Session 20) ───────────────────────

    async def _setup_wizard(self) -> None:
        """Bring up the setup wizard on the ingress port (8099).

        Three responsibilities:
          1. Ensure the addon has a stable device_id (generate + persist if
             missing) so the wizard has something to pair with.
          2. Bind the aiohttp server so HA's "Open Web UI" link works
             whether the addon is paired or not.
          3. Post a persistent_notification when unpaired so users actually
             find the wizard instead of staring at a silent addon.
        """
        # Make sure we have a device_id even on first ever boot (sidecar absent).
        ensured = ensure_device_id(self.config.hub_config)
        self.config.hub_config = ensured
        if not self.config.device_id:
            self.config.device_id = ensured.device_id
        logger.info("Phase 0: Setup wizard (device_id=%s, paired=%s)",
                    self.config.device_id, ensured.is_paired)

        self._wizard = WizardServer(
            relay_api_url=self.config.relay_api_url,
            get_config=lambda: self.config.hub_config,
            on_paired=self._on_wizard_paired,
            language=self.config.language,
            host="0.0.0.0",
            port=8099,
        )
        try:
            await self._wizard.start()
        except OSError as exc:
            logger.error("wizard server failed to bind 8099 (%s); "
                         "addon will continue but Web UI will be broken", exc)
            self._wizard = None
            return

        # First-start visibility: persistent notification if not paired.
        # Not awaited blocking — Supervisor reachability shouldn't gate startup.
        if not ensured.is_paired:
            asyncio.create_task(notify_setup_needed())

    async def _on_wizard_paired(self, payload: dict) -> None:
        """Wizard hand-off: addon now has device_secret and household.

        Fired from inside the wizard's pair handler. The sidecar file is
        already persisted at this point (the wizard wrote it before
        invoking us); our job is to catch the running addon up to the
        new credentials so the relay client connects without a restart.
        """
        # Update in-memory config so future code paths see the new identity.
        self.config.device_id = payload["device_id"]
        self.config.device_secret = payload["device_secret"]
        self.config.relay_url = payload["relay_websocket_url"]
        self.config.relay_api_url = payload["relay_api_url"]
        self.config.hub_config = HubConfig(
            device_id=payload["device_id"],
            device_secret=payload["device_secret"],
            household_id=payload["household_id"],
            household_name=payload.get("household_name", ""),
            owner_user_id=payload.get("owner_user_id", ""),
            paired_at=payload.get("paired_at", ""),
            relay_ws_url=payload["relay_websocket_url"],
            relay_api_url=payload["relay_api_url"],
        )

        await dismiss_setup_notification()

        # Spin up the relay client live; user gets functional Synork
        # without restarting the addon.
        if self._relay_client is None and self.config.is_hub:
            try:
                await self._setup_relay()
            except Exception as exc:
                logger.warning(
                    "relay startup after pairing failed (%s); will retry on next addon restart",
                    exc,
                )

        # If satellite broker is running with empty household_id (Phase 7
        # ran before we were paired), refresh its bound household so newly
        # paired Assistants get the right HubWelcome payload. The broker
        # already has a service handle; updating in place avoids a restart.
        if self._hub_pairing:
            self._hub_pairing.household_id = payload["household_id"]
            self._hub_pairing.household_name = payload.get("household_name", "")

    # ── Phase 7 (Hub): Satellite broker ────────────────────────────────

    async def _setup_satellite_broker(self) -> None:
        """Start the satellite WebSocket server for incoming Assistant connections."""
        logger.info("Phase 7: Starting satellite broker")

        self._hub_pairing = HubPairingService(
            hub_device_id=self.config.device_id,
            household_id=self._relay_client.household_id if self._relay_client else "",
            household_name=self._relay_client._household_name if self._relay_client else "",
        )
        await self._hub_pairing.initialize()

        self._satellite_broker = SatelliteBroker(hub_pairing=self._hub_pairing)

        # Register voice query handler: route from satellite through relay to backend
        self._satellite_broker.on(
            "voice_query_from_assistant",
            self._handle_satellite_voice_query,
        )

        await self._satellite_broker.start(
            host="0.0.0.0",
            port=self.config.satellite_port,
        )

    # ── Phase 8 (Hub): Thread coordinator ────────────────────────────

    async def _setup_thread_coordinator(self) -> None:
        """Start the Thread coordinator for mesh extension via Assistants."""
        if not self._satellite_broker:
            return

        # Check if Hub has a Thread network (OTBR running)
        has_thread = False
        if self._persona_resolution:
            has_thread = HardwareCapability.THREAD_RADIO in self._persona_resolution.probe_result.capabilities

        if not has_thread:
            logger.info("Phase 8: No Thread radio on Hub — Thread coordinator skipped")
            return

        logger.info("Phase 8: Starting Thread coordinator")

        self._thread_coordinator = ThreadCoordinator(
            hub_device_id=self.config.device_id,
            satellite_broker=self._satellite_broker,
            mock_mode=self.config.mock_hardware,
        )
        await self._thread_coordinator.initialize()

        # Link coordinator to satellite broker for post-pairing credential distribution
        self._satellite_broker.set_thread_coordinator(self._thread_coordinator)

    # ── Phase 6 (Assistant): Thread TBR ───────────────────────────

    async def _setup_assistant_thread_tbr(self) -> None:
        """Start the Thread TBR runner if the Assistant has a Thread radio."""
        if not self._assistant_client:
            return

        has_thread = False
        if self._persona_resolution:
            has_thread = HardwareCapability.THREAD_RADIO in self._persona_resolution.probe_result.capabilities

        if not has_thread:
            logger.info("Phase 6 (Thread): No Thread radio detected — TBR skipped")
            return

        logger.info("Phase 6 (Thread): Starting Thread TBR runner")

        satellite_id = self._assistant_client._satellite_id if hasattr(self._assistant_client, '_satellite_id') else ""

        self._assistant_thread_tbr = AssistantThreadTBR(
            satellite_id=satellite_id,
            assistant_client=self._assistant_client,
            mock_mode=self.config.mock_hardware,
        )
        await self._assistant_thread_tbr.start()

    async def _handle_satellite_voice_query(self, satellite_id: str, msg: Any) -> None:
        """Route a voice query from a satellite through the relay to the Synork backend."""
        if not isinstance(msg, VoiceQueryFromAssistant):
            return
        if not self._relay_client or not self._relay_client.connected:
            logger.warning("Cannot route satellite query — relay not connected")
            # Send error response to satellite
            if self._satellite_broker:
                error_resp = VoiceResponseToAssistant(
                    satellite_id=satellite_id,
                    conversation_id=msg.conversation_id or "",
                    chunk_index=0,
                    text_delta="Synork Hub unreachable — please try again later.",
                    is_final=True,
                    language=msg.language,
                )
                await self._satellite_broker.send_to_satellite(satellite_id, error_resp)
            return

        # Forward as AssistantInvoke to the relay (with room context)
        invoke = AssistantInvoke(
            asking_user_id="household_default",  # Session 17 adds room-based user inference
            household_id=self._relay_client.household_id or "",
            query=msg.query,
            language=msg.language,
            mode=AssistantMode.VOICE,
            conversation_id=msg.conversation_id,
        )

        # Set up response routing: when relay responds, forward to satellite
        async def _on_relay_response(resp: Any) -> None:
            if isinstance(resp, AssistantResponse):
                voice_resp = VoiceResponseToAssistant(
                    satellite_id=satellite_id,
                    conversation_id=resp.conversation_id,
                    chunk_index=0,
                    text_delta=resp.text,
                    is_final=True,
                    language=resp.language,
                    audio_url=resp.audio_url,
                    tool_calls=resp.tool_calls,
                )
                if self._satellite_broker:
                    await self._satellite_broker.send_to_satellite(satellite_id, voice_resp)

        # Temporarily register handler for this query
        self._relay_client.on("assistant_response", _on_relay_response)

        try:
            await self._relay_client.send(invoke)
        except Exception as exc:
            logger.error("Failed to forward satellite query to relay: %s", exc)

    # ── Phase 4 (Assistant): Hub connection ────────────────────────────

    async def _setup_hub_connection(self) -> None:
        """Connect to a Hub on the local network (Assistant mode only)."""
        logger.info("Phase 4 (Assistant): Connecting to Hub")

        # Check for persisted pairing config
        saved_config = AssistantClient.load_config()

        if saved_config and saved_config.get("device_secret"):
            # Already paired — connect directly
            logger.info("Found saved pairing config — connecting to Hub %s", saved_config.get("paired_hub_id"))
            self._assistant_client = AssistantClient(
                satellite_id=saved_config["satellite_id"],
                device_secret=saved_config["device_secret"],
                hub_url=saved_config["paired_hub_address"],
                capabilities=[c.value for c in self._persona_resolution.probe_result.capabilities] if self._persona_resolution else [],
                platform=self._persona_resolution.probe_result.platform.value if self._persona_resolution else "unknown",
                room=saved_config.get("room"),
            )
        else:
            # First boot — run provisioner to discover and pair with a Hub
            satellite_id = AssistantProvisioner.generate_satellite_id()
            capabilities = [c.value for c in self._persona_resolution.probe_result.capabilities] if self._persona_resolution else []
            platform = self._persona_resolution.probe_result.platform.value if self._persona_resolution else "unknown"

            self._assistant_provisioner = AssistantProvisioner(
                satellite_id=satellite_id,
                capabilities=capabilities,
                platform=platform,
                mock_mode=self.config.mock_hardware,
            )

            hub = await self._assistant_provisioner.run_oobe()
            if not hub:
                logger.error("Assistant OOBE failed — no Hub found or pairing failed")
                return

            # Create client for the discovered Hub (will pair during connect)
            self._assistant_client = AssistantClient(
                satellite_id=satellite_id,
                device_secret="",  # Will be set by Hub during pairing
                hub_url=hub.ws_url,
                capabilities=capabilities,
                platform=platform,
            )

        if not self._assistant_client:
            return

        # Register voice response handler
        self._assistant_client.on(
            "voice_response_to_assistant",
            self._handle_hub_voice_response,
        )

        # Start connection in background (auto-reconnects)
        self._assistant_task = asyncio.create_task(self._assistant_client.connect())

    async def _handle_hub_voice_response(self, msg: Any) -> None:
        """Handle a voice response from the Hub — play via TTS."""
        if not isinstance(msg, VoiceResponseToAssistant):
            return
        if self._pipeline and msg.text_delta:
            logger.info("Voice response from Hub: %s", msg.text_delta[:100])
            # The pipeline's TTS will handle playback
            # TODO: Wire into TurnTakingManager.play_response() in Session 17

    # ── Phase 1: Network ────────────────────────────────────────────────

    async def _setup_network(self) -> None:
        """Detect network and start provisioning services."""
        logger.info("Phase 1: Network detection")

        self._network_detector = NetworkDetector(mock_mode=self.config.mock_hardware)
        network = await self._network_detector.detect()

        logger.info(
            "Network: type=%s, interface=%s, ip=%s, internet=%s",
            network.interface_type,
            network.interface_name,
            network.ip_address,
            network.has_internet,
        )

        # Start mDNS if we have an IP
        # Hub Edition: advertise both _synork._tcp and _synork-hub._tcp
        # Assistant Edition: don't advertise (it scans for Hubs instead)
        if network.ip_address and self.config.is_hub:
            self._mdns = MDNSAdvertiser(
                device_id=self.config.device_id,
                addon_version=ADDON_VERSION,
                edition=self.config.mode,
                satellite_port=self.config.satellite_port,
            )
            paired = bool(self.config.device_secret)
            await self._mdns.start(network.ip_address, paired=paired)

        # If no network, start WiFi AP for provisioning
        if network.interface_type == "none":
            logger.info("No network detected — starting WiFi AP for provisioning")
            self._wifi_ap = WiFiAPFallback(self.config.device_id)
            await self._wifi_ap.start_ap()
            await self._wifi_ap.serve_captive_portal()

            # User submitted WiFi credentials
            ssid, passphrase = await self._wifi_ap.get_wifi_credentials()
            if ssid:
                success = await self._wifi_ap.apply_wifi_config()
                if success:
                    await self._wifi_ap.stop_ap()
                    # Re-detect network after WiFi connection
                    network = await self._network_detector.detect()
                    if network.ip_address and self._mdns:
                        await self._mdns.start(network.ip_address, paired=bool(self.config.device_secret))

    # ── Phase 2: Personas ───────────────────────────────────────────────

    async def _setup_personas(self) -> None:
        """Probe hardware and activate personas."""
        logger.info("Phase 2: Hardware detection and persona activation")

        self._hardware_probe = HardwareProbe(
            mock_mode=self.config.mock_hardware,
            edition=self.config.mode,
        )
        probe_result = await self._hardware_probe.probe()

        self._persona_resolver = PersonaResolver()
        self._persona_resolution = self._persona_resolver.resolve(probe_result)

        resolution = self._persona_resolution
        logger.info(
            "Personas: effective=%s, active=%s",
            resolution.effective_persona.value,
            [p.value for p in resolution.active_personas],
        )

        # Get persona configs
        configs = self._persona_resolver.get_configs(resolution)

        # Activate the appropriate persona implementation
        if resolution.effective_persona == Persona.COMPOSITE:
            self._persona_impl = CompositePersona()
            await self._persona_impl.start(configs)
        else:
            # Single persona
            config = configs[0] if configs else PersonaConfig(
                persona=resolution.effective_persona,
                services=[],
                priority=0,
            )

            if resolution.effective_persona == Persona.HUB:
                self._persona_impl = HubPersona()
            elif resolution.effective_persona == Persona.SATELLITE:
                self._persona_impl = SatellitePersona()
            elif resolution.effective_persona == Persona.SPEAKER:
                self._persona_impl = SpeakerPersona()

            if self._persona_impl:
                await self._persona_impl.start(config)

    # ── Phase 3: HA Bridge ──────────────────────────────────────────────

    async def _setup_ha_bridge(self) -> None:
        """Connect to HA's WebSocket API."""
        logger.info("Phase 3: Connecting to Home Assistant")

        self._ha_bridge = HABridge()

        try:
            await self._ha_bridge.connect()
        except Exception as exc:
            logger.error("Failed to connect to HA: %s", exc)
            logger.info("HA bridge will retry on next interaction")
            return

        # Register state change forwarding to relay
        self._ha_bridge.on_state_change(self._forward_state_to_relay)
        # Forward HA device/entity registry snapshots so Synork knows the
        # device→entity mapping (devices are first-class objects in the UI).
        self._ha_bridge.on_registry_change(self._forward_registry_to_relay)

        # Auto-provision HA integrations the active personas are responsible
        # for. Hub: ZHA / Z-Wave JS / Matter / OTBR. Satellite: Wyoming
        # whisper + openwakeword. Speaker: Wyoming Piper TTS. Each persona
        # walks any HA config flow it owns and installs the matching
        # Supervisor add-ons end-to-end so a fresh HA install needs zero
        # manual setup before voice + radio devices work.
        await self._auto_provision_persona_integrations()

    async def _auto_provision_persona_integrations(self) -> None:
        """Run ``auto_provision_ha`` on every active persona that defines it."""
        if not self._ha_bridge or not self._persona_impl:
            return

        candidates: list[Any] = []
        getter = getattr(self._persona_impl, "get_sub_persona", None)
        if callable(getter):
            for persona_type in (Persona.HUB, Persona.SATELLITE, Persona.SPEAKER):
                try:
                    sub = getter(persona_type)
                except Exception:
                    sub = None
                if sub is not None:
                    candidates.append(sub)
        else:
            candidates.append(self._persona_impl)

        for impl in candidates:
            provisioner = getattr(impl, "auto_provision_ha", None)
            if not callable(provisioner):
                continue
            try:
                result = await provisioner(self._ha_bridge)
            except Exception as exc:
                logger.warning(
                    "%s auto-provisioning failed: %s",
                    type(impl).__name__, exc,
                )
                continue
            # Voice personas (satellite/speaker) return {service_name: bool}
            # for the Wyoming add-ons they manage. Track successes so the
            # local mock pipeline can be skipped when HA owns voice.
            if isinstance(result, dict):
                for svc, ok in result.items():
                    if svc in ("wake_word", "stt", "tts") and ok:
                        self._wyoming_voice_provisioned[svc] = True

    async def _forward_state_to_relay(self, entities: list[EntityStatePayload]) -> None:
        """Forward HA state changes to the Synork relay."""
        if self._relay_client and self._relay_client.connected:
            try:
                await self._relay_client.send_entity_states(entities)
            except Exception as exc:
                logger.debug("Failed to forward state to relay: %s", exc)

    async def _forward_registry_to_relay(self, snapshot: DeviceRegistrySnapshot) -> None:
        """Forward HA device/entity registry snapshots to the Synork relay."""
        if self._relay_client and self._relay_client.connected:
            try:
                await self._relay_client.send(snapshot)
            except Exception as exc:
                logger.debug("Failed to forward registry snapshot to relay: %s", exc)

    # ── Phase 4: Relay ──────────────────────────────────────────────────

    async def _setup_relay(self) -> None:
        """Connect to the Synork relay backend."""
        logger.info("Phase 4: Connecting to Synork relay")

        if not self.config.device_id or not self.config.device_secret:
            logger.warning("No device credentials — relay connection skipped (device not paired)")
            return

        # Determine HA version
        ha_version = "unknown"
        if self._ha_bridge and self._ha_bridge.connected:
            try:
                ha_version = await self._ha_bridge.get_ha_version()
            except Exception:
                pass

        self._relay_client = RelayClient(
            relay_url=self.config.relay_url,
            device_id=self.config.device_id,
            device_secret=self.config.device_secret,
            addon_version=ADDON_VERSION,
            ha_version=ha_version,
        )

        # Set persona info for the AddonHello message
        if self._persona_resolution:
            self._relay_client.active_personas = [
                p.value for p in self._persona_resolution.active_personas
            ]
            self._relay_client.capabilities = [
                c.value for c in self._persona_resolution.probe_result.capabilities
            ]

        # Tell the relay which HA integrations are loaded so the backend can
        # advertise pairing capabilities (Zigbee, Z-Wave, Matter, Thread...).
        if self._ha_bridge:
            try:
                services = await self._ha_bridge.get_services()
                self._relay_client.loaded_integrations = sorted(services.keys())
                logger.info(
                    "Reporting %d loaded HA integrations to relay",
                    len(self._relay_client.loaded_integrations),
                )
            except Exception as exc:
                logger.warning("Could not enumerate HA services: %s", exc)
                self._relay_client.loaded_integrations = []

        if self._ha_bridge:
            self._relay_client.entity_count = self._ha_bridge.entity_count

        # Register message handlers
        self._relay_client.on("service_call_request", self._handle_service_call)
        self._relay_client.on("entity_state_query", self._handle_entity_query)
        self._relay_client.on("assistant_invoke", self._handle_assistant_invoke)
        self._relay_client.on("ha_registry_mutation_request", self._handle_registry_mutation)
        self._relay_client.on("homekit_pairing_request", self._handle_homekit_pairing)
        # Arlo "thinking" filler: backend pushes an AssistantStreamChunk while
        # the deep model is generating, so we can speak immediately.
        self._relay_client.on("assistant_stream_chunk", self._handle_assistant_stream_chunk)

        # Push the HA device/entity registry snapshot every time the relay
        # (re)connects, so the backend's notion of "devices" is always fresh.
        self._relay_client.on_connected(self._push_initial_registry_snapshot)

        # Start relay connection in background (auto-reconnects)
        self._relay_task = asyncio.create_task(self._relay_client.connect())

        # Block briefly so the relay session_token is available before Phase 5
        # constructs cloud TTS clients (which Bearer-auth against the relay).
        # Non-fatal: if the relay is unreachable, voice degrades to local
        # providers and picks up the token on the next reconnect.
        if await self._relay_client.wait_for_auth(timeout=10.0):
            logger.info("Phase 4: relay authenticated — session token ready for cloud calls")
        else:
            logger.warning(
                "Phase 4: relay auth not complete after 10s — "
                "voice will start with local fallbacks until the session token arrives"
            )

    async def _push_initial_registry_snapshot(self) -> None:
        """Build and send a fresh HA device/entity registry snapshot."""
        if not self._ha_bridge or not self._ha_bridge.connected:
            return
        try:
            snapshot = await self._ha_bridge.build_registry_snapshot()
            await self._relay_client.send(snapshot)
            logger.info(
                "Pushed initial registry snapshot: %d devices, %d orphan entities",
                len(snapshot.devices), len(snapshot.orphan_entity_ids),
            )
        except Exception as exc:
            logger.warning("Failed to push initial registry snapshot: %s", exc)

    async def _handle_service_call(self, msg: Any) -> None:
        """Handle a ServiceCallRequest from the relay."""
        if not isinstance(msg, ServiceCallRequest):
            return
        if not self._ha_bridge:
            return

        response = await self._ha_bridge.handle_service_call_request(msg)
        if self._relay_client and self._relay_client.connected:
            await self._relay_client.send(response)

    async def _handle_registry_mutation(self, msg: Any) -> None:
        """Handle a HaRegistryMutationRequest from the relay (area/device/entity edits)."""
        if not isinstance(msg, HaRegistryMutationRequest):
            return
        if not self._ha_bridge:
            return

        response = await self._ha_bridge.handle_registry_mutation(msg)
        if self._relay_client and self._relay_client.connected:
            await self._relay_client.send(response)

    async def _handle_homekit_pairing(self, msg: Any) -> None:
        """Drive a step of the HA homekit_controller config flow."""
        if not isinstance(msg, HomeKitPairingRequest):
            return
        if not self._ha_bridge:
            return

        response = await self._ha_bridge.handle_homekit_pairing_request(msg)
        if self._relay_client and self._relay_client.connected:
            await self._relay_client.send(response)

    async def _handle_entity_query(self, msg: Any) -> None:
        """Handle an EntityStateQuery from the relay."""
        if not isinstance(msg, EntityStateQuery):
            return
        if not self._ha_bridge:
            return

        entities = await self._ha_bridge.handle_entity_state_query(msg)
        if entities and self._relay_client and self._relay_client.connected:
            await self._relay_client.send_entity_states(entities)

    async def _handle_assistant_invoke(self, msg: Any) -> None:
        """Handle an AssistantInvoke from the relay (cloud → device query)."""
        if not isinstance(msg, AssistantInvoke):
            return

        # For now, forward back to relay as a text query
        # The cloud assistant brain handles the actual response generation
        logger.info("Received assistant invoke from relay: %s", msg.query[:100])

    async def _handle_assistant_stream_chunk(self, msg: Any) -> None:
        """Speak an in-flight stream chunk from the cloud brain.

        Used by Arlo's COMPLEX path: the backend pushes a short "thinking"
        filler so the user hears something while the deep model runs.
        Best-effort — silently drops if the pipeline isn't ready.
        """
        try:
            # Late-import to avoid loading protocol module when assistant disabled.
            from shared.protocol import AssistantStreamChunk  # type: ignore
        except Exception:
            return
        if not isinstance(msg, AssistantStreamChunk):
            return
        if not self._pipeline:
            return
        text = (msg.text_delta or "").strip()
        if not text:
            return
        try:
            audio = await self._pipeline._tts.synthesize(  # type: ignore[attr-defined]
                text, language=msg.language or self.config.language,
            )
            await self._pipeline._play_audio(audio)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.debug("Filler chunk playback failed (non-fatal): %s", exc)

    # ── Phase 5: Assistant Pipeline ─────────────────────────────────────

    async def _setup_assistant_pipeline(self) -> None:
        """Start the voice assistant pipeline if a microphone is available.

        Voice is the *primary* function for the Satellite persona, but a
        Hub with an attached mic should also offer Assist locally. The
        gate is therefore the MICROPHONE capability, not the persona.
        """
        if not self.config.assistant_pipeline:
            logger.info("Phase 5: Assistant pipeline disabled in config")
            return

        # Check the actual hardware probe result for a real microphone.
        # Fall back to the persona check only if we somehow have no probe
        # result (defensive — probe runs in Phase 2).
        has_mic = False
        mic_devices: list[str] = []
        if self._persona_resolution and self._persona_resolution.probe_result:
            for cap in self._persona_resolution.probe_result.capabilities:
                if cap.value == "microphone":
                    has_mic = True
                    break
            mic_devices = [
                f"{d.device_name} ({d.device_path})"
                for d in self._persona_resolution.probe_result.devices
                if d.capability.value == "microphone"
            ]
        elif self._persona_resolution:
            # No probe but persona resolution exists \u2014 fall back to persona.
            has_mic = Persona.SATELLITE in self._persona_resolution.active_personas

        if not has_mic:
            logger.info(
                "Phase 5: No microphone detected \u2014 assistant pipeline skipped "
                "(check that /dev/snd is mapped through and contains a "
                "pcmC*D*c capture node)"
            )
            return

        # If HA's Wyoming add-ons (whisper + openWakeWord, optionally piper)
        # were auto-provisioned successfully, voice flows through HA Assist
        # via the wyoming integration's mDNS-advertised pipeline. Starting
        # the local in-process pipeline on top would (a) duplicate audio
        # capture against the same /dev/snd device and (b) currently fall
        # back to mock mode anyway because the local STT/wake-word backends
        # are TODO stubs. Skip it and log clearly where voice lives.
        ww_ready = self._wyoming_voice_provisioned.get("wake_word", False)
        stt_ready = self._wyoming_voice_provisioned.get("stt", False)
        if ww_ready and stt_ready:
            logger.info(
                "Phase 5: Wyoming voice add-ons running (wake_word, stt"
                "%s) \u2014 voice handled by HA Assist; local pipeline skipped",
                ", tts" if self._wyoming_voice_provisioned.get("tts") else "",
            )
            return

        logger.info(
            "Phase 5: Starting assistant pipeline (mic: %s)",
            ", ".join(mic_devices) or "detected",
        )

        mock = self.config.mock_hardware

        try:
            # Initialize pipeline components
            wake_word = WakeWordDetector(
                model_name=self.config.wake_word,
                mock_mode=mock,
            )

            vad = VADProcessor(
                sensitivity=0.5,
                mock_mode=mock,
            )

            stt_backend = "local" if self.config.local_stt else "remote"
            stt = STTEngine(
                backend=stt_backend,
                language=self.config.language,
                relay_url=self.config.relay_api_url,
                mock_mode=mock,
            )
            await stt.load()

            tts = TTSRouter(
                provider=self.config.tts_provider,
                language=self.config.language,
                relay_url=self.config.relay_api_url,
                mock_mode=mock,
                # Synthesis is proxied through the Synork relay; the device
                # authenticates with its short-lived WS session_token so no
                # upstream provider API key ever touches the addon.
                session_token_getter=(
                    lambda: self._relay_client.session_token
                    if self._relay_client else None
                ),
                cartesia_voice_id=self.config.cartesia_voice_id,
            )
            await tts.initialize()

            # Create pipeline
            self._pipeline = TurnTakingManager(
                wake_word=wake_word,
                vad=vad,
                stt=stt,
                tts=tts,
                language=self.config.language,
                mock_mode=mock,
            )

            # Set up invocation callback (routes differently in hub vs assistant mode)
            if self.config.is_assistant:
                self._pipeline.set_invoke_callback(self._invoke_assistant_via_hub)
            else:
                self._pipeline.set_invoke_callback(self._invoke_assistant_via_relay)

            await self._pipeline.start()
            logger.info("Phase 5: Assistant pipeline started successfully")
        except Exception as exc:
            logger.error(
                "Phase 5: Assistant pipeline failed to start: %r",
                exc,
                exc_info=True,
            )
            # Surface a hint about the most common cause
            if "alsa" in str(exc).lower() or "pcm" in str(exc).lower():
                logger.error(
                    "Phase 5 hint: ALSA error \u2014 verify the addon has "
                    "/dev/snd mapped through and the user has audio group access"
                )
            self._pipeline = None

    async def _invoke_assistant_via_hub(
        self,
        query: str,
        language: str,
        user_id: str,
    ) -> Optional[str]:
        """Send a voice query to the Hub (Assistant mode)."""
        if not self._assistant_client or not self._assistant_client.connected:
            logger.warning("Cannot invoke assistant — Hub not connected")
            return None

        # Set up response future
        response_future: asyncio.Future[Optional[str]] = asyncio.get_running_loop().create_future()

        async def _on_response(resp: Any) -> None:
            if isinstance(resp, VoiceResponseToAssistant) and resp.is_final and not response_future.done():
                response_future.set_result(resp.text_delta)

        self._assistant_client.on("voice_response_to_assistant", _on_response)

        try:
            await self._assistant_client.send_voice_query(query, language)
            return await asyncio.wait_for(response_future, timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("Hub voice response timed out")
            return None
        finally:
            self._assistant_client.on("voice_response_to_assistant", lambda m: asyncio.sleep(0))

    async def _invoke_assistant_via_relay(
        self,
        query: str,
        language: str,
        user_id: str,
    ) -> Optional[str]:
        """Send a voice query to the Synork relay and return the response text (Hub mode)."""
        if not self._relay_client or not self._relay_client.connected:
            logger.warning("Cannot invoke assistant — relay not connected")
            return None

        msg = AssistantInvoke(
            asking_user_id=user_id,
            household_id=self._relay_client.household_id or "",
            query=query,
            language=language,
            mode=AssistantMode.VOICE,
        )

        # Send and wait for response
        response_future: asyncio.Future[Optional[str]] = asyncio.get_running_loop().create_future()

        async def _on_response(resp: Any) -> None:
            if isinstance(resp, AssistantResponse) and not response_future.done():
                response_future.set_result(resp.text)

        # Temporarily register handler
        self._relay_client.on("assistant_response", _on_response)

        try:
            await self._relay_client.send(msg)
            return await asyncio.wait_for(response_future, timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("Assistant response timed out")
            return None
        finally:
            # Restore default handler (or remove)
            self._relay_client.on("assistant_response", lambda m: asyncio.sleep(0))

    # ── Phase 6: Frontend ───────────────────────────────────────────────

    async def _setup_frontend(self) -> None:
        """Install Synork frontend theme and patcher."""
        logger.info("Phase 6: Installing frontend assets")

        self._frontend_installer = FrontendInstaller(
            enable_patcher=self.config.frontend_patcher,
        )

        try:
            await self._frontend_installer.install()
        except Exception as exc:
            logger.warning("Frontend installation error (non-fatal): %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# CLI entrypoint
# ═══════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synork Home Add-on")
    parser.add_argument("--mode", default="hub", choices=["hub", "assistant"], help="Edition mode: hub or assistant")
    parser.add_argument("--relay-url", default="wss://api.synork.dev/api/home/ws", help="Synork relay WebSocket URL")
    parser.add_argument("--relay-api-url", default="https://api.synork.dev", help="Synork relay REST API URL")
    parser.add_argument("--device-id", required=True, help="Device identifier")
    parser.add_argument("--device-secret", default="", help="Device secret for relay auth")
    parser.add_argument("--language", default="hu", help="Default language")
    parser.add_argument("--log-level", default="info", help="Logging level")
    parser.add_argument("--mock-hardware", default="false", help="Enable mock hardware mode")
    parser.add_argument("--local-stt", default="true", help="Enable local STT")
    parser.add_argument("--cloud-stt", default="false", help="Enable cloud STT")
    parser.add_argument("--wake-word", default="hey_synork", help="Wake word model name")
    parser.add_argument("--tts-provider", default="auto", help="TTS provider (auto|cartesia|piper|elevenlabs)")
    parser.add_argument("--cartesia-voice-id", default="", help="Cartesia voice ID (user-selectable; api key comes from relay)")
    parser.add_argument("--assistant-pipeline", default="true", help="Enable assistant pipeline")
    parser.add_argument("--frontend-patcher", default="true", help="Enable frontend patcher")
    parser.add_argument("--satellite-port", default="8765", help="Port for satellite WebSocket connections (Hub mode)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Configure logging
    level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = AddonConfig(args)
    addon = SynorkAddon(config)
    asyncio.run(addon.start())


if __name__ == "__main__":
    main()
