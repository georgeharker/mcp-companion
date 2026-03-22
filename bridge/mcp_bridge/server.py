"""FastMCP bridge server — proxies multiple MCP servers through one endpoint."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import httpx
import mcp.types as mt
from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_bridge.auth import build_auth
from mcp_bridge.config import (
    BridgeConfig,
    HealthResponse,
    ServerConfig,
    ServerStatusInfo,
    _interpolate_str,
)

logger = logging.getLogger("mcp-bridge")


def _safe_json_clone(obj: object) -> Any:
    """JSON round-trip to break Python-level circular object identity."""
    return json.loads(json.dumps(obj, default=str))


class SanitizeSchemaMiddleware(Middleware):
    """Intercept tools/list and rebuild tools that fail Pydantic serialization.

    FastMCP ProxyTool objects can carry circular Python object references
    (especially from servers with $ref schemas like Todoist).  Pydantic's
    ``model_dump()`` crashes with 'Circular reference detected (id repeated)'.

    Our middleware catches these failures and reconstructs the tool as a
    plain ``mcp.types.Tool`` (the wire-format dataclass) which has no internal
    state that can be circular.  The original FastMCP Tool objects remain
    in the tool registry for execution -- this only affects the listing
    response.

    NOTE: We override the *low-level* response by intercepting request_handler
    since FastMCP's middleware returns Tool objects that still get serialized
    by the MCP SDK's ``_send_response()``, which is where it crashes.
    """

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = list(await call_next(context))
        sanitized: list[Tool] = []
        for tool in tools:
            try:
                tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                sanitized.append(tool)
            except (ValueError, RecursionError):
                logger.warning("Replacing circular tool: %s", tool.name)
                sanitized.append(self._to_clean_tool(tool))
        return sanitized

    @staticmethod
    def _to_clean_tool(tool: Tool) -> Tool:
        """Build a minimal FunctionTool that serializes cleanly.

        We extract only the wire-format fields (name, description, parameters,
        annotations) and construct a new FunctionTool with a dummy fn.
        The original ProxyTool stays in FastMCP's registry for actual execution.
        """
        from fastmcp.tools.function_tool import FunctionTool

        # Clean the parameters via JSON round-trip
        try:
            clean_params: dict[str, Any] = _safe_json_clone(tool.parameters)
        except (ValueError, RecursionError, TypeError):
            clean_params = {"type": "object", "properties": {}}

        # Clean annotations if present
        clean_annotations: dict[str, Any] | None
        try:
            clean_annotations = _safe_json_clone(
                tool.annotations.model_dump() if tool.annotations else None
            )
        except (ValueError, RecursionError, TypeError, AttributeError):
            clean_annotations = None

        # Build a fresh FunctionTool with no circular refs
        dummy_fn = lambda: None  # noqa: E731 -- never called, just for FunctionTool ctor
        new_tool = FunctionTool(
            fn=dummy_fn,
            name=str(tool.name) if tool.name else "unknown",
            description=str(tool.description) if tool.description else "",
            parameters=clean_params,
            annotations=mt.ToolAnnotations(**clean_annotations) if clean_annotations else None,
        )

        # Verify it serializes
        try:
            new_tool.model_dump(by_alias=True, mode="json", exclude_none=True)
        except Exception:
            # Last resort: strip parameters entirely
            new_tool = FunctionTool(
                fn=dummy_fn,
                name=str(tool.name) if tool.name else "unknown",
                description=str(tool.description) if tool.description else "",
                parameters={"type": "object", "properties": {}},
            )

        return new_tool


def _create_server_proxy(config: BridgeConfig, name: str, srv: ServerConfig) -> FastMCP:
    """Create a proxy for a single upstream MCP server.

    When the server has auth configured, we create a ``Client`` with
    ``auth=`` set so the proxy's upstream HTTP requests carry the right
    credentials.  For servers without auth we fall back to the simpler
    dict-based ``create_proxy(config_dict)`` path.
    """
    auth: httpx.Auth | None = build_auth(
        name,
        auth_config=srv.auth,
        server_url=srv.url,
    )

    if auth is not None and srv.url:
        # Auth requires a Client so we can inject httpx.Auth into the transport
        client = Client(_interpolate_str(srv.url), auth=auth)
        return create_proxy(client, name=name)

    # No auth — use the standard config-dict path
    proxy_config = config.to_fastmcp_config(name)
    return create_proxy(proxy_config.model_dump(exclude_none=True), name=name)


def create_bridge(config_path: str) -> FastMCP:
    """Create the bridge FastMCP server from a config file.

    Reads servers.json, creates a proxy for each enabled server,
    mounts them under namespaced prefixes, and adds meta-tools + health.
    """
    config = BridgeConfig.load(config_path)
    bridge = FastMCP(
        name="mcp-bridge",
        instructions="MCP Bridge — proxies multiple MCP servers through a single endpoint.",
        dereference_schemas=False,  # Disabled: circular $ref causes infinite recursion
        middleware=[SanitizeSchemaMiddleware()],  # Strips circular refs before serialization
    )

    # Mount each enabled server as a namespaced proxy
    enabled = config.get_enabled_servers()
    for name, srv in enabled.items():
        try:
            proxy = _create_server_proxy(config, name, srv)
            bridge.mount(proxy, namespace=name)
            logger.info("Mounted server: %s (%s)", name, srv.transport.value)
        except Exception:
            logger.exception("Failed to mount server '%s'", name)

    # Register meta-tools
    from mcp_bridge.meta_tools import register_meta_tools

    register_meta_tools(bridge, config)

    # Health endpoint
    @bridge.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        server_statuses: dict[str, ServerStatusInfo] = {
            name: config.get_server_status(name) for name in config.servers
        }
        response = HealthResponse(
            status="ok",
            servers=server_statuses,
            config_path=config.config_path,
        )
        return JSONResponse(response.model_dump(mode="json"))

    return bridge
