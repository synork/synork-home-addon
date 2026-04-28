"""Synork Home — Wizard HTTP server (port 8099, HA ingress).

Runs an aiohttp app on the addon's ingress port. HA Supervisor proxies
the user's authenticated browser into this server. Routes:

  GET  /                       → wizard SPA shell (or "already paired" page)
  GET  /static/{*}             → CSS/JS assets bundled in src/wizard/static/
  GET  /api/health             → liveness
  GET  /api/wizard/state       → {paired, device_id, household?, language}
  POST /api/wizard/sign-in     → proxy to /home/mobile/auth/token
  POST /api/wizard/sign-out    → drop in-memory Bearer token
  GET  /api/wizard/households  → list user's Synork households
  POST /api/wizard/pair        → proxy to /home/pairing/self_install,
                                  persist sidecar, fire on_paired callback

The wizard never persists the user's Bearer token — it lives in
process memory for the wizard's lifetime only. The addon authenticates
to the relay long-term using the device_secret, not the user's token.

Static assets use *relative* URLs in HTML so they work under HA's
prefixed ingress URL (`/api/hassio_ingress/<token>/...`) without
configuration.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from aiohttp import web

from .persistence import HubConfig, save_hub_config
from .strings import get_strings

logger = logging.getLogger("synork.wizard.server")

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Generous timeouts on the upstream Synork backend — closed beta users may be
# on flaky home connections.
_BACKEND_TIMEOUT = aiohttp.ClientTimeout(total=20)


PairedCallback = Callable[[dict], Awaitable[None]]


class WizardServer:
    """aiohttp server hosting the setup wizard on the addon's ingress port.

    Lifecycle:
        srv = WizardServer(...)
        await srv.start()        # binds 0.0.0.0:port
        ...                      # served until stop()
        await srv.stop()

    The server holds an in-memory Bearer token from the most recent
    sign-in; it is dropped on sign-out, on pair completion, and on
    server stop. It is never written to disk.
    """

    def __init__(
        self,
        *,
        relay_api_url: str,
        get_config: Callable[[], HubConfig],
        on_paired: PairedCallback,
        language: str = "en",
        host: str = "0.0.0.0",
        port: int = 8099,
    ) -> None:
        self._relay_api_url = relay_api_url.rstrip("/")
        self._get_config = get_config
        self._on_paired = on_paired
        self._language = language
        self._host = host
        self._port = port

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Bearer token from /home/mobile/auth/token; in-memory only.
        self._bearer_token: Optional[str] = None
        self._signed_in_user: Optional[dict] = None

        # Reusable HTTP session for upstream calls.
        self._http: Optional[aiohttp.ClientSession] = None

    @property
    def language(self) -> str:
        return self._language

    @language.setter
    def language(self, value: str) -> None:
        self._language = value

    # ── Lifecycle ──

    async def start(self) -> None:
        """Bind the HTTP server. Idempotent — repeated calls are no-ops."""
        if self._runner is not None:
            return

        self._http = aiohttp.ClientSession(timeout=_BACKEND_TIMEOUT)

        app = web.Application(middlewares=[self._error_middleware])
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/health", self._handle_health)
        app.router.add_get("/api/wizard/state", self._handle_state)
        app.router.add_post("/api/wizard/sign-in", self._handle_sign_in)
        app.router.add_post("/api/wizard/sign-out", self._handle_sign_out)
        app.router.add_get("/api/wizard/households", self._handle_households)
        app.router.add_post("/api/wizard/pair", self._handle_pair)
        if _STATIC_DIR.exists():
            app.router.add_static("/static/", _STATIC_DIR, show_index=False)
        else:
            logger.warning("wizard static dir missing at %s", _STATIC_DIR)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info("wizard server bound on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Tear down the server and its upstream HTTP session."""
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._http:
            await self._http.close()
            self._http = None
        self._bearer_token = None
        self._signed_in_user = None
        logger.info("wizard server stopped")

    # ── Middleware ──

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler) -> web.StreamResponse:
        """Convert unexpected exceptions into JSON 500s instead of HTML stack traces."""
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("unhandled error in %s %s", request.method, request.path)
            return web.json_response(
                {"ok": False, "error": "internal", "detail": "Unexpected server error."},
                status=500,
            )

    # ── HTML / static ──

    async def _handle_index(self, request: web.Request) -> web.Response:
        index_html = _STATIC_DIR / "index.html"
        if not index_html.exists():
            return web.Response(
                text="Wizard assets missing; reinstall the add-on.",
                content_type="text/html",
                status=500,
            )
        return web.FileResponse(index_html)

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "synork-home-wizard"})

    # ── State ──

    async def _handle_state(self, request: web.Request) -> web.Response:
        config = self._get_config()
        return web.json_response({
            "ok": True,
            "paired": config.is_paired,
            "device_id": config.device_id,
            "household_id": config.household_id or None,
            "household_name": config.household_name or None,
            "owner_user_id": config.owner_user_id or None,
            "paired_at": config.paired_at or None,
            "language": self._language,
            "strings": get_strings(self._language),
            "signed_in": self._bearer_token is not None,
            "signed_in_user": self._signed_in_user,
        })

    # ── Auth proxy ──

    async def _handle_sign_in(self, request: web.Request) -> web.Response:
        """Proxy email+password to /home/mobile/auth/token and stash the token in memory."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)

        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or not password:
            return web.json_response({"ok": False, "error": "missing_credentials"}, status=400)

        url = f"{self._relay_api_url}/api/v1/home/mobile/auth/token"
        try:
            async with self._http.post(url, json={"email": email, "password": password}) as resp:
                resp_body = await resp.text()
                if resp.status == 401:
                    detail = ""
                    try:
                        detail = (await _safe_json(resp_body)).get("detail", "")
                    except Exception:
                        detail = resp_body
                    if "2FA" in detail:
                        return web.json_response({"ok": False, "error": "tfa_required"}, status=401)
                    return web.json_response({"ok": False, "error": "invalid_credentials"}, status=401)
                if resp.status >= 400:
                    logger.warning("sign-in upstream %d: %s", resp.status, resp_body[:300])
                    return web.json_response({"ok": False, "error": "upstream_error"}, status=502)
                data = await _safe_json(resp_body)
        except aiohttp.ClientError as exc:
            logger.warning("sign-in network error: %s", exc)
            return web.json_response({"ok": False, "error": "network"}, status=502)

        token = data.get("token")
        if not token:
            return web.json_response({"ok": False, "error": "upstream_error"}, status=502)

        self._bearer_token = token
        self._signed_in_user = {
            "user_id": data.get("user_id", ""),
            "expires_at": data.get("expires_at", ""),
        }
        return web.json_response({"ok": True, "user": self._signed_in_user})

    async def _handle_sign_out(self, request: web.Request) -> web.Response:
        self._bearer_token = None
        self._signed_in_user = None
        return web.json_response({"ok": True})

    # ── Household listing ──

    async def _handle_households(self, request: web.Request) -> web.Response:
        if not self._bearer_token:
            return web.json_response({"ok": False, "error": "not_signed_in"}, status=401)
        url = f"{self._relay_api_url}/api/v1/home/households"
        headers = {"Authorization": f"Bearer {self._bearer_token}"}
        try:
            async with self._http.get(url, headers=headers) as resp:
                if resp.status == 401:
                    self._bearer_token = None
                    self._signed_in_user = None
                    return web.json_response({"ok": False, "error": "session_expired"}, status=401)
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("households upstream %d: %s", resp.status, body[:300])
                    return web.json_response({"ok": False, "error": "upstream_error"}, status=502)
                data = await resp.json()
        except aiohttp.ClientError as exc:
            return web.json_response({"ok": False, "error": "network", "detail": str(exc)}, status=502)

        # Backend may return a list directly or wrap it; handle both shapes.
        households = data if isinstance(data, list) else data.get("households", [])
        normalized = [
            {
                "household_id": h.get("household_id") or h.get("id"),
                "name": h.get("name", ""),
                "owner_user_id": h.get("owner_user_id", ""),
            }
            for h in households
        ]
        return web.json_response({"ok": True, "households": normalized})

    # ── Pair (the money path) ──

    async def _handle_pair(self, request: web.Request) -> web.Response:
        if not self._bearer_token:
            return web.json_response({"ok": False, "error": "not_signed_in"}, status=401)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)

        config = self._get_config()
        if not config.device_id:
            # Defensive — main.py is supposed to ensure_device_id() at startup.
            return web.json_response(
                {"ok": False, "error": "no_device_id", "detail": "Add-on has no device_id; restart the add-on."},
                status=500,
            )
        if config.is_paired:
            return web.json_response(
                {"ok": False, "error": "already_paired", "detail": "This add-on is already paired. Reset to start over."},
                status=409,
            )

        household_id = (body.get("household_id") or "").strip() or None
        household_name = (body.get("household_name") or "").strip() or None
        device_location = (body.get("device_location") or "").strip() or None
        device_label = (body.get("device_label") or "").strip() or None

        if not household_id and not household_name:
            return web.json_response(
                {"ok": False, "error": "household_required", "detail": "Pick a household or enter a name."},
                status=400,
            )

        url = f"{self._relay_api_url}/api/v1/home/pairing/self_install"
        headers = {"Authorization": f"Bearer {self._bearer_token}"}
        payload = {
            "device_id": config.device_id,
            "household_id": household_id,
            "household_name": household_name,
            "device_location": device_location,
            "device_label": device_label,
        }

        try:
            async with self._http.post(url, headers=headers, json=payload) as resp:
                upstream_body = await resp.text()
                if resp.status == 401:
                    self._bearer_token = None
                    self._signed_in_user = None
                    return web.json_response({"ok": False, "error": "session_expired"}, status=401)
                if resp.status == 400:
                    detail = (await _safe_json(upstream_body)).get("detail", "Pairing rejected.")
                    return web.json_response({"ok": False, "error": "rejected", "detail": detail}, status=400)
                if resp.status >= 400:
                    logger.warning("pair upstream %d: %s", resp.status, upstream_body[:300])
                    return web.json_response({"ok": False, "error": "upstream_error", "detail": upstream_body[:200]}, status=502)
                pairing = await _safe_json(upstream_body)
        except aiohttp.ClientError as exc:
            logger.warning("pair network error: %s", exc)
            return web.json_response({"ok": False, "error": "network", "detail": str(exc)}, status=502)

        # Persist and notify the addon. We persist *before* invoking the
        # on_paired callback so even if the callback errors (e.g. starting
        # the relay), the next addon restart still has the credentials.
        new_config = HubConfig(
            device_id=pairing["device_id"],
            device_secret=pairing["device_secret"],
            household_id=pairing["household_id"],
            household_name=pairing["household_name"],
            owner_user_id=pairing["owner_user_id"],
            paired_at=pairing.get("paired_at", ""),
            relay_ws_url=pairing["relay_websocket_url"],
            relay_api_url=pairing["relay_api_url"],
        )
        save_hub_config(new_config)

        try:
            await self._on_paired(pairing)
        except Exception:
            logger.exception("on_paired callback raised; pairing IS persisted, "
                             "addon will pick it up on next restart")

        # Drop the user's bearer token — we don't need it anymore.
        self._bearer_token = None
        self._signed_in_user = None

        return web.json_response({
            "ok": True,
            "household_id": pairing["household_id"],
            "household_name": pairing["household_name"],
            "device_id": pairing["device_id"],
        })


async def _safe_json(text: str) -> dict:
    """Best-effort JSON parse; return empty dict on failure."""
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}
