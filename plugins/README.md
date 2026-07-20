# mcp-combiner plugins (Claude Code & OpenCode)

Standalone editor plugins that run the **mcp-combiner** aggregator via
[`sharedserver`](https://github.com/georgeharker/sharedserver) and register its
HTTP endpoint with your agent — the non-CodeCompanion way to share one combiner
process across Claude Code, OpenCode, and Neovim.

Both plugins live **in this repo** (previously separate repos, now retired):

| plugin | dir | for |
|---|---|---|
| `mcp-combiner` | [`plugins/claude`](https://github.com/georgeharker/mcp-companion/tree/main/plugins/claude) | Claude Code |
| `@geohar/opencode-mcp-combiner` | [`plugins/opencode`](https://github.com/georgeharker/mcp-companion/tree/main/plugins/opencode) | OpenCode |

Both attach to (or start) the combiner under the sharedserver name
`mcp-combiner`, so every client shares one refcounted process. When the host
editor already owns the combiner (CodeCompanion spawns the agent with
`MCP_COMPANION_COMBINER_URL` set), each plugin **registers only** and does not
launch — the host owns the lifecycle at its matching version.

## Prerequisites (both plugins)

1. **The combiner CLI** — **nothing to do**, as long as [`uv`](https://docs.astral.sh/uv/)
   is installed: the plugins fetch a pinned `mcp-combiner` from PyPI on demand.
   Install it only if you want one shared copy (faster start, works offline):

   ```sh
   uv tool install mcp-combiner     # or: pipx install mcp-combiner
   mcp-combiner --version           # sanity check
   ```

   ([How the combiner is resolved](#how-the-combiner-is-resolved) covers the full order.)

2. **`sharedserver`** — the refcounting process manager (Rust, on crates.io):

   ```sh
   cargo install sharedserver
   ```

   The Claude plugin bundles a resolver shim, so the binary just needs to be in
   a standard place: `$SHAREDSERVER_BIN`, `PATH`, `~/.cargo/bin`,
   `~/.local/bin`, `/opt/homebrew/bin`, or `/usr/local/bin`. OpenCode expects
   it on `PATH` (or `$SHAREDSERVER_BIN` / the `binary` option).

3. **A `servers.json`** — the combiner's config listing the MCP servers it
   fronts. Create it at `~/.config/mcp-combiner/servers.json`:

   ```json
   {
     "mcpServers": {
       "everything": {
         "command": "npx",
         "args": ["-y", "@modelcontextprotocol/server-everything"]
       },
       "clickup": {
         "url": "https://mcp.clickup.com/mcp",
         "transport": "http",
         "auth": "oauth"
       }
     }
   }
   ```

   Entries take the usual MCP fields — `command`/`args`/`env` (stdio) or
   `url`/`transport`/`headers` (HTTP/SSE) — plus combiner extras like
   `auth` (`"oauth"`, `{"bearer": "…"}`), `disabled`, `tool_filter`,
   `isolate`, and `sharedServer` (see the
   [root README](../README.md) for the full config reference).

   Both plugins and the CLI probe the same locations, in order: the
   `MCP_COMBINER_CONFIG` / `CLAUDE_MCP_COMBINER_CONFIG` env vars, then
   `~/.cache/secrets/$USER.mcpservers.json`,
   `~/.config/mcp-combiner/servers.json`, `~/.config/mcp/servers.json`.

## Claude Code — `mcp-combiner`

Installed from this repo's marketplace, inside a Claude Code session:

```
/plugin marketplace add georgeharker/mcp-companion
/plugin install mcp-combiner@mcp-companion
```

Then restart the session (the hooks run at session start). What it does:

- A `SessionStart` hook runs `sharedserver use … -- mcp-combiner --mcp
  --config … --port …` to attach to (or launch) the shared combiner;
  `SessionEnd` runs `unuse`. The process survives Claude restarts within the
  grace period (default 30m) and is shared with nvim / OpenCode sessions using
  the same name.
- The combiner's HTTP endpoint is registered as the `mcp-combiner` MCP server
  via the plugin's static `.mcp.json`
  (`${MCP_COMPANION_COMBINER_URL:-http://127.0.0.1:9741/mcp}`).

**Verify:** `/mcp` should list `mcp-combiner` as connected, and the session's
tools include the prefixed upstream tools (e.g. `everything_echo`) plus the
`combiner__status` / `combiner__reload_config` meta-tools. From a shell:
`mcp-combiner status` prints per-server state.

Knobs (env): `CLAUDE_MCP_COMBINER_COMMAND` / `_ARGS` / `_CHECKOUT` (which combiner
runs), `CLAUDE_MCP_COMBINER_CONFIG` (servers.json), `_PORT` (9741), `_GRACE`
(30m), `_NAME` (mcp-combiner), `_LOG`.

## OpenCode — `@geohar/opencode-mcp-combiner`

Add it to your `~/.config/opencode/config.json` `plugin` list — bare string for
all-defaults, or tuple form for options:

```json
{
  "plugin": [
    ["@geohar/opencode-mcp-combiner@latest", {
      "config": "~/.config/mcp-combiner/servers.json",
      "port": 9741
    }]
  ]
}
```

It injects the combiner into OpenCode's `mcp` config (a `type: "remote"` entry)
via the `config` hook, and drives sharedserver the same way as the Claude plugin.
Options (all optional): `mcpName`, `url`, `register`, `manage`, `binary`,
`lockdir`, `name`, `gracePeriod`, `logFile`, `command`/`args`/`checkout`,
`config`, `port` (9741), `host`, `notify`. See
[`plugins/opencode/README.md`](./opencode/README.md)
for the full table.

**Verify:** OpenCode toasts `mcp-combiner connected` after startup, and
`client.mcp.status()` / the MCP panel shows the combiner's tools.

`$MCP_COMPANION_COMBINER_URL` (host-owned) or `manage: false` → register-only.

## How the combiner is resolved

Both plugins pick a combiner the same way, highest first:

| # | Source | Notes |
|---|--------|-------|
| 1 | `$CLAUDE_MCP_COMBINER_COMMAND` / `$OPENCODE_MCP_COMBINER_COMMAND`, or the `command` option | explicit; never version-checked |
| 2 | `mcp-combiner` on `PATH` | **only when `>= 0.8.0`**; older is reported and skipped |
| 3 | `uv run -m mcp_combiner` from `$…_CHECKOUT` / the `checkout` option | the checkout must exist |
| 4 | `uvx mcp-combiner@<plugin version>` | the zero-install path; falls back to latest if that release is missing |

The floor at row 2 exists because the plugins depend on combiner ≥ 0.8.0 behaviour
(`--mcp` serve mode). A too-old `PATH` install is skipped rather than used, so a stale
binary self-heals into the pinned release instead of failing obscurely — and the skip is
reported, not silent (Claude Code: SessionStart `systemMessage`; OpenCode: a TUI toast),
since routing around a binary you installed on purpose should never be invisible.

## Choosing a port

The default is `9741`. Changing it takes **two** variables, exported **before** the client
starts:

```sh
export CLAUDE_MCP_COMBINER_PORT=9999                        # OPENCODE_… for OpenCode
export MCP_COMPANION_COMBINER_URL=http://127.0.0.1:9999/mcp
```

They answer different questions and neither can be derived from the other. The port says
where to *serve*; the URL is what the client *dials* — and for Claude Code that URL is the
only thing `.mcp.json` can be redirected by, because its `${VAR:-default}` expansion does
not nest (a `${A:-…${B:-9741}…}` default silently mangles into a literal rather than
erroring). Set only one and the session cannot connect, so both plugins say so.

Set the two to different ports and the **URL wins** — it is what the client acts on, so
serving anywhere else would be unreachable — and you get a warning naming the mismatch.

`MCP_COMPANION_COMBINER_URL` on its own still means "the host editor owns the combiner,
register only, don't launch" — that is how CodeCompanion/mcp-companion injects its tokened
per-session endpoint. Adding an explicit port is what distinguishes "I picked this port"
from "someone else is serving me".

## Troubleshooting

- **"no config file found"** — create `~/.config/mcp-combiner/servers.json`
  (or set `MCP_COMBINER_CONFIG`); the probe order is listed under
  Prerequisites. The hook logs to stderr (`CLAUDE_MCP_COMBINER_LOG` to
  capture) and the session simply has no combiner tools.
- **`sharedserver binary not found`** — `cargo install sharedserver`, or set
  `SHAREDSERVER_BIN=/path/to/sharedserver`.
- **Port 9741 already in use** — usually a previous combiner still inside its
  grace period (that's the design: reattach, don't respawn). `mcp-combiner
  status` to inspect it, `mcp-combiner restart --force` to bounce it, or move to
  a different port (see [Choosing a port](#choosing-a-port) — it needs the URL
  set too, not just `_PORT`).
- **Tools missing mid-session** — `combiner__status` (as a tool) or
  `mcp-combiner status` (shell) show per-server state; `combiner__restart_server`
  bounces one upstream without disturbing the rest.
- **Upgrades** — `uv tool upgrade mcp-combiner` (or `pipx upgrade`), then
  `mcp-combiner restart` (add `--force` if other clients are attached; they
  reconnect). Plugins update via `/plugin` / the `@latest` spec.

## Related plugins (other tools, same pattern)

- **crib** (cribsheet memory + code index): `@geohar/opencode-cribsheet` /
  the `cribsheet` Claude Code plugin — in the
  [cribsheet](https://github.com/georgeharker/cribsheet) repo.
- **svg-mcp**: `@geohar/opencode-svg-mcp` / the `svg-mcp` Claude Code plugin — in
  the [svg-mcp](https://github.com/georgeharker/svg-mcp) repo.
- **sharedserver** (generic process manager): `@geohar/opencode-sharedserver` — in
  the [sharedserver](https://github.com/georgeharker/sharedserver) repo.

Each per-tool plugin **stands down** (registers/launches nothing) when a combiner
already serves that backend — set `MCP_COMBINER=1`, or run `mcp-combiner
env-disable` to emit the per-backend `MCP_COMBINER_SERVES_*` exports.
