# @geohar/pi-mcp-combiner

A [Pi](https://pi.dev) extension + one shared **MCP aggregator**: a single refcounted
combiner process fronts *all* your MCP servers, and every client you use talks to
that one process.

**It reuses Pi's MCP stack rather than replacing it.** The transport is
`@earendil-works/pi-mcp` — Pi's own MCP client library (peer dependency, `^1.0.2`) —
and Pi's built-in MCP support keeps its role (`/mcp`, codemode, `tool_search`). The
extension is *additive*; it adds the pieces Pi's built-in can't do:

- **Elicitation** — upstream servers can ask *you*: consent ladders, deploy
  confirmations, structured forms. Pi's built-in MCP can't forward those; this
  extension bridges them to Pi's dialogs — and to your paired app over
  [un-bien](https://github.com/georgeharker/un-bien).
- **Interactive resources (mcp-app widgets)** — Pi's `read_mcp_resource` skips
  `ui://` resources entirely; we register `read_*` tools, host the widget, hold the
  call while you interact, and fold your actions back into the result.
- **A discovery router** — `mcp({search})` / `({describe})` / `({tool, args})`:
  ranked discovery over the whole aggregated surface, under the tool name `mcp`
  that tool_call gates already recognize.

**The sharing is the point.** One combiner process, supervised by
[`sharedserver`](https://github.com/georgeharker/sharedserver), is shared across Pi,
Claude Code, OpenCode, and Neovim — upstream connections, sessions, and OAuth live
once, not once per client. One `servers.json` permission policy (deny / elicit /
allow, per server) is enforced *at the combiner* for every client alike, keyed by
per-chat tokens that survive restarts: consent grants, parked sessions, and filters
carry across combiner restarts by handover, and `combiner__status` reports what a
boot restored.

It is the Pi counterpart of the
[Claude Code](https://github.com/georgeharker/mcp-companion/tree/main/plugins/claude)
and [OpenCode](https://github.com/georgeharker/mcp-companion/tree/main/plugins/opencode)
plugins, and shares the same combiner and the same `sharedserver` instance.

## How it fits together

Three halves, all in this one package:

1. **Process** — on `session_start` it drives

   ```sh
   sharedserver use <name> --pid <pi-pid> --grace-period <g> \
       -- <combiner> --mcp --config <servers.json> --port <port>
   ```

   `sharedserver` refcounts by PID with a grace period, so the combiner is shared
   across clients and outlives any single one. The refcount releases on
   `session_shutdown` only when `reason === "quit"` — reload/resume/fork keep the same
   Pi process and a fresh `session_start` re-attaches.

2. **Client** — the pi-mcp transport on an owned connection (per-Pi-session custody
   token, elicitation capability advertised and bridged to Pi's UI) that registers
   the agent-facing surface: every combiner tool first-class under its bare name
   (`github_search_code`) with pi-1.0 exposure/namespace/annotations, the `mcp()`
   discovery router on the same connection, `read_*` resource tools (interactive
   `ui://` widgets included), prompt slash commands, a status footer, and an
   interactive panel.
3. **Instructions** — on `before_agent_start` it appends the combiner's
   `<server>_`-prefix / "discover before assuming" directive to the system prompt.

`sharedserver` itself is fetched automatically if not installed (pinned release via
the cargo-dist installer), and the combiner is resolved from PATH / a checkout /
a pinned PyPI release — same resolvers as the sibling plugins.

## Running it

Install only this extension. Nothing else needed — the client half connects to the
combiner and registers everything:

```sh
pi install /path/to/mcp-companion/plugins/pi     # local checkout
# or the published package under settings.json "packages"
```

On startup you get: every combiner tool first-class under its bare name (your
`directTools` matches declared `direct`, the rest on codemode), the `mcp` discovery
router, `read_<resource>` tools, `/<server>__<prompt>` slash commands, a footer line
(`14 servers enabled (13 ready) · 671 tools`), and `/mcp-combiner panel`.

> **\*Via another mechanism — pi-mcp-adapter.** The target is Pi's native MCP support
> (Pi ≥ 1.0). If you already run
> [`pi-mcp-adapter`](https://pi.dev/packages/pi-mcp-adapter) for other servers, this
> extension can stand down its client half: `"adapter": false` (or the default
> `"auto"`, which detects the adapter and yields automatically) leaves the process
> half + instructions to us and MCP to the adapter. `/mcp-combiner install-config`
> still writes the shared-`mcp.json` entry for the adapter to read.

## Requirements

- **`mcp-combiner`** available as a command (`uv tool install mcp-combiner`), or just
  **`uv`** on PATH — a pinned release is fetched from PyPI on demand. Requires
  combiner ≥ 0.8.0 (version-gated automatically).
- A combiner **`servers.json`** (auto-probe locations below).
- pi-mcp-adapter **not** required.
- **Pi ≥ 1.0** with its bundled `@earendil-works/pi-mcp` (peer dependency,
  `^1.0.2`) — the transport is Pi's own MCP client library.

## Configuration — three layers

### 1. Pi settings file — `$PI_CODING_AGENT_DIR/extensions/mcp-combiner.json`

Pi-side knobs (see [`settings.example.json`](./settings.example.json)):

| Key               | Default  | Effect                                                                                                    |
| ----------------- | -------- | --------------------------------------------------------------------------------------------------------- |
| `toolName`        | `"mcp"`  | Name of the proxy tool. Rename (e.g. `"combiner"`) to coexist with pi-mcp-adapter's own `mcp`.            |
| `adapter`         | `"auto"` | Client-half gate — see above.                                                                             |
| `lazy`            | `"lazy"` | `"eager"` connects at session start; `"lazy"` on first use. Prompts/resources/directTools imply eager.    |
| `exposeResources` | `true`   | Register `read_<resource>` tools.                                                                         |
| `prompts`         | `true`   | Register prompt slash commands.                                                                           |
| `uiAutoOpen`      | `true`   | Auto-open interactive widget URLs in the browser (Stage 2 holds + resource reads).                        |
| `warnLargeDirectExposure` | `true` | Warn when >50 tools end up `direct`-exposed (every schema rides in every request); set `false` to silence. Native mode. |
| `mcpFooterStatus` | `"full"` | Footer text: `"full"` = `N servers enabled (M ready) · T tools`, `"compact"` = `MCP M/N`, `"off"` = none. |
| `mcpFooterKey`    | `"mcp"`  | The `ctx.ui.setStatus` key the footer publishes under (the slot oh-my-posh-style footers aggregate).      |
| `url`             | —        | Explicit combiner URL. Env wins.                                                                          |
| `notify`          | `true`   | Surface lifecycle messages via the Pi UI.                                                                 |

### 2. Shared MCP config ladder — read-only

The extension **reads** the standard MCP files (same ladder and precedence as
pi-mcp-adapter, later wins): `~/.config/mcp/mcp.json` → `~/.agents/mcp.json` →
`~/.agents/mcp/mcp.json` → `<agent dir>/mcp.json` → `.mcp.json` → `.pi/mcp.json`
(project). It **never writes them**.

The recognized `mcp-combiner` entry carries connection + per-project exposure:

```jsonc
{
  "mcpServers": {
    "mcp-combiner": {
      "url": "http://127.0.0.1:9741/mcp",
      "auth": "bearer",
      "bearerTokenEnv": "MCP_COMBINER_AUTH_TOKEN",
      "combiner": {
        // extension-specific, ignored by other readers
        "servers": { "allow": ["github", "svg-mcp"] }, // or "deny": [...]
        "exposeResources": true,
        "prompts": true,
        "directTools": ["combiner__status", "github_search_*"], // or "search"
      },
    },
  },
}
```

- **`servers.allow/deny`** — per-project exposure, enforced _at the combiner_ for this
  chat's token (sees through scripting too) and mirrored client-side.
- **`directTools`** — an allowlist (globs) of tools to declare `direct` (verbatim,
  in every request) instead of `codemode` (reachable from scripts and pi's
  `tool_search`). `"search"` degrades to all-codemode — pi's own discovery replaces
  the old search-promote. A >50-entry allowlist warns; `true` is deliberately not
  offered (context cost: every direct schema rides in every request).
- Project layers are read against the **session cwd** — worktree subagents and
  project switches get their own `.pi/mcp.json`. Commit the `combiner` block if you
  want worktree agents to honour it.
- URL precedence: `MCP_COMPANION_COMBINER_URL` (host-owned) → `PI_MCP_COMBINER_URL` →
  settings `url` → ladder entry `url` → `host:port/mcp`.

### 3. Environment — `PI_MCP_COMBINER_*`

| Variable                                        | Default           | Effect                                                             |
| ----------------------------------------------- | ----------------- | ------------------------------------------------------------------ |
| `PI_MCP_COMBINER_ADAPTER`                       | _(settings)_      | `off` / `on` / `auto` — client-half gate.                          |
| `PI_MCP_COMBINER_TOOL_NAME`                     | _(settings)_      | Proxy tool name override.                                          |
| `PI_MCP_COMBINER_URL`                           | —                 | Explicit combiner URL.                                             |
| `PI_MCP_COMBINER_PORT`                          | `9741`            | HTTP port the combiner serves on.                                  |
| `PI_MCP_COMBINER_HOST`                          | `127.0.0.1`       | HTTP host the combiner binds.                                      |
| `PI_MCP_COMBINER_CONFIG`                        | _(auto-probed)_   | Path to the combiner's `servers.json`.                             |
| `PI_MCP_COMBINER_COMMAND` / `_ARGS`             | _(auto-resolved)_ | Override the combiner invocation.                                  |
| `PI_MCP_COMBINER_CHECKOUT`                      | —                 | Checkout for `uv run --project <checkout> python -m mcp_combiner`. |
| `PI_MCP_COMBINER_NAME`                          | `mcp-combiner`    | `sharedserver` instance name.                                      |
| `PI_MCP_COMBINER_GRACE`                         | `30m`             | `sharedserver` grace period.                                       |
| `PI_MCP_COMBINER_LOG` / `_PYLOG` / `_LOG_LEVEL` | _(state dir)_     | Combiner logging; `"none"` disables.                               |
| `PI_MCP_COMBINER_MANAGE`                        | `true`            | `false` → don't launch (combiner runs elsewhere).                  |
| `PI_MCP_COMBINER_INSTRUCTIONS`                  | `true`            | `false` → skip the system-prompt directive.                        |
| `PI_MCP_COMBINER_NOTIFY`                        | `true`            | `false` → don't surface messages via the Pi UI.                    |
| `SHAREDSERVER_BIN` / `SHAREDSERVER_LOCKDIR`     | _(auto)_          | `sharedserver` binary / lock dir.                                  |

`servers.json` auto-probe: `$PI_MCP_COMBINER_CONFIG` →
`~/.cache/secrets/<user>.mcpservers.json` → `~/.config/mcp-combiner/servers.json` →
`~/.config/mcp/servers.json`.

## The UX

**The proxy tool** (`mcp`, or your `toolName`) — one tool instead of hundreds:

```ts
mcp({search: "github search code"})     → ranked hits + describe-next hint
mcp({describe: "github_search_code"})   → full schema (TS-shaped) + description
mcp({tool: "github_search_code", args: {...}})  → the call
mcp({})                                 → status
```

**Per-tool registration** — the combiner's tools are also declared first-class under
their own bare names (`github_search_code`): `directTools` matches verbatim, the rest
reachable from codemode scripts and pi's `tool_search` — while the `mcp` router above
keeps registering on the same connection (tool_call gates that match the `mcp` tool
name keep working). See [`docs/call-chain.md`](./docs/call-chain.md) for the full
layer map of a call.

**`read_<resource>` tools** — one zero-parameter tool per MCP resource; interactive
`mcp-app` resources are flagged and their `read_*` results append the combiner UI-host URL (`/ui/<token>/?resource=…`), auto-opened in the browser — the interactive widget runs combiner-side.

**Interactive widgets (mcp-app)** — when a widget-bound tool result arrives (e.g.
`todoist_find-tasks-by-date`), the combiner **holds the call in flight**: the
extension auto-opens the widget in your browser, the tool's data streams to it
over SSE, and you interact while the call waits. Hit **Done** in the widget and
the call resolves with a summary of what you did. Every widget action runs
through the combiner's permission pipeline, and the full interaction stays
retrievable: ask for `combiner__ui_messages` afterwards (the hold budget is 50s
by default — `MCP_COMBINER_UI_HOLD_TIMEOUT` combiner-side; the widget URL stays
valid after a timeout, you just lose the fold-into-result for that call).
Widgets that self-fetch (svg-mcp's preview) work the same way.

**Prompt slash commands** — `mcp__<server>__<name>` (e.g. `mcp__todoist__productivity_analysis`),
positional + `name=value` args with bash quoting.

**Footer** — `14 servers enabled (13 ready) · 671 tools` under the `mcp` status key;
refreshed on connect/changes and every 30s; honest degradation when unreachable.

**`/mcp-combiner` command**:

| Verb                                          | Effect                                                       |
| --------------------------------------------- | ------------------------------------------------------------ |
| _(none)_ / `status`                           | Connection state, per-server glyph table, session view       |
| `panel`                                       | Interactive panel (below)                                    |
| `enable` / `disable` / `restart-server <srv>` | Drive the combiner's meta-tools                              |
| `system-prompt`                               | Show the injected directive                                  |
| `install-config [path]`                       | Legacy: write the shared-`mcp.json` entry for pi-mcp-adapter |

**The panel** — `/mcp-combiner panel`: connection + port + session token, exposure
filter, fuzzy search (`/`), per-server rows with state glyphs (`● ○ ⊘ ✗ ◌`) and the
tools/resources/prompts counts trio, expandable tool lists with token estimates,
`[session off]` labels for project-filtered servers, the combiner's own `⬢` meta-tools
group. Keys: `↑↓/jk` move · `enter` expand/copy · `e` enable/disable · `c` copy ·
`/` filter · `r` refresh · `q` close.

## Chat identity

Each Pi session mints its own grouping token (`pi-<sessionId>`) into the combiner URL
path, so per-chat isolation (`isolate: true` servers), parked upstream sessions, and
restart handover all key on the chat — subagents automatically get their own tokens
(each child session binds fresh). A resumed chat continues its identity; a fork
deliberately starts fresh. An explicit token in the configured URL always wins.

Because identity lives at the client, a combiner restart degrades gracefully: the
sanctioned-restart handover carries consent grants, per-token filters and parked
sessions to the successor boot, every connection method retries a stale session once
(`withStaleRetry`), and `combiner__status` / `/health` report what this boot restored
(`_handover`) — the post-restart re-orientation receipt.

## Permissions

Tool-call policy is enforced **at the combiner** (`permissions` in `servers.json`:
deny/elicit/allow per server, with interactive elicitation bridged to Pi's UI —
subagents decline securely by default). "Allow for session" grants are keyed by the
chat token and carried across sanctioned combiner restarts. In native mode the
combiner's tool annotations (readOnly/destructive hints) ride pi's registration for
classification-aware gates. If you also run
[`pi-permission-system`](https://github.com/gotgenes/pi-packages), keep `toolName:
"mcp"` for its `mcp`-surface rules (tool-glob patterns like `github_*` match out of
the box).

## Acknowledgments

The client half began as a substantial reduction of
[**pi-mcp-adapter**](https://github.com/nicobailon/pi-mcp-adapter) (MIT, © Nico Bailon) —
the proxy-tool calling convention, search semantics, result-guard behavior, prompt
command format, resource naming, the schema-signature renderer, and the HTML/JS
widget contract derive from it, and the conformance cases in `test/` are ported from
its suite. Everything OAuth, multi-server, and transport-related is deliberately
_not_ here — the combiner owns that. This package would be a much worse tool
without Nico's design work; go star it.

## Host-owned mode

If **`$MCP_COMPANION_COMBINER_URL`** is set, an editor/host (e.g. Neovim) already owns
and refcounts the combiner — this extension never launches it (the client still
connects). Equivalent to `PI_MCP_COMBINER_MANAGE=false` for the process half.

## Development

```sh
npm install
npm run typecheck
npm run build      # emits dist/ (not committed; built on publish)
npm run smoke      # live suite against the running combiner on :9741
```

Design notes: [`docs/adapter-design.md`](./docs/adapter-design.md). The
`src/sharedserver-resolve.ts` file is **vendored byte-identical** from
[`georgeharker/sharedserver`](https://github.com/georgeharker/sharedserver) (via
`scripts/sync-vendored.sh`). Edit upstream; re-sync here.

## License

MIT © George Harker
