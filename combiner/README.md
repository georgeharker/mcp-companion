# mcp-combiner

An **MCP aggregator** — fronts multiple MCP servers behind a single Streamable HTTP endpoint, so
one connection exposes every backend server's tools. Built on
[FastMCP](https://github.com/jlowin/fastmcp). Shareable across clients (via `sharedserver`), it
powers the [mcp-companion](https://github.com/georgeharker/mcp-companion) Neovim plugin and the
[`claude-mcp-combiner`](https://github.com/georgeharker/claude-mcp-combiner) Claude Code plugin, and works standalone with any MCP client.

> PyPI package · command · import package: **`mcp-combiner`** / `mcp-combiner` / `mcp_combiner`.

> ⚠️ **Renamed from `mcp-bridge`.** If you ran an earlier build:
> - command/import are now `mcp-combiner` / `mcp_combiner`; reinstall:
>   `uv tool uninstall mcp-bridge` then `uv tool install …` (see Install below).
> - config env vars `MCP_BRIDGE_*` → `MCP_COMBINER_*` (and `MCP_COMPANION_COMBINER_URL` →
>   `MCP_COMPANION_COMBINER_URL`).
> - OAuth token storage moved to `~/.cache/mcp-combiner/` — you'll **re-authenticate each MCP
>   server once** (old tokens under `~/.cache/mcp-companion/` are no longer read).

## Install

Needs only [uv](https://docs.astral.sh/uv/) — `uvx` fetches and runs it, no venv to manage:

```bash
uvx mcp-combiner --help                                                # once published to PyPI
# before PyPI (or to track main) — the package lives in the combiner/ subdirectory:
uvx --from "git+https://github.com/georgeharker/mcp-companion#subdirectory=combiner" mcp-combiner
```

Or install it: `uv pip install mcp-combiner` (PyPI), or from the repo subdir
`uv pip install "git+https://github.com/georgeharker/mcp-companion#subdirectory=combiner"`.

## Usage

```bash
mcp-combiner --config /path/to/servers.json --port 9741
```

## Development

```bash
uv sync
pytest
```
