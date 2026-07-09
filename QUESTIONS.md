# Open questions / suspected bugs found during test-harness work

Per our working agreement: behavior that looks wrong is documented here and
discussed before any fix — it may encode learnings. Nothing below has been
changed in code; e2e tests pin the current behavior with `xfail(strict=True)`
where it looks broken, so an eventual fix flips them loudly.

## Q1 — Session-ID namespace split: token-route filters silently never apply

**Found by:** `tests/test_session_independence.py` (e2e, 2026-07-08).

**Symptom:** `POST /sessions/token/<token>/filter {"disable": "mockup"}`
returns success and records the filter, but the session's `tools/list` still
contains the server's tools and calls are not blocked. The same filter applied
through the meta-tool `combiner__session_disable_server` works.

**Mechanics — there are two session-id namespaces in play:**

1. **HTTP `mcp-session-id`** (32-hex, e.g. `2b17a6c51c46…`) — assigned by the
   MCP SDK's streamable-HTTP transport, returned as a response header.
   `TokenRewriteMiddleware` (`__main__.py:120-152`) maps `token → this id` in
   `_token_sessions`, and pending token filters are applied into
   `_session_disabled[this id]` (`__main__.py:133-137`).

2. **fastmcp `Context.session_id`** (dashed UUID, e.g. `e5fcd58a-…`) — in
   fastmcp 3.4.2, `Context.session_id` returns a cached
   `session._fastmcp_state_prefix` if present; otherwise it tries the
   *request* header `mcp-session-id`, else **generates `uuid4()` and caches
   it**. On the `initialize` request the client has no session header yet, and
   the combiner's `ToolProcessingMiddleware.on_request` (`server.py:881-896`)
   reads `ctx.session_id` during initialize (for `record_session_token`), so
   the generated dashed UUID gets cached and wins for the whole session
   lifetime. `_apply_session_filter` / `on_call_tool` / the session meta-tools
   /`/sessions` listing / `_notify_session_by_id` all key on THIS id.

**Consequences (verified e2e):**
- Filters via `/sessions/token/<t>/filter` (the Lua plugin's primary path for
  per-chat ACP/CLI/HTTP filters, `cc/init.lua:847`, `session_commands.lua:110`)
  land in namespace 1 and are **never read** → per-chat `allowed_servers`
  filters are likely silently inert in production on fastmcp 3.4.x.
- Pending token filters (set before first connect) are likewise applied into
  namespace 1 → inert.
- `GET /sessions/token/<t>` reports namespace-1 ids that don't appear in
  `GET /sessions` (namespace 2) — the two REST views cannot be joined.
- `_notify_session_by_id` (server.py:193) compares `_fastmcp_state_prefix`
  (namespace 2) — token-route calls pass namespace-1 ids → notifies nobody.
- Meta-tools (`combiner__session_{disable,enable,status}_server`) work — they
  key on `ctx.session_id` end to end. The Lua fallback path therefore works.

**Suspected origin:** fastmcp version drift — same class as the mounts
repr-sniffing bug. If an earlier fastmcp resolved `ctx.session_id` from the
HTTP header (or nothing touched `session_id` during initialize so the header
was seen first), both namespaces coincided and the token route worked.

**Candidate fixes (to discuss, NOT applied):**
- (a) In `TokenRewriteMiddleware`, stop trusting the response header; instead
  resolve token → fastmcp session id at the FastMCP layer (e.g. extend
  `record_session_token`, which already sees both the header token and
  `ctx.session_id`, to also fill `_token_sessions`). The middleware's response
  -header mapping becomes a fallback or is dropped.
- (b) Force namespace unification: have `on_request` set
  `session._fastmcp_state_prefix` to the transport's real `mcp-session-id`
  when available, before anything else touches `ctx.session_id`.
- (c) Key everything by token instead of session id internally.

**Question for George:** which namespace should be canonical? (a) is the
least invasive and keeps the header-correlation learnings intact; (b) makes
ids match what clients see on the wire but changes every existing key at
runtime; (c) is the biggest refactor. Also: do we believe per-chat filters
have been inert in production, or is there a code path I've missed that keeps
them working? (`test_session_filter.py` passes only because its TestClient
harness fabricates consistent ids.)

## Q1 — follow-up after checking the control-channel design (2026-07-08)

George pointed out the Lua side intentionally connects a **tokenless control
session** (the editor's singleton client on `/mcp`, `combiner/init.lua:347-353`)
distinct from every chat's token-bearing session. Verified — and that design is
intact and NOT the bug. It does sharpen where the bug bites:

- Because the control session ≠ the target chat's session, per-chat filter ops
  from the control channel must NAME their target. There are two naming
  mechanisms, and **both miss the target's actual filter key**
  (`Context.session_id` of the chat's session):
  - token REST route (`session_commands._call_session_tool:110`, primary path)
    → resolves token to the wire `mcp-session-id` (Q1 as described above);
  - meta-tool `chat_id` argument (fallback path, `session_commands.lua:145`)
    → **Q1b**: `chat_id` is stored VERBATIM as the filter key
    (`meta_tools.py:410` `sid = chat_id if chat_id else ctx.session_id`), and
    Lua passes `tostring(chat.bufnr)` — a buffer number that matches nothing.
- **Why nobody noticed:** for in-process CC chats the Lua side also mirrors
  the filter locally (`_session_state[bufnr]` + `_sync_cc_tool_group`), hiding
  tools in the CC registry — so the in-editor experience looks correct even
  though the combiner-side filter is inert. The server-side filter is the ONLY
  filter for **external ACP/CLI agents** on `/mcp/<token>` — those see
  unfiltered tool lists despite `allowed_servers` being posted for their token.
- Meta-tools called *by the chat's own session without chat_id* work (pinned
  passing); the control-channel patterns are pinned as
  `xfail(strict=True)` in `TestControlChannelFilters`.

Implication for the fix discussion: resolving Q1 needs a canonical way to go
**token → chat session filter key**. Fix (a) (build `_token_sessions` as the
inverse of `_session_tokens` at the fastmcp layer) gives that for the REST
route; the meta-tool `chat_id` path additionally needs `chat_id` to be
interpreted as a token (or bufnr passing to change) rather than used verbatim.

## Q2 — (minor) `/sessions` listing id fallback

`list_sessions` (`server.py:1624`) falls back to `str(id(session))` when
`_fastmcp_state_prefix` is missing — a memory address that matches nothing
else and changes across GC. Probably fine as a debug view, but worth deciding
whether the route should instead surface the transport session id.

## Q4 — CLI session ops: addressing model deferred (design notes for later)

**Status:** parked deliberately (2026-07-08). George: the CLI may not need to
toggle a chat's session at all — revisit with fresh eyes rather than design
it now. Recorded here so the analysis isn't re-derived.

**Decision (2026-07-08):** keep the verbs, document them as transient/WIP —
ctl.py help + docstring and the README Control CLI section now say so
(`session status` works; enable/disable/allow are recorded-not-applied
pending the rework). No removal, no new addressing machinery yet.

**The awkwardness, in three layers:**

1. **The CLI has no "this session".** Every `mcp-combiner` invocation opens a
   short-lived MCP session inside `ctl._call_meta` and drops it on exit — so
   the self-scoped meta-tools (`combiner__session_disable_server` without
   `chat_id`, which filter the *calling* session) are meaningless from the
   CLI: the filtered session dies with the process.
2. **Discovery.** Targeting another chat needs its token, but tokens are
   minted by the Lua plugin per chat and there is NO enumeration —
   `/sessions/token/<t>` requires already knowing `t`; nothing lists the
   token map. `GET /sessions` lists sessions, but…
3. **…listing and targeting are in different namespaces — Q1 again.**
   `/sessions` shows `Context.session_id` values (the namespace filtering
   reads); `--token` resolves through the wire-id map (currently inert, Q1).
   The only end-to-end-working CLI targeting today would be `--session-id`
   against `/sessions/{id}/filter` — which ctl.py does not currently expose.
   The shipped `session enable/disable/allow --token` subcommands faithfully
   drive the token route and therefore record-but-don't-apply until Q1 is
   fixed (noted in ctl.py's docstring).

**Sketch if/when revisited (dependency order):**
- (a) Fix Q1 first — prerequisite for token-addressed ergonomics; makes
  `/sessions` and the token map joinable so a pick list can show
  token + clientInfo + filter state in one table.
- (b) `--session-id` on the session subcommands → `/sessions/{id}/filter`
  (the working namespace); cheap and honest as a stopgap.
- (c) Stable CLI identity for self-scoped ops: the CLI adopts the chat
  correlation mechanism — send `X-MCP-Combiner-Session` from
  `$MCP_COMBINER_SESSION_TOKEN` (or `mcp-combiner session begin` mints one),
  so repeated CLI calls (incl. `call`) ride one logical session. Reuses the
  existing token dance; no new server machinery.
- (d) Interactive pick: no addressing flag → list live sessions (excluding
  the CLI's own), prompt; auto-select when exactly one candidate. Needs (a).

**Decision needed at revisit:** does the CLI need chat-session control at
all, or is global enable/disable + status its whole session story? If the
latter, consider *removing* the token-addressed session subcommands rather
than leaving a surface that silently no-ops (or gate them until Q1 lands).
