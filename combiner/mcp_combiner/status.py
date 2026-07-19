"""Single source of truth for a server's status snapshot.

``build_server_status`` feeds BOTH the ``/health`` endpoint (→ MCPStatus in
Neovim) and the ``combiner__status`` meta-tool (→ agent self-reporting), so
the two views can never diverge. Moved verbatim from server.py.
"""

from __future__ import annotations

import logging

from mcp_combiner.config import CombinerConfig, ServerStatusInfo
from mcp_combiner.connections import ConnectionManager
from mcp_combiner.runtime import RUNTIME

logger = logging.getLogger("mcp-combiner")


def build_server_status(
    config: CombinerConfig,
    conn_manager: ConnectionManager | None,
    name: str,
) -> ServerStatusInfo:
    """Single source of truth for a server's status snapshot + lifecycle state.

    Used by BOTH the ``/health`` endpoint (→ MCPStatus in Neovim) and the
    ``combiner__status`` meta-tool (→ self-reporting to the agent), so the two
    never diverge. Config fields come from ``CombinerConfig``; the runtime
    ``state`` is overlaid from the ``ConnectionManager`` for HTTP servers, or
    inferred for stdio/local servers (mounted ⇒ available ⇒ ``ready``).
    """
    info = config.get_server_status(name)
    srv = config.servers[name]
    failed = name in RUNTIME.tools.failed_servers
    if srv.disabled:
        state = "disabled"
    elif conn_manager is not None and conn_manager.has_connection(name):
        state = conn_manager.lifecycle_state(name)
        # A recorded call failure (transport/process death) overrides an
        # optimistic connection-derived "ready"/"connected".
        if failed and state in ("ready", "connected"):
            state = "disconnected"
    elif failed:
        # stdio / non-connection-managed server whose last call failed — its
        # subprocess has crashed. No connection lifecycle tracks this, so the
        # failure mark is the down signal until a successful call clears it.
        state = "disconnected"
    elif name in RUNTIME.tools.server_tools:
        # stdio / non-connection-managed server with a confirmed slice — a
        # real tools/list answered (seen in a fetch, or a priming list
        # succeeded). A warming server that only answered empty stores
        # nothing, so it deliberately reads "connected", never green-ready
        # with no tools.
        state = "ready"
    else:
        # Mounted/started but tools not yet confirmed — the same "connected"
        # (warming) rung of the tri-state HTTP servers get before "ready". We do
        # NOT assume ready just because it is mounted.
        state = "connected"
    return info.model_copy(update={"state": state})
