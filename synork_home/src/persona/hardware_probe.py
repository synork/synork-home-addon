"""Synork Home — Hardware Probe.

Probes the device hardware to detect available radios (Zigbee, Z-Wave, Thread),
audio interfaces, Bluetooth, and platform type. The probe result drives
persona resolution.

Detection methods:
  - /sys/class/ and /dev/ for device enumeration
  - pyudev for USB device hotplug and attribute reading
  - /proc/cpuinfo and /proc/device-tree/ for platform identification
  - ALSA (arecord/aplay) for audio device enumeration

Supports a mock mode for development without hardware.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from shared.persona_schema import (
    DetectedHardware,
    HardwareCapability,
    HardwareProbeResult,
    SupportedPlatform,
)

logger = logging.getLogger("synork.persona.probe")

# Known USB vendor:product IDs for common smart home radios
_KNOWN_RADIOS: dict[tuple[str, str], HardwareCapability] = {
    # Silicon Labs Zigbee dongles (EZSP)
    ("10c4", "ea60"): HardwareCapability.ZIGBEE_RADIO,  # CP2102 (Sonoff, HUSBZB-1)
    ("10c4", "8a2a"): HardwareCapability.ZIGBEE_RADIO,  # CP2104
    ("1cf1", "0030"): HardwareCapability.ZIGBEE_RADIO,  # ConBee II
    ("0451", "16a8"): HardwareCapability.ZIGBEE_RADIO,  # TI CC2531
    # Z-Wave dongles
    ("0658", "0200"): HardwareCapability.ZWAVE_RADIO,   # Sigma Designs / Aeotec
    ("10c4", "8856"): HardwareCapability.ZWAVE_RADIO,   # Zooz
    # Thread-capable dongles (Session 18)
    ("1915", "521f"): HardwareCapability.THREAD_RADIO,  # Nordic nRF52840 Dongle
    ("1915", "cafe"): HardwareCapability.THREAD_RADIO,  # Nordic nRF52840 (DFU mode variant)
}

# Multi-protocol dongles: These chips support both Zigbee and Thread.
# On Hub Edition: detected as ZIGBEE_RADIO (Hub runs ZHA for Zigbee).
# On Assistant Edition: detected as THREAD_RADIO (Assistants use Thread
# for mesh extension; Zigbee via Assistants requires custom router
# firmware and is deferred to v2+).
_MULTIPROTOCOL_RADIOS: dict[tuple[str, str], tuple[str, str]] = {
    # SkyConnect by Nabu Casa (EFR32MG21)
    ("10c4", "ea60"): ("SkyConnect", "efr32"),
    # Home Assistant Yellow onboard (EFR32MG21) — identified via device-tree, not USB
}

# Product name substrings that identify multi-protocol hardware
_MULTIPROTOCOL_NAMES = {"skyconnect", "efr32", "multiprotocol", "multipan"}


class HardwareProbe:
    """Probes device hardware for persona resolution.

    In mock mode, returns a configurable set of fake capabilities for
    development and testing without real hardware.
    """

    def __init__(
        self,
        mock_mode: bool = False,
        mock_capabilities: Optional[list[str]] = None,
        edition: str = "hub",
    ) -> None:
        self._mock_mode = mock_mode
        self._mock_capabilities = mock_capabilities or []
        self._edition = edition  # "hub" or "assistant" — affects multi-protocol resolution

    async def probe(self) -> HardwareProbeResult:
        """Run a full hardware probe and return the result."""
        if self._mock_mode:
            return self._mock_probe()

        platform = await self._detect_platform()
        devices: list[DetectedHardware] = []
        capabilities: list[HardwareCapability] = []

        # Probe in parallel
        radio_devices, audio_devices, net_devices, bt_devices = await asyncio.gather(
            self._detect_radios(),
            self._detect_audio(),
            self._detect_network(),
            self._detect_bluetooth(),
        )

        for dev in radio_devices + audio_devices + net_devices + bt_devices:
            devices.append(dev)
            if dev.capability not in capabilities:
                capabilities.append(dev.capability)

        # Detect GPIO (RPi only)
        if await self._has_gpio():
            capabilities.append(HardwareCapability.GPIO)

        storage_gb = await self._detect_storage()
        cpu_model = await self._read_cpu_model()
        ram_mb = await self._read_ram_mb()

        result = HardwareProbeResult(
            platform=platform,
            capabilities=capabilities,
            devices=devices,
            cpu_model=cpu_model,
            ram_mb=ram_mb,
            storage_gb=storage_gb,
        )

        logger.info(
            "Hardware probe complete: platform=%s, capabilities=%s, devices=%d",
            platform.value,
            [c.value for c in capabilities],
            len(devices),
        )
        return result

    # -- Mock mode ---------------------------------------------------------- #

    def _mock_probe(self) -> HardwareProbeResult:
        """Generate a mock probe result for development."""
        capabilities = []
        devices = []

        cap_map = {c.value: c for c in HardwareCapability}
        for cap_name in self._mock_capabilities:
            cap = cap_map.get(cap_name)
            if cap:
                capabilities.append(cap)
                devices.append(DetectedHardware(
                    capability=cap,
                    device_path=f"/dev/mock_{cap_name}",
                    device_name=f"Mock {cap_name}",
                ))

        # Default mock: Pi 4 with Zigbee + mic + speaker
        if not self._mock_capabilities:
            for cap, name, path in [
                (HardwareCapability.ZIGBEE_RADIO, "Mock Zigbee", "/dev/ttyMOCK0"),
                (HardwareCapability.MICROPHONE, "Mock Microphone", "/dev/snd/mockC0D0c"),
                (HardwareCapability.AUDIO_OUTPUT, "Mock Speaker", "/dev/snd/mockC0D0p"),
                (HardwareCapability.ETHERNET, "Mock Ethernet", "/sys/class/net/eth0"),
                (HardwareCapability.WIFI, "Mock WiFi", "/sys/class/net/wlan0"),
                (HardwareCapability.BLUETOOTH, "Mock Bluetooth", "/sys/class/bluetooth/hci0"),
            ]:
                capabilities.append(cap)
                devices.append(DetectedHardware(
                    capability=cap,
                    device_path=path,
                    device_name=name,
                ))

        return HardwareProbeResult(
            platform=SupportedPlatform.PI_4,
            capabilities=capabilities,
            devices=devices,
            cpu_model="Mock ARM Cortex-A72",
            ram_mb=4096,
            storage_gb=32.0,
        )

    # -- Platform detection ------------------------------------------------- #

    async def _detect_platform(self) -> SupportedPlatform:
        """Detect the hardware platform from device tree and CPU info."""
        model_path = Path("/proc/device-tree/model")
        if model_path.exists():
            try:
                model = model_path.read_text().strip().lower()
                if "raspberry pi 5" in model:
                    return SupportedPlatform.PI_5
                if "raspberry pi 4" in model:
                    return SupportedPlatform.PI_4
                if "raspberry pi zero 2" in model:
                    return SupportedPlatform.PI_ZERO_2W
            except OSError:
                pass

        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            try:
                text = cpuinfo.read_text()
                if "GenuineIntel" in text or "AuthenticAMD" in text:
                    return SupportedPlatform.X86_GENERIC
            except OSError:
                pass

        return SupportedPlatform.UNKNOWN

    # -- Radio detection ---------------------------------------------------- #

    async def _detect_radios(self) -> list[DetectedHardware]:
        """Detect smart home radio dongles via USB device enumeration."""
        devices: list[DetectedHardware] = []

        usb_path = Path("/sys/bus/usb/devices")
        if not usb_path.exists():
            return devices

        for dev_path in usb_path.iterdir():
            vendor_path = dev_path / "idVendor"
            product_path = dev_path / "idProduct"

            if not vendor_path.exists() or not product_path.exists():
                continue

            try:
                vendor = vendor_path.read_text().strip().lower()
                product = product_path.read_text().strip().lower()
            except OSError:
                continue

            capability = _KNOWN_RADIOS.get((vendor, product))

            # Read product name for multi-protocol detection
            dev_name = "Unknown Radio"
            product_name_path = dev_path / "product"
            if product_name_path.exists():
                try:
                    dev_name = product_name_path.read_text().strip()
                except OSError:
                    pass

            # Session 18: Multi-protocol dongle resolution
            # If the device name matches known multi-protocol hardware,
            # resolve based on edition: Hub → Zigbee, Assistant → Thread
            if capability and any(
                mp in dev_name.lower() for mp in _MULTIPROTOCOL_NAMES
            ):
                if self._edition == "assistant":
                    capability = HardwareCapability.THREAD_RADIO
                    logger.info(
                        "Multi-protocol dongle '%s' resolved to THREAD_RADIO (Assistant edition)",
                        dev_name,
                    )
                else:
                    logger.info(
                        "Multi-protocol dongle '%s' keeping %s (Hub edition)",
                        dev_name, capability.value,
                    )

            if capability:
                tty_path = await self._find_tty_for_usb(dev_path)

                devices.append(DetectedHardware(
                    capability=capability,
                    device_path=tty_path or str(dev_path),
                    device_name=dev_name,
                    vendor_id=vendor,
                    product_id=product,
                    metadata={"multiprotocol": any(
                        mp in dev_name.lower() for mp in _MULTIPROTOCOL_NAMES
                    )},
                ))

        return devices

    async def _find_tty_for_usb(self, usb_dev_path: Path) -> Optional[str]:
        """Find the /dev/ttyXXX path for a USB device."""
        for root, dirs, _files in os.walk(str(usb_dev_path)):
            for d in dirs:
                if d.startswith("tty"):
                    tty_name_path = Path(root) / d / "tty"
                    if tty_name_path.exists():
                        for tty_entry in tty_name_path.iterdir():
                            return f"/dev/{tty_entry.name}"
                    if d.startswith(("ttyUSB", "ttyACM")):
                        return f"/dev/{d}"
        return None

    # -- Audio detection ---------------------------------------------------- #

    async def _detect_audio(self) -> list[DetectedHardware]:
        """Detect audio input (microphone) and output (speaker) devices."""
        devices: list[DetectedHardware] = []

        capture_devices = await self._list_alsa_devices("capture")
        for name, path in capture_devices:
            devices.append(DetectedHardware(
                capability=HardwareCapability.MICROPHONE,
                device_path=path,
                device_name=name,
            ))

        playback_devices = await self._list_alsa_devices("playback")
        for name, path in playback_devices:
            devices.append(DetectedHardware(
                capability=HardwareCapability.AUDIO_OUTPUT,
                device_path=path,
                device_name=name,
            ))

        return devices

    async def _list_alsa_devices(self, direction: str) -> list[tuple[str, str]]:
        """List ALSA devices for a direction (capture or playback)."""
        cmd = "arecord" if direction == "capture" else "aplay"
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd, "-l",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return []

            results: list[tuple[str, str]] = []
            for line in stdout.decode().splitlines():
                if line.startswith("card "):
                    parts = line.split(":")
                    if len(parts) >= 2:
                        card_num = line.split()[1].rstrip(":")
                        name = parts[1].strip().split("[")[0].strip() if "[" in parts[1] else parts[1].strip()
                        suffix = "c" if direction == "capture" else "p"
                        dev_path = f"/dev/snd/pcmC{card_num}D0{suffix}"
                        results.append((name, dev_path))
            return results
        except FileNotFoundError:
            return []

    # -- Network detection -------------------------------------------------- #

    async def _detect_network(self) -> list[DetectedHardware]:
        """Detect network interfaces (Ethernet, WiFi)."""
        devices: list[DetectedHardware] = []
        net_path = Path("/sys/class/net")
        if not net_path.exists():
            return devices

        for iface in sorted(net_path.iterdir()):
            name = iface.name
            if name == "lo":
                continue

            if (iface / "wireless").exists() or name.startswith(("wlan", "wl")):
                devices.append(DetectedHardware(
                    capability=HardwareCapability.WIFI,
                    device_path=str(iface),
                    device_name=name,
                ))
            elif name.startswith(("eth", "en")):
                devices.append(DetectedHardware(
                    capability=HardwareCapability.ETHERNET,
                    device_path=str(iface),
                    device_name=name,
                ))

        return devices

    # -- Bluetooth detection ------------------------------------------------ #

    async def _detect_bluetooth(self) -> list[DetectedHardware]:
        """Detect Bluetooth adapters."""
        devices: list[DetectedHardware] = []
        bt_path = Path("/sys/class/bluetooth")
        if not bt_path.exists():
            return devices

        for adapter in sorted(bt_path.iterdir()):
            devices.append(DetectedHardware(
                capability=HardwareCapability.BLUETOOTH,
                device_path=str(adapter),
                device_name=adapter.name,
            ))

        return devices

    # -- GPIO / Storage / CPU / RAM ----------------------------------------- #

    async def _has_gpio(self) -> bool:
        return Path("/sys/class/gpio").exists()

    async def _detect_storage(self) -> float:
        try:
            stat = os.statvfs("/")
            return (stat.f_blocks * stat.f_frsize) / (1024**3)
        except OSError:
            return 0.0

    async def _read_cpu_model(self) -> str:
        cpuinfo = Path("/proc/cpuinfo")
        if not cpuinfo.exists():
            return "unknown"
        try:
            for line in cpuinfo.read_text().splitlines():
                if line.startswith("model name") or line.startswith("Model"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return "unknown"

    async def _read_ram_mb(self) -> int:
        meminfo = Path("/proc/meminfo")
        if not meminfo.exists():
            return 0
        try:
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // 1024
        except (OSError, ValueError):
            pass
        return 0
