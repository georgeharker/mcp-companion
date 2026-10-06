# Review: `dev` vs `main` — pi native MCP migration

_Date: 2026-10-05 · Range reviewed: `origin/main...dev` (40 commits)_

I reviewed `origin/main...dev`: 40 commits, centred on `plugins/pi/src/native/*` and `src/index.ts`. Local `main` is 31 commits ahead of `origin/main`, so a plain `main...dev` diff only shows the last 9 small commits and misses most of the migration.

The overall approach is sound: the extension owns the `McpClient` so it can advertise elicitation, tools are declared natively through `registerTool` with exposure and annotations, and the `mcp` router stays for permission gates. `tsc --noEmit` passes and all 51 tests pass, but none of those tests cover `src/native/`. That's where most of the problems below are.

## Bugs (should fix)

1. **A failed tool call can be re-run, including destructive ones.** `combiner-connection.ts:400-421`: `callTool` turns a server-reported tool error into an exception that includes the tool's own error text. That message then goes through the stale-session check, which is a very broad pattern (`/404|stale|session|not found|closed|fetch failed|illegal/i`).
   - Any tool error containing words like "not found" or "session" resets the connection and runs the tool a second time.
   - A `fetch failed` after the request was already delivered also triggers a re-run, even though the tool may have executed.
   - **Fix:** retry only on `McpSessionExpiredError`, which is safe because the server rejected the session before running anything. pi-mcp also exports `McpConnectionClosedError`, `McpHttpError` and `McpTimeoutError`, so errors can be classified by type and the pattern dropped. Do the tool-error check after the retry decision, not inside it. With that, `callTool` can use `withStaleRetry` like the other methods instead of keeping its own copy of the logic.

   **Action taken (fixed, `combiner-connection.ts`):** `callTool` now delegates to the shared `withStaleRetry` with `retryStaleOnly: true` — only a typed `McpSessionExpiredError` (raised by the server BEFORE executing anything) triggers the automatic re-run. The `isError` check moved OUTSIDE the retry layer, so a tool's own error text can never reach the staleness classification. For read-only methods the retry keeps a broader safety net, re-based on pi-mcp's typed taxonomy (`McpConnectionClosedError`, `McpTimeoutError`) with the message regex reduced to a belt for raw, untyped transport failures (e.g. `fetch failed`). Regression test 2 pins the "not found isError must not retry" case.

2. **Connection races (`ensureConnected`, `reset`, `withStaleRetry`).**
   - `reset()` clears `this.connecting`, but a connect already in flight still sets `this.client = client` when it finishes (`:285`). So `setToken` or `rebind` during an eager connect (e.g. `/new`) can bring back a client that has the old token.
   - The `finally { this.connecting = undefined }` at `:310` can also wipe out a newer connect that started in the meantime.
   - When several in-flight calls all see the session go stale, each one calls `reset()`. Each reset closes the client another call just reconnected, which fails with "closed", matches the pattern, and resets again.
   - **Fix:** add a generation counter, and only reset if `this.client` is still the client that failed. That's the same "detach, don't close" approach the code's own comment quotes from pi's builtin.

   **Action taken (fixed):** a `generation` counter bumps on every `reset()`; the connect-in-flight checks it before publishing `this.client` (a laggard connect from the old token is closed and rejected with a "superseded" error instead of resurrecting the old routing); the `finally` clears `this.connecting` only when it is still OUR promise (never wipes a newer connect); and `withStaleRetry` resets only when `this.client` is still the client that failed — a concurrent reset's fresh client is adopted, not closed. Regression test 3 pins the reset-during-connect case.

3. **The connect timeout leaks a client.** `:280-283`: when the timeout wins the race, `client.connect` keeps going. If it later succeeds, there's an untracked `McpClient`, its SSE stream and its handlers, still competing for the token's elicitation routing (the same problem the zombie-teardown work fixed). The timer is also never cleared or `unref`'d. On timeout, call `client.close()` and `clearTimeout`.

   **Action taken (fixed):** the timer is cleared in a `finally`; on ANY lost race (timeout or connect failure) the client is closed best-effort (non-blocking), so an untracked SSE stream can never compete for elicitation routing. The late `connect()` settlement is given a no-op catch so a slow-success rejection can never become an unhandled rejection (the pi-crash class).

4. **Abort doesn't work.** `tool-surface.ts:93-96` has `void signal` under a comment saying the signal "rides into the transport". It doesn't: `callTool(name, args, { signal })` is supported (`McpRequestOptions`) but the signal is never passed. Cancelling in pi leaves the call running for up to 90s, including widget holds.

   **Action taken (fixed):** `NativeCombinerConnection.callTool` gained `options?: McpRequestOptions` and passes it into pi-mcp's `callTool`; the `CombinerConnection` interface (`client/types.ts`) grew the optional `options.signal` member, and `tool-surface.ts` now forwards pi's cancel signal into the transport — cancellation aborts upstream work and widget holds.

5. **Per-session config / `session_start` re-resolution (`index.ts` native activation).**

  **Action taken (fixed, inserted here in review order — the item sits between 4 and 6 in the numbering):** `activateNativeMode` takes `directSpec`/`serverFilter` as GETTERS; `activateNativeTools`/`activateNativeResources` resolve them per pass, so worktree/project switches apply the current session's allow/deny and exposure spec, matching the router's `getServerFilter` pattern.

6. **`read_*` tools keep an old session token in their URL.** `resources.ts:56` computes `uiUrlFor(...)` when the tool is registered. After `/new` or resume the token changes, but the `registered` set stops re-registration, so the URL still carries the old token. Compute the URL inside `execute`.

   **Action taken (fixed):** the UI URL is computed per call inside `execute` — the registration-time capture is gone.

7. **The panel's promise rejection is unhandled.** `index.ts` `/mcp-combiner panel`: `openCombinerPanel` is async, and the last commit removed the `void` without adding a `.catch`. It was unhandled before as well. On Node 15+ an unhandled rejection kills the process, which is exactly what the stale-ctx sweep set out to prevent. Add `.catch(...)` that writes to stderr.

   **Action taken (fixed):** `void openCombinerPanel(...).catch(...)` with a stderr write that never re-enters the stale getter. Noted honestly: dropping the `void` was a regression introduced by the stale-ctx sweep's edit (the catch handler there touched `ctx.ui` again) — this review caught it; the stderr-fallback rule now covers it.

8. **Debug logging ships in production.** `ncDbg` writes to `/tmp/pi-combiner-dbg.log` on every request and error, unconditionally, with no rotation. It logs tool names, session IDs and the first 140 characters of error messages to a file anyone on the machine can read. Remove it, or put it behind an env flag.

   **Action taken (fixed):** gated behind `PI_MCP_COMBINER_NC_DBG=1` — ships disabled; the living-with instrumentation stays reachable by setting the env var.

9. **The lockfile is out of sync with `package.json`.** `package.json` now lists `@earendil-works/pi-mcp ^1.0.2` as a dependency. `package-lock.json` wasn't touched and still pins `1.0.0` with `"dev": true`, and `npm ls` shows 1.0.0 installed. CI runs `npm install`, so publishing works, but the local tree fails the range and `npm ci` would fail. Regenerate the lockfile.

   **Action taken (fixed):** lockfile regenerated — `@earendil-works/pi-mcp@1.0.4` now installed as a real (non-dev) dependency; `npm ci` valid again. In the same pass the duplicate declaration was resolved: removed from `devDependencies`, kept in `dependencies` with the peer marked `optional` in `peerDependenciesMeta`.

## Design / smaller issues

- **Tools are never unregistered.** When a server is disabled or filtered out, its native tools and `read_*` tools stay registered and just error when called. If pi has no unregister call, at least mark removed tools `exposure: "hidden"` on re-registration.

  **Action taken (fixed):** tools absent from the current listing are re-registered `hidden` at the same name (clear description, execute throws a guidance error) — they stop being declared/callable instead of lingering broken.
- **Re-registration cost.** `run()` re-registers every tool on each `list_changed`, reconnect and `session_start`. The code comment says pi "replaces by name with a warning", which could mean hundreds of warnings each time. Compare a schema signature and only re-register what changed. `schema-signature.test.ts` exists, so the building block may already be there.

  **Action taken (fixed):** `NativeToolSurfaceState` (names + per-tool `renderSchemaSignature`) is owned by `activateNativeMode` and diffed on every pass — unchanged declarations skip `pi.registerTool` entirely, changed/removed ones re-register. This also dissolves the `cachedNames`/`registered` alias issue.
- **Use pi-mcp's `toLlmContent`.** `tool-surface.ts` `toToolResult` turns images into `[image block]` and drops `structuredContent`. pi-mcp exports `toLlmContent`, which converts results to text and base64 images in the form pi-ai accepts. Use it, and widen the local `ToolResult` content type in `pi.ts` to allow images.

  **Action taken (fixed):** `toToolResult` now maps through pi-mcp's `toLlmContent` (text, real base64 image blocks, resource flattening, structured-only stringification); `pi.ts`'s `ToolResult` content widened to `(TextBlock | ImageBlock)[]`.
- **Undocumented codemode requirement.** With no `directTools` allowlist, every combiner tool gets `exposure: "codemode"`. According to the note in `native/index.ts:89`, codemode isn't switched on for extension tools unless the user adds `"defaultTools": ["+codemode"]`. That requirement appears only in that code comment, not in the README. Either document it prominently or fall back to `"deferred"` exposure (for `tool_search`) when codemode is off.

  **Action taken (documented):** README config section now carries the quirk (defaultTools applies at fresh-session creation; without codemode, non-direct tools stay reachable via the `mcp` router). The `deferred` fallback is deliberately not built — the `mcp` router covers discovery, and another exposure shape would need its own design pass.
- **Dependency declared twice.** pi-mcp is now both a `peerDependency` and a `dependency`. Keep one, or mark the peer optional in `peerDependenciesMeta`. Several comments still say "peer dep" (`combiner-connection.ts:2`, `native/index.ts:10`, `index.ts:49`).

  **Action taken (fixed):** single `dependencies` entry kept; the peer declaration marked optional via `peerDependenciesMeta`; the obsolete "peer dep" comment lines updated where the review flagged them (`native/index.ts` header rewritten; `combiner-connection.ts` contract header updated; `index.ts:49` import note retained but the peer-dep phrasing corrected).
- **Stale and unused values:**
  - `CLIENT_INFO.version` is hardcoded to `"0.14.3"`; read it from `package.json` or have `bump-version.sh` stamp it.
  - `listTools` has an unused `const client = await this.ensureConnected()` (`:342`).
  - The `namespaceName` and `reserved` options are declared but never used; the namespace is hardcoded twice.
  - `cachedNames` and `registered` are the same array, and the code pushes to `cachedNames` only.

  **Action taken (fixed):** `CLIENT_INFO` is stamped by `scripts/bump-version.sh` from the package version (a python stamp mirroring `write_ver`'s semantics, so the constant cannot drift from the package); the unused `ensureConnected` in `listTools` removed; the dead `namespaceName`/`reserved` options removed from `NativeToolSurfaceOptions` (namespace constant shared, `RESERVED_TOOL_NAMES` still enforced); the `cachedNames` alias folded into the per-pass state (`NativeToolSurfaceState.names`).
- **Comments that no longer match the code:** `combiner-connection.ts:14-19` still says this class mirrors `client/connection.ts`, which has been deleted, and that "there is no mcp() proxy", while `index.ts` does register one. `native/index.ts:5-8` describes a mode gate that no longer exists.

  **Action taken (fixed):** both headers rewritten to describe the current shape (the class implements `client/types.ts`'s interface; the mcp router rides the same connection; native/index describes factory + deferred first run + getters).
- **Stale-ctx sweep leftovers (last commit):**
  - In `session_start`, `ctxUi` is used in the closure before its `const` line. It works because the closure runs later, but it reads like a bug; move the line up.
  - `uiCtx = ctx.ui` and `bindUi({ ui: ctx.ui })` re-read the getter instead of using `ctxUi`.
  - The `status` and `enable` branches re-declare a `ctxUi` that shadows the handler-level one; drop the inner copies.
  - The `.then`/`.catch` "try notify, else stderr" block appears four times; pull it into a helper.

  **Action taken (fixed):** the `session_start` capture moved to the top of the handler and every getter re-read (`uiCtx`, `bindUi`, the notify arrow) now uses the captured object; the shadowed inner declarations removed; the four copies collapsed into one `uiNotifySafe` helper (notify via captured object → stderr fallback), which the panel catch also routes through semantically.
- **Duplicated log line:** the "non-combiner servers not connected" note is logged both at load time and on every `session_start`.

  **Action taken (fixed):** the load-time copy removed; the `session_start` note remains (per-session state, the useful one).
- **Interactive resources declared `direct`:** every `read_*` tool rides in every request. `codemode` or `deferred` may be the better default, consistent with how the regular tools are handled.

  **Action taken (fixed):** `read_*` tools now use the same exposure vocabulary as regular tools — `direct` only when the name matches the `directTools` allowlist, `codemode` otherwise.

## Tests

The native layer has no unit tests. The quickest win would be a fake transport (pi-mcp ships a `testing/` dir) covering three cases:

- a stale 404, which should retry once;
- a tool `isError` result containing "not found", which must **not** retry;
- `reset` during a connect, where the old client must not come back.

Items 1–3 would all have been caught by those.

**Action taken (added, `test/native-connection.test.ts`):** all three cases, over pi-mcp's `createInMemoryTransportPair` with a scripted fake combiner + a `transportFactory` seam added to `NativeCombinerConnection` for injection. Two harness lessons surfaced while writing them (worth keeping): `initialize` responses must carry a `result` field or the client's pending request never settles (silent hang), and `InMemoryTransport.close()` closes the server peer too — the fake combiner mints a fresh pair per connect, mirroring the real server's session lifecycle. 54/54 tests pass; tsc and biome clean.

**Priority:** items 1 and 2 first.
