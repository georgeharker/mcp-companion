"""Config loading and watching for mcp-combiner."""

from __future__ import annotations

import json
import logging
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("mcp-combiner")


def _warn_unknown_keys(kind: str, name: str, data: dict[str, Any], known: set[str]) -> None:
    """Log a warning for config keys we don't recognise.

    ``from_dict`` extracts known fields explicitly, so a misplaced or
    mistyped key (e.g. ``isolate`` on a ``sharedServers`` entry, where it
    does nothing) is otherwise silently ignored.  This surfaces it.
    """
    unknown = set(data) - known
    if unknown:
        logger.warning(
            "%s '%s': ignoring unknown config key(s): %s",
            kind,
            name,
            ", ".join(sorted(unknown)),
        )


class Transport(str, Enum):
    """Supported MCP transport types."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


class SharedServerConfig(BaseModel):
    """Configuration for spawning an HTTP server process via sharedserver.

    Lives under the top-level ``sharedServers`` dict in the config file.
    Server entries reference a shared server by name via ``"sharedServer": "<name>"``.

    Example JSON (top-level section)::

        "sharedServers": {
            "goog_ws": {
                "command": "uvx",
                "args": ["workspace-mcp", "--transport", "streamable-http"],
                "env": {
                    "WORKSPACE_MCP_PORT": "8002",
                    "MCP_ENABLE_OAUTH21": "true"
                },
                "grace_period": "30m",
                "health_timeout": 30
            }
        }

    Server entry::

        "google-workspace": {
            "url": "http://localhost:8002/mcp",
            "auth": "oauth",
            "sharedServer": "goog_ws"
        }
    """

    name: str
    """sharedserver server name — key in the top-level ``sharedServers`` dict."""

    command: str
    """Executable to run (e.g. ``"uvx"``)."""

    args: list[str] = Field(default_factory=list)
    """Arguments to the command (e.g. ``["workspace-mcp", "--transport", "streamable-http"]``)."""

    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment variables for the spawned process.
    Supports ``${VAR}``, ``${env:VAR}``, and ``${VAR:-default}`` interpolation.
    """

    grace_period: str | None = None
    """Grace period to pass to sharedserver (e.g. ``"30m"``).
    When the last client unuses the server it stays alive for this duration.
    """

    health_timeout: int = 30
    """Seconds to wait for the HTTP server to become reachable after starting.
    The combiner polls the server URL until it responds or this timeout expires.
    """

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> SharedServerConfig:
        """Parse a ``sharedServers`` entry dict, keyed by *name*."""
        _warn_unknown_keys(
            "sharedServer",
            name,
            data,
            {
                "command",
                "args",
                "env",
                "grace_period",
                "gracePeriod",
                "health_timeout",
                "healthTimeout",
            },
        )
        raw_env = data.get("env", {})
        env = {k: str(v) for k, v in raw_env.items()} if raw_env else {}
        return cls(
            name=name,
            command=data["command"],
            args=data.get("args", []),
            env=env,
            grace_period=data.get("grace_period") or data.get("gracePeriod"),
            health_timeout=int(data.get("health_timeout", data.get("healthTimeout", 30))),
        )


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

    shared_server: str | None = None
    """Name of a top-level ``sharedServers`` entry to start before connecting."""

    tool_filter: list[str] = Field(default_factory=list)
    """Glob patterns to filter which tools are exposed from this server.

    If empty (default), all tools are included.
    Patterns are matched against the tool name (without server prefix).
    Examples: ``["gmail_*", "calendar_*"]`` to only include Gmail and Calendar tools.
    """

    isolate: bool | None = None
    """Tri-state: give each downstream chat its own upstream MCP session.

    When isolated, the combiner opens a separate upstream session per downstream
    chat (still one upstream *server instance*, shared transport), so the server
    is handed a distinct, stable ``Mcp-Session-Id`` per chat and partitions its
    per-session state automatically — no clash between concurrent chats. When
    not isolated, all chats share one persistent upstream connection (one
    ``Mcp-Session-Id``), so a *stateful* server (e.g. svg-mcp's "current
    document") sees every chat as the same session and they clash.

    - ``None`` (absent — default): off. All chats share one upstream session.
    - ``True`` / ``False``: forced on / off.

    Tri-state (rather than a plain bool) so "absent" stays distinct from an
    explicit ``false`` for any layered override.

    Only applies to HTTP/SSE servers. stdio has one session per process, so it
    is never isolated (an explicit ``true`` is ignored with a warning) — per-chat
    isolation there would require a subprocess per chat, which the combiner does
    not do. See ``server._effective_isolate``.
    """

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ServerConfig:
        """Create ServerConfig from a config dict entry."""
        _warn_unknown_keys(
            "server",
            name,
            data,
            {
                "command",
                "args",
                "env",
                "transport",
                "url",
                "headers",
                "disabled",
                "autoApprove",
                "auth",
                "sharedServer",
                "shared_server",
                "toolFilter",
                "tool_filter",
                "isolate",
            },
        )
        transport_str = data.get("transport")
        if transport_str is None:
            transport_str = "http" if "url" in data else "stdio"

        raw_auto_approve = data.get("autoApprove", [])
        if raw_auto_approve is True:
            auto_approve: list[str] = ["*"]
        elif raw_auto_approve is False or raw_auto_approve is None:
            auto_approve = []
        else:
            auto_approve = list(raw_auto_approve)

        raw_env = data.get("env", {})
        env = {k: str(v) for k, v in raw_env.items()} if raw_env else {}

        # camelCase or snake_case key
        shared_server = data.get("sharedServer") or data.get("shared_server")
        tool_filter = data.get("toolFilter") or data.get("tool_filter") or []

        return cls(
            name=name,
            command=data.get("command"),
            args=data.get("args", []),
            env=env,
            transport=Transport(transport_str),
            url=data.get("url"),
            headers=data.get("headers", {}),
            disabled=data.get("disabled", False),
            auto_approve=auto_approve,
            auth=data.get("auth"),
            shared_server=shared_server,
            tool_filter=tool_filter,
            isolate=(None if data.get("isolate") is None else bool(data["isolate"])),
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
    shared_server: str | None = None
    """sharedServers key if this server has a managed process, else ``None``."""
    state: str = "unknown"
    """Runtime lifecycle state: ``"ready"``, ``"connected"`` (session up, tools
    warming), ``"disconnected"``, ``"auth_failed"``, or ``"disabled"``. Overlaid
    at report time from the ConnectionManager (config alone can't know it);
    defaults to ``"unknown"`` for a bare config-only snapshot."""


class HealthResponse(BaseModel):
    """Response body for the ``/health`` endpoint."""

    status: str = "ok"
    servers: dict[str, ServerStatusInfo] = Field(default_factory=dict)
    config_path: str = ""
    pending_oauth: list[str] = Field(default_factory=list)


class OAuthConfig(BaseModel):
    """Top-level OAuth settings for the combiner.

    These are global defaults; individual servers can override ``cache_tokens``
    inside their ``auth: {oauth: {cache_tokens: false}}`` block.

    Example JSON (top-level section)::

        "oauth": {
            "cache_tokens": true,
            "token_dir": "~/.cache/mcp-combiner/oauth-tokens"
        }
    """

    cache_tokens: bool = True
    """Persist OAuth tokens to disk (default ``true``).

    When ``true``, tokens are stored under *token_dir* and reused across combiner
    restarts.  When ``false``, tokens live in memory only and the browser OAuth
    flow runs again on each restart.
    """

    token_dir: str | None = None
    """Directory for OAuth token files.

    Defaults to ``~/.cache/mcp-combiner/oauth-tokens`` when ``null``.
    Supports ``~`` expansion.  Each server gets its own subdirectory.
    """

    @property
    def token_dir_path(self) -> Path:
        """Resolved :class:`~pathlib.Path` for *token_dir*, using default if not set."""
        if self.token_dir is None:
            return Path.home() / ".cache" / "mcp-combiner" / "oauth-tokens"
        return Path(self.token_dir).expanduser()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthConfig:
        """Parse the top-level ``oauth`` config dict."""
        return cls(
            cache_tokens=bool(data.get("cache_tokens", True)),
            token_dir=data.get("token_dir") or data.get("tokenDir"),
        )


class CombinerConfig(BaseModel):
    """Full combiner configuration."""

    servers: dict[str, ServerConfig] = Field(default_factory=dict)
    shared_servers: dict[str, SharedServerConfig] = Field(default_factory=dict)
    oauth: OAuthConfig = Field(default_factory=OAuthConfig)
    config_path: str = ""

    @classmethod
    def load(cls, config_path: str) -> CombinerConfig:
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

        raw_shared: dict[str, Any] = raw.get("sharedServers", {})
        shared_servers = {
            name: SharedServerConfig.from_dict(name, data) for name, data in raw_shared.items()
        }

        raw_oauth: dict[str, Any] = raw.get("oauth", {})
        oauth = OAuthConfig.from_dict(raw_oauth) if raw_oauth else OAuthConfig()

        return cls(
            servers=servers,
            shared_servers=shared_servers,
            oauth=oauth,
            config_path=str(path),
        )

    def get_enabled_servers(self) -> dict[str, ServerConfig]:
        """Return only enabled servers."""
        return {name: srv for name, srv in self.servers.items() if not srv.disabled}

    def resolve_shared_server(self, server_name: str) -> SharedServerConfig | None:
        """Return the SharedServerConfig for a server, if it has one."""
        srv = self.servers.get(server_name)
        if srv is None or srv.shared_server is None:
            return None
        ss = self.shared_servers.get(srv.shared_server)
        if ss is None:
            raise KeyError(
                f"Server '{server_name}' references unknown sharedServer '{srv.shared_server}'"
            )
        return ss

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
            shared_server=srv.shared_server,
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
