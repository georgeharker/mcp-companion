# Changelog

All notable changes to the combiner are documented here. The version moves in
lockstep with the rest of the repo (`scripts/bump-version.sh`); releases are
tagged `vX.Y.Z`.

## [0.20.0] — 2026-11-03

Both MCP revisions, upstream and down. Upgrade notes: upstreams default to
`protocol: auto`, so a server that offers the 2026-07-28 revision is connected
on it and owns its own sessions (no per-chat isolation) — set
`"protocol": "legacy"` for one that still keeps per-connection state (svg-mcp
< 0.10). The combiner still answers clients on the handshake unless
`MCP_COMBINER_OFFER_2026=1`. `bind.host` may now be a list; `auto` / `{auto}`
name this machine on its tailnet in bind, advertise and `oauth.public_url`.

### Added

- Era translation, part 2 (still behind `MCP_COMBINER_OFFER_2026=1`):
  permission consent prompts work on the 2026-07-28 revision. A call that
  needs consent returns an `InputRequiredResult` with the same question and
  choices as the handshake era's prompt; the client's retry carries the
  answer. The upstream is not called until consent is given; "Allow for
  session" is remembered per chat token as before. (Before, a 2026 client
  silently got the `elicitUnavailable` fallback.)
- **Bind to several addresses.** `bind.host` (servers.json) takes a list, and
  `--host` / `MCP_COMBINER_HOST` a comma-separated one: e.g.
  `["127.0.0.1", "auto"]` serves loopback and the tailnet but not the
  LAN. `auto` resolves to this machine's tailnet address at startup; an
  address that isn't up is skipped with a warning (startup fails only if none
  bind); a wildcard covers the rest. The UI relay (port + 1) follows the same
  bind. Any non-loopback bind without an inbound bearer token warns. The pi
  plugin connects to the first entry (loopback for a wildcard or `auto`).
  A host name that doesn't resolve at startup is skipped like an address
  that isn't up.
- **Portable tailnet settings.** `auto` as a whole value, or `{auto}` as the
  host inside a URL, means this machine on its tailnet, in `bind.host`,
  `MCP_COMBINER_UI_ADVERTISE` and `oauth.public_url`. Advertise `auto` (the
  default) now names the MagicDNS name (`tailscale status`) rather than the
  raw tailnet IP, falling back to the IP. `oauth.public_url: "auto"` is
  `http://<magicdns name>:<port>`; `"https://{auto}"` suits `tailscale serve`.
  New `MCP_COMBINER_OAUTH_PUBLIC_URL` (a literal URL) overrides the setting.
- `MCP_COMBINER_OAUTH_PUBLIC_URL` (and a literal `public_url`) may be a bare
  host: `http://` and, on plain http, the combiner's port are added. A bare
  host used to become a redirect URI with no scheme, which broke the OAuth
  connection outright.
- **`{oauth_redirect}` in server entries** expands to that server's callback,
  `<public_url>/oauth/callback/<server>`, in `args`, `env`, `headers` and `url`
  (and in a `sharedServers` / `externalServers` entry used by exactly one
  server), so a server can be told its redirect URI without a machine name in
  the config. Left as-is, with a warning, when there's no public URL.
- `MCP_COMBINER_OAUTH_PUBLIC_URL` now applies when servers.json has no `oauth`
  block (it was ignored then).
- Mock server `--issuer` (`auto` = the tailnet name): the authorization server
  URL it advertises, so a phone can complete the mock's OAuth flow.
- An explicit advertised origin keeps its own port
  (`MCP_COMBINER_UI_ADVERTISE=http://host:443` gave `http://host:443:9741`),
  and a bare IPv6 host is bracketed.
- **Per-server `protocol` setting** — `auto` (default), `legacy` or `2026`:
  which MCP revision the combiner speaks to an upstream. `auto` asks the server
  (`server/discover`) before mounting it; a server listing 2026-07-28 is
  treated as handling its own sessions (one shared connection, `isolate`
  ignored). Stdio, OAuth-protected and unreachable servers use the handshake.
  **Upgrade note:** every mcp 2 server answers discover with 2026 unless it
  pins itself, so a server that still keeps per-connection state (e.g.
  svg-mcp 0.9.0) needs `"protocol": "legacy"` until it supports explicit
  sessions. The bundled mock server pins itself unless `--offer-2026`.
- **2026 upstreams are health-checked with `server/discover`.** `ping` exists
  only on the handshake revisions, so a 2026 server answered every health
  check with `Method not found` and was reconnected every few intervals. A 2026
  connection also now makes first contact when it opens (the handshake's
  `initialize` did that implicitly), so an unreachable or not-yet-authorized
  2026 server is no longer reported connected; `protocol: "2026"` on an OAuth
  server goes through the normal authorization prompt.
- **The combiner listens to 2026 upstreams.** A 2026-era server can't push
  anything between calls unless its client opens `subscriptions/listen`, so the
  combiner keeps one listen stream to each: tool, prompt and resource list
  changes always (tool changes re-prime the server's cached slice), plus
  resource updates for the URIs some chat watches. A dropped stream reopens
  with backoff and re-primes the tool list. Watched URIs come from 2026 chats'
  own listen streams and from handshake chats' `resources/subscribe`, which the
  combiner now serves (so it advertises `resources.subscribe`); handshake
  upstreams still push unasked and are forwarded as before. Prompt list changes
  are now forwarded too.
- A change a 2026 upstream sends both as a plain notification and on the
  listen stream (svg-mcp does) is forwarded once: the listen copy.
- Mock server: serves `subscriptions/listen`, emits resource updates via
  `POST /control/resource-updated`, and reports open listen filters in `/stats`.
- Mock server `--sessions`: `mock__create_session` / `mock__end_session` and a
  visible `session_id` on `mock__remember` / `mock__recall` — a stand-in for a
  2026 server that owns its sessions.
- **Reverse bridge:** a handshake-era client calling a 2026 upstream that asks
  for input by returning `InputRequiredResult` is asked live
  (`elicitation/create`), and the call is retried with the answer, repeatedly
  if the upstream asks again (bounded). A 2026-era client reaches a 2026
  upstream natively.
- Era translation, part 3 (behind the same flag): **held calls**. When a
  handshake-era upstream (isolated, so it forwards elicitation) asks for
  input mid-call and the client is on the 2026 revision, the combiner keeps
  the upstream call running, returns the question as an
  `InputRequiredResult`, and resumes the call with the client's retry —
  repeatedly if the upstream asks again. A held call belongs to the chat
  token that started it and is cancelled after 60 minutes without an answer
  (`MCP_COMBINER_HOLD_TIMEOUT`); a retry after that, or after a combiner
  restart, is told to call the tool again. Sampling and roots requests from
  upstreams are not bridged yet.

## [0.19.0] — 2026-11-02

### Added

- **Authorize OAuth servers from another device** — new `oauth.public_url`
  and `oauth.prompt` settings (and a per-server `auth.oauth.prompt`). With
  `prompt` `elicit` or `both`, a server needing (re-)authorization no longer
  blocks the request that hit the 401: a tool call routed to it fails at once
  with MCP's URL-elicitation error (`-32042`) carrying the authorization link,
  which the client shows to the user and not to the model. With `public_url`
  set, the OAuth redirect comes back to the combiner's own
  `/oauth/callback/<server>` route (behind e.g. `tailscale serve`), so the
  link can be completed on a phone. On completion the combiner exchanges the
  code, reconnects the server without a manual `combiner__enable_server`, and
  sends `notifications/elicitation/complete`. Clients without URL elicitation
  get the link in the tool error text. `prompt` defaults to `both` with
  `public_url` and `browser` without, so existing setups are unchanged;
  `elicit` without `public_url` warns at load (the loopback redirect only
  completes on the combiner host). Links expire after 15 minutes and the next
  call issues a fresh one. The browser is not opened on hosts without one
  (containers, Linux without a display). Clients without URL elicitation get
  a short link on the combiner (`/oauth/authorize/<id>`, redirecting to the
  full ~400-character authorization URL) that fits on one line and stays
  clickable in terminals.
- `subscriptions/listen` for 2026-07-28 clients (the first piece of the
  era translation layer, active only with `MCP_COMBINER_OFFER_2026=1` for
  now): a chat's listen stream receives `tools/list_changed` and resource
  notifications — broadcasts plus its own per-chat events, keyed by its chat
  token — wherever handshake-era sessions get them today.
- The mock OAuth server (`mcp_combiner.mockserver --oauth`) gains
  `POST /control/oauth/revoke` (`?clients=1` also forgets registrations), to
  force re-authorization when testing.

### Changed

- **Built on fastmcp 4 / mcp 2** (was fastmcp 3 / mcp 1.26); the HTTP client
  is now `httpx2`, a direct dependency. The combiner stays on the stateful
  protocol: every client it opens (upstream connections, isolated sessions,
  the ctl and widget host) uses the `initialize` handshake, so upstream
  `Mcp-Session-Id` continuity, isolation, resume and handover behave as
  before. Today's clients (Claude Code, OpenCode, Pi) use the handshake too.
  A client on the new 2026-07-28 protocol revision would be served
  statelessly by mcp 2, without `tools/list_changed` or other pushes.
- **Clients are offered the handshake era only.** mcp 2 also serves the
  2026-07-28 revision, and a client that probes `server/discover` (Claude Code
  does) would pick it — losing `tools/list_changed`, consent prompts and
  forwarded elicitations, which the combiner can't yet translate for that
  revision. `server/discover` now lists no versions, so such clients fall back
  to the stateful handshake, as before fastmcp 4. `MCP_COMBINER_OFFER_2026=1`
  lifts the pin (for developing the translation layer).
- Downstream HTTP sessions never expire from inactivity (mcp 2's default is
  30 minutes; chats routinely idle longer, and the combiner's isolation
  lifecycle owns chat expiry).
- **Schema validation of proxied calls is the combiner's own**, since fastmcp
  4 / mcp 2 validate neither direction for proxied tools. Input validation is
  now **on by default**: a call whose arguments don't match the published
  schema is refused without an upstream round trip (`required` names the
  schema's own properties never declare are ignored). Output validation is
  now **off by default**: results are relayed and the end client validates
  what it consumes. `validation.input` / `validation.output` and
  `--[no-]input-validation` / `--[no-]output-validation` override both.
- Upstream tool schemas the MCP wire format rejects (a property given as
  `[]`, a non-object root, non-string `required` entries) are repaired before
  mcp 2's client validates the listing, which would otherwise drop the
  server's entire tool list; the combiner applies the same repair to what it
  publishes, since mcp 2 also refuses to send them.
- The `/sessions` listing and the token routes now report the same session
  ids (the wire `Mcp-Session-Id`), closing the last part of QUESTIONS.md Q1.
- The combiner's HTTP clients accept server-sent events up to 64 MiB
  (fastmcp 4 defaults to 1 MiB, which a combined `tools/list` across many
  servers exceeds — `mcp-combiner tools` / `restart-server` failed).

### Fixed

- A completed out-of-band authorization now reconnects the server even when
  the reconnect uses a different OAuth provider object than the one that
  issued the link: providers re-read the shared token store first. Before,
  the reconnect saw no tokens and issued a second link.
- A server whose tokens were revoked mid-chat now hands the next call a fresh
  authorization link instead of failing with "Connection closed".

- An OAuth failure while opening an upstream connection is now recognised as
  an auth failure (the server parks in `auth_failed`) even though fastmcp
  wraps it in `RuntimeError("Client failed to connect: ...")`. Before, the
  wrapped error was treated as a connection problem and the reconnect monitor
  kept retrying it.

## [0.18.0] — 2026-11-01

### Added

- **`isolate: true` now works on stdio servers — per-chat SUBPROCESS isolation**
  (was HTTP/SSE-only; stdio entries ignored the flag with a startup warning).
  Each chat (the same token-keyed identity the HTTP layer uses) gets its own
  `command` subprocess — a stdio subprocess IS a session, so state partitioning
  is total. Lifecycle reuses the token-keyed registry: grace expiry leaves the
  child running (in-memory state preserved — the analog of keeping an HTTP
  server-side session), the park-ttl forget terminates it, and a token
  re-appearing inside the window reattaches to the same child. On combiner
  shutdown children are always terminated (they cannot transfer to a successor)
  and disable/reload evicts them too (remount hands chats fresh children).
  A crashed child is detected and replaced transparently on that chat's
  next call — a fresh subprocess with fresh state; in-memory state is lost
  and only the call that was in flight when the child died errors. Per-chat
  clients are `StatefulProxyClient` clones, so fastmcp's proxy forwarding
  (log/progress/sampling/roots) still works under isolation; the plain
  (non-handover) shutdown now releases parked HTTP sessions concurrently
  with a best-effort DELETE (an N×5s sequential stall becomes one 5s bound)
  since no successor could resume them anyway; and the
  startup tools/list prime rides an ephemeral probe child that dies with
  its context (no startup leaks). Cost: N concurrent chats spawn N copies
  of the server's runtime.
- **`externalServers` — non-owned backing processes** (e.g. docker)
  - New top-level `externalServers` config section alongside `sharedServers`.
    Where a `sharedServer` entry is a process the combiner *owns* (spawned and
    refcounted by sharedserver, kept warm for `grace_period`), an
    `externalServer` entry is a process whose lifetime some other tool decides
    (`docker run`, `docker compose up -d`): the combiner optionally launches
    it, waits for the server's `url` within `health_timeout`, and on shutdown
    does exactly what the config states — run a `stop` command, terminate a
    `foreground` child, or leave it running.
  - Lifecycle semantics derive from three fields (`launch`, `stop`,
    `foreground`) rather than a mode enum; omitting `launch` makes the entry
    attach-only (reachability probe with no process management), giving plain
    `url`-only HTTP servers the same status reporting as backed ones — as a
    background probe that never delays combiner readiness.
  - `BackingManagerBase` (new `mcp_combiner/backing.py`) owns every lifecycle
    frame — `backing_state`/`start_all`/`ensure_started`/`ensure_stopped`/
    `restart`/`stop_all`, entry dedup, concurrent gather, and the shared
    backing-state vocabulary — so there is no kind-switch anywhere. The three
    kinds only implement their claim resolution and behaviour hooks.
    `BackingProcessManager` (same module) is an ordered registry facade: the
    first manager claiming a server wins, which is the single place backing
    precedence is decided; a trailing `PlainUrlProbeManager` claims everything
    no other kind manages (plain url-only servers) and background-probes it
    for status parity. `_overlay_backing_state` needed no fork.
  - `sharedServer` wins over `externalServer` when a server entry sets both
    (load-time warning; the facade's claim order is the single precedence
    source — a dangling `sharedServer` reference falls through to the
    external one). Dangling section references also warn at load. Ownership
    keys (`grace_period`, `command`/`args`) on an `externalServers` entry are
    warned about as unknown and ignored, as is a `stop` on an entry with no
    `launch` (nothing was launched, so nothing gets stopped).
  - Shutdown bounds: the `stop` command and the foreground-child termination
    **share** a 3s budget on the shutdown path (the ~5s SIGTERM→SIGKILL window
    sharedserver gives the combiner; `unuse`'s bound is 2s) — not 3s + 5s. A
    `docker stop` CLI killed at the budget still completes daemon-side, so
    containers stop. Off-shutdown (disable/restart) stops get a wider 10s
    budget. Reload (`combiner__reload_config`) now treats backing-entry
    rewrites as server changes too (teardown stops the old backend via the
    launch-time entry snapshot; remount relaunches with the new one), so
    added servers launch and changed launch/stop argv take effect (a rewritten
    `sharedServers` entry reattaches within its grace period instead — its new
    `command`/`args` need a real process restart, not a remount). Shutdown
    stops always use the launch-time snapshot; disable/restart use the live
    config. `restartable` requires a real stop path (`stop` configured or
    `foreground`) — attach-only and detached-without-stop entries fall back
    to the caller's reconnect path instead of relaunching into a name
    conflict — and a disable/enable that races an in-flight launch or restart
    is last-wins (an enable cancels a deferred stop; a disable skips the
    relaunch and the remount). Plain url-only probes are re-run on every
    (re-)enable, so a stale `stopped` verdict never outlives a disable. One
    narrow exception to last-wins: a reload whose teardown/remount races a
    still-running launch (≤ health_timeout + launch) keeps the in-flight
    old-argv backend; the next restart applies the new argv.
- **The package ships type information** (`py.typed`, PEP 561), so code
  importing `mcp_combiner` is type-checked against its annotations. The
  package and its tests are clean under pyright and mypy `--strict` with no
  `type: ignore` suppressions.

### Fixed

- A caller waiting on another caller's in-flight OAuth flow now gets an error
  when that flow finishes without an authorization code, instead of handing
  `None` to the token exchange as the code.

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
