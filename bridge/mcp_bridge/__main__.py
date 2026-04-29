"""CLI entry point for mcp-bridge."""

import argparse
import atexit
import logging
import os
import re
import signal
import sys
import types

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

from mcp_bridge.server import _pending_token_filters, _token_sessions, create_bridge
from mcp_bridge.sharedserver import cleanup as cleanup_sharedservers
from mcp_bridge.sharedserver import register_for_cleanup

logger = logging.getLogger(__name__)

_mcp_log = logging.getLogger("mcp-bridge.requests")

# Header name the Neovim plugin sets on ACP-injected mcpServers entries.
_ACP_TOKEN_HEADER = "x-mcp-bridge-session"

# UUID pattern: validates tokens from both header and URL path.
_TOKEN_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Match /mcp/<uuid>[/...] in the URL path.
_MCP_TOKEN_PATH_RE = re.compile(
    r"^/mcp/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(/.*)?$"
)


class TokenRewriteMiddleware(BaseHTTPMiddleware):
    """Map token -> MCP session-id and apply pending filters on connect.

    Accepts the token from two sources:
      1. URL path: /mcp/<token>[/...] — rewrites to /mcp so FastMCP sees a plain request.
      2. HTTP header: X-MCP-Bridge-Session — fallback.

    On first request carrying a token, records token->session_id from the response
    header.  If a pending filter was stored via POST /sessions/token/<token>/filter
    before the client connected, it is applied immediately.
    """

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

        # --- Source 2: token in header ---
        header_token: str | None = request.headers.get(_ACP_TOKEN_HEADER)
        if header_token and not _TOKEN_RE.match(header_token):
            header_token = None

        token = url_token or header_token

        if token is None:
            return await call_next(request)

        already_mapped = token in _token_sessions
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
                _token_sessions[token],
            )

        response = await call_next(request)

        if not already_mapped:
            sid = response.headers.get("mcp-session-id")
            if sid:
                _token_sessions[token] = sid
                logger.info(
                    "Token mapped: token=%s  session=%s  source=%s",
                    token,
                    sid,
                    "url" if url_token else "header",
                )
                # Apply any pending filter that was stored before the client connected
                pending = _pending_token_filters.pop(token, None)
                if pending:
                    from mcp_bridge.server import _session_disabled

                    _session_disabled[sid] = pending
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


def _signal_handler(signum: int, frame: types.FrameType | None) -> None:
    """Handle termination signals."""
    logger.info("Received signal %d, cleaning up...", signum)
    cleanup_sharedservers()
    sys.exit(0)


def create_app() -> Starlette:
    """Factory function for creating the bridge ASGI app.

    Reads config from environment variables set by main().
    """
    config_path = os.environ["MCP_BRIDGE_CONFIG"]
    oauth_cache_str = os.environ.get("MCP_BRIDGE_OAUTH_CACHE")
    oauth_cache_tokens: bool | None = None
    if oauth_cache_str == "True":
        oauth_cache_tokens = True
    elif oauth_cache_str == "False":
        oauth_cache_tokens = False
    oauth_token_dir = os.environ.get("MCP_BRIDGE_OAUTH_TOKEN_DIR")

    bridge, ss_manager = create_bridge(
        config_path,
        oauth_cache_tokens=oauth_cache_tokens,
        oauth_token_dir=oauth_token_dir,
        return_ss_manager=True,
    )

    # Register manager for cleanup on exit
    register_for_cleanup(ss_manager)

    # Use streamable HTTP with stateful mode.
    # Stateless mode doesn't support GET for SSE streams, which OpenCode needs.
    app = bridge.http_app(
        path="/mcp",
        stateless_http=False,
    )
    app.add_middleware(MCPRequestLogMiddleware)
    # TokenRewriteMiddleware is outermost (last-added in Starlette = outermost).
    # It extracts the ACP token from /mcp/<token> URL paths and rewrites to /mcp
    # before the log middleware and FastMCP see the request.
    app.add_middleware(TokenRewriteMiddleware)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-bridge",
        description="MCP proxy bridge server",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to servers.json config file",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9741,
        help="Port to listen on (default: 9741)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )

    # OAuth token-caching overrides (both override the config-file 'oauth' section)
    oauth_group = parser.add_mutually_exclusive_group()
    oauth_group.add_argument(
        "--oauth-cache",
        dest="oauth_cache",
        action="store_true",
        default=None,
        help="Enable OAuth disk token caching (overrides config; this is the default)",
    )
    oauth_group.add_argument(
        "--no-oauth-cache",
        dest="oauth_cache",
        action="store_false",
        help=(
            "Disable OAuth disk token caching — tokens kept in memory only "
            "and lost on restart (overrides config)"
        ),
    )
    parser.add_argument(
        "--oauth-token-dir",
        metavar="PATH",
        default=None,
        help=(
            "Directory for OAuth token files "
            "(default: ~/.cache/mcp-companion/oauth-tokens; overrides config)"
        ),
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        default=None,
        help="Write logs to this file in addition to stderr (default: none)",
    )
    parser.add_argument(
        "--log-level",
        choices=["trace", "debug", "info", "warn", "error"],
        default="info",
        help=(
            "Verbosity for the bridge logger and httpx/mcp-client loggers "
            "(default: info).  Use 'debug' to capture OAuth metadata-discovery, "
            "token refresh, and httpx request/response detail."
        ),
    )

    args = parser.parse_args()

    # Set env vars for app factory
    os.environ["MCP_BRIDGE_CONFIG"] = args.config
    if args.oauth_cache is not None:
        os.environ["MCP_BRIDGE_OAUTH_CACHE"] = str(args.oauth_cache)
    if args.oauth_token_dir:
        os.environ["MCP_BRIDGE_OAUTH_TOKEN_DIR"] = args.oauth_token_dir

    # Resolve --log-level to a stdlib logging numeric level.
    # "trace" is treated as DEBUG since stdlib has no TRACE.
    _level_map = {
        "trace": logging.DEBUG,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }
    level = _level_map[args.log_level]

    # Stderr handler on the bridge logger.  Without this only WARNING+ would
    # appear because Python's root logger defaults to WARNING.
    bridge_logger = logging.getLogger("mcp-bridge")
    bridge_logger.setLevel(level)
    if not bridge_logger.handlers:
        stderr_handler = logging.StreamHandler()
        stderr_handler.setLevel(level)
        stderr_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        bridge_logger.addHandler(stderr_handler)
        bridge_logger.propagate = False  # avoid duplicate messages via root

    # Configure file logging if requested.  File handler always runs at the
    # requested level (decoupled from the file's presence so you can pick
    # INFO+file or DEBUG+stderr-only independently).
    if args.log_file:
        import pathlib

        log_path = pathlib.Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        # Root catches non-bridge loggers (fastmcp, mcp.client.auth, httpx, …)
        logging.getLogger().addHandler(file_handler)
        logging.getLogger().setLevel(level)
        # propagate=False on bridge_logger means the root handler won't see
        # its messages — attach explicitly.
        bridge_logger.addHandler(file_handler)
        logger.info("Logging to %s at level %s", log_path, args.log_level)
    else:
        # No file — still apply level globally so DEBUG-on-stderr works.
        logging.getLogger().setLevel(level)

    # At DEBUG, also turn on the SDK loggers that carry the OAuth flow detail.
    if level <= logging.DEBUG:
        for name in ("httpx", "httpcore", "mcp.client.auth", "fastmcp.client.auth"):
            logging.getLogger(name).setLevel(logging.DEBUG)

    # Register cleanup handlers
    atexit.register(cleanup_sharedservers)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Single worker - async handles concurrency
    app = create_app()
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
