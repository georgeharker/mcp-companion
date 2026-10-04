# Call chain: how one MCP call moves through the combiner stack

The succession of layers, the naming at each layer, and who supplies every
token — verified against `connection.ts`, `index.ts`, and the combiner's
`asgi.py`. Phase: **legacy client, pi-builtin MCP disabled**
(the combiner entry in `~/.config/mcp/mcp.json` carries `"enabled": false`).

## The cast (outside-in)

| layer | owner | naming it produces |
|---|---|---|
| model tool-call | the model | bare `<server>_<tool>` (`mock_echo`), the router `mcp({tool, args})`, or `read_<server>_<name>` |
| our extension's surface | `plugins/pi` (`direct-tools`, `proxy-tool`, `resources`) | the *registered* pi tools: bare names, `mcp`, `mcpScript`, `read_*` |
| our connection | `client/connection.ts` (`CombinerConnection`) | URL `/mcp/<token>` (chat token in the path) |
| pi-builtin MCP | pi-core (`builtin:mcp`) | `mcp__mcp-combiner__<tool>` — **inert this phase** (entry `enabled:false`: listed, never connects) |
| combiner ASGI | `combiner/mcp_combiner/asgi.py` | strips `/mcp/<token>` → `/mcp`; maps token ↦ wire session |
| combiner gate/middleware | `middleware.py`, `permissions.py`, `toolcache.py` | keeps names `<server>_<tool>`; adds consent/meta surfaces (`combiner__*`, `open_widget`) |
| mounts / connection manager | `connections.py`, isolated registry | upstream tools keep their own names; per-chat upstream sessions for `isolate` servers |
| upstream | the MCP servers | their native tool/resource names |

No layer renames a tool except the combiner's `<server>_` prefix on expose,
and our `read_` prefix on resource reads.

## The call path, step by step (direct `mock_echo` example)

1. **Model → our tool.** The model calls the pi tool `mock_echo`
   (registered by `direct-tools.ts`) or `mcp({tool:"mock_echo", args})`
   (router): `proxy-tool.ts` resolves the name against `listTools()`
   (names as the combiner exposed them) and calls `connection.callTool`.
2. **Retry armor.** `withStaleRetry` wraps every upstream method: a combiner
   bounce mid-flight is noticed by whichever call gets there first; one
   reconnect+retry. (A hard failure reaches the model with the recovery hint.)
3. **Connect (lazy, single-flight).** `ensureConnected` builds
   `/mcp/<token>` + a bearer auth provider, opens a streamable-HTTP
   `Client` **advertising `elicitation` capability**.
4. **Inbound auth (combiner).** The bearer (`Authorization`) is checked
   against `MCP_COMBINER_AUTH_TOKEN` — required on `/mcp`, `/sessions`,
   `/handover`. This is the *process* secret, op-injected, shared by all clients.
5. **Token rewrite (combiner).** `TokenRewriteMiddleware` takes the chat
   token from the URL path (or `X-MCP-Combiner-Session` header fallback),
   rewrites `/mcp/<token>` → `/mcp`, records token ↦ wire-`Mcp-Session-Id`,
   and applies any pending token filters on first connect.
6. **The gate (combiner).** `permissions.enforce` resolves the server's
   policy; on elicit it forwards a consent ask to *this* session (our
   elicitation bridging), and grants are keyed by the **chat token**.
7. **Mount → upstream.** The call goes to the server's mount: for
   `isolate` servers, a per-`(server, token)` upstream session (parked at
   restart, resumed by token); otherwise the shared upstream session.
8. **Return.** Results render back through our surface; widget URIs get
   the UI-host URL `/ui/<token>/?resource=<namespaced-uri>`.

## Elicit and widget return paths

- **Elicit** returns the other way: upstream `ctx.elicit` → fastmcp's
  restoring handler → the combiner forwards to the *calling* downstream
  session (via the downstream-request ctxvar) → our `elicitation.ts`
  → `ctx.ui.select/confirm` → (this phase) the local TUI, or the un-bien
  relay → the paired app. If pi's builtin were the caller, none of this
  forwards — the strongest reason it must not make calls.
- **Widgets** are fetched/holded under the same chat token: `open_widget`
  reads `ui://` resources server-side and holds the call while the user
  interacts; the browser URL embeds the token.

## Tokens: who supplies what

| token | minted by | carried where | lives how long |
|---|---|---|---|
| **chat/grouping token** (`pi-<session-uuid>`) | the *client*, outside the combiner (custody principle): pi extension at session_start — resume-stable, derived from the session file's UUID; nvim mints a bare UUID, the Claude plugin presents its own | URL path `/mcp/<token>` (fallback: `X-MCP-Combiner-Session` header) | across combiner restarts — it is the rejoin identity |
| **inbound bearer** (`MCP_COMBINER_AUTH_TOKEN`) | 1Password (`op://` refs → secrets materialization), read from env at connect time | `Authorization` header | static-ish; rotates via `refresh_mcp_secrets` |
| **wire `Mcp-Session-Id`** | the combiner (transport-level, per downstream connection) | response/transport headers, invisible to us | dies with the combiner process; standard re-init covers it |
| **upstream session id** (per `(server, token)` for `isolate` servers) | the upstream | combiner-side isolated registry | persisted by parking; resumed by token on next boot |

The custody principle is the load-bearing bit: identities that must survive
a combiner restart are minted *outside* it, so nobody has to trust the
combiner's memory. The handover carries state *about* those identities
(grants, filters, binds, parked sessions); never the identities themselves.

## Phase notes

- **legacy-disabled (this phase):** the mcp.json entry carries
  `"enabled": false` — pi's builtin lists the server but never connects, so
  the only calls, identity, filter enforcement and consent routing are ours.
  Our ladder-reader deliberately ignores `enabled` (it reads the url to find
  the combiner).
- **native v1:** entry re-enabled with `"exposure": "hidden"` — pi = the
  transport/OAuth/resource layer; its identity exists but makes no tool
  calls, so the identity-fork is inert; ours owns the tool surface +
  elicit forwarding via the owned connection.
- **`registerMcpServer` endgame:** pi becomes the tool surface as well; the
  legacy client and our owned connection retire (see
  `src/legacy-client/README.md`).

## The pi-native path (contrast — how pi 1.0 calls the combiner on its own)

pi registers the SERVER, not per-tool pi-tools: tools surface under the
`mcp__<server>__<tool>` namespace (`mcp__mcp-combiner__mock_echo`), reached
by three mechanisms — `direct` exposure (declared to the model like a
built-in each request), `deferred` (undeclared until `tool_search` promotes
a match), and `codemode` (the model writes a script calling
`mcp__mcp-combiner__<tool>`; the runtime waits for the server it names).
There is no universal router tool.

| layer | what happens | identity/token |
|---|---|---|
| connect | pi connects the ladder entry in-process at session start (or first need) | bearer from the entry; **no chat token** — pi doesn't know custodied identity |
| declaration | `direct` tools → declared as `mcp__mcp-combiner__<tool>` per request; `deferred` → hidden until `tool_search`; `codemode` → callable from scripts | — |
| dispatch | name → server + tool; pi's own McpClient POSTs the combiner | **tokenless** leg |
| combiner ASGI | `_dispatch_tokenless`: the wire `Mcp-Session-Id` becomes the chat name; sid-keyed filters/grants | identity = the transport session — **dies at restart** |
| gate | policy resolve; elicit asks route to pi's session, which **cannot forward** — a gated call hangs unless `elicit_unavailable` fires | sid-keyed, self-scoped |
| mount → upstream | per-`(server, sid)` sessions for isolate servers | — |

Known gaps in this phase (why legacy remains the identity-correct path): no
restarting-durable identity, no consent-grant carry, upstream elicit cannot
forward to a human, no prompts surface (pi-mcp lacks `listPrompts`).
