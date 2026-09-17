"""UI-host session registry for mcp-app interactive resources.

One UiSession per (chat token, resource URI): holds the loopback MCP client that
talks to the combiner's own ``/mcp/<token>`` endpoint — so widget-initiated tool
calls flow through the full middleware pipeline (permissions gate, per-chat
isolation, token filters) exactly like agent-initiated ones — plus the recorded
facts the widget produces (messages, contexts, intents) for later retrieval.

Lifted from pi-mcp-adapter's ui-session.ts lifecycle (MIT, © 2026 Nico Bailon),
reduced to Stage 1: no in-flight tool holding, no streaming envelopes, no
cross-restart recovery.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("mcp_combiner.ui_host")

SESSION_TTL_SECONDS = 3600.0
"""Idle TTL: a widget session with no heartbeat for this long is expired on access."""

MAX_RECORDED = 200
"""Per-category cap on recorded facts (messages / contexts / intents)."""


@dataclass
class UiSession:
    token: str
    resource_uri: str
    session_token: str = field(default_factory=lambda: uuid.uuid4().hex, init=False)
    created: float = field(default_factory=time.time, init=False)
    last_seen: float = field(default_factory=time.time, init=False)
    completed: str | None = None
    # Widget tool-call consent: "unset" (prompt on next call) / "granted" / "denied".
    consent: str = "unset"
    messages: list[dict[str, Any]] = field(default_factory=list, init=False)
    contexts: list[dict[str, Any]] = field(default_factory=list, init=False)
    intents: list[dict[str, Any]] = field(default_factory=list, init=False)
    # Stage 2: widget-bound SSE events (tool-input / tool-result / tool-cancelled)
    # consumed by /events; an asyncio.Event the holder awaits for completion.
    events: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=64), init=False
    )
    done: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def publish(self, event: str, data: Any) -> None:
        """Queue an SSE event for this session's /events consumer (bounded)."""
        if self.events.qsize() < 64:
            self.events.put_nowait({"event": event, "data": data})

    def mark_completed(self, reason: str) -> None:
        """Signal completion WITHOUT evicting — the holder (if any) resolves,
        folds state, and evicts afterwards."""
        if self.completed is None:
            self.completed = reason
        self.done.set()

    def touch(self) -> None:
        self.last_seen = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_seen

    def record(self, bucket: list[dict[str, Any]], entry: dict[str, Any]) -> None:
        self.touch()
        bucket.append(entry)
        del bucket[:-MAX_RECORDED]


def resource_contents(result: object) -> list[Any]:
    """Normalize a read_resource result to its contents list. FastMCP's client
    returns a plain list of Text/BlobResourceContents; tolerate an object with
    .contents too (defensive against SDK shape changes)."""
    if isinstance(result, (list, tuple)):
        return list(result)
    return list(getattr(result, "contents", None) or [])


def _loopback_headers() -> dict[str, str]:
    """Headers for the UI host's loopback MCP clients: the inbound bearer when
    the endpoint is locked down. Without it every widget action 401s — the
    auth middleware does not exempt the combiner's own loopback traffic."""
    from mcp_combiner.runtime import RUNTIME

    tok = RUNTIME.inbound_auth_token
    return {"Authorization": f"Bearer {tok}"} if tok else {}


async def call_as_token(
    base_origin: str, token: str, tool: str, arguments: dict[str, Any] | None
) -> object:
    """Run a tools/call through the combiner's own /mcp/<token> endpoint as a real
    MCP client — the full middleware pipeline (permissions gate, per-chat
    isolation, token filters) applies to widget-initiated calls exactly like
    agent-initiated ones. One loopback connection per action; no private APIs."""
    from fastmcp.client import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async with Client(
        transport=StreamableHttpTransport(
            url=f"{base_origin}/mcp/{token}", headers=_loopback_headers()
        )
    ) as client:
        return await client.call_tool(tool, arguments or {})


async def read_as_token(base_origin: str, token: str, resource_uri: str) -> object:
    """Run a resources/read through the combiner's own /mcp/<token> endpoint —
    same attribution path as widget tool calls."""
    from fastmcp.client import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async with Client(
        transport=StreamableHttpTransport(
            url=f"{base_origin}/mcp/{token}", headers=_loopback_headers()
        )
    ) as client:
        return await client.read_resource(resource_uri)


class UiSessionRegistry:
    """Token-keyed UI sessions with lazy expiry (expired on access, like the
    combiner's parked-session tiers — no background sweeper needed)."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], UiSession] = {}

    def get_or_create(self, token: str, resource_uri: str) -> UiSession:
        key = (token, resource_uri)
        session = self._sessions.get(key)
        if session is not None and session.idle_seconds > SESSION_TTL_SECONDS:
            # Expired: drop (without terminating the underlying client — the
            # upstream sessions park per the combiner's usual semantics).
            self._sessions.pop(key, None)
            session = None
        if session is None:
            session = UiSession(token=token, resource_uri=resource_uri)
            self._sessions[key] = session
        session.touch()
        return session

    def get(self, token: str, resource_uri: str | None = None) -> UiSession | None:
        if resource_uri is not None:
            session = self._sessions.get((token, resource_uri))
        else:
            matches = [s for (t, _), s in self._sessions.items() if t == token]
            session = matches[0] if len(matches) == 1 else None
        if session is None:
            return None
        if session.idle_seconds > SESSION_TTL_SECONDS:
            self._sessions.pop((session.token, session.resource_uri), None)
            return None
        return session

    async def close_session(self, session: UiSession) -> None:
        # The session holds no connection anymore (per-request clients), so this
        # just evicts it from the registry.
        self._sessions.pop((session.token, session.resource_uri), None)

    def for_token(self, token: str) -> list[UiSession]:
        """All live (non-expired) sessions for a chat token, creation order."""
        sessions = [s for (t, _), s in self._sessions.items() if t == token]
        return [s for s in sessions if s.idle_seconds <= SESSION_TTL_SECONDS]

    def summary(self, token: str) -> dict[str, Any]:
        sessions = [s for (t, _), s in self._sessions.items() if t == token]
        return {
            "open": len(sessions),
            "sessions": [
                {
                    "resource": s.resource_uri,
                    "idle_seconds": round(s.idle_seconds, 1),
                    "completed": s.completed,
                    "messages": len(s.messages),
                    "contexts": len(s.contexts),
                    "intents": len(s.intents),
                }
                for s in sessions
            ],
        }


# refresh
