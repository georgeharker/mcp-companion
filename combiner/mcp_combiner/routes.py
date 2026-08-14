"""Session-management and health REST API.

The endpoints external clients (the Neovim plugin, the CLI) use to inspect
sessions and manage per-session server filters by session id or chat token —
without being the session owner. Handlers moved verbatim from
create_combiner; ``register_routes`` re-attaches them.

NOTE (QUESTIONS.md Q1): the token routes resolve tokens through
``RUNTIME.sessions.token_sessions``, whose values are wire ``mcp-session-id``s
— a different namespace from the ``Context.session_id`` keys the filtering
middleware reads. Preserved as-is pending discussion.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_combiner.config import CombinerConfig, HealthResponse, ServerStatusInfo
from mcp_combiner.connections import ConnectionManager
from mcp_combiner.runtime import RUNTIME
from mcp_combiner.status import build_server_status
from mcp_combiner.toolcache import _notify_session_by_id

logger = logging.getLogger("mcp-combiner")


def register_routes(
    combiner: FastMCP,
    config: CombinerConfig,
    conn_manager: ConnectionManager,
) -> None:
    """Attach the /health and /sessions* routes to *combiner*."""

    # Health endpoint
    @combiner.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        server_statuses: dict[str, ServerStatusInfo] = {
            name: build_server_status(config, conn_manager, name) for name in config.servers
        }
        auth_failed = [n for n in conn_manager._connections if conn_manager.is_auth_failed(n)]
        response = HealthResponse(
            status="ok",
            servers=server_statuses,
            config_path=config.config_path,
            pending_oauth=auth_failed,
        )
        payload = response.model_dump(mode="json")
        # boot_id changes only when this combiner *process* (re)starts. Clients use
        # it to detect a restart and re-register Neovim instances + token binds.
        payload["boot_id"] = RUNTIME.boot_id
        return JSONResponse(payload)

    # Sanctioned-restart handover: ctl flags the NEXT shutdown to write the
    # one-shot handover payload to *path*. Only `mcp-combiner restart` calls
    # this; an unflagged shutdown (crash, kill, grace expiry) writes nothing.
    @combiner.custom_route("/handover/prepare", methods=["POST"])
    async def handover_prepare(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            path = str(body["path"])
        except Exception:
            return JSONResponse({"error": "body must be {'path': <str>}"}, status_code=400)
        parent = Path(path).expanduser().parent
        if not parent.is_dir():
            return JSONResponse(
                {"error": f"directory does not exist: {parent}"}, status_code=400
            )
        RUNTIME.handover_path = str(Path(path).expanduser())
        logger.info("handover: shutdown will write %s", RUNTIME.handover_path)
        return JSONResponse({"status": "armed", "path": RUNTIME.handover_path})

    # --- Session management REST API ---
    # These endpoints allow external clients (e.g. the Neovim plugin) to
    # list active MCP sessions and manage per-session server filters by
    # session ID, without needing to be the session owner.

    @combiner.custom_route("/sessions", methods=["GET"])
    async def list_sessions(request: Request) -> JSONResponse:
        """List active MCP sessions with their IDs, client info, and filter state."""
        sessions_out: list[dict[str, Any]] = []
        for sess in RUNTIME.sessions.sessions():
            try:
                sid = getattr(sess, "_fastmcp_state_prefix", None) or str(id(sess))
            except AttributeError:
                sid = str(id(sess))
            blocked = RUNTIME.sessions.disabled.get(sid, set())
            # Extract client info from the MCP initialize handshake
            client_info: dict[str, Any] | None = None
            try:
                cp = getattr(sess, "client_params", None)
                ci = getattr(cp, "clientInfo", None) if cp else None
                if ci:
                    client_info = {
                        "name": getattr(ci, "name", None),
                        "version": getattr(ci, "version", None),
                    }
            except Exception:
                pass
            entry: dict[str, Any] = {
                "session_id": sid,
                "disabled_servers": sorted(blocked),
            }
            if client_info:
                entry["client_info"] = client_info
            sessions_out.append(entry)
        return JSONResponse({"sessions": sessions_out})

    @combiner.custom_route("/sessions/{session_id}/filter", methods=["GET", "POST", "DELETE"])
    async def manage_session_filter(request: Request) -> JSONResponse:
        """Manage per-session server blocklist by session ID.

        GET: Get current disabled servers for a session.
        POST: Set disabled servers for a session.
              Body: { "disabled_servers": ["server1", "server2"] }
              Or:   { "allowed_servers": ["server1"] } — inverts to disable all others
        DELETE: Clear all session filters for a session.
        """
        session_id = request.path_params.get("session_id", "")
        if not session_id:
            return JSONResponse({"error": "session_id required"}, status_code=400)

        if request.method == "GET":
            disabled = RUNTIME.sessions.disabled.get(session_id, set())
            return JSONResponse(
                {
                    "session_id": session_id,
                    "disabled_servers": sorted(disabled),
                }
            )

        if request.method == "DELETE":
            removed = RUNTIME.sessions.clear_disabled(session_id)
            # Notify the session so its tool list refreshes
            await _notify_session_by_id(session_id)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "action": "cleared",
                    "previously_disabled": sorted(removed) if removed else [],
                }
            )

        # POST — manage disabled servers
        # Accepts:
        #   { "disabled_servers": ["srv1", "srv2"] } — set explicit disable list
        #   { "allowed_servers": ["srv1"] } — allow list, inverts to disable all others
        #   { "enable": "srv1" } — enable a single server (remove from disabled)
        #   { "disable": "srv1" } — disable a single server (add to disabled)
        # Note: allowed_servers=[] means disable ALL servers (not "allow all")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Handle single-server toggle operations first
        enable_server = body.get("enable")
        disable_server = body.get("disable")

        if enable_server is not None:
            if enable_server not in config.servers:
                return JSONResponse({"error": f"Unknown server: {enable_server}"}, status_code=400)
            RUNTIME.sessions.enable(session_id, enable_server)
            await _notify_session_by_id(session_id)
            logger.info("REST: session %s enabled server %s", session_id, enable_server)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "action": "enabled",
                    "server": enable_server,
                    "disabled_servers": RUNTIME.sessions.disabled_snapshot(session_id),
                }
            )

        if disable_server is not None:
            if disable_server not in config.servers:
                return JSONResponse({"error": f"Unknown server: {disable_server}"}, status_code=400)
            RUNTIME.sessions.disable(session_id, disable_server)
            await _notify_session_by_id(session_id)
            logger.info("REST: session %s disabled server %s", session_id, disable_server)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "action": "disabled",
                    "server": disable_server,
                    "disabled_servers": RUNTIME.sessions.disabled_snapshot(session_id),
                }
            )

        # Bulk operations: allowed_servers or disabled_servers
        allowed = body.get("allowed_servers")
        disabled = body.get("disabled_servers")

        # If allowed_servers is provided, compute disabled as inverse
        if allowed is not None:
            if not isinstance(allowed, list):
                return JSONResponse({"error": "allowed_servers must be a list"}, status_code=400)
            allowed_set = set(allowed)
            # Disable all servers not in allowed list (except _combiner meta-server)
            disabled_list = [s for s in config.servers if s not in allowed_set and s != "_combiner"]
        elif disabled is None:
            disabled_list = []
        else:
            disabled_list = list(disabled) if isinstance(disabled, list) else []

        if not isinstance(disabled_list, list):
            return JSONResponse({"error": "disabled_servers must be a list"}, status_code=400)

        # Validate server names
        unknown = [s for s in disabled_list if s not in config.servers]
        if unknown:
            return JSONResponse({"error": f"Unknown servers: {unknown}"}, status_code=400)

        RUNTIME.sessions.set_disabled(session_id, set(disabled_list))

        # Notify the target session
        await _notify_session_by_id(session_id)

        logger.info(
            "REST: session %s filter set to disabled=%s",
            session_id,
            disabled_list,
        )
        return JSONResponse(
            {
                "session_id": session_id,
                "disabled_servers": RUNTIME.sessions.disabled_snapshot(session_id),
            }
        )

    @combiner.custom_route("/sessions/token/{token}", methods=["GET"])
    async def lookup_session_token(request: Request) -> JSONResponse:
        """Look up the combiner session_id associated with a token.

        The token is a UUID generated by the Neovim plugin per chat and
        embedded as the URL path suffix (/mcp/<token>).
        TokenRewriteMiddleware records the mapping when FastMCP assigns the
        session_id on the first initialize response.
        """
        token = request.path_params.get("token", "")
        session_id = RUNTIME.sessions.session_for_token(token)
        if session_id is None:
            return JSONResponse({"error": "token not found"}, status_code=404)
        logger.debug("Token lookup: %s -> %s", token, session_id)
        return JSONResponse({"token": token, "session_id": session_id})

    @combiner.custom_route("/sessions/token/{token}/filter", methods=["GET", "POST", "DELETE"])
    async def manage_token_filter(request: Request) -> JSONResponse:
        """Manage per-session server blocklist by token.

        The token is the stable identifier the Lua plugin holds for both ACP
        and HTTP adapter sessions.  If the token is already mapped to a
        session_id the operation is applied immediately; otherwise it is stored
        as pending and applied by TokenRewriteMiddleware when the client connects.

        GET:    Returns current or pending filter state.
        POST:   Same body format as /sessions/{session_id}/filter.
                If the session is not yet connected, stores as pending.
        DELETE: Clears filter (and any pending state).
        """
        token = request.path_params.get("token", "")
        if not token:
            return JSONResponse({"error": "token required"}, status_code=400)

        session_id = RUNTIME.sessions.session_for_token(token)

        if request.method == "GET":
            if session_id:
                disabled = RUNTIME.sessions.disabled.get(session_id, set())
                return JSONResponse(
                    {"token": token, "session_id": session_id, "disabled_servers": sorted(disabled)}
                )
            pending = RUNTIME.sessions.pending_for(token)
            return JSONResponse(
                {
                    "token": token,
                    "session_id": None,
                    "pending": True,
                    "disabled_servers": sorted(pending),
                }
            )

        if request.method == "DELETE":
            RUNTIME.sessions.set_pending(token, None)
            if session_id:
                removed = RUNTIME.sessions.clear_disabled(session_id)
                await _notify_session_by_id(session_id)
                return JSONResponse(
                    {
                        "token": token,
                        "session_id": session_id,
                        "action": "cleared",
                        "previously_disabled": sorted(removed) if removed else [],
                    }
                )
            return JSONResponse({"token": token, "session_id": None, "action": "cleared"})

        # POST — parse body (same format as /sessions/{id}/filter)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Resolve to a disabled set using the same logic as manage_session_filter
        enable_server = body.get("enable")
        disable_server = body.get("disable")
        allowed = body.get("allowed_servers")
        disabled_list = body.get("disabled_servers")

        def _resolve_disabled(current: set[str]) -> set[str] | None:
            """Return new disabled set or None to clear."""
            if enable_server is not None:
                current.discard(enable_server)
                return current if current else None
            if disable_server is not None:
                current.add(disable_server)
                return current
            if allowed is not None:
                allowed_set = set(allowed)
                d = {s for s in config.servers if s not in allowed_set and s != "_combiner"}
                return d if d else None
            if disabled_list is not None:
                return set(disabled_list) if disabled_list else None
            return current if current else None

        if session_id:
            # Session already connected — apply immediately
            current = set(RUNTIME.sessions.disabled.get(session_id, set()))
            new_disabled = _resolve_disabled(current)
            RUNTIME.sessions.set_disabled(session_id, new_disabled)
            await _notify_session_by_id(session_id)
            logger.info(
                "REST token filter: token=%s session=%s disabled=%s",
                token,
                session_id,
                RUNTIME.sessions.disabled_snapshot(session_id),
            )
            return JSONResponse(
                {
                    "token": token,
                    "session_id": session_id,
                    "disabled_servers": RUNTIME.sessions.disabled_snapshot(session_id),
                }
            )

        # Session not yet connected — store as pending
        current = set(RUNTIME.sessions.pending_for(token))
        new_disabled = _resolve_disabled(current)
        RUNTIME.sessions.set_pending(token, new_disabled)
        logger.info(
            "REST token filter (pending): token=%s disabled=%s",
            token,
            sorted(new_disabled) if new_disabled else [],
        )
        return JSONResponse(
            {
                "token": token,
                "session_id": None,
                "pending": True,
                "disabled_servers": sorted(new_disabled) if new_disabled else [],
            }
        )
