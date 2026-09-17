"""UI-host routes — Starlette route family served on the combiner's app.

Ported from pi-mcp-adapter's ui-server.ts (MIT, © 2026 Nico Bailon), reduced to
Stage 1:

- host page, app-bridge bundle, sandbox relay (second loopback origin), resource
  content — the render path
- /proxy/tools/call, /proxy/ui/*, /proxy/ui/complete, /proxy/ui/heartbeat — the
  widget→combiner action path, attributed to the chat's grouping token by talking
  to the combiner's own /mcp/<token> endpoint as a real MCP client
- /events — SSE: ready frame, per-session widget events (tool-input /
  tool-result / tool-cancelled / session-complete)

Stage 2 status: in-flight tool-call holding lives in `holder.py` (invoked from
the tool-call middleware). Still future: result-patch stream envelopes,
consent-manager persistence, cross-restart session recovery.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from importlib import resources
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .sessions import (
    SESSION_TTL_SECONDS,
    UiSession,
    UiSessionRegistry,
    call_as_token,
    read_as_token,
    resource_contents,
)
from .templates import (
    SANDBOX_PROXY_PATH,
    build_host_html,
    build_sandbox_proxy_csp,
    build_sandbox_proxy_html,
    build_sandbox_resource_csp,
)

logger = logging.getLogger("mcp_combiner.ui_host")

BUNDLE_PATH = "/app-bridge.bundle.js"


@dataclass
class UiHostConfig:
    """How the UI host reaches the combiner's own MCP endpoint and where the
    sandbox relay origin lives (a second loopback bind — provider HTML is
    same-origin with the RELAY, never with the capability-holding host page)."""

    base_origin: str
    # Relay binds the same host on port+1 (the second loopback/tailscale origin).
    sandbox_relay_port: int
    # Stage 2: how long a tool call referencing a widget stays in flight before
    # the holder resolves with the recorded state. Must fit the CLIENT's own
    # request timeout (pi default 60s), so the default sits under it.
    hold_timeout: float = 50.0


class UiHost:
    def __init__(self, config: UiHostConfig) -> None:
        self.config = config
        self.hold_timeout = config.hold_timeout
        self.registry = UiSessionRegistry()

    # -- helpers ---------------------------------------------------------

    async def _host_page(self, request: Request) -> Response:
        token = request.path_params["token"]
        resource_uri = request.query_params.get("resource", "")
        if not resource_uri:
            return JSONResponse({"error": "missing ?resource=<uri>"}, status_code=400)
        session = self.registry.get_or_create(token, resource_uri)
        # Resource meta (CSP/permissions) comes from resources/list metadata —
        # read via the session's loopback client.
        meta: dict[str, Any] = {}
        server_name = ""
        resource_name = ""
        try:
            result = await read_as_token(self.config.base_origin, session.token, resource_uri)
            contents = resource_contents(result)
            if contents:
                first = contents[0]
                meta = getattr(first, "meta", None) or getattr(first, "_meta", None) or {}
                meta = (meta.get("ui") or {}) if isinstance(meta.get("ui") or {}, dict) else {}
                server_name = resource_uri.split("://")[-1].split("/")[0]
                resource_name = getattr(first, "name", "") or resource_uri
        except Exception as exc:  # noqa: BLE001 — the page renders even if meta read fails
            logger.warning("ui host: resource meta read failed: %s", exc)

        html = build_host_html(
            session_token=session.session_token,
            server_name=server_name,
            tool_name=resource_name,
            tool_args={},
            resource_meta=meta,
            allow_attribute=_allow_attribute(meta.get("permissions")),
            # Client-side origin derivation: the sandbox relay origin is built
            # in the browser from location.hostname, so the SAME page works on
            # loopback, a LAN bind, or a tailscale address. The server only
            # supplies the relay port (+1) and the path+query.
            sandbox_relay_port=self.config.sandbox_relay_port,
            sandbox_proxy_path_query=SANDBOX_PROXY_PATH
            + f"?resource={resource_uri}&pt={token}"
            # The relay validates + targets the parent with this origin; without
            # it its script throws on the first postMessage and never relays.
            + f"&parent={quote(self.config.base_origin, safe='')}",
            # Token-scoped: the bundle is only served under /ui/<token>/… — the
            # default root-relative module URL 404s and the whole host script
            # (module import) never runs, leaving the page on "Loading UI...".
            app_bridge_module_url=f"/ui/{token}/app-bridge.bundle.js",
            ui_base=f"/ui/{token}",
        )
        return Response(
            html,
            media_type="text/html",
            headers={"Cache-Control": "no-store", "X-MCP-UI-Session": session.session_token},
        )

    async def _bundle(self, request: Request) -> Response:
        content = (
            resources.files("mcp_combiner.ui_host")
            .joinpath("static/app-bridge.bundle.js")
            .read_text(encoding="utf-8")
        )
        return Response(
            content, media_type="application/javascript", headers={"Cache-Control": "no-store"}
        )

    async def _sandbox_proxy(self, request: Request) -> Response:
        # Served on the SECOND origin: the relay document never carries the host's
        # session capability, so allow-same-origin is safe for the provider frame.
        parent_origin = request.query_params.get("parent", "")
        resource_uri = request.query_params.get("resource", "")
        # Both mounts: /ui/<token>/sandbox on the main app (path token) and
        # /sandbox?pt=<token> on the standalone relay app (query token).
        pt = request.query_params.get("pt") or request.path_params.get("token", "")
        # The relay's INNER frame navigates here to load the provider content —
        # /sandbox would recurse the relay into itself (nested sandboxes, about:blank).
        resource_path = f"/resource?resource={resource_uri}&pt={pt}"
        html = build_sandbox_proxy_html(parent_origin, resource_path, "")
        return Response(
            html,
            media_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": build_sandbox_proxy_csp(),
            },
        )

    async def _resource(self, request: Request) -> Response:
        """Provider content, served same-origin with the relay (the inner frame's
        navigation target). CSP from the resource's _meta, response-level."""
        # Both mounts: /ui/<token>/resource on the main app (path token) and
        # /resource?pt=<token> on the standalone relay app (query token).
        token = request.query_params.get("pt") or request.path_params.get("token", "")
        resource_uri = request.query_params.get("resource", "")
        session = self.registry.get(token, resource_uri)
        if session is None:
            return JSONResponse({"error": "no session"}, status_code=404)
        try:
            result = await read_as_token(self.config.base_origin, session.token, resource_uri)
            contents = resource_contents(result)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=502)
        if not contents:
            return JSONResponse({"error": "empty resource"}, status_code=404)
        first = contents[0]
        text = getattr(first, "text", None)
        meta = getattr(first, "meta", None) or getattr(first, "_meta", None) or {}
        csp = (meta.get("ui") or {}).get("csp") if isinstance(meta.get("ui"), dict) else None
        if text is None:
            blob = getattr(first, "blob", None)
            if blob is None:
                return JSONResponse({"error": "unreadable resource"}, status_code=415)
            import base64

            text = base64.b64decode(blob)
            mime = getattr(first, "mimeType", "application/octet-stream")
            return Response(
                text,
                media_type=mime,
                headers={"Content-Security-Policy": build_sandbox_resource_csp(csp)},
            )
        mime = getattr(first, "mimeType", "text/html")
        return Response(
            text,
            media_type=mime,
            headers={"Content-Security-Policy": build_sandbox_resource_csp(csp)},
        )

    # -- proxy endpoints (widget → combiner, CSRF via session token) ------

    async def _authorized_session(
        self, request: Request
    ) -> tuple[UiSession, dict[str, Any]] | JSONResponse:
        token = request.path_params["token"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
        session_token = body.get("token", "")
        session = self.registry.get(token)
        if session is None or session.session_token != session_token:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return session, body

    async def _proxy_tools_call(self, request: Request) -> Response:
        authorized = await self._authorized_session(request)
        if isinstance(authorized, JSONResponse):
            return authorized
        session, body = authorized
        params = body.get("params", {})
        tool = params.get("name", "")
        if not tool:
            return JSONResponse({"ok": False, "error": "missing tool name"}, status_code=400)
        # Consent gate (widget-side confirm already ran in the host page; the
        # server-side policy gate runs inside the call pipeline).
        if session.consent == "denied":
            return JSONResponse({"ok": False, "error": "consent denied"}, status_code=403)
        try:
            result = await call_as_token(
                self.config.base_origin, session.token, tool, params.get("arguments")
            )
            payload = _tool_result_payload(result)
        except Exception as exc:  # noqa: BLE001 — surface upstream/tool errors to the widget
            payload = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        session.record(
            session.intents,
            {"tool": tool, "arguments": params.get("arguments"), "result": payload},
        )
        return JSONResponse({"ok": True, "result": payload})

    async def _proxy_ui_message(self, request: Request) -> Response:
        authorized = await self._authorized_session(request)
        if isinstance(authorized, JSONResponse):
            return authorized
        session, body = authorized
        params = body.get("params", {})
        session.record(session.messages, params)
        return JSONResponse({"ok": True, "result": {}})

    async def _proxy_ui_context(self, request: Request) -> Response:
        authorized = await self._authorized_session(request)
        if isinstance(authorized, JSONResponse):
            return authorized
        session, body = authorized
        params = body.get("params", {})
        session.record(session.contexts, params)
        return JSONResponse({"ok": True, "result": {}})

    async def _proxy_ui_intent(self, request: Request) -> Response:
        authorized = await self._authorized_session(request)
        if isinstance(authorized, JSONResponse):
            return authorized
        session, body = authorized
        params = body.get("params", {})
        session.record(session.intents, {"generated": params})
        return JSONResponse({"ok": True, "result": {}})

    async def _proxy_ui_consent(self, request: Request) -> Response:
        authorized = await self._authorized_session(request)
        if isinstance(authorized, JSONResponse):
            return authorized
        session, body = authorized
        approved = body.get("params", {}).get("approved")
        if isinstance(approved, bool):
            session.consent = "granted" if approved else "denied"
        return JSONResponse({"ok": True, "result": {}})

    async def _proxy_ui_heartbeat(self, request: Request) -> Response:
        authorized = await self._authorized_session(request)
        if isinstance(authorized, JSONResponse):
            return authorized
        authorized[0].touch()
        return JSONResponse({"ok": True, "result": {}})

    async def _proxy_ui_complete(self, request: Request) -> Response:
        authorized = await self._authorized_session(request)
        if isinstance(authorized, JSONResponse):
            return authorized
        session, body = authorized
        # Mark WITHOUT evicting: a Stage 2 holder awaiting this session resolves
        # and evicts itself; a Stage 1 session lingers (completed) so the
        # retrieval loop can still read what the user did, until the TTL.
        session.mark_completed(str(body.get("params", {}).get("reason", "done")))
        return JSONResponse({"ok": True, "result": {}})

    async def _proxy_ui_open_link(self, request: Request) -> Response:
        authorized = await self._authorized_session(request)
        if isinstance(authorized, JSONResponse):
            return authorized
        # Stage 1: links open client-side in the host page (window.open) — the
        # server just records the intent.
        session, body = authorized
        session.record(session.intents, {"open_link": body.get("params", {}).get("url")})
        return JSONResponse({"ok": True, "result": {}})

    async def _proxy_ui_download(self, request: Request) -> Response:
        # Stage 1: no host-mediated downloads; the widget falls back gracefully.
        return JSONResponse({"ok": False, "error": "not supported in this stage"}, status_code=501)

    async def _proxy_ui_display_mode(self, request: Request) -> Response:
        return JSONResponse({"ok": True, "result": {"mode": "inline"}})

    async def _events(self, request: Request) -> Response:
        """Minimal SSE: keepalive + session-complete polling. Stage 2 replaces
        this with stream envelopes / result patches."""
        token = request.path_params["token"]
        session_token = request.query_params.get("session", "")
        session = self.registry.get(token)
        if session is None or session.session_token != session_token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        async def stream() -> AsyncIterator[str]:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(session.events.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    session.touch()
                    if session.idle_seconds > SESSION_TTL_SECONDS:
                        return
                    continue
                import json as _json

                yield f"event: {evt['event']}\ndata: {_json.dumps(evt['data'])}\n\n"
                if evt["event"] == "session-complete":
                    return

        # Stream the generator — an EventSource client (the host page's status
        # dot lives on this connection) must receive the ready frame immediately.
        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "Connection": "keep-alive"},
        )

    def build_relay_app(self) -> Starlette:
        """The SECOND-origin app: the sandbox relay page + provider resource
        content, nothing else. Provider HTML is same-origin with THIS app, so
        it can never read the capability-holding host page (different port)."""
        return Starlette(
            routes=[
                Route("/sandbox", self._sandbox_proxy, methods=["GET"]),
                Route("/resource", self._resource, methods=["GET"]),
            ]
        )

    # -- attach -----------------------------------------------------------

    def attach(self, combiner: FastMCP) -> None:
        """Attach the /ui/{token}/... route family to the FastMCP app."""

        @combiner.custom_route("/ui/{token}/{rest:path}", methods=["GET", "POST"])
        async def dispatch(request: Request) -> Response:
            rest = request.path_params.get("rest", "")
            if request.method == "GET":
                if rest == "" or rest == "/":
                    return await self._host_page(request)
                if rest == BUNDLE_PATH.lstrip("/"):
                    return await self._bundle(request)
                if rest == "sandbox":
                    return await self._sandbox_proxy(request)
                if rest.startswith("resource"):
                    return await self._resource(request)
                if rest == "events":
                    return await self._events(request)
            else:
                handlers = {
                    "proxy/tools/call": self._proxy_tools_call,
                    "proxy/ui/message": self._proxy_ui_message,
                    "proxy/ui/context": self._proxy_ui_context,
                    "proxy/ui/generated-tool-call-intent": self._proxy_ui_intent,
                    "proxy/ui/consent": self._proxy_ui_consent,
                    "proxy/ui/heartbeat": self._proxy_ui_heartbeat,
                    "proxy/ui/complete": self._proxy_ui_complete,
                    "proxy/ui/open-link": self._proxy_ui_open_link,
                    "proxy/ui/download-file": self._proxy_ui_download,
                    "proxy/ui/request-display-mode": self._proxy_ui_display_mode,
                }
                handler = handlers.get(rest)
                if handler is not None:
                    return await handler(request)
            return JSONResponse({"error": "not found"}, status_code=404)


def _allow_attribute(permissions: Any) -> str:
    if not isinstance(permissions, dict):
        return ""
    parts = []
    for key in ("camera", "microphone", "geolocation", "clipboardWrite"):
        if isinstance(permissions.get(key), dict):
            parts.append(key if key != "clipboardWrite" else "clipboard-write")
    return "; ".join(parts)


def _tool_result_payload(result: object) -> dict[str, Any]:
    """Normalize a fastmcp call_tool result into the MCP CallToolResult shape
    the widget bridge expects."""
    content = getattr(result, "content", []) or []
    blocks = []
    for block in content:
        block_type = getattr(block, "type", "text")
        if block_type == "text":
            blocks.append({"type": "text", "text": getattr(block, "text", "")})
        elif block_type == "image":
            blocks.append(
                {
                    "type": "image",
                    "data": getattr(block, "data", ""),
                    "mimeType": getattr(block, "mimeType", ""),
                }
            )
        else:
            blocks.append({"type": "text", "text": str(getattr(block, "text", ""))})
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    payload: dict[str, Any] = {"content": blocks}
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        payload["isError"] = True
    if structured is not None:
        payload["structuredContent"] = structured
    return payload
