"""Tool-call permission enforcement (allow / deny / elicit).

The policy *models* — :class:`~mcp_combiner.config.PermissionPolicy`,
:class:`~mcp_combiner.config.ResolvedPolicy`,
:class:`~mcp_combiner.config.PermissionAction` — live in ``config.py``; they are
pure config with no runtime dependencies.  This module is the runtime
enforcement half: given the resolved policy for a call, allow it, deny it
(``ToolError``), or elicit the user's decision (cached per session in the
:class:`~mcp_combiner.runtime.SessionRegistry`).

It is deliberately a module of functions, not a class — there is no per-instance
state to hold (session grants live in ``RUNTIME.sessions``).  Kept out of
``ToolProcessingMiddleware`` so the request path stays lean and the permission
logic is cohesive and independently testable.

Gates tool **calls** only — never tool publication.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation

from mcp_combiner.config import PermissionAction, ResolvedPolicy
from mcp_combiner.runtime import RUNTIME

logger = logging.getLogger("mcp-combiner")

# Offered when a call resolves to ELICIT. "for session" is cached in the
# SessionRegistry so repeated calls to the same tool aren't re-prompted.
_ELICIT_OPTIONS = ["Allow once", "Allow for session", "Deny"]


async def enforce(ctx: Any, server: str, local_name: str, policy: ResolvedPolicy) -> None:
    """Apply *policy* to one tool call for ``(server, local_name)``.

    ``ctx`` is the FastMCP request context (``MiddlewareContext.fastmcp_context``).
    Returns normally to allow the call; raises :class:`ToolError` to reject it
    (deny, or a declined/failed elicitation).  An ELICIT decision prompts the
    user unless it was already granted for this session; a client that cannot
    elicit falls back per ``policy.elicit_unavailable``.
    """
    action = policy.resolve(local_name)
    if action is PermissionAction.ALLOW:
        return

    tool_name = f"{server}_{local_name}"
    if action is PermissionAction.DENY:
        raise ToolError(f"Tool '{tool_name}' is denied by the combiner permission policy.")

    # ELICIT — ask the user (once per session unless already granted).
    sid = ctx.session_id
    key = f"{server}/{local_name}"
    if sid and RUNTIME.sessions.is_granted(sid, key):
        return

    decision = await _elicit(ctx, server, local_name, policy)
    if decision == "deny":
        raise ToolError(f"Tool '{tool_name}' was denied by the user.")
    if decision == "session" and sid:
        RUNTIME.sessions.grant(sid, key)
    # "once" → allow just this call, without caching.


async def _elicit(ctx: Any, server: str, local_name: str, policy: ResolvedPolicy) -> str:
    """Prompt the user to approve a call. Returns ``"once" | "session" | "deny"``.

    Any elicitation failure (client lacks the capability, timeout, transport
    error) applies the configured ``elicit_unavailable`` fallback instead of
    hanging on a prompt no one will answer.
    """
    message = f"Allow '{server}' to run tool '{local_name}'?"
    try:
        result = await ctx.elicit(message, response_type=_ELICIT_OPTIONS)
    except Exception as exc:  # noqa: BLE001 - any failure → configured fallback
        fallback = policy.elicit_unavailable
        logger.warning(
            "Elicitation unavailable for '%s/%s' (%s); applying elicitUnavailable=%s",
            server,
            local_name,
            exc,
            fallback.value,
        )
        return "once" if fallback is PermissionAction.ALLOW else "deny"

    if isinstance(result, AcceptedElicitation):
        choice = result.data
        if choice == "Allow for session":
            return "session"
        if choice == "Allow once":
            return "once"
        return "deny"
    # DeclinedElicitation / CancelledElicitation
    return "deny"
