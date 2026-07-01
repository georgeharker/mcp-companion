# Native Neovim MCP Server — Design

## Background

[mcphub.nvim](https://github.com/ravitemer/mcphub.nvim) shipped a builtin `@neovim`
native server that let an LLM control the running editor (read/write/edit files,
diagnostics, run commands, run arbitrary Lua). We want to bring that capability back
to mcp-companion — but **better designed**, and made to work with our architecture,
which is fundamentally different from mcphub's.

### Why this is harder for us than it was for mcphub

mcphub's `@neovim` server is **pure in-process Lua with no external transport**. Its
handlers run inside Neovim and the result is handed straight to Avante/CodeCompanion.
It never needed a back-channel.

mcp-companion is a two-tier system:

- A **shared Python FastMCP combiner** (one process, serving N Neovim instances) exposed
  at `http://127.0.0.1:<port>/mcp`.
- External agents — ACP (OpenCode) and CLI agents (claude_code, gemini_cli) — connect
  to the combiner **from outside Neovim** and call tools autonomously (see
  `docs/design.md` → ACP Forwarding).

For those external agents, a "control Neovim" tool **must be reachable through the
combiner**, and the combiner — a separate OS process — must reach *back into the specific
live Neovim instance* that owns the chat. That back-channel is the crux of this design.

### What we improve over mcphub

The mcphub `@neovim` server has clear design smells we deliberately avoid:

1. **`execute_lua` is a god-tool.** mcphub funnels most editor control (cursor, windows,
   buffer edits) through arbitrary Lua. That is unsafe to auto-approve, opaque in the
   approval UI (the human sees a Lua blob, not intent), and error-prone for the model.
   → We ship **curated, schema-typed tools**; raw Lua/shell is an opt-in escape hatch,
   off by default, never auto-approved.
2. **File-path editing desyncs from unsaved buffers.** `write_file`/`edit_file` rewrite
   files on disk and can clobber a modified open buffer.
   → Our write tools are **buffer-oriented** and reuse an in-editor diff as the
   confirmation surface.
3. **Caller-context branching leaks into every handler** (`if caller.type == "avante"
   ...`). → We resolve a **normalized `req.nvim` context** once in the dispatcher.
4. **Coarse approval.** `auto_approve = true` green-lights `execute_command` alongside
   `read_file`. → We classify tools into **risk tiers** and gate by tier.
5. **Diagnostics/workspace as prose.** → We return **structured JSON** (with an optional
   text rendering).

## Goals & non-goals

**Goals**

- Expose a curated `neovim` MCP server that controls the live instance.
- Work for **both** delivery paths from a single set of Lua tool definitions:
  in-process CodeCompanion chats *and* external ACP/CLI agents via the combiner.
- Precondition by design: native tools exist for a session **iff** a live Neovim
  instance has registered a channel for it. No instance → tools degrade cleanly.
- Reuse existing machinery: per-chat token routing, `state.native_servers`,
  `cc/approval.lua`, the `:MCPStatus` UI, per-chat session filtering.

**Goals (cont.)**

- A public **registration-only** `add_server`/`add_tool`/`add_resource`/
  `add_resource_template`/`add_prompt` API (re-exported on the top-level module). The
  built-in `neovim` server is curated, but plugins may register additional native
  servers/tools. **Constraint:** registration is *setup-time* — call before any editor
  connects to the combiner — and must be **identical across instances**, because the combiner
  freezes the tool catalog once per process (see *Frozen global manifest*). Tools added
  late, or diverging between instances, won't appear in the combiner's advertised manifest
  (they still work in-process); divergent toolsets would require per-connection manifests.

**Non-goals**

- Headless/spawned Neovim purely to serve tools — control targets the *user's* editor.

## Transport: msgpack-RPC socket + single dispatch entrypoint

Chosen over the alternatives (reverse long-poll queue, dedicated `vim.uv` listener, raw
remote). Rationale:

- Neovim's msgpack-RPC is the **native, mature** control channel; the combiner is already
  Python, so [pynvim](https://github.com/neovim/pynvim) gives us a connection for free.
- The known risk of the standard remote — that it exposes the *entire* `vim.api` to
  anything holding the socket — is mitigated by the **single dispatch contract** below:
  the combiner **never** calls arbitrary API. It only ever invokes one curated Lua function.

### Dedicated socket with a unique per-instance name

We do **not** use `v:servername` — it is not guaranteed unique and can collide across
instances. Instead, each Neovim instance generates a stable, unique `instance_id` once
at setup (e.g. a UUID, or `<hostname>-<pid>-<rand>`) and creates a **private** listening
pipe named by it with `vim.fn.serverstart()` in a user-only directory:

```
$XDG_RUNTIME_DIR/mcp-companion/<instance_id>.sock   # falls back to a private tmp dir
```

The `instance_id` and socket path are disclosed only to the combiner via registration
(below). This narrows the exposure surface, guarantees no name collisions, and lets the
combiner keep exactly one connection per instance shared across all that instance's chats.

### The single dispatch contract

The combiner's *only* RPC into Neovim targets one Lua function — `dispatch` — over the
async msgpack session (never raw `vim.api`). Calls are serialized through the
per-instance queue (see *Async & combiner liveness*); the worker awaits the response future
rather than blocking:

```python
# Python (combiner side) — driven by the per-instance queue worker, on the event loop:
call_id = await session.request(           # async msgpack-RPC, msgid-correlated future
    "nvim_exec_lua",
    "return require('mcp_companion.native').dispatch(...)",
    [tool_name, arguments, call_ctx],
)
# fast tools resolve here; jobs return {pending, call_id} and resolve via rpcnotify
```

```lua
-- Lua (native/init.lua) — the curation boundary
--- @param name string   registered tool name
--- @param args table    validated arguments
--- @param ctx  table    { token, caller = "acp"|"cli", session_id, call_id }
--- @return table        MCP-shaped result, or { status = "pending", call_id }, or { isError = true }
function M.dispatch(name, args, ctx) ... end
```

`dispatch` validates `name` against the registered tool set, resolves a normalized
`req.nvim` context, executes the (already-approved) handler, and returns an MCP-shaped
result inline — or `{ pending, call_id }` for async jobs that complete later via
`rpcnotify`. It performs **no approval** (that is the host's job). Even though the socket
*could* run arbitrary API, the combiner code path cannot — it knows only this one function.

## Routing & registration

Two-level registry, keyed by a stable per-**instance** id and the existing per-**chat**
token (`_token_sessions`, `_pending_token_filters` in `combiner/server.py`):

1. **Instance registration (once per Neovim instance).** On setup the plugin creates its
   private socket and registers the instance:

   ```
   POST   /neovim/instances   { "instance_id": "<uid>", "socket": "<path>", "pid": 12345 }
   DELETE /neovim/instances   { "instance_id": "<uid>" }     # on VimLeave
   ```

   The combiner stores `instance_id -> socket` and keeps **one** pynvim connection per
   instance, shared across all of that instance's chats.

2. **Token binding (per chat/session).** The plugin already mints a token per chat
   (ACPSessionPre / chat open). It binds the token to its instance:

   ```
   POST   /neovim/bind   { "token": "<uuid>", "instance_id": "<uid>" }
   DELETE /neovim/bind   { "token": "<uuid>" }     # on chat close
   ```

   The combiner stores `token -> instance_id`. (TokenRewriteMiddleware already records
   `token -> session_id`; we add the reverse `session_id -> token` so a tool call can
   recover its token.)

3. **Resolution on call.** A `neovim_*` tool resolves
   `session_id -> token -> instance_id -> socket`, gets/creates the cached pynvim
   connection for that instance, and calls `dispatch`. If the token is unbound or the
   instance is gone → `ToolError("No Neovim instance is attached to this session.")`.

This means the `neovim` server automatically flows through **per-chat session
filtering** — it is just another server a chat can toggle, with no new UI.

## Dual delivery from one definition

Tools are authored once in Lua. Two carriers:

- **In-process (CodeCompanion chat):** `cc/tools.lua` calls `M.dispatch(name, args, ctx)`
  directly. No socket, no combiner round-trip.
- **External (ACP/CLI agent → combiner):** the combiner's synthetic `neovim` server forwards
  to `M.dispatch` over msgpack-RPC as above.

## Async & combiner liveness

The combiner is a **single shared process** serving N instances and multiple agents. The
governing constraint is that a tool call into one slow / human-waiting Neovim must never
degrade the combiner's ability to serve everyone else (other sessions, `/health`, upstream
keepalives, capability polls).

**Rule 0 — never block the event loop, and never busy-wait.** All Neovim RPC happens off
the loop thread; the combiner only ever `await`s a future — no polling, no `vim.wait` on the
combiner side, no parked threads.

### Approval is the caller's job, not the editor's

Tool approval is handled by the **agent / hosting harness in the normal way** — exactly
like every other tool the agent can call:

- **In-process CodeCompanion:** CC's own tool-approval flow (`cc/approval.lua`) gates the
  call before it is ever dispatched.
- **External ACP / CLI agents:** the agent (OpenCode, Claude Code, …) confirms with its
  own user before issuing `tools/call`. The combiner does **not** re-approve.

The combiner therefore performs **no editor-side confirmation** — there is no
`vim.fn.confirm()`, no `vim.ui.select`, nothing that waits on a human inside the Neovim
dispatch. By the time a call reaches Neovim it is already approved. This removes
unbounded human-wait from the liveness story at the source: every dispatch is **bounded
work** (a read, an edit, or a job with a timeout). What the combiner governs is *exposure*,
not per-call approval — see *Exposure & safety*.

### Per-instance FIFO queue

Calls to a given Neovim session execute **in order** — the combiner holds one **FIFO queue
per instance**, drained by a single async worker coroutine. The worker processes exactly
one call at a time:

1. Dequeue the next approved call; send it over the async msgpack session.
2. `await` its completion future with a **per-call timeout** (no busy-wait):
   - **fast tools** (read / navigate / edit) resolve inline from the RPC response;
   - **async tools** (`run_command`) return `{ pending, call_id }` immediately and resolve
     later when Neovim `rpcnotify`s `{ call_id, result }` back over the same channel
     (standard bidirectional msgpack-RPC; the combiner holds a notification handler).
3. On **complete** → resolve the agent's `tools/call`, advance the queue.
   On **timeout** → fail that call, send a best-effort `abort call_id` to Neovim (so it
   discards the edit / kills the job before the next runs, preserving order), advance.

One in-flight call per instance falls out of the queue, so editor mutations are strictly
ordered and Neovim's single main loop is never contended. The combiner event loop stays
free throughout — other instances' queues and all other sessions run concurrently.

Async msgpack session (msgid-correlated futures) is preferred over `to_thread` + sync
pynvim: a timed-out call is just an abandoned future (the late reply is matched by msgid
and dropped — no connection-poisoning), and there are no parked threads. Per-instance
serialization is the queue's job, not the transport's.

### Cross-cutting liveness rules

- **Every call has a deadline.** Fast tools short; jobs longer but bounded, and cancelled
  on session close. A future that never resolves is the leak.
- **Cancellation propagates.** Chat ends mid-call → drop the queued/in-flight call *and*
  notify Neovim (`abort call_id`) so it kills the job. No orphaned work.
- **Stale-instance detection is lazy.** `VimLeave` sends `DELETE /neovim/instances`, but
  `kill -9` won't. A connection error on the next call evicts the instance and fails its
  pending futures. No eager heartbeat waking idle editors.
- **Listing is liveness-independent.** The catalog is static, so `tools/list` never
  touches an instance; only `tools/call` needs a live one.
- **Combiner restart:** in-flight futures vanish (agents get errors); a **Neovim-side
  timeout** on pending `call_id`s self-cleans orphaned jobs.
- **Per-instance ordering** is the queue's guarantee, reinforced by Neovim's single main
  loop (each handler runs to completion atomically). Serialization lives in the queue, not
  the transport — so it never becomes global head-of-line blocking across instances.

### Re-entrancy & ordering (the loop-back topology)

The originator of a combiner call may *indirectly be Neovim itself*: a CodeCompanion chat in
instance X prompts an ACP agent, the agent calls a `neovim_*` tool on the combiner, and the
combiner routes it **back into instance X**. Originator and target are the same instance.
This has two implications the locking design must respect:

1. **The combiner queue is not a total order.** In-process CC calls reach `dispatch`
   *directly* (they never traverse the combiner queue); external/looped-back calls arrive
   *through* it. So the per-instance FIFO only orders the external calls among themselves.
   The single thing that serializes **all** calls to an instance is **Neovim's main loop** —
   it runs one `dispatch` to completion before the next. Execution atomicity per call is
   therefore free; the queue exists to bound in-flight *combiner* RPCs and order them, not to
   be the sole serializer.

2. **No lock may span an agent round-trip.** A per-instance mutex held across the
   originator → agent → callback span would be re-acquired by the looped-back call and
   self-deadlock. Any lock must scope to a **single dispatch's execution only** — which,
   for synchronous tools, is exactly what the main loop already provides for free.

Deadlock-avoidance rules (enforced from the async phase onward):

- **Native handlers never block the main loop** — no `vim.wait` / `vim.fn.system` that
  could transitively depend on the agent round-trip. Async work uses `jobstart` +
  `rpcnotify` (the deferred path), so a looped-back dispatch can always make progress.
- **The in-process originator must not synchronously block the main loop** while awaiting
  an agent turn that may loop back (CC's ACP path is already async — keep it so).
- **Locks scope to one dispatch**, never across the network round-trip.
- Calls carry their **origin instance id** in `ctx` for loop-back detection / observability
  (and so the combiner can avoid pathological self-queuing).

**Phase 1 is safe by construction:** all native tools are synchronous, so the main loop
fully serializes every `dispatch` (in-process or looped-back) with no lock at all. These
rules become load-bearing only when the async `jobstart` path and the combiner queue land.

## Tool surface (curated, risk-tiered)

The catalog covers **everything mcphub's `@neovim` could do** — read/search/edit/move/
delete/exec/diagnostics — but as typed, tiered tools. The cursor/buffer/open primitives
exist specifically so the model almost never needs raw `exec_lua` (mcphub's god-tool).

### Coverage vs. mcphub

| mcphub capability | Our equivalent | Change |
|---|---|---|
| `read_file` | `read_file` | same |
| `read_multiple_files` | `read_files` | same (parallel) |
| `find_files` | `find_files` | same (glob) |
| `list_directory` | `list_directory` | same |
| `move_item` | `move_item` | same |
| `delete_items` | `delete_items` | destructive, never auto-approved |
| `write_file` | `write_file` | on-disk write for files **not** open in a buffer |
| `edit_file` (SEARCH/REPLACE) | `edit_buffer` + `edit_file` | buffer-oriented for open files (no disk desync); diff is the approval surface |
| `execute_command` | `run_command` | exec tier, deferred (async job) |
| `execute_lua` (god-tool) | `exec_lua` | retained only as escape hatch — the typed tools below replace its common uses |
| resource `neovim://buffer` | `read_buffer` + resource | structured |
| resource `neovim://workspace` | resource | structured JSON |
| resource `neovim://diagnostics/*` | `get_diagnostics` + resource | structured, not prose |
| *(was forced through `execute_lua`)* | `list_buffers`, `get_cursor`, `set_cursor`, `open_file`, `goto_diagnostic`, `set_buffer_lines`, `save_buffer` | new typed primitives |

### Tools by tier

Tier drives **exposure** (see *Exposure & safety*); the sync/deferred split ties to the
liveness model above. Host approval is orthogonal and applies to every tier.

**read** — fast-sync, auto-approvable

| Tool | Params | Returns |
|---|---|---|
| `read_file` | `path, start_line?, end_line?` | text |
| `read_files` | `paths[]` | per-file text |
| `read_buffer` | `buffer?` | content + line numbers, cursor, marks (defaults to active) |
| `list_buffers` | — | `[{id, name, active, modified, filetype, lines}]` |
| `list_directory` | `path?` | entries with type/size/symlink |
| `find_files` | `pattern, path?, recursive?` | paths |
| `get_diagnostics` | `scope: buffer\|workspace, buffer?` | `[{severity, range, code, source, message}]` |
| `get_cursor` | — | `{buffer, line, col}` |
| `get_selection` | — | `{buffer, range, text}` |

**navigate** — fast-sync, low risk

| Tool | Params |
|---|---|
| `open_file` | `path, line?` — open/focus buffer |
| `set_cursor` | `buffer?, line, col?` |
| `goto_diagnostic` | `direction: next\|prev\|first, severity?` |

**write** — fast-sync, buffer-oriented (host approval applies, like any write tool)

| Tool | Params | Notes |
|---|---|---|
| `edit_buffer` | `buffer\|path, diff` | SEARCH/REPLACE blocks vs. the **live buffer**; returns before/after diff + per-block match feedback |
| `set_buffer_lines` | `buffer, start, end, lines[]` | typed range replace |
| `write_file` | `path, content` | on-disk, for files **not** open in a buffer |
| `save_buffer` | `buffer` | persist a modified buffer to disk |
| `move_item` | `path, new_path` | |
| `delete_items` | `paths[]` | **destructive** — host approval strongly advised |

**exec** — deferred (async job), off by default, opt-in (not exposed unless enabled)

| Tool | Params | Notes |
|---|---|---|
| `run_command` | `command, cwd?` | async via `jobstart`; result via deferred-notify |
| `exec_lua` | `code` | escape hatch only |

### Resources (read-only, structured JSON + text rendering)

- `neovim://buffers` — open buffers; active/modified flags
- `neovim://buffer/{id}` — content with line numbers, cursor, marks, selection
- `neovim://diagnostics/{scope}` — structured severity/range/code/source (scope: buffer|workspace)
- `neovim://workspace` — cwd, git status, directory listing
- `neovim://selection` — current visual selection

### Normalized context

The dispatcher resolves `req.nvim` once: `{ current_buf, cursor, selection, caller }`.
Handlers never reimplement mcphub's per-plugin caller branching.

## Exposure & safety

Per-call approval is the agent / hosting harness's responsibility (see *Async & combiner
liveness*), so the `neovim` server does **not** implement its own confirmation. Instead it
controls **what an agent can reach**, which composes with whatever approval the host
already enforces:

- **Risk tiers gate exposure.** The catalog is tagged read / navigate / write / exec. The
  **exec tier (`run_command`, `exec_lua`) is off by default** — those tools are not even
  listed unless explicitly enabled, so an over-eager agent can't reach the escape hatch.
- **Per-chat session filtering** (existing) lets a user disable the whole `neovim` server
  for a chat — it is just another toggleable server, no new mechanism.
- **In-process CC** additionally keeps `cc/approval.lua` in the loop, since CC tools flow
  through that gate regardless.

This is consistent with how the combiner already exposes its other ~180 tools to agents:
safety comes from **exposure control + the host's normal approval**, not from a bespoke
editor-side dialog. It still avoids mcphub's footgun (one `auto_approve` boolean covering
`read_file` and `execute_command`) because the dangerous tier is opt-in at the exposure
layer rather than relying on a single approval flag.

## Combiner changes (Python)

Implemented in `mcp_combiner/nvim_channel.py` (`NvimChannelManager`):

- A `NvimChannelManager`: one `instance_id -> pynvim` connection per instance, lazy
  connect, evict on deregister or connection/timeout error. Every interaction (tool
  dispatch *and* manifest fetch) runs as a job through a **per-instance FIFO queue +
  single worker**, so calls to one instance are strictly ordered with at most one
  in-flight RPC. Phase 2 uses `asyncio.to_thread` around synchronous pynvim (permitted for
  the fast-sync phase); the async msgpack session + `rpcnotify` deferred path is later
  hardening for long-running jobs.
- **Frozen global manifest.** The tool/resource catalog is fetched over the channel
  (`require('mcp_companion.native').manifest()`) from **whichever instance connects
  first**, then **locked for the life of the combiner process** (`ensure_manifest()`,
  single-flight under a lock). No `neovim` tools are advertised until the first editor has
  connected; thereafter the catalog is stable even as instances come and go. Call routing
  remains per-session — only the catalog is global.

REST + routing (implemented in `server.py`/`__main__.py`):

- `POST/DELETE /neovim/instances` (`instance_id -> socket`) and `POST/DELETE /neovim/bind`
  (`token -> instance_id`) custom routes.
- `neovim_*` tools surface through the existing **`ToolProcessingMiddleware`** (not a
  separate FastMCP mount — the catalog is per-session-routable, which a static mount can't
  model): `on_list_tools` appends the frozen manifest's tools when the session is bound to
  a live instance and `neovim` isn't session-disabled; `on_call_tool` intercepts
  `neovim_*`, resolves `session_id -> token -> instance_id`, and routes to
  `NvimChannelManager.call(...)`, returning a clean error if no instance is bound.
- New dependency: `pynvim` (in `combiner/pyproject.toml`).

**Which editor a call targets.** The catalog is global (once-off frozen manifest), but a
call must reach a *specific* editor. Every `neovim_*` tool carries an optional
`nvim_instance` arg (injected by the combiner, stripped before dispatch), plus a
`neovim_list_instances` discovery tool that returns connected editors with metadata
(`instance_id`, `cwd`, `name`, `servername`). Target resolution at **call time** (not from
guessing by instance count):

1. explicit `nvim_instance` arg → that editor (validated);
2. else the **connection's own association** — a token-bound ACP chat defaults to the
   editor that spawned it (`session_id -> token -> instance_id`);
3. else (a directly-configured client with no association) → an instructive error telling
   the caller to pass `nvim_instance` and pointing at `neovim_list_instances`.

Tool *listing* is independent of association: once any editor has connected, `neovim_*`
tools are advertised to every combiner client (ACP-injected or directly configured).

> **Gotcha — two session ids.** The MCP `mcp-session-id` *header* (recorded into
> `_token_sessions` by `TokenRewriteMiddleware`) is **not** the same value as
> `context.fastmcp_context.session_id` seen by the tool middleware. So the `session_id ->
> token` reverse map must be built in `ToolProcessingMiddleware.on_request` keyed by the
> *context* session id, reading the token from the `X-MCP-Combiner-Session` header.
> `TokenRewriteMiddleware` now injects that header when the token arrives via the URL path
> (`/mcp/<token>`), so URL-token and header-token clients correlate identically.

## Lua changes

Back-channel registration is implemented in `lua/mcp_companion/native/channel.lua`:
a unique per-instance id + private `serverstart()` socket, `POST /neovim/instances`
registration with **bounded retry/backoff** (registration may race the combiner becoming
healthy), per-chat `bind(token)`/`unbind(token)`, and `deregister()` on `VimLeave`.
Wired from `init.lua` (`start()` on `combiner_ready`, `deregister()` on `VimLeavePre`) and
`cc/init.lua` (`bind` in `_apply_token_filter`, `unbind` in `_cleanup_session_filter`).

- Rewrite `native/init.lua` (currently a stub): an internal tool/resource registry,
  `M.dispatch(name, args, ctx)`, `M.manifest()`, and the **registration-only** public
  `add_server/add_tool/add_resource/add_resource_template/add_prompt` API (re-exported on
  the top-level `mcp_companion` module). Registration is setup-time + homogeneous across
  instances; see *Goals*.
- Built-in tool/resource handlers under `lua/mcp_companion/native/neovim/`.
- Private socket lifecycle (`serverstart`/`serverstop`) + registration calls on chat
  open/close and `VimLeave`.
- `cc/tools.lua`: surface native-server tools to in-process chats by calling `dispatch`
  directly (alongside combiner tools).
- `state.lua` already has a `native_servers` slot; populate it so `:MCPStatus` shows the
  `neovim` server like any other.

## Security considerations

- The dedicated socket lives in a user-only directory; its path is disclosed only to the
  combiner via registration.
- The **single dispatch contract** is the real guardrail: the combiner cannot call raw
  `vim.api`, only the curated dispatcher, which enforces the registered tool set + tiered
  approval. Residual risk — any *other* local process that learns the socket path could
  use full RPC — is inherent to the standard remote and is documented, not eliminated.
- `exec`-tier tools (`run_command`, `exec_lua`) are off by default and never
  auto-approved by default.

## Phased plan

1. **Lua dispatcher + in-process delivery.** Implement `native/init.lua` + the `neovim`
   server + `read`/`navigate`/buffer `write` tools. Wire into CodeCompanion chats. Zero
   combiner changes — fully testable in-editor.
2. **Back-channel.** Add pynvim, the `NvimChannelManager`, the per-instance FIFO queue +
   worker, the `/neovim/instances` + `/neovim/bind` routes, the synthetic `neovim` server,
   and `session_id -> token -> instance_id -> socket` routing. Fast-sync path only at this
   step. External ACP/CLI agents can now drive the editor.
3. **Deferred-notify (async jobs).** Add the `rpcnotify` completion path and the
   combiner-side `call_id` future map so async work resolves out-of-band without holding the
   queue on a thread. Wire `:MCPStatus` integration + per-chat filtering verification.
4. **exec tier** (`run_command` async via `jobstart` over the deferred path, `exec_lua`)
   behind explicit opt-in (exposure gated, off by default).

## Open questions

- **Streaming exec.** The deferred-notify path delivers a single terminal result per
  `call_id`. Long-running/streaming commands that should report incremental output need a
  progress-notification channel (multiple notifications per `call_id`). Deferred.
- **Combiner-side concurrency cap.** Liveness knob still open: bound in-flight calls
  *per instance* (Neovim's loop is serial anyway) vs. *globally* on the combiner. Lean
  per-instance, but unset until phase 2 measurements.
- **Multiple instances racing one token.** Tokens are per-chat and minted in one instance,
  so a token maps to exactly one `instance_id`; documented assumption to validate.
