"""CLI entry point for mcp-bridge."""

import argparse

from mcp_bridge.server import create_bridge


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

    bridge = create_bridge(
        args.config,
        oauth_cache_tokens=args.oauth_cache,
        oauth_token_dir=args.oauth_token_dir,
    )
    bridge.run(
        transport="http",
        host=args.host,
        port=args.port,
        stateless_http=True,  # No session IDs — prevents proxy corruption on client disconnect
    )


if __name__ == "__main__":
    main()
