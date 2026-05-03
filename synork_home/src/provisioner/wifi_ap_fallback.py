"""Synork Home — Wi-Fi AP Fallback.

When no network is available at first boot, the addon creates a WiFi
access point named `Synork-XXXX` and serves a captive portal. The user
connects from their phone/laptop, completes the OOBE flow (enter WiFi
credentials + Synork account), and the device joins their home network.

Implementation:
  - hostapd for the AP (started via subprocess)
  - dnsmasq for DHCP + DNS redirect (captive portal detection)
  - aiohttp for the HTTP server serving the OOBE web UI

The OOBE web UI itself is built in Session 6 (frontend) and bundled
into the addon. This module serves whatever is in /data/frontend/oobe/
or a fallback "connect to WiFi" page.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from aiohttp import web

logger = logging.getLogger("synork.provisioner.wifi_ap")

# Default AP settings
_AP_CHANNEL = 6
_AP_SUBNET = "192.168.4"
_AP_IP = f"{_AP_SUBNET}.1"
_AP_DHCP_START = f"{_AP_SUBNET}.10"
_AP_DHCP_END = f"{_AP_SUBNET}.50"
_PORTAL_PORT = 80

# OOBE web UI directory (bundled from frontend in Session 6)
_OOBE_DIR = Path("/data/frontend/oobe")

# Fallback HTML when OOBE bundle isn't available yet
_FALLBACK_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Synork Home Setup</title>
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 20px;
               background: #0f0f23; color: #e2e8f0; min-height: 100vh; }
        .container { max-width: 400px; margin: 60px auto; text-align: center; }
        h1 { color: #8b5cf6; font-size: 24px; }
        p { color: #94a3b8; line-height: 1.6; }
        form { margin-top: 30px; text-align: left; }
        label { display: block; margin-top: 16px; font-size: 14px; color: #94a3b8; }
        input { width: 100%%; padding: 12px; margin-top: 4px; border: 1px solid #334155;
                border-radius: 8px; background: #1e293b; color: #e2e8f0; font-size: 16px;
                box-sizing: border-box; }
        button { width: 100%%; padding: 14px; margin-top: 24px; border: none;
                 border-radius: 8px; background: #6366f1; color: white; font-size: 16px;
                 cursor: pointer; font-weight: 600; }
        button:hover { background: #4f46e5; }
        .status { margin-top: 16px; font-size: 14px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Synork Home</h1>
        <p>Connect this device to your WiFi network to get started.</p>
        <form id="wifi-form">
            <label for="ssid">WiFi Network Name</label>
            <input type="text" id="ssid" name="ssid" required placeholder="Your WiFi SSID">
            <label for="passphrase">Password</label>
            <input type="password" id="passphrase" name="passphrase" required>
            <button type="submit">Connect</button>
        </form>
        <div class="status" id="status"></div>
    </div>
    <script>
        document.getElementById('wifi-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            var status = document.getElementById('status');
            status.textContent = 'Connecting...';
            try {
                var resp = await fetch('/api/wifi', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        ssid: document.getElementById('ssid').value,
                        passphrase: document.getElementById('passphrase').value,
                    }),
                });
                var data = await resp.json();
                if (data.success) {
                    status.textContent = 'Connected! This page will redirect shortly...';
                } else {
                    status.textContent = 'Failed: ' + (data.error || 'Unknown error');
                }
            } catch (err) {
                status.textContent = 'Connection error. Please try again.';
            }
        });
    </script>
</body>
</html>
"""


class WiFiAPFallback:
    """Fallback Wi-Fi AP with captive portal for device provisioning.

    Lifecycle:
      1. start_ap() -- launches hostapd + dnsmasq
      2. serve_captive_portal() -- runs HTTP server for OOBE
      3. On WiFi config submission -- connects to user's WiFi
      4. stop_ap() -- tears down AP, DHCP, portal
    """

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self._ap_name = f"Synork-{device_id[-4:]}" if len(device_id) >= 4 else f"Synork-{device_id}"
        self._hostapd_proc: Optional[asyncio.subprocess.Process] = None
        self._dnsmasq_proc: Optional[asyncio.subprocess.Process] = None
        self._web_runner: Optional[web.AppRunner] = None
        self._running = False
        self._wifi_configured_event = asyncio.Event()
        self._wifi_ssid: Optional[str] = None
        self._wifi_passphrase: Optional[str] = None
        self._tmpdir: Optional[str] = None

    @property
    def ap_name(self) -> str:
        return self._ap_name

    @property
    def wifi_configured(self) -> bool:
        return self._wifi_configured_event.is_set()

    async def start_ap(self) -> None:
        """Start the fallback Wi-Fi access point."""
        self._running = True
        self._tmpdir = tempfile.mkdtemp(prefix="synork_ap_")

        wlan_iface = await self._find_wireless_interface()
        if not wlan_iface:
            logger.error("No wireless interface found -- cannot start AP")
            return

        logger.info("Starting WiFi AP '%s' on %s", self._ap_name, wlan_iface)

        # Configure interface with static IP
        await self._run_cmd("ip", "link", "set", wlan_iface, "down")
        await self._run_cmd("ip", "addr", "flush", "dev", wlan_iface)
        await self._run_cmd("ip", "addr", "add", f"{_AP_IP}/24", "dev", wlan_iface)
        await self._run_cmd("ip", "link", "set", wlan_iface, "up")

        # Write and start hostapd
        hostapd_conf = os.path.join(self._tmpdir, "hostapd.conf")
        with open(hostapd_conf, "w") as f:
            f.write(
                f"interface={wlan_iface}\n"
                f"driver=nl80211\n"
                f"ssid={self._ap_name}\n"
                f"hw_mode=g\n"
                f"channel={_AP_CHANNEL}\n"
                f"wmm_enabled=0\n"
                f"macaddr_acl=0\n"
                f"auth_algs=1\n"
                f"ignore_broadcast_ssid=0\n"
            )

        self._hostapd_proc = await asyncio.create_subprocess_exec(
            "hostapd", hostapd_conf,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Write and start dnsmasq for DHCP + captive portal DNS redirect
        dnsmasq_conf = os.path.join(self._tmpdir, "dnsmasq.conf")
        with open(dnsmasq_conf, "w") as f:
            f.write(
                f"interface={wlan_iface}\n"
                f"dhcp-range={_AP_DHCP_START},{_AP_DHCP_END},12h\n"
                f"address=/#/{_AP_IP}\n"
                f"no-resolv\n"
            )

        self._dnsmasq_proc = await asyncio.create_subprocess_exec(
            "dnsmasq", "-C", dnsmasq_conf, "--no-daemon",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        logger.info("WiFi AP started: %s (IP: %s)", self._ap_name, _AP_IP)

    async def serve_captive_portal(self) -> None:
        """Serve the captive portal. Blocks until WiFi credentials are submitted."""
        app = web.Application()
        app.router.add_get("/", self._handle_portal_root)
        app.router.add_get("/generate_204", self._handle_captive_detect)  # Android
        app.router.add_get("/hotspot-detect.html", self._handle_captive_detect)  # Apple
        app.router.add_get("/connecttest.txt", self._handle_captive_detect)  # Windows
        app.router.add_post("/api/wifi", self._handle_wifi_submit)

        if _OOBE_DIR.exists():
            app.router.add_static("/oobe/", _OOBE_DIR)

        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, _AP_IP, _PORTAL_PORT)
        await site.start()

        logger.info("Captive portal serving on http://%s:%d", _AP_IP, _PORTAL_PORT)
        await self._wifi_configured_event.wait()

    async def stop_ap(self) -> None:
        """Stop the WiFi AP, DHCP, and captive portal."""
        self._running = False

        if self._web_runner:
            await self._web_runner.cleanup()

        for proc, name in [
            (self._hostapd_proc, "hostapd"),
            (self._dnsmasq_proc, "dnsmasq"),
        ]:
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                logger.info("Stopped %s", name)

        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

        logger.info("WiFi AP stopped")

    async def get_wifi_credentials(self) -> tuple[Optional[str], Optional[str]]:
        """Return the submitted WiFi credentials (ssid, passphrase)."""
        return self._wifi_ssid, self._wifi_passphrase

    async def apply_wifi_config(self) -> bool:
        """Apply WiFi credentials via nmcli. Returns True on success."""
        if not self._wifi_ssid:
            return False

        try:
            await self._run_cmd(
                "nmcli", "device", "wifi", "connect",
                self._wifi_ssid,
                "password", self._wifi_passphrase or "",
            )
            logger.info("WiFi connected to: %s", self._wifi_ssid)
            return True
        except Exception as exc:
            logger.error("Failed to apply WiFi config: %s", exc)
            return False

    # -- HTTP handlers ------------------------------------------------------ #

    async def _handle_portal_root(self, request: web.Request) -> web.Response:
        if (_OOBE_DIR / "index.html").exists():
            return web.HTTPFound("/oobe/index.html")
        return web.Response(text=_FALLBACK_HTML, content_type="text/html")

    async def _handle_captive_detect(self, request: web.Request) -> web.Response:
        return web.HTTPFound("/")

    async def _handle_wifi_submit(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            data = dict(await request.post())

        ssid = data.get("ssid", "").strip()
        passphrase = data.get("passphrase", "")

        if not ssid:
            return web.json_response({"success": False, "error": "SSID is required"})

        self._wifi_ssid = ssid
        self._wifi_passphrase = passphrase
        self._wifi_configured_event.set()

        return web.json_response({"success": True})

    # -- Helpers ------------------------------------------------------------ #

    async def _find_wireless_interface(self) -> Optional[str]:
        """Find the first wireless network interface."""
        net_path = Path("/sys/class/net")
        if not net_path.exists():
            return None
        for iface_path in sorted(net_path.iterdir()):
            name = iface_path.name
            if (iface_path / "wireless").exists() or name.startswith(("wlan", "wl")):
                return name
        return None

    async def _run_cmd(self, *args: str) -> str:
        """Run a command via subprocess and return stdout.

        We deliberately do NOT log argv or stderr here — callers may pass Wi-Fi
        passwords or other secrets in `args`, and some tools (e.g. nmcli) echo
        argv back in their error output. We log only the program name and exit
        status to avoid leaking credentials into log files.
        """
        program = args[0] if args else "<unknown>"
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("Command %s exited with code %s", program, proc.returncode)
        return stdout.decode().strip()
