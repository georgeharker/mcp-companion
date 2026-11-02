"""The MCP client session every upstream connection uses.

mcp 2's ``ClientSession`` validates each result against the negotiated
protocol revision's wire models before returning it, and a ``tools/list`` with
one non-conformant tool schema (a property given as ``[]``, a non-object root)
fails as a whole — the server would show no tools at all. fastmcp 3 handed the
combiner the raw list and ``schemafix`` repaired it; this session keeps that
tolerance by repairing ``tools/list`` results *before* the SDK validates them,
with the same normalizer the combiner applies to what it publishes
(:func:`~mcp_combiner.schema.wire_conformant_schema`).

It extends fastmcp's proxy session (which relays results without enforcing the
backend's output schema — the end client validates what it consumes), and is
installed on every upstream client at its point of use
(:func:`use_tolerant_session`): fastmcp's proxy factories set their own session
class when they build or clone a client, so construction-time options don't
survive.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any

from fastmcp import Client
from fastmcp.client.transports.base import TransportOptions
from fastmcp.server.providers.proxy import _ForwardingClientSession
from mcp.shared.dispatcher import CallOptions

from mcp_combiner.schema import wire_conformant_schema

logger = logging.getLogger("mcp-combiner")

_SCHEMA_KEYS = ("inputSchema", "outputSchema")

# Tools already reported as repaired (one warning per tool, not per listing).
_reported: set[str] = set()


def repair_tool_listing(raw: dict[str, Any]) -> None:
    """Make every tool schema in a raw ``tools/list`` result wire-conformant, in place."""
    tools = raw.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name"))
        if "inputSchema" not in tool:
            tool["inputSchema"] = {}
        for key in _SCHEMA_KEYS:
            if tool.get(key) is None:
                continue  # outputSchema is optional; inputSchema was defaulted above
            fixed = wire_conformant_schema(tool[key])
            if fixed is tool[key]:
                continue
            tool[key] = fixed
            report_key = f"{name}:{key}"
            if report_key not in _reported:
                _reported.add(report_key)
                logger.warning(
                    "Upstream tool '%s' published a non-conformant %s; repaired it", name, key
                )


_SendRaw = Callable[[str, Mapping[str, Any] | None, CallOptions | None], Awaitable[dict[str, Any]]]


def _repairing(inner: _SendRaw) -> _SendRaw:
    """Wrap a dispatcher's ``send_raw_request`` to repair ``tools/list`` results."""

    async def send_raw_request(
        method: str, params: Mapping[str, Any] | None, opts: CallOptions | None = None
    ) -> dict[str, Any]:
        raw = await inner(method, params, opts)
        if method == "tools/list":
            repair_tool_listing(raw)
        return raw

    return send_raw_request


class TolerantClientSession(_ForwardingClientSession):
    """fastmcp's proxy session, with ``tools/list`` repaired before validation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The SDK validates inside send_request, between the dispatcher
        # returning the raw result and parsing it; the dispatcher is this
        # session's own (built in __init__), so wrapping its one entry point
        # is the narrowest place to repair.
        setattr(self._dispatcher, "send_raw_request", _repairing(self._dispatcher.send_raw_request))


def use_tolerant_session(client: Client[Any]) -> Client[Any]:
    """Install :class:`TolerantClientSession` on *client* (idempotent; before connect)."""
    options = client._transport_options or TransportOptions()
    if options.session_class is not TolerantClientSession:
        client._transport_options = replace(options, session_class=TolerantClientSession)
    return client
