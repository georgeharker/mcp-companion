# mcp-bridge

FastMCP proxy bridge for [mcp-companion.nvim](https://github.com/georgeharker/mcp-companion.nvim).

Aggregates multiple MCP servers through a single Streamable HTTP endpoint.

## Usage

```bash
mcp-bridge --config /path/to/servers.json --port 9741
```

## Development

```bash
uv sync
pytest
```
