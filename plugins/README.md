# mcp-combiner plugins (Claude Code & OpenCode)

Standalone editor plugins that run the **mcp-combiner** aggregator via
[`sharedserver`](https://github.com/georgeharker/sharedserver) and register its
HTTP endpoint with your agent — the non-CodeCompanion way to share one combiner
process across Claude Code, OpenCode, and Neovim.

Both plugins live **in this repo** (previously separate repos, now retired):

| plugin | dir | for |
|---|---|---|
| `claude-mcp-combiner` | [`plugins/claude-mcp-combiner`](./claude-mcp-combiner) | Claude Code |
| `@geohar/opencode-mcp-combiner` | [`plugins/opencode-mcp-combiner`](./opencode-mcp-combiner) | OpenCode |

Both attach to (or start) the combiner under the sharedserver name
`mcp-combiner`, so every client shares one refcounted process. When the host
editor already owns the combiner (CodeCompanion spawns the agent with
`MCP_COMPANION_COMBINER_URL` set), each plugin **registers only** and does not
launch — the host owns the lifecycle at its matching version.

## Claude Code — `claude-mcp-combiner`

Installed from this repo's marketplace:

```
/plugin marketplace add georgeharker/mcp-companion
/plugin install claude-mcp-combiner@mcp-companion
```

A `SessionStart` hook runs `sharedserver use … -- mcp-combiner --mcp --config … --port …`
to attach/launch; `SessionEnd` runs `unuse`. The combiner's HTTP endpoint is
registered via the plugin's static `.mcp.json`
(`${MCP_COMPANION_COMBINER_URL:-http://127.0.0.1:9741/mcp}`).

Knobs (env): `CLAUDE_MCP_COMBINER_COMMAND` / `_ARGS` / `_CHECKOUT` (which combiner
runs), `CLAUDE_MCP_COMBINER_CONFIG` (servers.json), `_PORT` (9741), `_GRACE`
(30m), `_NAME` (mcp-combiner), `_LOG`.

## OpenCode — `@geohar/opencode-mcp-combiner`

Add it to your `opencode.json` `plugin` list:

```json
{ "plugin": ["@geohar/opencode-mcp-combiner@latest"] }
```

It injects the combiner into OpenCode's `mcp` config (a `type: "remote"` entry)
via the `config` hook, and drives sharedserver the same way as the Claude plugin.
Options (all optional): `mcpName`, `url`, `register`, `manage`, `binary`,
`lockdir`, `name`, `gracePeriod`, `logFile`, `command`/`args`/`checkout`,
`config`, `port` (9741), `host`, `notify`. See
[`plugins/opencode-mcp-combiner/README.md`](./opencode-mcp-combiner/README.md)
for the full table.

`$MCP_COMPANION_COMBINER_URL` (host-owned) or `manage: false` → register-only.

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
