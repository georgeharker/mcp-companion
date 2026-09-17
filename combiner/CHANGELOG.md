# Changelog

All notable changes to the combiner are documented here. The version moves in
lockstep with the rest of the repo (`scripts/bump-version.sh`); releases are
tagged `vX.Y.Z`.

## [0.14.0] — 2026-09-17

### Added

- **Interactive resource host (mcp-app widgets)** — `/ui/<token>/…` route
  family: sandboxed host page (provider HTML on a second loopback origin),
  app-bridge bundle, resource serving, `/proxy/*` action path, `/events` SSE.
- **Stage 2 in-flight widget hold** — a tool result referencing a widget
  resource holds the agent's call while the user interacts; completion folds
  the recorded state into the result. `MCP_COMBINER_UI_HOLD_TIMEOUT`
  configures the budget.
- **Widget interaction meta-tools** — `combiner__ui_sessions` /
  `combiner__ui_messages` (the widget→agent retrieval loop).
- **Widget-bound tool lookup** — tool definitions binding a widget resource
  (`meta.ui.resourceUri`) trigger the hold; uris normalized to the
  server-namespaced form.
- **Delivery instrumentation** — mockserver serves a spec-speaking widget
  (`ui://mock/mock/widget`) plus `mock__widget_open` / `mock__widget_ping`;
  the host page and widget record deliveries on the session.
- `pyrightconfig.json` for editor import resolution.

### Fixed

- Widget delivery chain (found via live debugging): relay `/sandbox` +
  `/resource` token-param mounts, `/events` StreamingResponse wiring,
  root-relative page paths (bundle import, EventSource, proxy posts),
  missing `parent` origin param, upstream-vs-namespaced uri form split.
- Loopback MCP clients present the inbound bearer on `/mcp/<token>` calls.
- `read_resource` result-shape normalization (fastmcp returns a list).
- Resource `_meta` read via the SDK's `.meta` attribute (declared CSP /
  permissions were silently dropped).

## [0.13.4] and earlier

See the [GitHub releases](https://github.com/georgeharker/mcp-companion/releases).
