"""Config loading and watching for mcp-bridge."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ServerConfig:
    """Configuration for a single MCP server."""

    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"  # "stdio" | "http" | "sse"
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    disabled: bool = False
    auto_approve: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "ServerConfig":
        """Create ServerConfig from a config dict entry."""
        # Infer transport from fields
        transport = data.get("transport")
        if transport is None:
            if "url" in data:
                transport = "http"
            else:
                transport = "stdio"

        return cls(
            name=name,
            command=data.get("command"),
            args=data.get("args", []),
            env=data.get("env", {}),
            transport=transport,
            url=data.get("url"),
            headers=data.get("headers", {}),
            disabled=data.get("disabled", False),
            auto_approve=data.get("autoApprove", []),
        )


@dataclass
class BridgeConfig:
    """Full bridge configuration."""

    servers: dict[str, ServerConfig] = field(default_factory=dict)
    config_path: str = ""

    @classmethod
    def load(cls, config_path: str) -> "BridgeConfig":
        """Load config from a servers.json file."""
        path = Path(config_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path) as f:
            raw = json.load(f)

        servers = {}
        raw_servers = raw.get("servers", raw.get("mcpServers", {}))
        for name, srv_data in raw_servers.items():
            servers[name] = ServerConfig.from_dict(name, srv_data)

        return cls(servers=servers, config_path=str(path))

    def get_enabled_servers(self) -> dict[str, ServerConfig]:
        """Return only enabled servers."""
        return {name: srv for name, srv in self.servers.items() if not srv.disabled}

    def to_fastmcp_config(self, name: str) -> dict:
        """Convert a single server config to FastMCP proxy config dict."""
        srv = self.servers[name]
        if srv.transport == "stdio":
            if not srv.command:
                raise ValueError(f"Server '{name}' has stdio transport but no command")
            config_entry: dict[str, Any] = {
                "command": srv.command,
                "args": srv.args,
            }
            if srv.env:
                config_entry["env"] = _resolve_env(srv.env)
            return {"mcpServers": {"default": config_entry}}
        else:
            if not srv.url:
                raise ValueError(f"Server '{name}' has {srv.transport} transport but no url")
            config_entry = {
                "url": srv.url,
                "transport": srv.transport,
            }
            if srv.headers:
                config_entry["headers"] = _resolve_env_in_dict(srv.headers)
            return {"mcpServers": {"default": config_entry}}


def _interpolate(value: str) -> str:
    """Resolve ${VAR} and ${env:VAR} references anywhere in a string value."""

    def _replace(m: re.Match) -> str:
        inner = m.group(1)
        # VS Code style: ${env:VAR_NAME}
        if inner.startswith("env:"):
            var_name = inner[4:]
        else:
            var_name = inner
        return os.environ.get(var_name) or m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _resolve_env(env: dict[str, str]) -> dict[str, str]:
    """Resolve ${VAR} and ${env:VAR} references in env values."""
    return {k: _interpolate(v) if isinstance(v, str) else v for k, v in env.items()}


def _resolve_env_in_dict(d: dict[str, str]) -> dict[str, str]:
    """Resolve ${VAR} references in dict values."""
    return _resolve_env(d)
