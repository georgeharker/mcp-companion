# Changelog

All notable changes to the Pi extension are documented here. The version moves
in lockstep with the rest of the repo (`scripts/bump-version.sh`); releases are
tagged `vX.Y.Z`.

## [0.16.0] — 2026-10-04

### Removed

- **The legacy SDK client** (`@modelcontextprotocol/client`) — native mode is the
  only mode. The transport is `@earendil-works/pi-mcp` (peer dep, `^1.0.2`):
  Pi's own MCP client library on our owned connection.
- `mcpScript` (pi's built-in codemode is the superset), the `directTools`
  search-promote (native exposure + `tool_search` replace it), and the legacy
  resource sync (native resources own the interactive `read_*` surface).
- `mode` and `scriptMode` settings — moot.

### Added

- `client/types.ts` — the `CombinerConnection` interface + shared summary types;
  the native connection `implements` it, so surface modules depend on the
  contract, not the implementation.
- `exposeResources` gating for the native resource surface.

### Fixed

- Native surface registration deferred to `session_start` (post-token): a
  factory-time run logged "no grouping token set" startup warnings, and the
  wiring's `setHooks` replaced the activation's hook outright (last-wins).
- Zombie connection teardown on `/reload`: pi re-imports the module without
  firing `session_shutdown`, so the old instance's stream stayed alive and
  competed for elicit routing. A process-global registry survives re-imports;
  the fresh factory closes prior connections.
- Stale-ctx crash on `/reload` (pi 1.0's hard assertion): the teardown's
  reset-reason log rode the orphaned instance's captured `ctx.ui` — an
  uncaught throw in the fire-and-forget microtask killed pi. `clientLog`
  now degrades a stale UI capture to stderr instead of throwing, and the
  teardown retires the whole instance (footer timer, UI captures, connection).

## [0.15.0] — 2026-10-03

### Added

- **Native mode** (`"mode": "native"`): per-tool pi-1.0 registration
  (exposure/namespace/annotations) via the pi-mcp transport on an owned
  connection — same custody token, elicitation capability advertised and
  bridged. The `mcp` router registers alongside in both modes (tool_call
  gates recognize the `mcp` tool name).
- `docs/call-chain.md` — the layer map, naming, and token custody for a call
  in both modes, with the pi-native contrast.

### Fixed

- Connection-wide one-shot stale-session retry: a combiner restart mid-flight
  surfaced raw "Session not found" from the router's cache refresh before any
  `callTool` ran.
- Recovery-hint error messages name what died, what survives, and the retry.

### Changed

- The legacy client factored to `src/legacy-client/` (deletion checklist in
  its README); shared modules extracted to `src/client/`.

## [0.14.0] — 2026-09-17

### Added

- **The client half** — the extension speaks MCP to the combiner itself:
  the `mcp()` proxy tool (search/describe/call/list/status), `mcpScript`
  batching, `read_<resource>` tools with `ui://` detection and widget
  auto-open, prompt slash commands (`mcp__<server>__<name>`), the elicitation
  bridge (SDK structured dialogs), the footer chip, and the `/mcp-combiner`
  panel + control verbs.
- **Interactive widgets (Stage 2)** — widget-bound tool results hold the call;
  the extension auto-opens the widget URL from the hold notification
  (`uiAutoOpen` setting, default true) and the combiner streams the data.
- **directTools** — allowlist + search-mode tool promotion.
- `uiAutoOpen` / `mcpFooterKey` settings; adapter tri-state (`auto`/`true`/
  `false`) coexistence with pi-mcp-adapter.
- Conformance tests ported from pi-mcp-adapter (51 passing).

### Fixed

- TUI renderers implement `invalidate()` — pi-tui's Component contract; the
  missing method crashed pi on transcript invalidation (session restore,
  theme change, resize).

## [0.13.4] and earlier

See the [GitHub releases](https://github.com/georgeharker/mcp-companion/releases).
