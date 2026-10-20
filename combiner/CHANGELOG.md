# Changelog

All notable changes to the combiner are documented here. The version moves in
lockstep with the rest of the repo (`scripts/bump-version.sh`); releases are
tagged `vX.Y.Z`.

## [0.17.4] — 2026-10-20

### Fixed

- **Wedged tools/list after an external combiner reset** — the wedge that made
  every client (pi sessions, ctl) see endless "stale connection" warnings until
  a bounce (COMBINER-RESTART-BUG.md follow-up 2026-10-20). Two layers:
  - Every mounted upstream provider's LISTING (tools/resources/prompts) is now
    time-bounded (`BoundedListProvider` wraps the mount;
    `MCP_COMBINER_LIST_TIMEOUT`, default 15s) — `UPSTREAM_TOOL_LIST_TIMEOUT`,
    previously defined and imported but never wired up, is finally live. A
    wedged or half-dead upstream is logged and skipped from the aggregate
    fetch instead of hanging it; its tools reappear when the health monitor
    reconnects it.
  - The single-flight tools/list fetch is bounded (`FETCH_TIMEOUT`,
    `MCP_COMBINER_FETCH_TIMEOUT`, default 60s): the owner falls back to
    last-known-good tools on timeout (same shape as its upstream-error path),
    joiners never hang on a future whose owner died or was cancelled, and a
    pre-wedged future (the historical stuck state) is dropped + served from
    the stale cache instead of stranding every session forever.
  - `test_tool_fetch_wedge.py` covers all paths; `test_mounts.py`'s e2e
    restart/visibility suites cover the real mount shape (the bounded
    provider sits UNDER fastmcp's namespace wrapper so call routing is
    unaffected — verified by live round-trip tests).

- **FastMCPProvider surface honesty in the fake tests** (`FakeCombiner`):
  models `add_provider` to match the real mount path.

## [0.16.0] — 2026-10-04

### Added

- **Token-keyed consent grants** — "allow for session" is keyed by the chat
  token (the custody principle), not the transport session, and carried
  across sanctioned-restart handovers (snapshot/restore, grant counts in
  the handover log lines).
- **Restart receipt** — `combiner__status` and `/health` carry `_handover`:
  what this boot restored (grants / parked / filters / binds) or why a
  handover was refused — the post-restart re-orientation record.
- `restart_server` meta-tool — teardown + respawn for dead upstream children
  (per-chat isolated sessions reset, honestly reported).
- mockserver: `mock__elicit_form` (the 4-property elicitation proof),
  `mock__remember`/`mock__recall` (token identity tests), widget tooling.

### Fixed

- **Advertised UI origin** — widget URLs name a reachable origin, not
  loopback: `MCP_COMBINER_UI_ADVERTISE` (`auto` default — tailscale daemon
  probe, then a 100.64.0.0/10 interface scan, then the bind host;
  `loopback`; or an explicit origin for the tailscale-serve variant). URL
  construction only — binding stays `--host`. Remote renderers (un-bien's
  paired app over the tailnet) now load widgets end to end.
- The sandbox relay (the second origin, port+1) follows the main bind
  instead of hardcoded loopback — without this, remote host pages loaded
  furniture but the widget content iframe ("loading ui…") never resolved.
- Last-wins token→session mapping: a reloaded/reconnected client mints a new
  downstream session for the same chat token; the map now follows the live
  session instead of first-wins routing elicit forwards to dead streams.

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
