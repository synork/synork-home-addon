"""Synork Home — Network Detection.

Detects whether the device has Ethernet with DHCP, WiFi configured,
or no network — determines whether provisioning needs to start AP mode.

On HA OS, network interfaces are managed by NetworkManager via D-Bus.
We probe /sys/class/net/ for interface types and use simple heuristics:
  - eth*  / en* with carrier = ethernet
  - wlan* / wl* with carrier = wifi
  - No carrier on any interface = needs AP fallback
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("synork.provisioner.network")


@dataclass
class NetworkStatus:
    """Result of network detection."""
    interface_type: str  # "ethernet", "wifi", "none"
    interface_name: Optional[str] = None
    ip_address: Optional[str] = None
    has_internet: bool = False
    gateway: Optional[str] = None


class NetworkDetector:
    """Detects network connectivity state and interface type.

    Probes /sys/class/net/ for interface state. Falls back to socket-based
    detection if sysfs is unavailable (e.g., during development/mock mode).
    """

    def __init__(self, mock_mode: bool = False) -> None:
        self._mock_mode = mock_mode

    async def detect(self) -> NetworkStatus:
        """Detect the active network interface and connectivity.

        Returns NetworkStatus with interface type, IP, and internet reachability.
        """
        if self._mock_mode:
            return NetworkStatus(
                interface_type="ethernet",
                interface_name="eth0_mock",
                ip_address="192.168.1.100",
                has_internet=True,
            )

        # Check sysfs for network interfaces
        net_path = Path("/sys/class/net")
        if not net_path.exists():
            return await self._detect_via_socket()

        best: Optional[NetworkStatus] = None

        for iface_path in sorted(net_path.iterdir()):
            name = iface_path.name
            if name == "lo":
                continue

            # Check if interface has carrier (link is up)
            carrier_path = iface_path / "carrier"
            try:
                carrier = carrier_path.read_text().strip()
            except (OSError, FileNotFoundError):
                carrier = "0"

            if carrier != "1":
                continue

            iface_type = self._classify_interface(name, iface_path)
            ip = await self._get_interface_ip(name)

            status = NetworkStatus(
                interface_type=iface_type,
                interface_name=name,
                ip_address=ip,
            )

            # Prefer ethernet over wifi
            if iface_type == "ethernet":
                best = status
                break
            elif iface_type == "wifi" and best is None:
                best = status

        if best is None:
            return NetworkStatus(interface_type="none")

        best.has_internet = await self._check_internet()
        return best

    async def get_ip_address(self) -> Optional[str]:
        """Get the current IP address of the primary interface."""
        status = await self.detect()
        return status.ip_address

    def _classify_interface(self, name: str, path: Path) -> str:
        """Classify an interface as ethernet or wifi based on name and sysfs."""
        if (path / "wireless").exists():
            return "wifi"
        if name.startswith(("eth", "en")):
            return "ethernet"
        if name.startswith(("wlan", "wl")):
            return "wifi"

        type_path = path / "type"
        try:
            iface_type = int(type_path.read_text().strip())
            if iface_type == 1:  # ARPHRD_ETHER
                return "ethernet"
        except (OSError, ValueError):
            pass

        return "ethernet"

    async def _get_interface_ip(self, name: str) -> Optional[str]:
        """Get the IPv4 address of a named interface using subprocess."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ip", "-4", "-o", "addr", "show", name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            for line in stdout.decode().strip().splitlines():
                parts = line.split()
                for i, part in enumerate(parts):
                    if part == "inet" and i + 1 < len(parts):
                        return parts[i + 1].split("/")[0]
        except Exception:
            pass
        return None

    async def _check_internet(self) -> bool:
        """Check if the device can reach the internet via ping."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", "3", "1.1.1.1",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def _detect_via_socket(self) -> NetworkStatus:
        """Fallback detection using socket connection."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.connect(("1.1.1.1", 53))
            ip = s.getsockname()[0]
            s.close()
            return NetworkStatus(
                interface_type="ethernet",
                ip_address=ip,
                has_internet=True,
            )
        except Exception:
            return NetworkStatus(interface_type="none")
