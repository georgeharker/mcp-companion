# Design: the Pi extension becomes the MCP adapter

Status: draft (post-discussion, 2026-09-15)
Scope: v1 is **local-only** — the extension speaks MCP client to a loopback
`mcp-combiner` and registers the tool surface in Pi itself. No second package
(`pi-mcp-adapter`) required.

---

## 1. Summary

Today `@geohar/pi-mcp-combiner` runs the combiner (via `sharedserver`) and
injects the tool-discovery directive; `pi-mcp-adapter` (nicobailon, MIT) is the
piece that actually speaks MCP. This design folds the adapter's _client_ role
into the extension — a thin, combiner-specific MCP client — so one package
install gives Pi the full combiner surface.

We deliberately do **not** port the generic adapter (~600 KB TS). The combiner
already owns aggregation, `<server>_` prefixing, upstream OAuth, process
supervision, schema sanitization, permissions (elicit gate), per-chat isolation,
and a hysteresis-stable `tools/list`. The extension needs one transport
(streamable HTTP to loopback), one auth mode (static bearer), and the agent/
user-facing surface. Verified non-goals: the combiner does not forward
`sampling/createMessage`, and its inbound auth advertises no OAuth challenge —
so no OAuth client and no sampling bridge are needed at all.

### Locked decisions

| Decision             | Choice                                                                                                                                                                                                                                                                                                                                                                              |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Project config file  | **Shared `.pi/mcp.json` / `.mcp.json`** (pi-mcp-adapter format) — read, never written by us                                                                                                                                                                                                                                                                                         |
| v1 breadth           | **Full**: `mcp()` proxy + mcpScript + prompts→slash-commands + resources→`read_*` tools                                                                                                                                                                                                                                                                                             |
| Proxy tool name      | **`mcp`** (parity with pi-mcp-adapter), configurable (`toolName` setting / `PI_MCP_COMBINER_TOOL_NAME`) for coexistence; client half gated by `adapter` tri-state, default `"auto"`                                                                                                                                                                                                 |
| Elicitation          | **In** — required for the combiner's permissions gate                                                                                                                                                                                                                                                                                                                               |
| Identity             | Per-Pi-session grouping token minted from `ctx.sessionManager.getSessionId()`                                                                                                                                                                                                                                                                                                       |
| Upstream lift status | **Snapshot-pinned** (audited vs adapter 2.34.0): verbatim = `schema-signature.ts` (7/8 fns ≈1.00 vs `ts-shape.ts`), prompt arg parsing/resolution, `resourceNameToToolName`; reimplemented (shared semantics, no shared code) = ranking, render, renderers, elicitation, script, direct-tools. Upstream fixes to the verbatim areas do NOT auto-flow — re-diff on adapter releases. |

---

## 2. Module layout

```text
plugins/pi/src/
├── index.ts                  today: sharedserver launch + instructions — kept, gains the client wiring
├── pi.ts                     type shim — extended as needed
├── sharedserver-resolve.ts   unchanged
└── client/
    ├── connection.ts         Client + StreamableHTTPClientTransport, bearer, token URL,
    │                         connect/reconnect/backoff, tools/list cache + change handling
    ├── proxy-tool.ts         the `mcp()` tool (verbs below) — calls into adapter-ported helpers
    ├── search-ranking.ts     PORTED (near-verbatim) from pi-mcp-adapter
    ├── tool-metadata.ts      PORTED (near-verbatim) — describe rendering, token estimates
    ├── schema-signature.ts   PORTED — annotated schema-signature rendering for describe
    ├── output-guard.ts       PORTED (mcp-output-guard.ts) — result size/truncation guards
    ├── result-renderer.ts    PORTED (tool-result-renderer.ts, trimmed) — compact rendering
    ├── elicitation.ts        NEW (simplified from elicitation-handler.ts) — ctx.ui bridge
    ├── prompts.ts            PORTED (adapted) — prompts → /<server>__<name> commands
    ├── resources.ts          PORTED (resource-tools.ts, tiny) — read_* tools
    ├── script.ts             PORTED (mcp-code.ts + mcp-script-worker.mjs) — mcpScript
    ├── config-ladder.ts      NEW — shared-file reading/merging (combiner entry only)
    ├── control.ts            NEW — /mcp-combiner verbs (health, sessions, filters, meta-tools)
    └── state.ts              runtime guard: generation counter vs session restarts
```

Ported files keep an `Adapted from pi-mcp-adapter (MIT, © 2026 Nico Bailon)`
header; the package README gains a credits section.

---

## 3. Connection & identity

- **Transport**: `@modelcontextprotocol/client@2.0.0` `Client` +
  `StreamableHTTPClientTransport` (same pin the adapter uses today against this
  same combiner — proven pairing). No SSE fallback, no stdio, no unix socket.
- **Auth**: `Authorization: Bearer $MCP_COMBINER_AUTH_TOKEN` when set; absent
  otherwise. No OAuth discovery, ever (combiner returns a plain 401).
- **Grouping token** (chat identity toward the combiner):
  1. Explicit path token in the resolved URL (user override) wins — same rule
     the combiner applies (URL form beats header).
  2. Else mint `pi-<sessionId>` from `ctx.sessionManager.getSessionId()` and
     connect to `/mcp/pi-<sessionId>`.
  3. `session_start` with `reason: "resume"` / `"fork"`: derive the token from
     `previousSessionFile`'s basename when it parses as the prior session id, so
     a resumed chat **continues its combiner identity** (parked isolated
     upstream sessions, e.g. svg-mcp documents, resume instead of orphaning).
     Fallback: fresh token.
- **URL resolution precedence** (first hit wins):
  1. `MCP_COMPANION_COMBINER_URL` — host-owned mode: never launch, only connect
     (existing semantic, kept).
  2. `PI_MCP_COMBINER_URL` — explicit extension-only override.
  3. `url` of the recognized combiner entry from the config ladder (§4).
  4. `PI_MCP_COMBINER_HOST`/`_PORT` defaults → `http://127.0.0.1:9741/mcp`.

### Lifecycle

- **Lazy by default**: connect on first `mcp()` call (or first prompt/resource
  need). Loopback + warm combiner ⇒ sub-100 ms. `PI_MCP_COMBINER_LAZY=eager`
  connects at `session_start` (after the sharedserver attach).
- **Reconnect**: on transport close or 404/stale-session (combiner bounced by
  `:MCPRestart` / sharedserver grace expiry): re-`initialize` with the same
  token, re-fetch `tools/list`, backoff 1s→30s. The combiner's handover means
  the same token resumes its isolated upstream sessions — restart continuity is
  _better_ than today's static per-instance token.
- **Shutdown**: `session_shutdown` closes the client (all reasons — the
  connection is session-scoped, unlike the sharedserver refcount which only
  detaches on `"quit"`). Parked upstream state survives server-side.
- `tools/list_changed` notifications → refetch list, refresh prompt/resource
  surfaces (registered commands are idempotent re-registrations).

---

## 4. Config story — the shared `.pi/mcp.json` interplay

The extension **reads** the standard MCP files; it **never writes them** (the
only writer remains the user or pi-mcp-adapter's own setup flows). pi-mcp-adapter's
validator is lenient (`isServerEntry` accepts any record; unknown keys pass
through — verified in its `config.ts`), so extension-specific keys are
coexistence-safe.

### Ladder (mirrors the adapter's, later wins)

Read and merge, combiner entry only:

1. `~/.config/mcp/mcp.json`
2. `~/.agents/mcp.json`, `~/.agents/mcp/mcp.json`
3. `<Pi agent dir>/mcp.json` (`~/.config/pi/agent/mcp.json`)
4. `.mcp.json` (project)
5. `.pi/mcp.json` (project — highest)

Continuity bonus: today's `/mcp-combiner install-config` writes the entry to
`~/.config/mcp/mcp.json` — existing installs keep working unchanged.

### Recognizing the combiner entry

An `mcpServers` entry is "ours" if **any** of:

- name is `mcp-combiner`, or
- `x-combiner: true`, or
- its `url` origin+path matches the resolved combiner URL.

Exactly one expected; multiple → first recognized wins + warn.

### Entry shape we honor

```jsonc
{
  "mcpServers": {
    "mcp-combiner": {
      "url": "http://127.0.0.1:9741/mcp", // overrides default URL incl. path token
      "auth": "bearer", // compat: we also read bearerTokenEnv
      "bearerTokenEnv": "MCP_COMBINER_AUTH_TOKEN",
      "combiner": {
        // namespaced, extension-specific — ignored by the adapter
        "servers": {
          // per-project exposure (§5)
          "allow": ["github", "svg-mcp"], //   or "deny": [...]
        },
        "exposeResources": true, // read_* tools (default true)
        "prompts": true, // slash commands (default true)
      },
    },
  },
}
```

Non-combiner entries in the ladder are **not connected** by the extension (v1
scope). If pi-mcp-adapter is absent and non-combiner entries exist, the
extension surfaces a one-line notice at first connect listing ignored entries —
honest about why a configured server isn't available.

### Coexistence matrix

The client half is gated by a **tri-state**: `adapter: true | false | "auto"`
(settings file; env `PI_MCP_COMBINER_ADAPTER=off|on|auto` wins). `"auto"` is the
default and turns the client half **off when pi-mcp-adapter is detected installed** —
so existing adapter users upgrade with zero behaviour change, while fresh installs
get the one-package experience. With the client half off, the extension is
byte-for-byte the legacy behaviour: process launch + instructions only, no `mcp`
tool, no connection.

| Setup                        | Behaviour                                                                                                                                                                                                                                                                                                      |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Extension only** (target)  | auto → client ON. Everything works; combiner entry optional (defaults suffice)                                                                                                                                                                                                                                 |
| **Adapter only**             | Status quo, untouched — the extension never speaks MCP                                                                                                                                                                                                                                                         |
| **Both installed** (upgrade) | auto → client **OFF** automatically (one info line per session says why); pi-mcp-adapter keeps serving the combiner entry + other servers. Force ours with `"adapter": true` — then remove the combiner entry from the shared file (else the adapter double-connects) and rename or accept the `mcp` collision |
| **Both, ours named**         | `toolName: "combiner"` (or `PI_MCP_COMBINER_TOOL_NAME`) registers ours under a distinct name; both tools coexist, each with its own connection/token                                                                                                                                                           |

Collision detection heuristic (Pi exposes no registry query): `<agent dir>/npm/node_modules/pi-mcp-adapter` presence. This drives the auto default; a project-local adapter install the probe misses still gets the forced-ON collision warning. Pi's behaviour on duplicate tool names is undocumented — test empirically during the build (open question Q1).

---

## 5. Per-project exposure — token filters

Project allow/deny of _upstream servers_ maps 1:1 onto the combiner's
per-token filter API (verified in `combiner/mcp_combiner/routes.py:300`):

```http
POST /sessions/token/{token}/filter
  { "allowed_servers": ["github", "svg-mcp"] }   // inverts: all others disabled
  { "disabled_servers": [...] }                  // explicit blocklist
  { "enable": "name" } / { "disable": "name" }   // incremental
```

- Accepts **pending** filters before first connect (applied by
  TokenRewriteMiddleware on initialize) — so the extension posts the project
  filter at `session_start`, before the lazy connection even opens.
- Server-side canonical, read-through enforcement: survives reconnects and
  `mcp-combiner restart` (rides the handover), and connected sessions get a
  `tools/list_changed` nudge to refetch.
- The extension additionally applies the same sets **client-side** over
  `tools/list` (belt) so search/describe never surface filtered-out servers
  even mid-race.
- Editing the project file: `/reload` restarts the session (fresh token, fresh
  pending filter) — or `/mcp-combiner apply-filters` re-posts without a reload.

Server _definitions_ stay global (`servers.json`) in v1. Per-project servers =
later (§9).

---

## 6. Tool surface

### 6.1 The `mcp()` proxy tool — the context-economy core

Registered via `pi.registerTool` with `promptSnippet` (one line in Available
tools). Verbs (mirroring pi-mcp-adapter's calling convention, minus what the
combiner makes moot):

| Call                                                 | Behaviour                                                                                                                                                                  |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mcp({})` / `{status:...}`                           | Combiner health, upstream server states (from `/health` + `tools/list`), connection state                                                                                  |
| `mcp({search: q, regex?, limit?, offset?, server?})` | Weighted fuzzy ranking (ported `search-ranking.ts`: name > server > description > keywords, phrase multipliers, ≥60% token coverage); regex via `recheck` guard. Paginated |
| `mcp({describe: "github_search_code"})`              | Full schema (TS-shape rendered) + description + server + risk notes                                                                                                        |
| `mcp({tool: "github_search_code", args})`            | `tools/call` through the combiner; errors rendered with combiner context; output guarded + compact-rendered                                                                |
| `mcp({connect: "server"})`                           | No-op connectively (combiner owns upstreams) — resolves to a status line for that server                                                                                   |
| `mcp({list: true, server?})` / `mcp({server: name})` | List tools (all or one server)                                                                                                                                             |
| `mcp({instructions: "server"})`                      | The server's instructions text (combiner forwards upstream instructions)                                                                                                   |

**Not ported**: `action: "auth-start"/"auth-complete"` (no inbound OAuth),
`action: "ui-messages"` (ext-apps, later), `install` (no runtime server-add).

### 6.2 `mcpScript`

Port of the adapter's batching tool: `{code, timeoutMs?}` executing trusted JS
with `await tools.search/describe/call()` — same shape agents already know.

### 6.3 Prompts → slash commands

Combiner `prompts/list` → register `/<server>__<name>` (adapter naming parity,
sanitization rules ported). Arguments: positional + `name=value`, bash-style
quoting. Result flattened `[role] text` → `pi.sendUserMessage()`.

### 6.4 Resources → `read_*` tools

`resources/list` over the (filtered) tool set → `read_<name>` tools, opt-out
via `combiner.exposeResources: false`. Default **on** (adapter parity).

---

## 7. Elicitation bridge (required)

The combiner's permission gate elicits (Allow once / Allow for session / Deny),
secure default `elicitUnavailable: deny`. Register an elicitation handler on the
SDK `Client`:

- **The structured dialogs (`ui.select`/`confirm`/`input`) ARE the right
  presentation layer** — each mode (interactive, RPC, print) provides its own
  implementation, so a gate question is a native TUI dialog locally AND extends
  over the wire as `extension_ui_request` frames in RPC mode, which remote
  clients (e.g. un-bien's paired app) render natively. Do NOT replace them with
  custom TUI overlays (interactive-only, never reach remote clients) or couple
  to pi-ask's event flow (a separate protocol with its own runtime/ack
  ownership). Options round-trip VERBATIM — the combiner string-matches the
  gate choice; single-property forms ask under the bare message, multi-property
  forms prefix the property name.

- **Options-only payloads** (the combiner gate) → single `ctx.ui.select`.
- **JSON-schema forms** → sequential `ctx.ui` dialogs per property:
  enum→select, boolean→confirm, string/number→input, array→repeat-input;
  validate with ajv between steps; final review prompt before submit.
- **No UI / headless** (`!ctx.hasUI`) → return the MCP "elicit refused" error;
  the combiner applies `elicitUnavailable` (deny by default). Honest failure,
  never a hang.
- "Allow for session" caching is combiner-side — we just relay the choice.

---

## 8. Commands & control

`/mcp-combiner` (existing) gains verbs; every verb degrades to a clear error
when the combiner is unreachable:

| Verb                                                      | Effect                                                                                                          |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `status`                                                  | `/health` glyph table + `/sessions/map` for _our_ token + tool counts                                           |
| `servers`                                                 | upstream states from `/health`                                                                                  |
| `enable <srv>` / `disable <srv>` / `restart-server <srv>` | drive `combiner__*` meta-tools through our client (simplest path; auth already in place)                        |
| `filters` / `apply-filters`                               | show / re-post the project-derived token filter                                                                 |
| `call <tool> --args '{...}'`                              | ad-hoc debug invocation                                                                                         |
| `tools`                                                   | list advertised tools                                                                                           |
| `system-prompt`                                           | existing (directive)                                                                                            |
| `install-config`                                          | kept — now self-consistent (writes the entry _our_ ladder reads, plus optional `combiner` block); marked legacy |

Instructions injection: existing `before_agent_start` belt stays; the
initialize result's `instructions` (combiner serves it) is surfaced via
`mcp({instructions:...})` and appended once at connect (braces).

---

## 9. Now / Later

| Area         | **Now (v1)**                                   | Later                                                                                                                                          |
| ------------ | ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Transport    | streamable-HTTP loopback, bearer               | remote/TLS combiner (CA handling)                                                                                                              |
| Identity     | per-Pi-session tokens, resume-aware            | token mgmt UI (`/sessions/map` browser)                                                                                                        |
| Tools        | `mcp()` full verb set, mcpScript               | direct-tools promotion (allowlist, e.g. `combiner__*` as first-class tools)                                                                    |
| Capabilities | elicitation, prompts, resources                | ext-apps interactive UIs (ui-server + browser viewer + `ui-messages`); sampling (only if combiner ever forwards)                               |
| Config       | shared-file read ladder, exposure filters, env | per-project server defs: (a) project-scoped combiner instance (second sharedserver name + merged config) or (b) new combiner runtime-add route |
| Ops          | control verbs, reconnect, instructions         | TUI status panel; metadata cache (instant search pre-connect); tracing; conformance suite vs `mockserver`                                      |

## 10. Open questions

1. **Pi duplicate-tool behaviour**: does last-registration win, first, or error?
   Test empirically; until known, the collision warning + rename env is the
   mitigation.
2. **Eager vs lazy default**: lazy chosen for startup cost; revisit if first-turn
   tool visibility matters (e.g. prompts as commands need the connection at
   registration time — eager may be the better default once prompts land).
3. **Session-file basename → session id**: confirm Pi's file naming makes the
   resume-token derivation reliable across versions.
4. **Resource volume**: if upstreams expose many resources, `read_*` direct
   registration may need the same search-first laziness the adapter uses for
   direct tools — decide after seeing real counts.

## 11. Testing

- **Unit (vitest)**: config ladder merge, entry recognition, filter resolution,
  search ranking, output guard, elicitation form mapping.
- **Integration**: scratch `servers.json` with `mockserver` upstreams; `uv run
python -m mcp_combiner --mcp --port <test>`; connect extension client
  headlessly. Fixture patterns from pi-mcp-adapter's `__tests__/fixtures`
  (`modern-discover-server.mjs`, `elicitation-server.mjs`, …) are reusable
  references.
- **Manual**: full Pi session against the real combiner; elicitation via the
  permissions gate (`elicit: [...]` in a scratch servers.json); restart
  continuity (`mcp-combiner restart` mid-chat → same token resumes).
