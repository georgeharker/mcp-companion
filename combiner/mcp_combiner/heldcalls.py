"""Held calls: a 2026-era client in front of a handshake-era upstream.

A handshake-era upstream asks for input mid-call (``elicitation/create``) and
blocks until it is answered. A 2026-07-28 client cannot receive a
server-initiated request: it is asked by returning an ``InputRequiredResult``
and answers by retrying the call with ``inputResponses``. The combiner bridges
the two by *holding* the upstream call open across the client's requests:

1. ``ToolProcessingMiddleware`` runs the upstream call as a task, under a
   :class:`HeldCall` registered for the chat token and the request id.
2. The upstream's elicitation reaches :func:`bridged_elicitation_handler` (the
   proxy client's handler, with the original request's context restored). For
   a 2026-era request it parks the question on the held call and waits.
3. The middleware sees the question before the task finishes and returns it to
   the client as an ``InputRequiredResult`` whose ``request_state`` names the
   held call (fastmcp seals ``request_state`` on the wire).
4. The client's retry carries the answer; the middleware hands it to the
   waiting handler and waits again — for the result, or another question.

A held call belongs to the chat token that started it: a retry from another
token cannot resume it. It is cancelled after :data:`HOLD_TIMEOUT_S` (60 min by
default, ``MCP_COMBINER_HOLD_TIMEOUT``) or when its client never comes back. Held
calls do not survive a combiner restart; a retry then gets a clear "call the
tool again". Handshake-era requests never touch this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastmcp.client.elicitation import create_elicitation_callback
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.server.providers.proxy import (
    ProxyClient,
    _make_restoring_handler,
    default_proxy_elicitation_handler,
)
from fastmcp.tools import ToolResult
from fastmcp.tools.base import InputRequiredToolResult
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestParams,
    ElicitResult,
    InputRequiredResult,
    InputResponses,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

logger = logging.getLogger("mcp-combiner")


def _hold_timeout() -> float:
    try:
        return float(os.environ["MCP_COMBINER_HOLD_TIMEOUT"])
    except (KeyError, ValueError):
        return 3600.0


#: How long a held call waits for its client to come back with an answer.
HOLD_TIMEOUT_S = _hold_timeout()

STATE_PREFIX = "held:"
_TOKEN_HEADER = "x-mcp-combiner-session"


@dataclass
class _Ask:
    """One question the upstream is blocked on."""

    key: str
    request: ElicitRequest
    answer: asyncio.Future[ElicitResult]


@dataclass
class HeldCall:
    """An upstream call kept running across a 2026-era client's requests."""

    token: str | None
    origin_request_id: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task: asyncio.Task[ToolResult] | None = None
    asks: asyncio.Queue[_Ask] = field(default_factory=asyncio.Queue)
    pending: dict[str, _Ask] = field(default_factory=dict)
    expiry: asyncio.TimerHandle | None = None


_by_id: dict[str, HeldCall] = {}
_by_origin: dict[tuple[str | None, str], HeldCall] = {}


def _register(held: HeldCall) -> None:
    _by_id[held.id] = held
    _by_origin[(held.token, held.origin_request_id)] = held
    held.expiry = asyncio.get_running_loop().call_later(HOLD_TIMEOUT_S, _expire, held.id)


def _forget(held: HeldCall) -> None:
    _by_id.pop(held.id, None)
    if _by_origin.get((held.token, held.origin_request_id)) is held:
        del _by_origin[(held.token, held.origin_request_id)]
    if held.expiry is not None:
        held.expiry.cancel()
    for ask in held.pending.values():
        if not ask.answer.done():
            ask.answer.cancel()


def _expire(held_id: str) -> None:
    held = _by_id.get(held_id)
    if held is None:
        return
    logger.info("held call %s expired waiting for its client; cancelling", held_id[:8])
    _forget(held)
    if held.task is not None and not held.task.done():
        held.task.cancel()


def reset() -> None:
    """Cancel every held call (tests; combiner shutdown)."""
    for held in list(_by_id.values()):
        _forget(held)
        if held.task is not None and not held.task.done():
            held.task.cancel()


def resumes(request_state: str | None) -> bool:
    """Whether a request is the retry of a held call (skip the first-leg gates)."""
    return request_state is not None and request_state.startswith(STATE_PREFIX)


async def start(
    run: Callable[[], Awaitable[ToolResult]], token: str | None, request_id: str
) -> ToolResult:
    """Run an upstream call for a 2026-era request, holding it if it asks."""
    held = HeldCall(token=token, origin_request_id=request_id)
    _register(held)

    async def _run() -> ToolResult:
        return await run()

    held.task = asyncio.create_task(_run(), name=f"held-call-{held.id[:8]}")
    return await _await_outcome(held)


async def resume(
    request_state: str, token: str | None, responses: InputResponses | None
) -> ToolResult:
    """Continue a held call from its client's retry."""
    held = _by_id.get(request_state.removeprefix(STATE_PREFIX))
    if held is None or held.token != token:
        raise ToolError(
            "This call was paused waiting for input and is no longer available "
            "(it expired, or the combiner restarted). Call the tool again."
        )
    answered = responses or {}
    for key, ask in list(held.pending.items()):
        response = answered.get(key)
        if not ask.answer.done():
            ask.answer.set_result(
                response if isinstance(response, ElicitResult) else ElicitResult(action="cancel")
            )
        del held.pending[key]
    return await _await_outcome(held)


async def _await_outcome(held: HeldCall) -> ToolResult:
    """The call's result, or an InputRequiredResult for its next question."""
    task = held.task
    if task is None:
        raise RuntimeError("held call has no task (start() sets it before waiting)")
    next_ask = asyncio.ensure_future(held.asks.get())
    try:
        done, _ = await asyncio.wait({task, next_ask}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not next_ask.done():
            next_ask.cancel()
    if next_ask in done and not task.done():
        ask = next_ask.result()
        held.pending[ask.key] = ask
        return InputRequiredToolResult(
            InputRequiredResult(
                input_requests={ask.key: ask.request},
                request_state=STATE_PREFIX + held.id,
            )
        )
    if next_ask in done:  # both finished: the question is moot
        ask = next_ask.result()
        if not ask.answer.done():
            ask.answer.cancel()
    _forget(held)
    return task.result()  # re-raises the upstream call's own exception


async def bridged_elicitation_handler(
    message: str,
    response_type: type,
    params: ElicitRequestParams,
    context: Any,
) -> ElicitResult:
    """The proxy client's elicitation handler (with the downstream request's
    context restored): 2026-era requests park the question on their held call;
    handshake-era requests forward it live, as fastmcp does by default."""
    held = _held_for_current_request()
    if held is None:
        return await default_proxy_elicitation_handler(message, response_type, params, context)
    requested_schema = (
        params.requested_schema
        if isinstance(params, ElicitRequestFormParams)
        else {"type": "object", "properties": {}}
    )
    ask = _Ask(
        key=f"elicit-{uuid.uuid4().hex[:8]}",
        request=ElicitRequest(
            params=ElicitRequestFormParams(message=message, requested_schema=requested_schema)
        ),
        answer=asyncio.get_running_loop().create_future(),
    )
    await held.asks.put(ask)
    try:
        return await ask.answer
    except asyncio.CancelledError:
        # Expired or abandoned: tell the upstream the user didn't answer.
        return ElicitResult(action="cancel")


def _held_for_current_request() -> HeldCall | None:
    try:
        rc = get_context().request_context
    except Exception:
        return None
    if rc is None or rc.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        return None
    if rc.request_id is None:
        return None
    token = rc.request.headers.get(_TOKEN_HEADER) if rc.request is not None else None
    return _by_origin.get((token or None, str(rc.request_id)))


def bind_bridged_elicitation(client: ProxyClient[Any]) -> None:
    """Route *client*'s upstream elicitations through :func:`bridged_elicitation_handler`.

    Only when fastmcp installed its own default forwarding handler (a caller
    that passed an explicit ``elicitation_handler`` keeps it). Call after
    construction AND from ``_bind_restoring_handlers`` overrides, since a
    per-chat ``Client.new()`` clone rebinds the defaults.
    """
    if "elicitation_handler" not in client._proxy_restoring_handler_keys:
        return
    client._session_kwargs["elicitation_callback"] = create_elicitation_callback(
        _make_restoring_handler(bridged_elicitation_handler, client._proxy_rc_ref)
    )
