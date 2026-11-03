"""Config loading and watching for mcp-combiner."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

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


class ExternalServerConfig(BaseModel):
    """Configuration for a non-owned backing process, launched by a plain CLI
    tool (e.g. ``docker run``) instead of sharedserver.

    Lives under the top-level ``externalServers`` dict in the config file.
    Server entries reference one by name via ``"externalServer": "<name>"``
    (mutually exclusive with ``sharedServer`` — a load-time warning is logged
    if both are set and ``sharedServer`` wins).

    Unlike ``SharedServerConfig``, there is no refcount and no grace period:
    the combiner launches the process (if ``launch`` is set), waits for the
    server entry's ``url`` to answer, and does exactly the shutdown behavior
    the config states — nothing more.  Lifecycle semantics are *derived from
    the fields* rather than a mode enum:

    - ``launch`` only                 — launch + attach; left running on shutdown
    - ``launch`` + ``stop``           — launch + attach; ``stop`` run on shutdown
    - ``launch`` + ``foreground``     — combiner owns the child process; on
                                        shutdown a configured ``stop`` runs
                                        first, then a still-alive child is
                                        SIGTERMed as a fallback (``docker run``
                                        without ``-d`` forwards the signal to
                                        the container — add ``--sig-proxy=true``
                                        /``--init`` on the launch line if your
                                        image needs it)
    - no ``launch``                   — attach-only; just polls ``url`` (parity
                                        with plain url-only servers)

    Example JSON (a detached docker container)::

        "externalServers": {
            "postgres_mcp": {
                "launch": ["docker", "run", "--rm", "-d", "--name", "postgres-mcp",
                           "-p", "8002:8000", "ghcr.io/org/postgres-mcp"],
                "stop": ["docker", "stop", "postgres-mcp"],
                "env": {"DOCKER_HOST": "${env:DOCKER_HOST}"},
                "health_timeout": 30
            }
        }

    Server entry::

        "postgres": {
            "url": "http://localhost:8002/mcp",
            "externalServer": "postgres_mcp"
        }

    .. note::
        Launch is **not** made idempotent by the combiner — re-booting the
        combiner re-runs ``launch`` verbatim. ``docker run -d --name x`` fails
        when a container of that name already exists (a loud, self-explanatory
        failure); prefer idempotent launchers (``docker compose up -d``) or
        attach-only entries for processes you manage outside the combiner.

    .. note::
        Ownership fields that belong to sharedserver (``grace_period``,
        ``command``/``args``, ``use``-style keys) are deliberately **not**
        accepted here: they are warned about as unknown keys and ignored, per
        the warn-and-continue posture used elsewhere in config parsing.
    """

    name: str
    """externalServers entry name — key in the top-level ``externalServers`` dict."""

    launch: list[str] | None = None
    """argv to launch the backing process (e.g. ``["docker", "run", "-d", ...]``).
    Supports ``${VAR}`` interpolation. ``None`` = attach-only: no process is
    launched, the entry only gates the reachability poll of the server's ``url``.
    """

    stop: list[str] | None = None
    """Optional argv to stop the backing process on disable/shutdown (e.g.
    ``["docker", "stop", "name"]``). Absent = leave the process running.
    Ignored (with no effect on shutdown) when ``launch`` is absent.
    """

    foreground: bool = False
    """Treat ``launch`` as a foreground child the combiner owns: the PID is
    kept and SIGTERMed on shutdown (``docker run`` without ``-d`` forwards the
    signal to the container — add ``--sig-proxy=true``/``--init`` on the
    launch line if your image needs it). ``False`` = launch detached and leave
    running (the CLI tool owns the lifetime, e.g. ``docker run -d``).
    """

    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment variables for the *launcher* process (e.g.
    ``DOCKER_HOST``). Supports ``${VAR}``, ``${env:VAR}``, ``${VAR:-default}``
    interpolation; layered over the combiner's own environment."""

    health_timeout: int = 30
    """Seconds to poll the server URL after launch before giving up.
    Same meaning as ``SharedServerConfig.health_timeout``.
    """

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ExternalServerConfig:
        """Parse an ``externalServers`` entry dict, keyed by *name*."""
        _warn_unknown_keys(
            "externalServer",
            name,
            data,
            {
                "launch",
                "stop",
                "foreground",
                "env",
                "health_timeout",
                "healthTimeout",
            },
        )
        raw_env = data.get("env", {})
        env = {k: str(v) for k, v in raw_env.items()} if raw_env else {}
        if data.get("stop") is not None and data.get("launch") is None:
            logger.warning(
                "externalServer '%s': 'stop' set without 'launch' — ignored "
                "(the combiner never launches anything, so it never stops anything)",
                name,
            )
        return cls(
            name=name,
            launch=data.get("launch"),
            stop=data.get("stop"),
            foreground=bool(data.get("foreground", False)),
            env=env,
            health_timeout=int(data.get("health_timeout", data.get("healthTimeout", 30))),
        )


class PermissionAction(str, Enum):
    """What to do with a tool call: allow it, deny it, or ask the user."""

    ALLOW = "allow"
    DENY = "deny"
    ELICIT = "elicit"


def _matches_any(name: str, patterns: list[str]) -> bool:
    """True if *name* matches any of the fnmatch glob *patterns*."""
    return any(fnmatch.fnmatch(name, p) for p in patterns)


class PermissionPolicy(BaseModel):
    """Per-call permission policy (config shape).

    Applies to tool *calls* only — never to tool publication.  Patterns are
    globs matched against the **local** tool name (without the ``<server>_``
    prefix), the same fnmatch semantics as ``tool_filter``.

    Scalar fields are ``None`` = *inherit* so a per-server block can override
    just one of them while inheriting the rest from the global policy.  The
    resolved default when nothing is set is :attr:`PermissionAction.ALLOW`
    (i.e. **off** — no gate), so an unconfigured combiner behaves exactly as
    before this feature existed.
    """

    default: PermissionAction | None = None
    """Action for tools that match no pattern.  ``None`` inherits; the global
    fallback is ``ALLOW`` (off)."""
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    elicit: list[str] = Field(default_factory=list)
    elicit_unavailable: PermissionAction | None = None
    """What to do when a call resolves to ELICIT but the client cannot elicit.
    ``None`` inherits; the global fallback is ``DENY`` (secure)."""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PermissionPolicy | None:
        """Parse a ``permissions`` block; ``None``/empty → ``None`` (no policy)."""
        if not data:
            return None
        if not isinstance(data, dict):
            raise ValueError("permissions must be an object")

        def _action(key: str) -> PermissionAction | None:
            v = data.get(key)
            if v is None:
                return None
            return PermissionAction(str(v))

        def _patterns(*keys: str) -> list[str]:
            for k in keys:
                if k in data and data[k]:
                    return list(data[k])
            return []

        return cls(
            default=_action("default"),
            allow=_patterns("allow"),
            deny=_patterns("deny"),
            elicit=_patterns("elicit"),
            elicit_unavailable=_action("elicitUnavailable") or _action("elicit_unavailable"),
        )


@dataclass(frozen=True)
class ResolvedPolicy:
    """A concrete, merged policy for one server (global ⊕ server ⊕ auto_approve)."""

    default: PermissionAction
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    elicit: tuple[str, ...]
    elicit_unavailable: PermissionAction

    def is_active(self) -> bool:
        """False when this policy can only ever allow — lets callers fast-path."""
        return self.default is not PermissionAction.ALLOW or bool(self.deny) or bool(self.elicit)

    def resolve(self, local_tool: str) -> PermissionAction:
        """Action for *local_tool* — precedence deny > elicit > allow > default."""
        if _matches_any(local_tool, list(self.deny)):
            return PermissionAction.DENY
        if _matches_any(local_tool, list(self.elicit)):
            return PermissionAction.ELICIT
        if _matches_any(local_tool, list(self.allow)):
            return PermissionAction.ALLOW
        return self.default


def _merge_policies(
    global_p: PermissionPolicy | None,
    server_p: PermissionPolicy | None,
    auto_approve: list[str],
) -> ResolvedPolicy:
    """Layer a per-server policy over the global one.

    Pattern sets are the **union** (global first, then server, then the
    server's ``auto_approve`` as additional allow patterns).  Scalars take the
    server's value when set, else the global's, else the secure fallback
    (default → ALLOW/off, elicit_unavailable → DENY).
    """

    def pick(
        get: "Callable[[PermissionPolicy], PermissionAction | None]",
        fallback: PermissionAction,
    ) -> PermissionAction:
        for p in (server_p, global_p):
            if p is not None and (v := get(p)) is not None:
                return v
        return fallback

    def union(get: "Callable[[PermissionPolicy], list[str]]") -> tuple[str, ...]:
        out: list[str] = []
        for p in (global_p, server_p):
            if p is not None:
                out.extend(get(p))
        return tuple(out)

    return ResolvedPolicy(
        default=pick(lambda p: p.default, PermissionAction.ALLOW),
        allow=union(lambda p: p.allow) + tuple(auto_approve),
        deny=union(lambda p: p.deny),
        elicit=union(lambda p: p.elicit),
        elicit_unavailable=pick(lambda p: p.elicit_unavailable, PermissionAction.DENY),
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

    external_server: str | None = None
    """Name of a top-level ``externalServers`` entry (non-owned process, e.g. a
    ``docker run`` container) to launch/attach before connecting. Mutually
    exclusive with ``shared_server`` — a warning is logged at load time and
    ``shared_server`` wins if both are set."""

    tool_filter: list[str] = Field(default_factory=list)
    """Glob patterns to filter which tools are exposed from this server.

    If empty (default), all tools are included.
    Patterns are matched against the tool name (without server prefix).
    Examples: ``["gmail_*", "calendar_*"]`` to only include Gmail and Calendar tools.
    """

    permissions: PermissionPolicy | None = None
    """Per-call permission policy for this server (overrides/extends the global
    ``permissions`` block).  ``None`` = inherit the global policy.  See
    :class:`PermissionPolicy`.  Gates tool *calls* only, never publication."""

    protocol: UpstreamProtocol = "auto"
    """Which MCP protocol era the combiner speaks to this upstream.

    - ``"auto"`` (default): probe the server's ``server/discover`` before
      mounting it. A server listing the 2026-07-28 revision is treated as
      handling its own sessions (explicit session ids on its tools): it is
      connected on that revision over one shared connection, and ``isolate``
      is ignored. Otherwise — and for servers that can't be probed (stdio,
      OAuth, unreachable) — the ``initialize`` handshake.
    - ``"legacy"``: always the handshake (stateful ``Mcp-Session-Id``; isolation
      and resume apply). Use for a server that answers discover with 2026 but
      keeps per-connection state (every mcp 2 server does by default).
    - ``"2026"``: always the 2026-07-28 revision.
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

    Applies to both transports, with transport-shaped semantics:

    - HTTP/SSE: per-chat upstream **sessions** over one shared server instance
      (``TokenKeyedStatefulClient``) — see ``mcp_combiner.isolated``.
    - stdio: per-chat **subprocesses** (``TokenKeyedStdioClient``) — one
      ``command`` child per chat, kept running through the grace window
      (state lives in the child), terminated at the park-ttl forget. Cost:
      N chats spawn N copies of the server's runtime.

    See ``proxyfactory._effective_isolate``.
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
                "externalServer",
                "external_server",
                "toolFilter",
                "tool_filter",
                "permissions",
                "isolate",
                "protocol",
            },
        )
        transport_str = data.get("transport")
        if transport_str is None:
            transport_str = "http" if "url" in data else "stdio"

        raw_auto_approve = data.get("autoApprove", [])
        if isinstance(raw_auto_approve, bool):
            auto_approve: list[str] = ["*"] if raw_auto_approve else []
        elif raw_auto_approve is None:
            auto_approve = []
        else:
            auto_approve = list(raw_auto_approve)

        raw_env = data.get("env", {})
        env = {k: str(v) for k, v in raw_env.items()} if raw_env else {}

        # camelCase or snake_case key
        shared_server = data.get("sharedServer") or data.get("shared_server")
        external_server = data.get("externalServer") or data.get("external_server")
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
            external_server=external_server,
            tool_filter=tool_filter,
            permissions=PermissionPolicy.from_dict(data.get("permissions")),
            isolate=(None if data.get("isolate") is None else bool(data["isolate"])),
            protocol=_upstream_protocol(name, data.get("protocol")),
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
    external_server: str | None = None
    """externalServers key if this server has a non-owned backing process, else ``None``."""
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


UpstreamProtocol = Literal["auto", "legacy", "2026"]
UPSTREAM_PROTOCOLS: tuple[UpstreamProtocol, ...] = ("auto", "legacy", "2026")


def _upstream_protocol(server_name: str, raw: object) -> UpstreamProtocol:
    """A server's ``protocol`` setting (absent = ``"auto"``)."""
    if raw is None:
        return "auto"
    for choice in UPSTREAM_PROTOCOLS:
        if raw == choice:
            return choice
    raise ValueError(
        f"Server '{server_name}': protocol must be one of {', '.join(UPSTREAM_PROTOCOLS)}; "
        f"got {raw!r}"
    )


AuthPrompt = Literal["elicit", "browser", "both"]
"""How the combiner presents an OAuth authorization URL to the user:

* ``"browser"`` — open a browser on the combiner host (the original behaviour).
* ``"elicit"`` — hand the URL to the MCP client as a URL-mode elicitation
  (a tool call routed to the server fails with ``-32042`` carrying the URL),
  so a remote client can show it to the user.
* ``"both"`` — elicit and open the browser.
"""

AUTH_PROMPTS: tuple[AuthPrompt, ...] = ("elicit", "browser", "both")


def oauth_options(auth: dict[str, Any] | str | None) -> dict[str, Any] | None:
    """A server's OAuth options: ``{}`` for bare ``auth: "oauth"``, the
    ``auth.oauth`` block for ``{"oauth": {...}}``, ``None`` when the server
    does not use OAuth (or the block is malformed — ``build_auth`` reports that).
    """
    if auth == "oauth":
        return {}
    if isinstance(auth, dict):
        opts = auth.get("oauth")
        if isinstance(opts, dict):
            return opts
    return None


def resolve_auth_prompt(
    server_name: str, per_server: object, default: AuthPrompt | None, public_url: str | None
) -> AuthPrompt:
    """The effective :data:`AuthPrompt` for one server.

    The server's own ``auth.oauth.prompt`` wins, then the global
    ``oauth.prompt``; with neither set it is ``"both"`` when a ``public_url``
    is configured and ``"browser"`` otherwise — so an install without
    ``public_url`` keeps the original behaviour unchanged.
    """
    chosen = per_server if per_server is not None else default
    if chosen is None:
        return "both" if public_url else "browser"
    for prompt in AUTH_PROMPTS:
        if chosen == prompt:
            return prompt
    raise ValueError(
        f"Server '{server_name}': oauth prompt must be one of {', '.join(AUTH_PROMPTS)}; "
        f"got {chosen!r}"
    )


class OAuthConfig(BaseModel):
    """Top-level OAuth settings for the combiner.

    These are global defaults; individual servers can override ``cache_tokens``
    and ``prompt`` inside their ``auth: {oauth: {...}}`` block.

    Example JSON (top-level section)::

        "oauth": {
            "cache_tokens": true,
            "token_dir": "~/.cache/mcp-combiner/oauth-tokens",
            "public_url": "https://combiner.tailnet.ts.net",
            "prompt": "both"
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

    public_url: str | None = None
    """The combiner's externally reachable base URL (e.g. its ``tailscale
    serve`` address). When set, OAuth redirects come back to
    ``<public_url>/oauth/callback/<server>`` on the combiner itself instead of
    a loopback callback server, so a user authorizing on another device (a
    phone) can complete the flow. The redirect URI must be registered with the
    OAuth client (for Google: a Web-application client). When unset, the
    loopback callback is used, which completes only from a browser on the
    combiner host.
    """

    prompt: AuthPrompt | None = None
    """Default :data:`AuthPrompt` for OAuth servers (see
    :func:`resolve_auth_prompt` for the default when unset)."""

    @property
    def token_dir_path(self) -> Path:
        """Resolved :class:`~pathlib.Path` for *token_dir*, using default if not set."""
        if self.token_dir is None:
            return Path.home() / ".cache" / "mcp-combiner" / "oauth-tokens"
        return Path(self.token_dir).expanduser()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthConfig:
        """Parse the top-level ``oauth`` config dict."""
        public_url = data.get("public_url") or data.get("publicUrl")
        if public_url is not None and not isinstance(public_url, str):
            raise ValueError("oauth.public_url must be a string")
        public_url = public_url.rstrip("/") if public_url else None
        raw_prompt = data.get("prompt")
        prompt = (
            None
            if raw_prompt is None
            else resolve_auth_prompt("(global oauth)", raw_prompt, None, public_url)
        )
        return cls(
            cache_tokens=bool(data.get("cache_tokens", True)),
            token_dir=data.get("token_dir") or data.get("tokenDir"),
            public_url=public_url,
            prompt=prompt,
        )


class IsolationConfig(BaseModel):
    """Lifecycle knobs for token-keyed isolated upstream sessions.

    Example JSON (top-level section)::

        "isolation": {
            "grace_seconds": 300,
            "park_ttl_seconds": 3600
        }
    """

    grace_seconds: float = 300.0
    """How long a chat's upstream client stays connected after its last
    downstream session exits. Reconnects inside the window reattach live."""

    park_ttl_seconds: float = 3600.0
    """How long a parked upstream session id stays resumable. The effective
    resume window is min(this, the upstream server's own session expiry) —
    this bounds how long a resume probe is worth attempting, it is not a
    state-retention guarantee."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IsolationConfig:
        """Parse the top-level ``isolation`` config dict."""
        return cls(
            grace_seconds=float(data.get("grace_seconds", 300.0)),
            park_ttl_seconds=float(data.get("park_ttl_seconds", 3600.0)),
        )


class BindConfig(BaseModel):
    """The process bind (``"bind": {"host": ...}`` in servers.json).

    The durable home for the serve bind: CLI ``--host`` > env
    ``MCP_COMBINER_HOST`` > this setting > the ``127.0.0.1`` default. Binding
    ``0.0.0.0`` serves loopback + every interface (LAN + tailnet) — required
    for the remote widget leg (docs/designs/advertised-ui-origin.md).

    ``host`` may be a list — ``["127.0.0.1", "tailscale"]`` listens on loopback
    and the tailnet only (``tailscale`` resolves to this machine's tailnet
    address at startup); see ``mcp_combiner.binding``.
    """

    host: str | list[str] = "127.0.0.1"


class ValidationConfig(BaseModel):
    """Schema-validation gates (top-level ``"validation"`` in servers.json).

    Example JSON (top-level section)::

        "validation": {"input": true}

    ``true`` turns a gate on, ``false`` off, a missing key keeps the combiner
    default (input on, output off). Launch-path flags take precedence:
    ``--input-validation`` / ``--no-input-validation`` (or their
    ``MCP_COMBINER_*`` env forms) > the config value > the default. The gates
    run in ``ToolProcessingMiddleware`` (fastmcp 4 / mcp 2 validate neither
    for proxied tools).
    """

    input: bool | None = None
    """Strict JSON-schema validation of tool *call arguments* against the
    aggregated tool's inputSchema, before forwarding upstream. On (default) —
    a mismatched call fails at the combiner (``isError`` "Input validation
    error: ...", no upstream call). ``required`` names the schema's own
    ``properties`` never declare are ignored, and a call made before the
    tool list was ever fetched is not checked (fails open). ``false`` —
    inputs pass through and the upstream decides."""

    output: bool | None = None
    """Tool *result* validation against a declared outputSchema. Off
    (default): results are relayed and the end client validates what it
    consumes. ``true`` — a result contradicting the schema becomes an
    "Output validation error" at the combiner."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationConfig":
        """Parse the top-level ``validation`` config dict."""
        return cls(
            input=data.get("input"),
            output=data.get("output"),
        )


class CombinerConfig(BaseModel):
    """Full combiner configuration."""

    servers: dict[str, ServerConfig] = Field(default_factory=dict)
    shared_servers: dict[str, SharedServerConfig] = Field(default_factory=dict)
    external_servers: dict[str, ExternalServerConfig] = Field(default_factory=dict)
    oauth: OAuthConfig = Field(default_factory=OAuthConfig)
    isolation: IsolationConfig = Field(default_factory=IsolationConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    bind: BindConfig = Field(default_factory=BindConfig)
    permissions: PermissionPolicy | None = None
    """Global tool-call permission policy applied to every server, unless a
    server overrides it.  ``None`` (default) means **off** — no gate, identical
    to pre-feature behavior.  See :class:`PermissionPolicy`."""
    config_path: str = ""

    @classmethod
    def load(cls, config_path: str) -> CombinerConfig:
        """Load config from a ``servers.json`` file."""
        path = Path(config_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        try:
            with open(path) as f:
                raw: dict[str, Any] = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Config file {path} is not valid JSON: {exc}") from exc

        raw_servers: dict[str, Any] = raw.get("servers", raw.get("mcpServers", {}))
        servers = {
            name: ServerConfig.from_dict(name, srv_data) for name, srv_data in raw_servers.items()
        }

        raw_shared: dict[str, Any] = raw.get("sharedServers", {})
        shared_servers = {
            name: SharedServerConfig.from_dict(name, data) for name, data in raw_shared.items()
        }

        raw_external: dict[str, Any] = raw.get("externalServers", {})
        external_servers = {
            name: ExternalServerConfig.from_dict(name, data) for name, data in raw_external.items()
        }

        # A server entry can only have one backing process. sharedServer wins;
        # warn so the misconfiguration isn't silently dropped. Also warn about
        # dangling references now instead of at first lifecycle call.
        for name, srv in servers.items():
            if srv.shared_server and srv.external_server:
                logger.warning(
                    "Server '%s': both sharedServer and externalServer set — "
                    "using sharedServer '%s', ignoring externalServer '%s'",
                    name,
                    srv.shared_server,
                    srv.external_server,
                )
            for field, table, label in (
                (srv.shared_server, shared_servers, "sharedServers"),
                (srv.external_server, external_servers, "externalServers"),
            ):
                if field and field not in table:
                    logger.warning(
                        "Server '%s': references missing %s entry '%s' — "
                        "server will have no backing-process lifecycle",
                        name,
                        label,
                        field,
                    )

        raw_oauth: dict[str, Any] = raw.get("oauth", {})
        oauth = OAuthConfig.from_dict(raw_oauth) if raw_oauth else OAuthConfig()
        # Validates every server's prompt up front (a typo fails the load, not
        # the first re-auth) and flags elicitation that can't reach a remote device.
        for name, srv in servers.items():
            opts = oauth_options(srv.auth)
            if opts is None:
                continue
            prompt = resolve_auth_prompt(name, opts.get("prompt"), oauth.prompt, oauth.public_url)
            if prompt != "browser" and not oauth.public_url:
                logger.warning(
                    "Server '%s': oauth prompt %r without oauth.public_url — the "
                    "authorization link redirects to a loopback callback, so it "
                    "completes only from a browser on this host (a local chat), "
                    "not from another device",
                    name,
                    prompt,
                )

        raw_isolation: dict[str, Any] = raw.get("isolation", {})
        isolation = IsolationConfig.from_dict(raw_isolation) if raw_isolation else IsolationConfig()

        raw_validation: dict[str, Any] = raw.get("validation", {})
        validation = (
            ValidationConfig.from_dict(raw_validation) if raw_validation else ValidationConfig()
        )

        permissions = PermissionPolicy.from_dict(raw.get("permissions"))

        bind = BindConfig(**raw["bind"]) if isinstance(raw.get("bind"), dict) else BindConfig()

        return cls(
            servers=servers,
            shared_servers=shared_servers,
            external_servers=external_servers,
            bind=bind,
            oauth=oauth,
            isolation=isolation,
            validation=validation,
            permissions=permissions,
            config_path=str(path),
        )

    def effective_policy(self, server_name: str) -> ResolvedPolicy:
        """The concrete permission policy for *server_name*.

        Merges the global ``permissions`` with the server's own block and its
        ``auto_approve`` allow-list.  Cheap to call per request; callers should
        check :meth:`ResolvedPolicy.is_active` to skip the gate entirely.
        """
        srv = self.servers.get(server_name)
        return _merge_policies(
            self.permissions,
            srv.permissions if srv else None,
            list(srv.auto_approve) if srv else [],
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

    def resolve_external_server(self, server_name: str) -> ExternalServerConfig | None:
        """Return the ExternalServerConfig for a server, if it has one.

        ``None`` for url-only servers with no externalServer reference. Mirrors
        :meth:`resolve_shared_server` for the server's *own* reference — which
        section handles a server is decided by ``BackingProcessManager``'s
        manager order, not here; a server that sets both ``sharedServer`` and
        ``externalServer`` resolves under both, and the facade's claim order
        picks sharedserver (the load-time warning tells the author).
        """
        srv = self.servers.get(server_name)
        if srv is None or srv.external_server is None:
            return None
        ext = self.external_servers.get(srv.external_server)
        if ext is None:
            raise KeyError(
                f"Server '{server_name}' references unknown externalServer '{srv.external_server}'"
            )
        return ext

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
            external_server=srv.external_server,
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
