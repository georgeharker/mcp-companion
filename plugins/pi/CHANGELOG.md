# Changelog

All notable changes to the Pi extension are documented here. The version moves
in lockstep with the rest of the repo (`scripts/bump-version.sh`); releases are
tagged `vX.Y.Z`.

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
