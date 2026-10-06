# Upstream: extension seams for pi's MCP surface

## 0. Paste-ready texts

`upstream-pr-drafts.md` holds PR A (enabler) and RFC B (seams + stability tier) ready to file.

Status: draft — file as (a) PR `extension-host-provided-pi-mcp` (branch drafted
in ~/Development/pi/pi @ `extension-host-provided-pi-mcp`) + (b) RFC issue,
**both together, after mcp-companion 0.16.0 ships stable** so we can cite a
published extension. Hold until then.

Real-world instance to cite: `@geohar/pi-mcp-combiner` — shares MCP servers
across clients with a single permission policy, per-chat token identity,
elicitation and interactive `ui://` resources. Everything below was derived
from what it works around today.

## 1. The enabler (PR, already drafted)

`@earendil-works/pi-mcp` is pi's own MCP client library and already a real
dependency of the coding-agent, but extensions importing it fail to resolve
(published extensions install into a tree disconnected from pi's node_modules).

- `virtual-modules.ts`: add `@earendil-works/pi-mcp` + `/oauth` to
  `VIRTUAL_MODULES` (the runtime alias table). `/testing` intentionally skipped.
- `resource-loader.ts`: add both to `HOST_PROVIDED_EXTENSION_PACKAGES` (the
  manifest-warning guard).

Mechanical, reviewable alone; unblocks every other ask since examples can't
even import otherwise.

## 2. The seam (RFC: elicitation + resources as callable hooks)

Today the only way an extension can intercept elicitation or `resources/read`
on pi's built-in MCP connections is to own the connection itself (our native
plugin does: pi-mcp transport, capability advertise
`elicitation: {}`, `setRequestHandler("elicitation/create")`, `list_changed`
listeners). That forecloses coexistence with anything else and duplicates the
connection lifecycle.

Proposal: sanctioned hooks on the extension API, e.g.
`pi.onMcpElicitRequest(handler)` and `pi.onMcpResourceRead(handler)` (or a
general `onMcpRequest(kind, handler)`), receiving requests raised on
built-in-owned connections, async handlers, typed.

Design questions for the RFC (not for us to decide unilaterally):
- **Claim model**: exclusive (first registered wins) vs composed (chain of
  responsibility). Combiner's position: consent authority must be singular —
  multiple askers should be a composition bug, not a UX.
- **Two-switch capability advertisement**: pi should advertise `elicitation`
  server-capability only when an extension claims the handler (otherwise the
  server sends into the void), and the tool-exposure switch stays independent.
- **Resources**: interactive `ui://` resources are the interesting case — pi's
  built-in `read_mcp_resource` skips them; extensions are the right renderer.

## 3. Typed transport-error taxonomy (fold into the RFC)

`withStaleRetry`'s classification regex in our connection,
`/404|stale|session|not found|closed|fetch failed|illegal/i`, exists because
typed catching is impossible: `McpSessionExpiredError` is fine (exported,
subclass), but `McpConnectionClosedError` / `McpTimeoutError` are plain
`Error`s with no kind/status, and fetch failures arrive as raw Node errors.

- Export the complete error surface (the rule: if a caller can meet it at all,
  it can catch it — selective exports just move users to string-matching).
- A shared base with a `kind`/`status` discriminator.
- Preserve/standardize a `cause` chain so diagnostics can name the real reason.

## 4. Prompts convenience (fold into RFC as low-priority)

`McpClient` has tools/resources convenience; prompts missing
(`listPrompts`/`getPrompt`) — our native client calls `request()` generically.

## 5. Examples (post-ship, separate)

An example extension in the pi repo: connect via pi-mcp, intercept an elicit,
serve a resource, tool-surface/router pattern — citing the published
mcp-combiner as the demonstration that the seams are used in anger.
