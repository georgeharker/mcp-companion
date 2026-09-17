"""Stage 2: the in-flight tool-call holder.

When a proxied tool result references an interactive resource (a `ui://` URI in
the result's `ui.resourceUri` meta), the upstream's intent is "render my output
interactively". Instead of returning immediately, the holder:

1. creates/attaches the widget UiSession for the chat token,
2. pushes the tool input + result to the session's /events SSE (the data the
   widget renders),
3. notifies the connected client (progress when a progressToken was supplied,
   a log notification otherwise) with the widget UI URL,
4. awaits widget completion (`done` event from /proxy/ui/complete) or timeout,
5. resolves the agent's call with the recorded widget state folded into the
   result — the UI URL always surfaces in the text so the model can reference
   it, and the retrieval loop (`combiner__ui_messages`) sees everything.

Design: docs/designs/interactive-resource-host.md#Stage 2 — tool-triggered.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("mcp_combiner.ui_host")


def extract_ui_uri(result: object) -> str | None:
    """Pull the widget resource URI out of a tool result: result.meta.ui.resourceUri
    (the mcp-app tool-triggered shape), tolerating structuredContent._meta."""
    meta = getattr(result, "meta", None)
    if not isinstance(meta, dict):
        structured = getattr(result, "structured_content", None) or getattr(
            result, "structuredContent", None
        )
        meta = getattr(structured, "_meta", None) if structured is not None else None
    if not isinstance(meta, dict):
        return None
    ui = meta.get("ui")
    if not isinstance(ui, dict):
        return None
    uri = ui.get("resourceUri")
    if isinstance(uri, str) and uri.startswith("ui://"):
        return uri
    return None


def _result_content_list(result: object) -> list[Any]:
    return list(getattr(result, "content", None) or [])


def _fold_text(result: object, text: str) -> object:
    """Return a copy of the result with one extra text block appended (the
    widget summary + UI URL)."""
    import mcp.types as mt
    from fastmcp.tools.tool import ToolResult

    block = mt.TextContent(type="text", text=text)
    content = _result_content_list(result) + [block]
    kwargs: dict[str, Any] = {"content": content}
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if structured is not None:
        kwargs["structured_content"] = structured
    meta = getattr(result, "meta", None)
    if meta is not None:
        kwargs["meta"] = meta
    return ToolResult(**kwargs)


async def hold_for_widget(
    context: Any, result: object, tool_name: str, token: str | None
) -> object:
    """Run the Stage 2 hold flow for a result referencing an interactive resource.
    Returns the (possibly folded) result; never raises."""
    from mcp_combiner.runtime import RUNTIME

    host = RUNTIME.ui_host
    if host is None or token is None:
        return result
    uri = extract_ui_uri(result)
    if not uri:
        return result

    from .routes import _tool_result_payload

    # A completed session from an earlier hold must not short-circuit this one
    # (its done event is already set) — start fresh.
    stale = host.registry.get(token, uri)
    if stale is not None and stale.completed is not None:
        await host.registry.close_session(stale)
    session = host.registry.get_or_create(token, uri)
    session.publish("tool-input", {"name": tool_name, "arguments": {}})
    session.publish("tool-result", _tool_result_payload(result))

    url = f"{host.config.base_origin}/ui/{token}/?resource={quote(uri, safe='')}"
    ctx = getattr(context, "fastmcp_context", None)
    if ctx is not None:
        try:
            # Logging notification — clients that surface MCP logs show the URL
            # even without a progressToken; the folded result text carries it too.
            await ctx.info(f"Interactive UI ready: {url}")
        except Exception:  # noqa: BLE001 — notification failures never fail the call
            logger.debug("ui hold: client notification failed", exc_info=True)

    logger.info("ui hold: %s/%s held for widget %s", token, tool_name, uri)
    try:
        await asyncio.wait_for(session.done.wait(), timeout=host.hold_timeout)
    except asyncio.TimeoutError:
        session.mark_completed("timeout")

    summary = (
        f"Interactive UI session for {uri}: completed={session.completed or 'open'}; "
        f"recorded messages={len(session.messages)}, contexts={len(session.contexts)}, "
        f"intents={len(session.intents)}. UI URL: {url} "
        f"(retrieve with combiner__ui_messages chat_id={token!r})."
    )
    # NOT evicted: the session lingers (completed) until the TTL so the
    # retrieval loop (combiner__ui_messages) can still read what the user did.
    return _fold_text(result, summary)
