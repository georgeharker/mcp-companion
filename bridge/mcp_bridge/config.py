"""Config loading and watching for mcp-bridge."""

from __future__ import annotations

import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Transport(str, Enum):
    """Supported MCP transport types."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


class ServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    transport: Transport = Transport.STDIO
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False
    auto_approve: list[str] = Field(default_factory=list)
    auth: dict[str, Any] | str | None = None
    """Authentication config.

    Supported values:
    - ``None``                — no authentication (default)
    - ``"oauth"``             — OAuth 2.1 Authorization Code + PKCE
    - ``{"bearer": "tok"}``   — static Bearer token
    - ``{"oauth": {...}}``    — OAuth with explicit options (scopes, client_id, ...)
    """

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ServerConfig:
        """Create ServerConfig from a config dict entry."""
        transport_str = data.get("transport")
        if transport_str is None:
            transport_str = "http" if "url" in data else "stdio"

        return cls(
            name=name,
            command=data.get("command"),
            args=data.get("args", []),
            env=data.get("env", {}),
            transport=Transport(transport_str),
            url=data.get("url"),
            headers=data.get("headers", {}),
            disabled=data.get("disabled", False),
            auto_approve=data.get("autoApprove", []),
            auth=data.get("auth"),
        )


class FastMCPServerEntry(BaseModel):
    """A single server entry in the FastMCP config format."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    url: str | None = None
    transport: str | None = None
    headers: dict[str, str] | None = None


class FastMCPConfig(BaseModel):
    """FastMCP proxy config structure: ``{"mcpServers": {"default": ...}}``."""

    mcpServers: dict[str, FastMCPServerEntry]  # noqa: N815


class ServerStatusInfo(BaseModel):
    """Status snapshot of a single server (returned by meta-tools and health)."""

    transport: Transport
    disabled: bool
    command: str | None = None
    url: str | None = None
    auto_approve: list[str] = Field(default_factory=list)
    auth_type: str | None = None
    """Authentication type: ``"oauth"``, ``"bearer"``, or ``None``."""


class HealthResponse(BaseModel):
    """Response body for the ``/health`` endpoint."""

    status: str = "ok"
    servers: dict[str, ServerStatusInfo] = Field(default_factory=dict)
    config_path: str = ""


class BridgeConfig(BaseModel):
    """Full bridge configuration."""

    servers: dict[str, ServerConfig] = Field(default_factory=dict)
    config_path: str = ""

    @classmethod
    def load(cls, config_path: str) -> BridgeConfig:
        """Load config from a ``servers.json`` file."""
        path = Path(config_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path) as f:
            raw: dict[str, Any] = json.load(f)

        raw_servers: dict[str, Any] = raw.get("servers", raw.get("mcpServers", {}))
        servers = {
            name: ServerConfig.from_dict(name, srv_data) for name, srv_data in raw_servers.items()
        }

        return cls(servers=servers, config_path=str(path))

    def get_enabled_servers(self) -> dict[str, ServerConfig]:
        """Return only enabled servers."""
        return {name: srv for name, srv in self.servers.items() if not srv.disabled}

    def to_fastmcp_config(self, name: str) -> FastMCPConfig:
        """Convert a single server config to a typed FastMCP proxy config.

        Environment variable expansion (``${VAR}``, ``${VAR:-default}``,
        ``${env:VAR}``) is applied to *command*, *args*, *env*, *url*, and
        *headers* at this point — **not** at load time — so that the raw
        config can be round-tripped without loss.
        """
        srv = self.servers[name]
        if srv.transport == Transport.STDIO:
            if not srv.command:
                raise ValueError(f"Server '{name}' has stdio transport but no command")
            entry = FastMCPServerEntry(
                command=_interpolate_str(srv.command),
                args=_interpolate_list(srv.args),
                env=_interpolate_dict(srv.env) if srv.env else None,
            )
        else:
            if not srv.url:
                raise ValueError(f"Server '{name}' has {srv.transport.value} transport but no url")
            entry = FastMCPServerEntry(
                url=_interpolate_str(srv.url),
                transport=srv.transport.value,
                headers=_interpolate_dict(srv.headers) if srv.headers else None,
            )
        return FastMCPConfig(mcpServers={"default": entry})

    def get_server_status(self, name: str) -> ServerStatusInfo:
        """Build a typed status snapshot for a single server."""
        srv = self.servers[name]

        # Derive auth type string
        auth_type: str | None = None
        if isinstance(srv.auth, str):
            auth_type = srv.auth  # "oauth"
        elif isinstance(srv.auth, dict):
            if "bearer" in srv.auth:
                auth_type = "bearer"
            elif "oauth" in srv.auth:
                auth_type = "oauth"

        return ServerStatusInfo(
            transport=srv.transport,
            disabled=srv.disabled,
            command=srv.command,
            url=srv.url,
            auto_approve=srv.auto_approve,
            auth_type=auth_type,
        )


def _interpolate(value: str) -> str:
    """Resolve environment variable references in a string.

    Supported syntax::

        ${VAR}            — value of VAR, or empty string if unset
        ${env:VAR}        — same (compat with VS Code / MCP configs)
        ${VAR:-default}   — value of VAR, or *default* if unset/empty
        ${env:VAR:-default}
    """

    def _replace(m: re.Match[str]) -> str:
        inner = m.group(1)
        # Strip optional ``env:`` prefix
        if inner.startswith("env:"):
            inner = inner[4:]
        # Split on ``:-`` for default value
        if ":-" in inner:
            var_name, default = inner.split(":-", 1)
        else:
            var_name, default = inner, ""
        return os.environ.get(var_name, default)

    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _interpolate_str(value: str) -> str:
    """Interpolate a single string value."""
    return _interpolate(value)


def _interpolate_list(values: list[str]) -> list[str]:
    """Interpolate all strings in a list."""
    return [_interpolate(v) for v in values]


def _interpolate_dict(d: dict[str, str]) -> dict[str, str]:
    """Interpolate all values in a string dict."""
    return {k: _interpolate(v) for k, v in d.items()}
