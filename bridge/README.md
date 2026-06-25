# mcp-bridge

FastMCP proxy bridge for [mcp-companion](https://github.com/georgeharker/mcp-companion).

Aggregates multiple MCP servers through a single Streamable HTTP endpoint.

## Install

Needs only [uv](https://docs.astral.sh/uv/) — `uvx` fetches and runs it, no venv to manage:

```bash
uvx mcp-bridge --help                                                # once published to PyPI
# before PyPI (or to track main) — note the subdirectory, the package lives in bridge/:
uvx --from "git+https://github.com/georgeharker/mcp-companion#subdirectory=bridge" mcp-bridge
```

Or install it: `uv pip install mcp-bridge` (PyPI), or from the repo subdir
`uv pip install "git+https://github.com/georgeharker/mcp-companion#subdirectory=bridge"`.

## Usage

```bash
mcp-bridge --config /path/to/servers.json --port 9741
```

## Development

```bash
uv sync
pytest
```
