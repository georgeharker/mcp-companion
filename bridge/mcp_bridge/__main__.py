"""CLI entry point for mcp-bridge."""

import argparse
import atexit
import logging
import os
import signal
import sys

import uvicorn

from mcp_bridge.server import create_bridge
from mcp_bridge.sharedserver import cleanup as cleanup_sharedservers
from mcp_bridge.sharedserver import register_for_cleanup

logger = logging.getLogger(__name__)


def _signal_handler(signum, frame):
    """Handle termination signals."""
    logger.info("Received signal %d, cleaning up...", signum)
    cleanup_sharedservers()
    sys.exit(0)


def create_app():
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

    args = parser.parse_args()

    # Set env vars for app factory
    os.environ["MCP_BRIDGE_CONFIG"] = args.config
    if args.oauth_cache is not None:
        os.environ["MCP_BRIDGE_OAUTH_CACHE"] = str(args.oauth_cache)
    if args.oauth_token_dir:
        os.environ["MCP_BRIDGE_OAUTH_TOKEN_DIR"] = args.oauth_token_dir

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
