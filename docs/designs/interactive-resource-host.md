# Interactive resource host — serving browser resources combiner-side

Status: design (post-discussion 2026-09-15); **Stage 1 implemented + route-tested
(2026-09-16)** — gaps below. Companion doc:
[`plugins/pi/docs/adapter-design.md`](../../plugins/pi/docs/adapter-design.md) (the Pi
client half — its "later: ext-apps interactive UIs" row is _this_ design).

## The problem

MCP servers increasingly expose **interactive resources** — widgets with
`mimeType: text/html;profile=mcp-app` and `ui://` URIs (e.g. `todoist-task-list`,
served by this very combiner today). Reading them as text returns markup, not the
app: the widget expects to run in a sandboxed iframe with a **host** that answers
its postMessage protocol — prompts, notifications, model-context updates, tool
calls, streaming result patches.

`pi-mcp-adapter` implements that host client-side (TypeScript): a loopback HTTP
server, a vendored 295 KB `app-bridge.bundle.js` for the iframe side, a battery of
proxy routes (`/proxy/tools/call`, `/proxy/ui/message`, `/proxy/ui/context`,
`/proxy/ui/consent`, `/proxy/ui/open-link`, `/proxy/ui/download-file`,
`/proxy/ui/request-display-mode`, `/proxy/ui/heartbeat`, `/events` SSE), consent
gating, session recovery, and `mcp({action:"ui-messages"})` retrieval. Only Pi
benefits. Claude Code, OpenCode, and Neovim would each need their own host — none
has one.

## The decision

**The combiner serves the host.** One Python implementation, every client gets
interactive resources by opening a URL.

Why combiner-side is the right architecture here:

1. **Build once, serve every client.** A `/ui/…` route family on the combiner's
   existing Starlette app replaces a ~2–3 k LOC host _per client_ with one shared
   implementation plus a ~100 LOC "detect and open the browser" shim per client.
2. **The grouping token is already the right key.** The UI URL carries the chat's
   token (`/ui/<token>/…`), so widget-initiated tool calls flow back attributed to
   _that chat_: the permissions gate (elicit) applies to widget calls natively, and
   per-chat upstream isolation holds. The TS host approximates this by proxying
   through its own client connection; the combiner _is_ the enforcement point, so
   it needs no approximation — and a widget session survives its client
   disconnecting (parked like any isolated upstream session).
3. **The infrastructure exists**: custom routes (`routes.py`), inbound bearer auth,
   the token→downstream-session registry (`/sessions/*`), elicitation toward the
   connected client, and `mockserver` as the instrumentable upstream for tests.

What we **vendor** vs **port**:

- **Vendored as-is**: `app-bridge.bundle.js` (MIT, from pi-mcp-adapter) — the
  iframe-side bridge; static file serving, unmodified.
- **Ported to Python**: the host-side proxy routes and session lifecycle (~1–2 k
  LOC), with the adapter's `ui-server.ts` / `ui-session.ts` as the reference
  implementation.

## Reference: what the adapter's host actually does

Detection — a resource or tool is a UI surface when `mimeType` is
`text/html;profile=mcp-app`, or `_meta["ui/resourceUri"]` / `_meta.ui.resourceUri`
names a `ui://` URI. Resources may carry `_meta.ui` with `prefersBorder` and CSP
hints (`connectDomains`, `resourceDomains`).

| Route                                                   | Direction      | Purpose                                                                                                                                         |
| ------------------------------------------------------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `/` (host page)                                         | → browser      | sandboxed iframe (`allow-scripts allow-forms allow-modals allow-popups allow-downloads`), CSRF token, Done/Cancel controls                      |
| `/app-bridge.bundle.js`                                 | → iframe       | the vendored bridge                                                                                                                             |
| `/resource/<session>/<path>`                            | → iframe       | same-origin serving of the resource content itself                                                                                              |
| `/proxy/tools/call`                                     | widget → host  | widget invokes MCP tools                                                                                                                        |
| `/proxy/ui/message`                                     | widget → host  | prompts (user input needed) and notifications (recorded)                                                                                        |
| `/proxy/ui/context`                                     | widget → host  | `update-model-context` content/structuredContent — recorded for later retrieval                                                                 |
| `/proxy/ui/generated-tool-call-intent`                  | widget → host  | recorded intents                                                                                                                                |
| `/proxy/ui/consent`                                     | host ↔ widget  | per-server iframe/resource consent handshake                                                                                                    |
| `/proxy/ui/open-link`, `/proxy/ui/download-file`        | widget → host  | capability requests with host mediation                                                                                                         |
| `/proxy/ui/request-display-mode`, `/proxy/ui/heartbeat` | widget → host  | display negotiation / liveness                                                                                                                  |
| `/events` (SSE)                                         | host → browser | streaming updates: result patches (`patch` / `checkpoint` / `final` frames in `shell→narrative→structure→detail→settled` phases), notifications |

Widget sessions are reused when the same tool reopens, watchdog-closed when idle
(~60 s), and their collected prompts/notifications/intents/contexts are retrievable
by the agent (`ui-messages`) so the model can act on what the user did.

## User-visible flows

### Stage 1 — resource-triggered (view + record)

A chat (or `read_*` tool output, or a curious agent) encounters a `ui://`
resource. The client opens:

```text
http://127.0.0.1:9741/ui/<token>/<resource-uri-path>
```

The combiner serves the host page; the widget renders and interacts. Everything
the widget sends — prompts, notifications, model-context updates, intents, tool
calls — is recorded against the session. **Prompts** are intended for the chat's
connected client as MCP elicitation (the existing elicitation path); as shipped,
they are recorded and surfaced via `combiner__ui_messages` — the live relay is
pending (see Risks #0b). Retrieval is a
meta-tool:

```text
combiner__ui_messages            → recorded messages/contexts/intents for this token
combiner__ui_sessions            → open/recent widget sessions for this token
```

No in-flight tool call exists in Stage 1 — the widget is a _view_ whose side
effects are recorded and retrievable. This alone makes every client
browser-capable.

### Stage 2 — tool-triggered (the widget _is_ the tool's UI)

A tool's result (or its `_meta`) references a UI resource — the upstream's intent
is "render my output interactively." The combiner:

1. Holds the proxied `tools/call` in flight (async, with the call's existing
   timeout budget).
2. Emits an MCP **progress notification** carrying the UI URL, so a connected
   client can open the browser (clients that don't listen just don't open; the
   widget still works if the user finds the URL).
3. Records widget interactions as in Stage 1; `update-model-context` payloads and
   the final widget state fold into the eventual tool result.
4. Resolves the call when the widget signals done (or the watchdog/timeout fires,
   returning what was recorded — never hanging forever).

Stage 2 is where the streaming envelope (`patch`/`checkpoint`/`final` frames over
`/events` SSE) becomes load-bearing for long-running visualizations.

## Architecture

```
combiner (Starlette/FastMCP)
├── mcp_combiner/ui_host/
│   ├── routes.py        /ui/<token>/… family: host page, bundle, sandbox relay,
│   │                    resource serving, /proxy/tools/call + /proxy/ui/*, /events SSE
│   ├── sessions.py      UiSession registry: id, token, resource uri, age; recorded
│   │                    buckets (messages/contexts/intents); call_as_token / read_as_token
│   │                    (loopback MCP clients into the combiner's own /mcp/<token>)
│   ├── templates.py     host page + sandbox proxy documents (svg-mcp-family dark chrome)
│   └── static/app-bridge.bundle.js   (vendored, MIT)
├── meta_tools.py        combiner__ui_sessions / combiner__ui_messages (retrieval loop)
└── runtime.py           UiSession registry lives with the other token-keyed state (RUNTIME.ui_host)
```

(The design originally split widget-calls / prompt-relay / stream into separate
modules; the implementation kept them in `routes.py` — the doc reflects what
ships.)

- **Routing key**: everything hangs off `<token>` — the same grouping token the
  `/sessions/token/*` routes already resolve. An unknown token → 404 (no session
  invention from the UI surface).
- **Widget tool calls** (`/proxy/tools/call`): executed through the combiner's
  normal call path with the token's downstream session identity — permissions
  gate, per-chat isolation, and tool prefixing all apply unchanged.
- **Session lifecycle**: create on first GET, reuse while active, watchdog-close
  after idle, retain recorded messages until the token's parked-state TTL
  matches isolation semantics.

## Security model

Same posture as the adapter's host, adapted to the combiner:

- **Loopback by default**: the UI routes serve on the combiner's existing bind —
  fine on 127.0.0.1; if bound wider, the inbound bearer (`MCP_COMBINER_AUTH_TOKEN`)
  gates `/ui/*` exactly like `/mcp` and the control routes.
- **Token in path**: already trusted for `/sessions/token/*`; `/ui/<token>/*` is
  the same trust level (loopback control plane).
- **Sandboxed iframe**: `allow-scripts allow-forms allow-modals allow-popups
allow-downloads`; camera/mic/geo/clipboard only when the resource's
  `_meta.ui` permissions say so (buildAllowAttribute).
- **CSP from `_meta`**: `connectDomains` / `resourceDomains` constrain the iframe;
  the host page sets them, the proxy refuses out-of-domain requests.
- **CSRF**: per-session token on every `/proxy/*` call (the bridge already sends
  it); UI routes reject mismatches.
- **No secrets in URLs**: the grouping token is a chat correlation key, not a
  credential; the bearer stays a header.

## Client contract (thin)

Each client plugin implements only:

1. **Detect**: `ui://` resource or `_meta` UI reference in a tool result / resource.
2. **Open**: `open <combiner-origin>/ui/<token>/<path>` (browser, or a native
   window viewer where one exists).
3. _(Optional)_ **Listen** for progress notifications carrying UI URLs (Stage 2)
   and open those too.
4. _(Optional)_ **Surface** `combiner__ui_messages` results to the model
   prominently after a widget session closes.

The Pi extension's slice is ~100 LOC; the Claude/OpenCode/Neovim plugins get the
same four lines each.

## Risks & open questions

0. **KNOWN DIVERGENCES (Stage 1 as shipped)**: (a) the design's "unknown token →
   404" is NOT enforced — any token gets a session, because the grouping token is
   already the /mcp/<t> capability (pinned by
   `tests/test_ui_host.py::test_any_token_gets_a_session`); (b) widget prompts are
   recorded but NOT yet relayed to the chat client as elicitation — retrieval via
   `combiner__ui_messages` is the only read path until the relay lands.
1. **Holding `tools/call` in flight** (Stage 2) while a human interacts: timeout
   budget, watchdog, reconnect semantics when the _client_ drops mid-widget
   (answer: the call is combiner-side, so it survives; the client reconnects and
   the result waits in the session). Needs careful tests.
2. **Protocol drift**: the ext-apps surface is moving (the adapter's stream
   vocabulary already carries its own namespaced notification methods). We port
   the route set the vendored bridge speaks — version-pin the bundle and port
   together, re-vendor when the adapter's bundle updates.
3. **Multi-client concurrency** on one widget session: last-writer-wins on
   prompts; recorded messages are append-only, so concurrent viewers degrade
   gracefully. Acceptable.
4. **Async ergonomics in FastMCP**: the UI routes are plain Starlette (`custom_route`
   pattern already used); the in-flight call holder needs a task handle in
   `CombinerRuntime` — must not block the event loop or the request plane.
5. **Auth on `/events` SSE**: long-lived GET with bearer header; EventSource can't
   set headers — use a per-session query token minted at host-page render (same
   CSRF token), acceptable on loopback.

## Testing

**Implemented — `combiner/tests/test_ui_host.py` (e2e tier, 11 cases)**: host-page
render (session mint, session token, chrome), missing-resource 400, unknown route
404, proxy auth 401, heartbeat, message/context recording verified through the
`combiner__ui_messages` meta-tool, widget tool calls through the real
`/mcp/<token>` pipeline (intent recorded), consent deny → 403 / grant → allowed,
`/events` SSE `ready` frame, sandbox relay on the second origin. The mockserver
carries a `ui://mock/widget` fixture (mcp-app mime) + `mock__widget_ping` tool for
both CI and the manual matrix.

**Not testable without a browser** — the manual matrix: the todoist task-list
widget (and the mock fixture) from Pi (extension detect+open), Claude Code
(plugin open), and a bare browser against a tokened URL. Route-level tests bypass
the bridge deliberately: `/proxy/*` is plain HTTP and the bundle is just one
client of it.

**Still to build (test-first when picked up)**: prompt→elicitation relay (record +
forward + queue fallback), Stage 2 call-hold/release, progress-URL emission,
timeout-return, streaming envelope frames.

## Staging

1. **Stage 1** — host page + vendored bridge + resource serving + recorded
   interactions + prompt→elicitation relay + `combiner__ui_messages` /
   `combiner__ui_sessions`. Every client becomes browser-capable.
2. **Stage 2** — tool-triggered in-flight flow, progress-URL, streaming
   envelopes.
3. **Later** — consent UI polish, display modes, download mediation, native
   window viewers per client (Glimpse-style), multi-widget dashboards.
