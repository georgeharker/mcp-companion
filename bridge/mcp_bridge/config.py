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
        """Convert a single server config to a typed FastMCP proxy config."""
        srv = self.servers[name]
        if srv.transport == Transport.STDIO:
            if not srv.command:
                raise ValueError(f"Server '{name}' has stdio transport but no command")
            entry = FastMCPServerEntry(
                command=srv.command,
                args=srv.args,
                env=_resolve_env(srv.env) if srv.env else None,
            )
        else:
            if not srv.url:
                raise ValueError(f"Server '{name}' has {srv.transport.value} transport but no url")
            entry = FastMCPServerEntry(
                url=srv.url,
                transport=srv.transport.value,
                headers=_resolve_env_in_dict(srv.headers) if srv.headers else None,
            )
        return FastMCPConfig(mcpServers={"default": entry})

    def get_server_status(self, name: str) -> ServerStatusInfo:
        """Build a typed status snapshot for a single server."""
        srv = self.servers[name]
        return ServerStatusInfo(
            transport=srv.transport,
            disabled=srv.disabled,
            command=srv.command,
            url=srv.url,
            auto_approve=srv.auto_approve,
        )


def _interpolate(value: str) -> str:
    """Resolve ``${VAR}`` and ``${env:VAR}`` references anywhere in a string value."""

    def _replace(m: re.Match[str]) -> str:
        inner = m.group(1)
        var_name = inner[4:] if inner.startswith("env:") else inner
        return os.environ.get(var_name) or m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _resolve_env(env: dict[str, str]) -> dict[str, str]:
    """Resolve ``${VAR}`` and ``${env:VAR}`` references in env values."""
    return {k: _interpolate(v) if isinstance(v, str) else v for k, v in env.items()}


def _resolve_env_in_dict(d: dict[str, str]) -> dict[str, str]:
    """Resolve ``${VAR}`` references in dict values."""
    return _resolve_env(d)
