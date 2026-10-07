# Upstream PR drafts — paste-ready texts

Companion to `upstream-extension-seams.md`. File both together (after
mcp-companion 0.16.0 ships stable), citing `@geohar/pi-mcp-combiner`.

---

## PR A — Host-provide `@earendil-works/pi-mcp` to extensions

Branch (drafted in ~/Development/pi/pi): `extension-host-provided-pi-mcp` —
2 files, +6 lines.

### Title

Host-provide `@earendil-works/pi-mcp` to extensions (VIRTUAL_MODULES + host guard)

### Body

pi's built-in MCP support is built on `@earendil-works/pi-mcp`, and the package
is already a real dependency of the coding-agent (`^1.0.3`) — but it is absent
from both `VIRTUAL_MODULES` and `HOST_PROVIDED_EXTENSION_PACKAGES`, so
extensions cannot import it:

- Published extensions install into a tree disconnected from pi's own
  `node_modules`; the import fails with `Cannot find module
  '@earendil-works/pi-mcp'`.
- The workaround an extension ships today is its own npm-resolved copy —
  exactly the duplicate-runtime-modules risk the host-provided guard exists to
  prevent.

Observed with `@geohar/pi-mcp-combiner` (published on npm; MCP sharing +
elicitation + interactive resources), whose connection layer is built on
pi-mcp so that its shared-server architecture gets the same session handling
as pi's built-in connections.

This adds the package (root + `/oauth`; `/testing` left out as test-only) to:

1. `virtual-modules.ts` — the loader alias table, so extension imports resolve
   against pi's bundled copy at runtime.
2. `resource-loader.ts` — the `HOST_PROVIDED_EXTENSION_PACKAGES` guard, so
   declaring it as a dependency triggers the same "peer-with-`*`" hygiene
   warning as the other host-provided packages.

Extensions that mirror the built-in's MCP-client behavior (session-expired
retry, elicitation capability advertisement) then share pi's single copy,
versioned with pi, with no private installs.

A companion RFC (filed alongside) proposes the longer-term shape: sanctioned
hooks for elicitation/resources on built-in connections, and a documented
stability tier for the pi-mcp surface extensions may build on.

---

## RFC B — Extension seams for pi's MCP surface (hooks + stability tier)

### Title

RFC: MCP extension seams — elicitation/resources hooks on built-in connections, and how much of pi-mcp is the stable surface

### Body

**Context.** mcp-combiner (published:
`@geohar/pi-mcp-combiner` — MCP server sharing across clients, per-chat token
identity, consent policy, elicitation, interactive `ui://` resources) works
today by owning its own pi-mcp connection rather than pi's built-in one. This
RFC derives from that experience the seams that would let such extensions ride
built-in connections instead, and proposes an explicit stability tier for the
pi-mcp surface they would build on.

### The shape experiment: what ownership would hooks delete?

What the extension owns today *solely because the hooks don't exist*:

| Today (extension-owned workaround) | With hooks | Net |
|---|---|---|
| Connection lifecycle: connect/reconnect, teardown, reload-time zombie disposal, stale-session retry | pi owns all of it | **deleted** |
| Capability advertisement (`elicitation: {}` in client capabilities so servers will send elicit requests) | pi advertises when a hook is claimed | **deleted** |
| `setRequestHandler("elicitation/create")` claim | `pi.on("mcp_elicit", handler)` — pi advertises elicitation while a handler exists (the `mcp_servers_change` claim-by-listener idiom) | **replaced by the seam** |
| `list_changed` listeners ×2 (tool-plane churn) | pi refreshes internally; extension may want a lighter hook for its *own* additions | mostly deleted |
| Tool exposure per server tool + the router call-form | pi's built-in `mcp` tool + bare-name tools already cover this | mostly deleted |
| Interactive `ui://` resources (pi's `read_mcp_resource` skips ui://) | `pi.on("mcp_resource_read", handler)` — extension becomes the renderer | **replaced by the seam** |
| Per-chat token identity sent as connection headers | `pi.on("mcp_connect", handler)` (headers/auth/timeouts; static subsets can stay per-server config) | **new seam needed** |
| Consent gate on `tools/call` | stays with our backend (combiner middleware) — deliberately not an extension hook | unchanged (correct place) |

The headline: with three hooks — **elicit-claim, resource-read, connect-options**
— the extension stops owning a connection entirely. That deletes the entire
class of lifecycle hazards we debugged (stale-ctx crashes on reload being the
crash-visible one), and it is the difference between "extending pi's MCP
surface" being a sanctioned act versus a workaround with a known blast radius.

### Proposed seams (names TBD by you, shape for discussion)

These follow pi's existing `.on()` interception idiom — NOT register-verbs: nothing
nameable is being added to a registry; these are flow-interception points, the
same shape as `before_provider_request` / `before_provider_headers` /
`session_before_fork` (result-bearing events with sequential folding), and pi
ALREADY uses claim-by-event-listener for MCP (`mcp_servers_change`: "Handling
this event marks an extension as the one that connects registered servers";
the runner gates its own behavior on `hasHandlers("mcp_servers_change")`).
Sourced from the same `mcp_*` family:

```ts
// Fired when a built-in-owned MCP connection raises elicitation/create.
// Returning an ElicitResult answers; void declines-to-handle (pi's own default
// applies). pi advertises the elicitation capability for that server exactly
// while any handler exists (hasHandlers-driven — the two-switch).
pi.on("mcp_elicit",
    (event: McpElicitEvent, ctx: ExtensionContext) => Promise<ElicitResult | void>,
): () => void
// McpElicitEvent: { type: "mcp_elicit", server, request: ElicitRequest, signal: AbortSignal }

// Fired for resources/read on built-in connections. A reader result renders;
// void defers to pi's default behavior (which skips ui:// entirely).
pi.on("mcp_resource_read",
    (event: McpResourceReadEvent, ctx: ExtensionContext) => Promise<ReadResourceResult | void>,
): () => void

// Fired before a server connection is established; the event carries the
// transport options — mutate-in-place, exactly like `before_provider_headers`
// passes a live ProviderHeaders. This is how per-session token identity reaches
// the wire without the extension owning the connection.
pi.on("mcp_connect", (event: McpConnectEvent, ctx: ExtensionContext) => void | Promise<void>): () => void
```

Composition semantics, stated per pi's fold idiom but constrained where consent
is at stake:
- `mcp_elicit`: FIRST handler returning a result wins the answer; pi warns when
  multiple extensions register the event (two simultaneous askers are a
  composition bug, not a UX — consent authority must not fork silently). The
  event's `AbortSignal` carries call cancellation into in-flight dialogs.
- `mcp_resource_read` / `mcp_connect`: sequential fold, all handlers run (same
  as `tool_result` / `before_provider_headers`).
- Lifecycle: `on()` returns the unsubscribe function, and handler sets snapshot
  per live extension instance — a stale instance's claim retires with its
  extension record (the runner's existing behavior), so no new zombie-claim
  machinery is requested. (Confirm, maintainers, since that is your code.)

Two-switch capability advertisement: pi advertises `elicitation` (and honors
reader claims) ONLY while a handler exists for the event — otherwise a server
sends requests into the void. This is the mechanism `mcp_servers_change` already
uses. Tool exposure stays an independent switch regardless.

### How much of pi-mcp is reasonable to make stable?

Since extensions would build on pi-mcp types even when not owning connections
(hook payload shapes are its protocol types), the practical question is
tiering. Suggested:

**Tier 1 — semver-stable, the extension contract:**
- `McpClient` public methods: `connect`, `callTool`, `ping`, `close`,
  `listTools`, `listResources(+Page/Templates)`, `readResource`,
  `notify`, `setRequestHandler`, `onNotification`, `onError`, `onClose`
  — plus `listPrompts`/`getPrompt`, which today don't exist and there is no
  generic `request()`: prompts are unreachable for extensions.
- `McpClientOptions` (`capabilities`, `roots`, timeouts).
- Complete type re-exports from the package root — including
  `ElicitResult`/`ElicitRequestParams`, which are currently absent from the
  barrel (extensions writing elicit handlers must hand-roll shapes today).
- The complete error taxonomy, with a shared base class carrying a
  `kind`/`status` discriminator and a preserved `cause` chain.
  `McpConnectionClosedError`/`McpTimeoutError` are raisable but plain-`Error`
  today — consumers string-match messages (`/404|closed|fetch failed|stale/i`
  is a live example from the published extension). Rule of thumb: if a caller
  can meet an error at all, it can import it — selective exports just move
  users to string-matching.
- Transport constructor signatures (`StreamableHttpTransport`) for extensions
  that still want standalone/shared-out-of-process connections.

**Tier 2 — documented best-effort:** the `/oauth` subpath (provider interface
is stable-shaped but flows evolve).

**Explicitly unsupported:** `/testing`, and anything not re-exported from the
root (underscore the internals in docs).

### Sequence

1. PR "Host-provide `@earendil-works/pi-mcp`" (filed alongside this RFC) —
   unblocks imports so any of this can even be written.
2. If the hook shapes land in principle, an example extension in-repo
   (connect, claim an elicit, render a resource) citing mcp-combiner as the
   deployed instance.

---

*Drafted by geohar; combiner extension = mcp-companion
(/Users/geohar/Development/neovim-plugins/mcp-companion), native plugin
src under plugins/pi/src/native/.*