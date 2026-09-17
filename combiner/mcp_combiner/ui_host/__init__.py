"""Interactive resource host (mcp-app widgets) — Stage 1.

Serves the host page + sandbox relay + widget proxy routes on the combiner's
own app, so ANY client (Pi, Claude Code, OpenCode, Neovim) gets interactive
mcp-app resources by opening ``<combiner>/ui/<token>/?resource=<uri>``.

Widget tool calls and resource reads are attributed to the chat's grouping
token by talking to the combiner's own ``/mcp/<token>`` endpoint as a real MCP
client — the full middleware pipeline (permissions gate, per-chat isolation,
token filters) applies to widget-initiated calls exactly like agent-initiated
ones.

Lifted from pi-mcp-adapter's ui-server/ui-session/templates (MIT,
© 2026 Nico Bailon); Stage 1 scope: no in-flight tool holding, no streaming
envelopes, no consent persistence, no cross-restart recovery.
"""

from __future__ import annotations

from fastmcp import FastMCP
from starlette.applications import Starlette

from .routes import UiHost, UiHostConfig
from .sessions import UiSessionRegistry

__all__ = ["UiHost", "UiHostConfig", "UiSessionRegistry"]


def attach_ui_host(
    combiner: FastMCP, base_origin: str, sandbox_relay_port: int, hold_timeout: float = 50.0
) -> UiHost:
    """Attach the /ui/{token}/... route family to the combiner's app."""
    host = UiHost(
        UiHostConfig(
            base_origin=base_origin,
            sandbox_relay_port=sandbox_relay_port,
            hold_timeout=hold_timeout,
        )
    )
    host.attach(combiner)

    from mcp_combiner.runtime import RUNTIME

    RUNTIME.ui_host = host
    return host


def get_relay_app(options: object) -> Starlette:
    """The second-origin app for the sandbox relay (bind on port+1 in a daemon
    thread — see __main__). Reads the combiner's port from ServeOptions-shaped
    options to stay consistent with the host page's origin arithmetic."""
    from mcp_combiner.runtime import RUNTIME

    host = RUNTIME.ui_host
    if host is None:
        raise RuntimeError("UI host not attached — create_app never ran?")
    relay: Starlette = host.build_relay_app()
    return relay
