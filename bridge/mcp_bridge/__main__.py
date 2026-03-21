"""CLI entry point for mcp-bridge."""

import argparse

from mcp_bridge.server import create_bridge


def main():
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
    args = parser.parse_args()

    bridge = create_bridge(args.config)
    bridge.run(
        transport="http",
        host=args.host,
        port=args.port,
        stateless_http=True,  # No session IDs — prevents proxy corruption on client disconnect
    )


if __name__ == "__main__":
    main()
