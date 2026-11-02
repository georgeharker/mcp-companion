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
from fastmcp.server.elicitation import AcceptedElicitation, parse_elicit_response_type
from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from mcp_combiner.config import PermissionAction, ResolvedPolicy
from mcp_combiner.runtime import RUNTIME

logger = logging.getLogger("mcp-combiner")

# Offered when a call resolves to ELICIT. "for session" is cached in the
# SessionRegistry so repeated calls to the same tool aren't re-prompted.
_ELICIT_OPTIONS = ["Allow once", "Allow for session", "Deny"]
# The 2026-era ask: one form elicitation, the same choices as ctx.elicit sends.
_CONSENT_INPUT = "consent"
_CONSENT_SCHEMA: dict[str, Any] = parse_elicit_response_type(_ELICIT_OPTIONS).schema


def _is_2026_request(ctx: Any) -> bool:
    """Whether *ctx*'s request speaks the 2026-07-28 revision (no server-initiated
    requests: consent must be asked with an ``InputRequiredResult``)."""
    rc = getattr(ctx, "request_context", None)
    return rc is not None and rc.protocol_version in MODERN_PROTOCOL_VERSIONS


def _consent_request(server: str, local_name: str) -> InputRequiredResult:
    """The 2026-era consent ask: the same question and choices as the handshake
    era's ``ctx.elicit``, returned as the call's result; the client retries the
    call with the answer. ``request_state`` names the tool asked about, so an
    answer can only apply to the call it was given for (fastmcp seals it on the
    wire, so it can't be forged)."""
    return InputRequiredResult(
        input_requests={
            _CONSENT_INPUT: ElicitRequest(
                params=ElicitRequestFormParams(
                    message=_consent_message(server, local_name),
                    requested_schema=_CONSENT_SCHEMA,
                )
            )
        },
        request_state=_consent_state(server, local_name),
    )


def _consent_answer(ctx: Any, server: str, local_name: str) -> str | None:
    """The decision (``"once" | "session" | "deny"``) a 2026-era retry carries
    for this tool, or ``None`` when the call hasn't been asked yet."""
    if ctx.request_state != _consent_state(server, local_name):
        return None
    responses = ctx.input_responses or {}
    answer = responses.get(_CONSENT_INPUT)
    if not isinstance(answer, ElicitResult):
        return None
    if answer.action != "accept":
        return "deny"
    return _decision_from_choice((answer.content or {}).get("value"))


def _consent_message(server: str, local_name: str) -> str:
    return f"Allow '{server}' to run tool '{local_name}'?"


def _consent_state(server: str, local_name: str) -> str:
    return f"consent:{server}/{local_name}"


def _decision_from_choice(choice: object) -> str:
    if choice == "Allow for session":
        return "session"
    if choice == "Allow once":
        return "once"
    return "deny"


async def enforce(
    ctx: Any, server: str, local_name: str, policy: ResolvedPolicy
) -> InputRequiredResult | None:
    """Apply *policy* to one tool call for ``(server, local_name)``.

    ``ctx`` is the FastMCP request context (``MiddlewareContext.fastmcp_context``).
    Returns ``None`` to allow the call; raises :class:`ToolError` to reject it
    (deny, or a declined/failed elicitation). On a 2026-era request it returns
    an ``InputRequiredResult`` asking for consent — the caller returns that as
    the call's result, and the client's retry carries the answer.  An ELICIT decision prompts the
    user unless it was already granted for this session; a client that cannot
    elicit falls back per ``policy.elicit_unavailable``.
    """
    action = policy.resolve(local_name)
    if action is PermissionAction.ALLOW:
        return None

    tool_name = f"{server}_{local_name}"
    if action is PermissionAction.DENY:
        raise ToolError(f"Tool '{tool_name}' is denied by the combiner permission policy.")

    # ELICIT — ask the user (once per session unless already granted). Grants are
    # keyed by the CHAT TOKEN when the session carries one (the handover carries
    # them across combiner restarts: "allow for session" means my CHAT, not the
    # transport lifetime); tokenless sessions key by sid (dies with the session,
    # which is correct: there is no durable identity to inherit).
    sid = ctx.session_id
    key = f"{server}/{local_name}"
    from mcp_combiner import nvim_proxy

    ident = (nvim_proxy.token_for_session(sid) if sid else None) or sid
    if ident and RUNTIME.sessions.is_granted(ident, key):
        logger.info("consent: grant hit ident=%s key=%s (no ask)", ident, key)
        return None

    if _is_2026_request(ctx):
        answer = _consent_answer(ctx, server, local_name)
        if answer is None:
            return _consent_request(server, local_name)
        decision = answer
    else:
        decision = await _elicit(ctx, server, local_name, policy)
    if decision == "deny":
        raise ToolError(f"Tool '{tool_name}' was denied by the user.")
    if decision == "session" and ident:
        RUNTIME.sessions.grant(ident, key)
        logger.info("consent: grant RECORDED ident=%s key=%s", ident, key)
    # "once" → allow just this call, without caching.
    return None


async def _elicit(ctx: Any, server: str, local_name: str, policy: ResolvedPolicy) -> str:
    """Prompt the user to approve a call. Returns ``"once" | "session" | "deny"``.

    Any elicitation failure (client lacks the capability, timeout, transport
    error) applies the configured ``elicit_unavailable`` fallback instead of
    hanging on a prompt no one will answer.
    """
    message = _consent_message(server, local_name)
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
        return _decision_from_choice(result.data)
    # DeclinedElicitation / CancelledElicitation
    return "deny"
