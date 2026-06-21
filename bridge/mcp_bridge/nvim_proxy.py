"""Virtual `neovim` MCP server: advertise `neovim_*` tools and route calls back
into live editors.

Factored out of ``server.py`` for clarity. The bridge's core proxy/middleware
calls a handful of entry points here:

* ``record_session_token`` — from ToolProcessingMiddleware.on_request, builds the
  session_id -> token reverse map used for routing.
* ``append_nvim_tools`` — from on_list_tools, injects the neovim tool catalog.
* ``is_nvim_tool`` / ``call_nvim_tool`` — from on_call_tool, intercepts and routes
  ``neovim_*`` calls over the channel instead of the upstream proxy.
* ``register_routes`` — from create_bridge, mounts /neovim/instances and /neovim/bind.

The actual back-channel (sockets, per-instance queues) lives in
``nvim_channel.NvimChannelManager``; this module is the MCP-facing glue +
session/token/instance routing tables.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import Tool
from fastmcp.tools.tool import ToolResult
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_bridge.nvim_channel import NoInstanceError, NvimChannelManager

logger = logging.getLogger("mcp-bridge")

# The virtual native server name and its tool-name prefix (FastMCP-style).
_NVIM_SERVER = "neovim"
_NVIM_PREFIX = "neovim_"

# Routing argument injected into every neovim tool so a caller can choose which
# editor to target. Stripped by call_nvim_tool before dispatch.
_NVIM_INSTANCE_ARG = "nvim_instance"
_NVIM_INSTANCE_DESC = (
    "Which Neovim instance to target (instance_id). Optional: defaults to the editor "
    "bound to this chat, or the only connected one. Call neovim_list_instances to see ids."
)

# --- Routing tables ---
# session_id -> token: keyed by context.fastmcp_context.session_id (the id the
# tool middleware sees), recovered from the X-MCP-Bridge-Session header.
_session_tokens: dict[str, str] = {}
# token -> instance_id: a chat (token) is bound to one editor via POST /neovim/bind.
_token_instances: dict[str, str] = {}
# The back-channel into live Neovim instances (lazily created).
_nvim_channel: NvimChannelManager | None = None


def get_nvim_channel() -> NvimChannelManager:
    """Return the process-wide Neovim channel manager (created on first use)."""
    global _nvim_channel
    if _nvim_channel is None:
        _nvim_channel = NvimChannelManager()
    return _nvim_channel


def is_nvim_tool(name: str) -> bool:
    """Whether a tool name is one of the virtual `neovim_*` tools."""
    return name.startswith(_NVIM_PREFIX)


def record_session_token(session_id: str | None) -> None:
    """Build the session_id -> token reverse map for neovim_* routing.

    Keyed by context.fastmcp_context.session_id (the id the tool middleware
    sees), which differs from the transport's mcp-session-id header. The token
    arrives via the X-MCP-Bridge-Session header — sent by the client directly,
    or injected from the /mcp/<token> URL by TokenRewriteMiddleware.
    """
    if not session_id or session_id in _session_tokens:
        return
    from fastmcp.server.dependencies import get_http_headers

    headers = get_http_headers()
    token = headers.get("x-mcp-bridge-session")
    if token:
        _session_tokens[session_id] = token


def _instance_for_session(session_id: str | None) -> str | None:
    """Resolve session_id -> token -> instance_id, if bound to a live instance."""
    if not session_id:
        return None
    token = _session_tokens.get(session_id)
    if token is None:
        return None
    instance_id = _token_instances.get(token)
    if instance_id is None:
        return None
    if _nvim_channel is None or not _nvim_channel.has_instance(instance_id):
        return None
    return instance_id


def _inject_instance_arg(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an inputSchema with the optional nvim_instance arg added."""
    out = dict(params) if isinstance(params, dict) else {"type": "object"}
    props = dict(out.get("properties") or {})
    props[_NVIM_INSTANCE_ARG] = {"type": "string", "description": _NVIM_INSTANCE_DESC}
    out["properties"] = props
    out.setdefault("type", "object")
    return out


def _build_nvim_tools(manifest: dict[str, Any]) -> list[Tool]:
    """Build virtual FastMCP Tool objects from the frozen Neovim manifest.

    These tools are never registered in FastMCP — they are injected into
    tools/list and intercepted in on_call_tool, then routed over the channel.
    Each tool gains an optional nvim_instance routing arg; a discovery tool
    (neovim_list_instances) lets a caller enumerate the available editors.
    """
    from fastmcp.tools.function_tool import FunctionTool

    out: list[Tool] = []
    server = manifest.get(_NVIM_SERVER) or {}
    for tool in server.get("tools", []):
        params = tool.get("inputSchema") or {"type": "object", "properties": {}}
        out.append(
            FunctionTool(
                fn=lambda: None,  # never called — handled in call_nvim_tool
                name=_NVIM_PREFIX + str(tool["name"]),
                description=str(tool.get("description") or ""),
                parameters=_inject_instance_arg(params),
            )
        )

    # Discovery tool — enumerate connected editors so a caller can pick one.
    out.append(
        FunctionTool(
            fn=lambda: None,
            name=_NVIM_PREFIX + "list_instances",
            description="List connected Neovim instances (instance_id + metadata such as "
            "cwd/name) so you can pass one as nvim_instance to other neovim_* tools.",
            parameters={"type": "object", "properties": {}},
        )
    )
    return out


def _dispatch_result_to_tool_result(result: Any, tool_name: str) -> ToolResult:
    """Convert the Lua dispatcher's MCP-shaped table into a FastMCP ToolResult.

    A dispatcher ``isError`` result is surfaced as a ToolError so FastMCP sets
    ``isError=True`` on the CallToolResult (correct MCP "tool ran but failed").
    """
    blocks: list[mt.ContentBlock] = []
    texts: list[str] = []
    if isinstance(result, dict):
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", ""))
                texts.append(text)
                blocks.append(mt.TextContent(type="text", text=text))

    if isinstance(result, dict) and result.get("isError"):
        detail = texts[0] if texts else "unknown error"
        raise ToolError(f"{tool_name}: {detail}")

    return ToolResult(content=blocks)


async def append_nvim_tools(
    context: MiddlewareContext[mt.ListToolsRequest],
    tools: list[Tool],
    session_disabled: dict[str, set[str]],
) -> list[Tool]:
    """Append the virtual `neovim_*` tools whenever a Neovim instance has
    connected — i.e. the `neovim` server is just another entry in the bridge
    aggregate, advertised to every client (ACP-injected or directly configured).
    The catalog is the frozen manifest captured over the channel from the first
    instance to connect.

    Listing is independent of any per-session token binding; the binding only
    decides *which* instance a call is routed to (see call_nvim_tool).
    """
    manifest = await get_nvim_channel().ensure_manifest()
    if not manifest:
        return tools  # no editor has ever connected → no neovim tools

    # Still honour an explicit per-session disable of the neovim server.
    if context.fastmcp_context is not None:
        try:
            sid = context.fastmcp_context.session_id
            if _NVIM_SERVER in session_disabled.get(sid, set()):
                return tools
        except (RuntimeError, AttributeError):
            pass

    return list(tools) + _build_nvim_tools(manifest)


async def call_nvim_tool(
    context: MiddlewareContext[mt.CallToolRequestParams],
    tool_name: str,
    session_disabled: dict[str, set[str]],
) -> ToolResult:
    """Route a `neovim_*` call to the target editor over the channel."""
    sid: str | None = None
    if context.fastmcp_context is not None:
        try:
            sid = context.fastmcp_context.session_id
        except (RuntimeError, AttributeError):
            sid = None

    if sid and _NVIM_SERVER in session_disabled.get(sid, set()):
        raise NotFoundError(
            f"Tool '{tool_name}' is unavailable — the 'neovim' server is disabled "
            "for this session."
        )

    channel = get_nvim_channel()
    local_name = tool_name[len(_NVIM_PREFIX):]
    args = dict(context.message.arguments or {}) if context.message else {}

    # Discovery tool: enumerate connected editors (no routing needed).
    if local_name == "list_instances":
        return ToolResult(
            content=[mt.TextContent(type="text", text=json.dumps({
                "instances": channel.instances(),
                "bound": _instance_for_session(sid),
            }))]
        )

    # Resolve the target editor. The default comes from how THIS connection was
    # instantiated — not from guessing by instance count:
    #   explicit nvim_instance arg  >  the connection's bound association.
    # A connection with no association must name an instance explicitly.
    requested = args.pop(_NVIM_INSTANCE_ARG, None)
    if requested:
        if not channel.has_instance(str(requested)):
            raise ToolError(
                f"Tool '{tool_name}': unknown nvim_instance '{requested}'. "
                "Call neovim_list_instances to see valid ids."
            )
        instance_id: str | None = str(requested)
    else:
        instance_id = _instance_for_session(sid)
        if instance_id is None:
            if not channel.instance_ids():
                raise ToolError(
                    f"Tool '{tool_name}' is unavailable — no Neovim instance is "
                    "connected to the bridge."
                )
            raise ToolError(
                f"Tool '{tool_name}': this connection is not associated with a "
                "specific Neovim instance, so you must say which one. Pass the "
                "'nvim_instance' argument — call neovim_list_instances to see the "
                "connected editors and their ids."
            )

    ctx = {
        "caller": "bridge",
        "session_id": sid,
        "token": _session_tokens.get(sid or ""),
    }

    assert instance_id is not None  # every None path above raises
    try:
        result = await channel.call(instance_id, local_name, args, ctx)
    except NoInstanceError as e:
        raise ToolError(f"Tool '{tool_name}' failed — {e}") from e
    except TimeoutError as e:
        raise ToolError(f"Tool '{tool_name}' timed out: {e}") from e

    return _dispatch_result_to_tool_result(result, tool_name)


def register_routes(
    bridge: FastMCP,
    notify_tool_list_changed: Callable[[], Awaitable[None]],
) -> None:
    """Mount the back-channel REST API on the bridge.

    The plugin opens a private msgpack-RPC socket and registers it here so the
    bridge can route `neovim_*` tool calls back into the live editor.
    """

    @bridge.custom_route("/neovim/instances", methods=["POST", "DELETE"])
    async def manage_nvim_instances(request: Request) -> JSONResponse:
        """Register/deregister a Neovim instance by its private socket.

        POST   { "instance_id": str, "socket": str, "pid"?: int, "cwd"?, "name"?, "servername"? }
        DELETE { "instance_id": str }
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        instance_id = body.get("instance_id")
        if not instance_id:
            return JSONResponse({"error": "instance_id required"}, status_code=400)

        channel = get_nvim_channel()

        if request.method == "DELETE":
            channel.deregister(instance_id)
            # Drop any token bindings that pointed at this instance.
            stale = [t for t, iid in _token_instances.items() if iid == instance_id]
            for t in stale:
                _token_instances.pop(t, None)
            logger.info("REST: deregistered nvim instance %s (unbound %d token[s])",
                        instance_id, len(stale))
            return JSONResponse({"instance_id": instance_id, "action": "deregistered"})

        socket = body.get("socket")
        if not socket:
            return JSONResponse({"error": "socket required"}, status_code=400)
        meta = {
            k: body[k] for k in ("cwd", "name", "pid", "servername") if body.get(k) is not None
        }
        channel.register(instance_id, socket, meta)
        logger.info("REST: registered nvim instance %s at %s (meta=%s)", instance_id, socket, meta)
        return JSONResponse({"instance_id": instance_id, "action": "registered"})

    @bridge.custom_route("/neovim/bind", methods=["POST", "DELETE"])
    async def manage_nvim_bind(request: Request) -> JSONResponse:
        """Bind/unbind a chat token to a Neovim instance.

        POST   { "token": str, "instance_id": str }
        DELETE { "token": str }
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        token = body.get("token")
        if not token:
            return JSONResponse({"error": "token required"}, status_code=400)

        if request.method == "DELETE":
            _token_instances.pop(token, None)
            return JSONResponse({"token": token, "action": "unbound"})

        instance_id = body.get("instance_id")
        if not instance_id:
            return JSONResponse({"error": "instance_id required"}, status_code=400)
        if not get_nvim_channel().has_instance(instance_id):
            return JSONResponse(
                {"error": f"unknown instance: {instance_id}"}, status_code=400
            )
        _token_instances[token] = instance_id
        logger.info("REST: bound token %s -> nvim instance %s", token, instance_id)
        # If the agent already connected and listed tools before this bind, it
        # won't have the neovim_* tools. Fire tools/list_changed so it re-lists.
        await notify_tool_list_changed()
        return JSONResponse({"token": token, "instance_id": instance_id, "action": "bound"})
