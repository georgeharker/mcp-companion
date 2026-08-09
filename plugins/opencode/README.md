# @geohar/opencode-mcp-combiner

An [OpenCode](https://opencode.ai) plugin that makes the **`mcp-combiner`** MCP
aggregator available to OpenCode: it starts the combiner (supervised by
[`sharedserver`](https://github.com/georgeharker/sharedserver)) and registers its
HTTP MCP endpoint with OpenCode.

It is the OpenCode counterpart of the Claude Code plugin
[`mcp-combiner`](https://github.com/georgeharker/mcp-companion/tree/main/plugins/claude), and
shares the same combiner and the same `sharedserver` instance — so OpenCode,
Claude Code, and Neovim can all talk to **one** refcounted combiner process.

## What it does

The plugin has two independent responsibilities (mirroring the Claude plugin,
which uses session hooks + a static `.mcp.json`):

1. **Run the combiner** — drives `sharedserver`:
   ```
   sharedserver use <name> --pid <opencode-pid> --grace-period <g> \
       -- <combiner> --mcp --config <servers.json> --port <port>
   ```
   `sharedserver` refcounts by PID with a grace period, so the combiner is
   shared across clients and outlives any single one. The plugin runs `unuse` on
   OpenCode exit.

2. **Register the endpoint** — via the OpenCode `config` hook it injects a
   remote MCP server into your config:
   ```jsonc
   { "mcp": { "mcp-combiner": { "type": "remote", "url": "http://127.0.0.1:9741/mcp", "enabled": true } } }
   ```
   (OpenCode has no static `.mcp.json`, so registration is programmatic.)

The two halves are independent: registration is unconditional — OpenCode tracks
connection state (`McpStatus`) and reconnects if the endpoint is briefly absent
while the combiner binds its port. After startup the plugin polls
`client.mcp.status()` and toasts `connected` / `failed` accordingly.

## Requirements

- **`sharedserver`** on `PATH` (or `$SHAREDSERVER_BIN`) — `cargo install sharedserver`.
- **`mcp-combiner`** available as a command (`uv tool install mcp-combiner`, a
  console script on `PATH`), or a checkout runnable via `uv run`. Requires
  combiner ≥ 0.8.0 (the `--mcp` serve flag; version-gated automatically).
- A combiner **`servers.json`** (see auto-probe locations below).

## Install

Add it to `~/.config/opencode/config.json` under `plugin`, tuple form
`[spec, options]`:

```json
{
  "plugin": [
    ["@geohar/opencode-mcp-combiner@latest", {
      "port": 9741,
      "config": "~/.config/mcp-combiner/servers.json",
      "gracePeriod": "30m"
    }]
  ]
}
```

Bare-string form (`"@geohar/opencode-mcp-combiner@latest"`) also works and uses
all defaults.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `port` | `9741` | HTTP port the combiner serves on. |
| `host` | `127.0.0.1` | HTTP host the combiner binds. |
| `config` | *(auto-probed)* | Path to the combiner's `servers.json`. |
| `command` | *(auto-resolved)* | Override the combiner command. |
| `args` | `[]` | Extra combiner args (before the serve args). |
| `checkout` | — | Combiner checkout for `uv run --project <checkout> python -m mcp_combiner`. |
| `name` | `mcp-combiner` | `sharedserver` instance name. |
| `gracePeriod` | `30m` | `sharedserver` grace period (`30m`, `1h`, …). |
| `logFile` | `~/.local/state/mcp-combiner/mcp-combiner.log` | Capture the combiner's stdout/stderr (`sharedserver --log-file`); `"none"` disables. |
| `pyLogFile` | `~/.local/state/mcp-combiner/mcp-combiner-py.log` | The combiner's own `--log-file` (fastmcp/OAuth/httpx detail); `"none"` disables. |
| `logLevel` | `info` | The combiner's `--log-level`. |
| `binary` | *(auto-resolved)* | Path to the `sharedserver` binary. |
| `lockdir` | — | Override `SHAREDSERVER_LOCKDIR`. |
| `mcpName` | `mcp-combiner` | Key under OpenCode's `mcp` config. |
| `url` | `http://127.0.0.1:<port>/mcp` | Explicit MCP URL to register. |
| `register` | `true` | Register the endpoint with OpenCode. |
| `manage` | `true` | Start the combiner. `false` → **register only** (something else runs it). |
| `notify` | `true` | Show TUI toasts for attach/health outcomes. |

### Combiner command resolution

In priority order: `command` option → `$OPENCODE_MCP_COMBINER_COMMAND`
(+`$OPENCODE_MCP_COMBINER_ARGS`) → `mcp-combiner` on `PATH` →
`uv run --project <checkout> python -m mcp_combiner` (when `checkout` /
`$OPENCODE_MCP_COMBINER_CHECKOUT` is set).

### `servers.json` auto-probe

`config` option → `$OPENCODE_MCP_COMBINER_CONFIG` →
`~/.cache/secrets/<user>.mcpservers.json` →
`~/.config/mcp-combiner/servers.json` → `~/.config/mcp/servers.json`.

## Host-owned mode

If **`$MCP_COMPANION_COMBINER_URL`** is set, an editor/host (e.g. Neovim) already
owns and refcounts the combiner. The plugin then **only registers** that URL and
never starts or stops the process — the same early-exit as the Claude Code `mcp-combiner` plugin.
Equivalent to setting `manage: false` with an explicit `url`.

## Environment variables

| Variable | Effect |
|----------|--------|
| `MCP_COMPANION_COMBINER_URL` | Host owns the combiner → register only, don't launch. |
| `OPENCODE_MCP_COMBINER_COMMAND` / `_ARGS` | Override the combiner invocation. |
| `OPENCODE_MCP_COMBINER_CHECKOUT` | Checkout path for `uv run`. |
| `OPENCODE_MCP_COMBINER_CONFIG` | `servers.json` path. |
| `OPENCODE_MCP_COMBINER_PORT` | Port the combiner serves on. Default `9741`. |
| `OPENCODE_MCP_COMBINER_NAME` | `sharedserver` instance name. Default `mcp-combiner`. |
| `OPENCODE_MCP_COMBINER_GRACE` | `sharedserver` grace period. Default `30m`. |
| `OPENCODE_MCP_COMBINER_LOG` | Stdout/stderr capture path (default `~/.local/state/mcp-combiner/mcp-combiner.log`; `none` disables). |
| `OPENCODE_MCP_COMBINER_PYLOG` | The combiner's own `--log-file` (default `…/mcp-combiner-py.log`; `none` disables). |
| `OPENCODE_MCP_COMBINER_LOG_LEVEL` | The combiner's `--log-level`. Default `info`. |
| `SHAREDSERVER_BIN` | Path to the `sharedserver` binary. |
| `SHAREDSERVER_LOCKDIR` | `sharedserver` lock directory. |

Plugin options take precedence over the environment. These mirror the Claude plugin's
`CLAUDE_MCP_COMBINER_*` set, so running both clients means one namespace per client
rather than options in one place and env vars in the other.

If `mcp-combiner` is on `PATH` but older than `0.8.0`, the plugin logs **and toasts** a
warning, then falls back to a pinned release fetched with `uvx` — it will not silently
use a stale binary, nor silently route around one you installed deliberately.

## Relationship to `opencode-sharedserver`

[`@geohar/opencode-sharedserver`](https://github.com/georgeharker/opencode-sharedserver)
is a **generic** process supervisor plugin. This plugin is
**combiner-specific**: it knows the correct serve invocation (`--mcp --config
--port`, version-gated) and — unlike the generic one — registers the MCP
endpoint with OpenCode. It vendors the same `sharedserver` attach/detach logic
rather than depending on the generic plugin, so either can be used independently.

## Development

```sh
bun install        # or npm install
npm run typecheck
npm run build      # emits dist/ (not committed; built on publish)
```

## License

MIT © George Harker
