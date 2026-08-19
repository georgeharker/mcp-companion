# @geohar/pi-mcp-combiner

A [Pi](https://pi.dev) extension that makes the **`mcp-combiner`** MCP aggregator
available to Pi: it starts the combiner (supervised by
[`sharedserver`](https://github.com/georgeharker/sharedserver)) and appends the
combiner's tool-discovery directive to the system prompt.

It is the Pi counterpart of the
[Claude Code](https://github.com/georgeharker/mcp-companion/tree/main/plugins/claude)
and
[OpenCode](https://github.com/georgeharker/mcp-companion/tree/main/plugins/opencode)
plugins, and shares the same combiner and the same `sharedserver` instance — so Pi,
Claude Code, OpenCode, and Neovim can all talk to **one** refcounted combiner process.

## How it fits together

Pi has no MCP of its own. Two pieces give it the combiner:

1. **[`pi-mcp-adapter`](https://pi.dev/packages/pi-mcp-adapter)** — the Pi package that
   actually speaks MCP. It reads its own `mcp.json` and connects to the combiner over
   HTTP. **This extension does not replace it — you install both.**
2. **This extension** — the process + instructions half, mirroring the sibling plugins:
   - **Run the combiner** — on `session_start` it drives
     ```
     sharedserver use <name> --pid <pi-pid> --grace-period <g> \
         -- <combiner> --mcp --config <servers.json> --port <port>
     ```
     `sharedserver` refcounts by PID with a grace period, so the combiner is shared
     across clients and outlives any single one. It releases the refcount on
     `session_shutdown` — but only when `reason === "quit"`, since reload/resume/fork
     keep the same Pi process and a fresh `session_start` re-attaches.
   - **Inject instructions** — on `before_agent_start` it appends the combiner's
     `<server>_`-prefix / "discover before assuming" directive to the system prompt. The
     combiner also serves the same text as its MCP `instructions`, which `pi-mcp-adapter`
     surfaces on connect, so this is a belt to those braces.

`sharedserver` itself is fetched automatically if it is not already installed (a pinned
release via the cargo-dist installer — no Rust toolchain needed), using the exact same
resolver as the other two plugins.

## Requirements

- **`pi-mcp-adapter`** installed in Pi: `pi install npm:pi-mcp-adapter`.
- **`mcp-combiner`** available as a command (`uv tool install mcp-combiner`), or just
  **`uv`** on PATH — a pinned release is fetched from PyPI on demand. Requires combiner
  ≥ 0.8.0 (the `--mcp` serve flag; version-gated automatically).
- A combiner **`servers.json`** (see auto-probe locations below).

## Install

### 1. Point `pi-mcp-adapter` at the combiner

Drop the combiner entry into one of `pi-mcp-adapter`'s `mcp.json` locations — e.g.
project-local `.pi/mcp.json`, or global `~/.config/mcp/mcp.json`:

```json
{
  "mcpServers": {
    "mcp-combiner": {
      "url": "http://127.0.0.1:9741/mcp",
      "auth": "bearer",
      "bearerTokenEnv": "MCP_COMBINER_AUTH_TOKEN"
    }
  }
}
```

Or let the extension write it for you — **`/mcp-combiner install-config`** merges
exactly this entry into `~/.config/mcp/mcp.json` (or a path you pass), preserving
any other servers and any existing `url` you set. (It only writes when you ask —
the extension never edits the adapter's config on its own.)

`"auth": "bearer"` with `"bearerTokenEnv"` is the correct single pairing — it does
two jobs at once:

- **Sends the token.** `pi-mcp-adapter` only attaches `Authorization: Bearer …`
  when `auth === "bearer"` (`server-manager.ts`); `bearerTokenEnv` names the env
  var it reads at connect. So if you lock the combiner down with an inbound bearer
  (`MCP_COMBINER_AUTH_TOKEN`, see the combiner README), the header is presented —
  nothing is written to disk. **Note:** `bearerTokenEnv` alone, or with `auth:
  false`, does **not** send the header — the adapter gates it on `auth === "bearer"`.
- **Suppresses OAuth.** `auth: "bearer"` also makes the adapter's `supportsOAuth()`
  false, so a wrong/missing-token 401 surfaces as an honest error instead of the
  spurious `Failed to start OAuth … DCR rejected (HTTP 404)` probe.

Harmless when the combiner is **open**: the env var is unset, so no header is sent,
the endpoint returns 200, and OAuth still never fires. So this one entry is correct
whether or not inbound auth is enabled.

(See [`mcp.json.example`](./mcp.json.example).) To give this Pi instance its own chat
identity toward the combiner — parking its isolated upstream sessions separately — add a
path token: `"url": "http://127.0.0.1:9741/mcp/pi-<yourname>"`. The combiner gives URL
tokens first priority.

If you edit `mcp.json` while Pi is running, run `/reload` (or `mcp({ connect:
"mcp-combiner" })`) so the adapter re-reads it.

### 2. Install this extension

Any of Pi's extension-loading mechanisms — all support a **local directory**:

```sh
# a) drop-in package dir (uses "main": dist/index.js — run `npm run build` first)
npm --prefix plugins/pi run build
ln -s "$PWD/plugins/pi" ~/.pi/agent/extensions/mcp-combiner

# b) one-off, build-free live dev — Pi loads the TS source directly
pi -e ./plugins/pi/src/index.ts

# c) settings.json — list a local path under "extensions"
#    { "extensions": ["/abs/path/to/mcp-companion/plugins/pi/src/index.ts"] }

# d) published package — list under "packages" in settings.json
#    { "packages": ["@geohar/pi-mcp-combiner@latest"] }
```

## Configuration

Everything is via the `PI_MCP_COMBINER_*` environment namespace, which mirrors the Claude
plugin's `CLAUDE_MCP_COMBINER_*` and the OpenCode plugin's `OPENCODE_MCP_COMBINER_*` — so
running several clients means one namespace per client.

| Variable | Default | Effect |
|----------|---------|--------|
| `PI_MCP_COMBINER_PORT` | `9741` | HTTP port the combiner serves on. |
| `PI_MCP_COMBINER_HOST` | `127.0.0.1` | HTTP host the combiner binds. |
| `PI_MCP_COMBINER_CONFIG` | *(auto-probed)* | Path to the combiner's `servers.json`. |
| `PI_MCP_COMBINER_COMMAND` / `_ARGS` | *(auto-resolved)* | Override the combiner invocation. |
| `PI_MCP_COMBINER_CHECKOUT` | — | Checkout for `uv run --project <checkout> python -m mcp_combiner`. |
| `PI_MCP_COMBINER_NAME` | `mcp-combiner` | `sharedserver` instance name. |
| `PI_MCP_COMBINER_GRACE` | `30m` | `sharedserver` grace period. |
| `PI_MCP_COMBINER_LOG` | `~/.local/state/mcp-combiner/mcp-combiner.log` | Capture the combiner's stdout/stderr; `"none"` disables. |
| `PI_MCP_COMBINER_PYLOG` | `~/.local/state/mcp-combiner/mcp-combiner-py.log` | The combiner's own `--log-file`; `"none"` disables. |
| `PI_MCP_COMBINER_LOG_LEVEL` | `info` | The combiner's `--log-level`. |
| `PI_MCP_COMBINER_MANAGE` | `true` | `false` → don't launch (assume the combiner runs elsewhere); instructions only. |
| `PI_MCP_COMBINER_INSTRUCTIONS` | `true` | `false` → don't append the directive to the system prompt. |
| `PI_MCP_COMBINER_NOTIFY` | `true` | `false` → don't surface attach/health messages via the Pi UI. |
| `SHAREDSERVER_BIN` | *(auto-resolved)* | Path to the `sharedserver` binary. |
| `SHAREDSERVER_LOCKDIR` | — | `sharedserver` lock directory. |

### `servers.json` auto-probe

`$PI_MCP_COMBINER_CONFIG` → `~/.cache/secrets/<user>.mcpservers.json` →
`~/.config/mcp-combiner/servers.json` → `~/.config/mcp/servers.json`.

### Combiner command resolution

`$PI_MCP_COMBINER_COMMAND` (+`$PI_MCP_COMBINER_ARGS`) → `mcp-combiner` on `PATH` (only
when ≥ 0.8.0) → `uv run --project <checkout> python -m mcp_combiner` → a pinned release
via `uvx`. If the `mcp-combiner` on PATH is older than 0.8.0, the extension warns and
falls back to a pinned `uvx` release rather than silently using a stale binary.

## Host-owned mode

If **`$MCP_COMPANION_COMBINER_URL`** is set, an editor/host (e.g. Neovim) already owns
and refcounts the combiner. The extension then **only injects instructions** and never
starts or stops the process — the same early-exit as the sibling plugins. Equivalent to
`PI_MCP_COMBINER_MANAGE=false`.

## Development

```sh
npm install
npm run typecheck
npm run build      # emits dist/ (not committed; built on publish)
```

The `src/sharedserver-resolve.ts` file is **vendored byte-identical** from
[`georgeharker/sharedserver`](https://github.com/georgeharker/sharedserver) (via
`scripts/sync-vendored.sh`), shared with the OpenCode plugin so both answer "which
sharedserver, and why" identically. Edit it upstream; re-sync here.

## License

MIT © George Harker
