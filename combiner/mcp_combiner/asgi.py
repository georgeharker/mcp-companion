"""ASGI app assembly for serve mode.

``create_app`` builds the combiner ASGI app from explicit ``ServeOptions``
(the CLI→env→re-parse round-trip is gone; the ``MCP_COMBINER_*`` env vars
remain as a fallback input for factory-style launches). The HTTP-layer
middlewares moved here verbatim from __main__.py; their session bookkeeping
goes through RUNTIME instead of reaching into server-module privates.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

from mcp_combiner.runtime import RUNTIME
from mcp_combiner.server import create_combiner
from mcp_combiner.sharedserver import register_for_cleanup

logger = logging.getLogger(__name__)

_mcp_log = logging.getLogger("mcp-combiner.requests")

# Header name the Neovim plugin sets on ACP-injected mcpServers entries.
_ACP_TOKEN_HEADER = "x-mcp-combiner-session"

# UUID pattern: validates tokens from both header and URL path.
_TOKEN_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Match /mcp/<uuid>[/...] in the URL path.
_MCP_TOKEN_PATH_RE = re.compile(
    r"^/mcp/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(/.*)?$"
)

# NOTE (QUESTIONS.md Q1): token→session mappings recorded here carry the wire
# mcp-session-id — a different namespace from Context.session_id. Preserved.


@dataclass
class ServeOptions:
    """Everything serve mode needs, resolved from CLI args (or env fallback)."""

    config: str
    host: str = "127.0.0.1"
    port: int = 9741
    oauth_cache: bool | None = None
    oauth_token_dir: str | None = None
    normalize_schema: bool = False
    schema_fixes: list[str] = field(default_factory=list)
    input_validation: bool | None = None
    output_validation: bool | None = None
    stale_tool_grace: float | None = None
    restore: str | None = None
    log_file: str | None = None
    log_level: str = "info"

    @classmethod
    def from_env(cls) -> ServeOptions:
        """Fallback construction from MCP_COMBINER_* env vars (factory launches)."""

        def _tristate(name: str) -> bool | None:
            v = os.environ.get(name)
            if v is None:
                return None
            return v in ("1", "True")

        fixes_env = os.environ.get("MCP_COMBINER_SCHEMA_FIXES")
        grace_env = os.environ.get("MCP_COMBINER_STALE_TOOL_GRACE")
        return cls(
            config=os.environ["MCP_COMBINER_CONFIG"],
            oauth_cache=_tristate("MCP_COMBINER_OAUTH_CACHE"),
            oauth_token_dir=os.environ.get("MCP_COMBINER_OAUTH_TOKEN_DIR"),
            normalize_schema=os.environ.get("MCP_COMBINER_NORMALIZE_SCHEMA") == "1",
            schema_fixes=[f for f in fixes_env.split(",") if f] if fixes_env else [],
            input_validation=_tristate("MCP_COMBINER_INPUT_VALIDATION"),
            output_validation=_tristate("MCP_COMBINER_OUTPUT_VALIDATION"),
            stale_tool_grace=float(grace_env) if grace_env else None,
        )


class TokenRewriteMiddleware(BaseHTTPMiddleware):
    """Map token -> MCP session-id and apply pending filters on connect.

    Accepts the token from two sources:
      1. URL path: /mcp/<token>[/...] — rewrites to /mcp so FastMCP sees a plain request.
      2. HTTP header: X-MCP-Combiner-Session — fallback.

    On first request carrying a token, records token->session_id from the response
    header.  If a pending filter was stored via POST /sessions/token/<token>/filter
    before the client connected, it is applied immediately.
    """

    # (client host, client port) -> (old wire session id, monotonic deadline).
    # Populated when a request presents a restored-but-unknown session id and
    # gets the spec 404; claimed by the re-initialize that follows. Shared
    # across dispatches — BaseHTTPMiddleware is one instance per app.
    _pending_reassoc: dict[tuple[str, int], tuple[str, float]]
    _REASSOC_TTL = 10.0

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._pending_reassoc = {}

    async def _dispatch_tokenless(
        self, request: StarletteRequest, call_next: RequestResponseEndpoint
    ) -> Response:
        """Tokenless requests: the wire ``Mcp-Session-Id`` is the chat name.

        PROPOSAL under validation (see the design graph): across a sanctioned
        combiner restart the old wire id dies with the predecessor, but the
        client's first post-restart request still PRESENTS it — the request
        that 404s before the SDK re-initializes. That 404 is the correlation
        point: stash (client addr → old id), and when the re-initialize on
        the same kept-alive connection mints the new id, rename the old id's
        restored parked state to the new identity ("the chat formerly called
        X"). Every miss fails open to a fresh session.
        """
        import time as _time

        from mcp_combiner.isolated import (
            REGISTRY as _isolated_registry,
        )
        from mcp_combiner.isolated import (
            SID_PREFIX as _sid_prefix,
        )

        request_sid = request.headers.get("mcp-session-id")
        client = request.client
        addr = (client.host, client.port) if client is not None else None

        response = await call_next(request)

        # Session DELETE from a tokenless client: same park-expedite hint as
        # the tokened path — the wire id is the group key.
        if request.method == "DELETE" and request_sid:
            _isolated_registry.expedite_park(_sid_prefix + request_sid)
            return response

        now = _time.monotonic()

        # A restored identity was presented and refused: remember who asked.
        if (
            response.status_code == 404
            and request_sid
            and _isolated_registry.has_parked_sid(request_sid)
        ):
            for key, (_sid, deadline) in list(self._pending_reassoc.items()):
                if deadline < now:
                    self._pending_reassoc.pop(key, None)
            if addr is not None:
                self._pending_reassoc[addr] = (request_sid, now + self._REASSOC_TTL)
                logger.info(
                    "reassoc: chat formerly called %s… is reconnecting (from %s:%d)",
                    request_sid[:8],
                    addr[0],
                    addr[1],
                )
            return response

        # A fresh initialize minted a new id: claim a pending old identity —
        # same connection (addr match) first, else an unambiguous singleton.
        if request_sid is None:
            new_sid = response.headers.get("mcp-session-id")
            if new_sid and self._pending_reassoc:
                live = {
                    k: v for k, v in self._pending_reassoc.items() if v[1] >= now
                }
                old_sid: str | None = None
                if addr is not None and addr in live:
                    old_sid = live[addr][0]
                    self._pending_reassoc.pop(addr, None)
                elif len(live) == 1:
                    key, (old_sid, _deadline) = next(iter(live.items()))
                    self._pending_reassoc.pop(key, None)
                if old_sid is not None:
                    _isolated_registry.rename_sid(old_sid, new_sid)

        return response

    async def dispatch(
        self, request: StarletteRequest, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # --- Source 1: token in URL path ---
        url_token: str | None = None
        path_match = _MCP_TOKEN_PATH_RE.match(path)
        if path_match:
            url_token = path_match.group(1)
            remainder = path_match.group(2) or ""
            new_path = f"/mcp{remainder}"
            logger.info(
                "Token in URL path: token=%s  %s -> %s",
                url_token,
                path,
                new_path,
            )
            # Mutate scope in-place; BaseHTTPMiddleware passes the same scope dict
            # to call_next so FastMCP receives the rewritten path.
            request.scope["path"] = new_path
            request.scope["raw_path"] = new_path.encode()
            # Re-surface the URL token as a header so the FastMCP-layer
            # middleware can build the session_id -> token reverse map (it only
            # sees context.session_id, never the URL — see
            # ToolProcessingMiddleware.on_request).
            #
            # WHY this is needed: header-sending clients (Claude Code, OpenCode,
            # the documented ACP entry) already send X-MCP-Combiner-Session, so for
            # them this is a redundant no-op. But URL-only transports — notably
            # the stdio `mcp-remote` fallback, which forwards neither env nor
            # headers, only the URL — would otherwise never get the token to the
            # FastMCP layer, breaking neovim_* routing for that session. This
            # injection makes /mcp/<token> a self-sufficient correlation channel.
            # Replace any existing value so URL wins over a stale header.
            hdr = _ACP_TOKEN_HEADER.encode()
            headers = [(k, v) for (k, v) in request.scope["headers"] if k.lower() != hdr]
            headers.append((hdr, url_token.encode()))
            request.scope["headers"] = headers

        # --- Source 2: token in header ---
        header_token: str | None = request.headers.get(_ACP_TOKEN_HEADER)
        if header_token and not _TOKEN_RE.match(header_token):
            header_token = None

        token = url_token or header_token

        if token is None:
            return await self._dispatch_tokenless(request, call_next)

        already_mapped = RUNTIME.sessions.session_for_token(token) is not None
        if not already_mapped:
            logger.info(
                "Token not yet mapped: token=%s  source=%s  method=%s",
                token,
                "url" if url_token else "header",
                request.method,
            )
        else:
            logger.debug(
                "Token already mapped: token=%s  session=%s",
                token,
                RUNTIME.sessions.session_for_token(token),
            )

        response = await call_next(request)

        # (Observed via GET /sessions/map: isolated_live → isolated_parked.)
        # An explicit session DELETE from a tokened client skips the grace
        # window for that token's isolated upstream sessions: the client
        # declared this downstream session finished, so park now (never
        # forget — DELETE is ambiguous between chat-done and a clean
        # transport cycle, and parked serves both).
        if request.method == "DELETE":
            from mcp_combiner.isolated import REGISTRY as _isolated_registry

            _isolated_registry.expedite_park(token)

        if not already_mapped:
            sid = response.headers.get("mcp-session-id")
            if sid:
                RUNTIME.sessions.map_token(token, sid)
                logger.info(
                    "Token mapped: token=%s  session=%s  source=%s",
                    token,
                    sid,
                    "url" if url_token else "header",
                )
                # Apply any pending filter that was stored before the client connected
                pending = RUNTIME.sessions.pop_pending(token)
                if pending:
                    RUNTIME.sessions.set_disabled(sid, pending)
                    logger.info(
                        "Pending token filter applied: token=%s  session=%s  disabled=%s",
                        token,
                        sid,
                        sorted(pending),
                    )
            else:
                logger.debug(
                    "Token seen but no mcp-session-id in response: token=%s  status=%d  source=%s",
                    token,
                    response.status_code,
                    "url" if url_token else "header",
                )

        return response


class MCPRequestLogMiddleware(BaseHTTPMiddleware):
    """Log /mcp requests: debug-level detail on every request, warnings on non-2xx."""

    async def dispatch(
        self, request: StarletteRequest, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        is_mcp = path == "/mcp" or path.startswith("/mcp/")
        if is_mcp and _mcp_log.isEnabledFor(logging.DEBUG):
            session_id = request.headers.get("mcp-session-id", "-")
            acp_token_hdr = request.headers.get(_ACP_TOKEN_HEADER, "-")
            user_agent = request.headers.get("user-agent", "-")
            accept = request.headers.get("accept", "-")
            _mcp_log.debug(
                "%s %s  session=%s  acp-token-hdr=%s  ua=%s  accept=%s  all_headers=%s",
                request.method,
                path,
                session_id,
                acp_token_hdr,
                user_agent,
                accept,
                dict(request.headers),
            )
        response = await call_next(request)
        if is_mcp and response.status_code >= 400:
            session_id = request.headers.get("mcp-session-id", "-")
            user_agent = request.headers.get("user-agent", "-")
            _mcp_log.warning(
                "%s %s  => %d  session=%s  ua=%s",
                request.method,
                path,
                response.status_code,
                session_id,
                user_agent,
            )
        return response


def create_app(options: ServeOptions | None = None) -> Starlette:
    """Build the combiner ASGI app.

    Pass explicit *options* (the serve path); with ``None``, options are read
    from ``MCP_COMBINER_*`` env vars for factory-style launches.
    """
    if options is None:
        options = ServeOptions.from_env()

    combiner, ss_manager = create_combiner(
        options.config,
        oauth_cache_tokens=options.oauth_cache,
        oauth_token_dir=options.oauth_token_dir,
        normalize_schemas=options.normalize_schema,
        schema_fixes=frozenset(options.schema_fixes) if options.schema_fixes else None,
        input_validation=options.input_validation,
        output_validation=options.output_validation,
        stale_tool_grace=options.stale_tool_grace,
        return_ss_manager=True,
    )

    # Register manager for cleanup on exit
    register_for_cleanup(ss_manager)

    # Sanctioned-restart handover: consume the predecessor's one-shot payload
    # (token filters, nvim binds/instances, parked upstream sessions) before
    # serving. The file is deleted regardless of outcome; a refused snapshot
    # (version/staleness) just means booting fresh.
    if options.restore:
        from mcp_combiner.handover import load_handover

        load_handover(options.restore)

    # Use streamable HTTP with stateful mode.
    # Stateless mode doesn't support GET for SSE streams, which OpenCode needs.
    app = combiner.http_app(
        path="/mcp",
        stateless_http=False,
    )
    app.add_middleware(MCPRequestLogMiddleware)
    # TokenRewriteMiddleware is outermost (last-added in Starlette = outermost).
    # It extracts the ACP token from /mcp/<token> URL paths and rewrites to /mcp
    # before the log middleware and FastMCP see the request.
    app.add_middleware(TokenRewriteMiddleware)
    return app
