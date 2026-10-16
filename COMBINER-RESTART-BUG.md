# Bug report: `restart_server` leaves a stale provider mounted — every proxied call then fails until disable/enable

Diagnosed 2026-07-07 from a live session, restarting the `cribsheet` server through
`combiner__restart_server`. Written for an agent picking this up cold; everything
needed to reproduce and fix is below.

## Symptom

1. `combiner__restart_server("cribsheet")` → `"Server 'cribsheet' restarted
   (process restarted; 0 provider(s) replaced)"`. The backing sharedserver process
   really did restart (new PID).
2. Every subsequent proxied tool call failed with:
   `"Server 'cribsheet' persistent connection is down. It will reconnect automatically."`
   It never reconnected. A second `restart_server` bounced the process again — same
   result, still `0 provider(s) replaced`.
3. `combiner__status` reported cribsheet `state: "ready"` while the calls failed.
4. `combiner__disable_server` reported `"no active providers found to remove — it
   may not have been mounted"` (it was mounted and had served tools all session);
   `enable_server` then fixed routing. The `crib` CLI (which talks straight to the
   daemon on :7732, bypassing the combiner) worked the whole time.

## Root cause

`_drop_providers` in `combiner/mcp_combiner/meta_tools.py` (~line 205) identifies a
server's mounted providers by repr-sniffing:

```python
def _matches(p: object) -> bool:
    r = repr(p)
    if f"namespace='{server_name}'" in r or f'namespace="{server_name}"' in r:
        return True
    return getattr(p, "_namespace", None) == server_name
```

The installed fastmcp no longer produces either shape. Verified against the live
library:

```
>>> parent.mount(child, namespace='cribsheet'); repr(parent.providers[-1])
"_WrappedProvider(FastMCPProvider(), transforms=[Namespace('cribsheet')])"
>>> getattr(parent.providers[-1], '_namespace', '<missing>')
'<missing>'
```

- The repr carries `Namespace('cribsheet')` — capital N, call syntax — never
  `namespace='cribsheet'`.
- There is no `_namespace` attribute. The name lives in the wrapper's
  `transforms[0]._prefix` (a `Namespace` transform; `_prefix`/`_name_prefix` are
  its private attrs — I checked, there is no public accessor carrying the name).

**So `_drop_providers` matches nothing and returns 0 for every server, always.**
This is fastmcp-version drift: the matcher presumably fit an older mount
representation.

## Failure chain (why calls fail *persistently* after a restart)

In `combiner__restart_server` (meta_tools.py ~line 222):

1. `conn_manager.disconnect(server_name)` tears down the connection **object**.
2. `_drop_providers(server_name)` removes **nothing** (above) — the old provider
   stays mounted, its tool factories closing over the now-dead connection.
3. The backing process restarts fine (`ss_manager.restart` → `admin stop --force`
   + respawn).
4. `_mount_server` mounts a **second** provider under the same namespace.
5. Tool routing keeps resolving `cribsheet_*` names through the STALE provider.
   Its `get_client_factory` closure (connections.py, `_factory`) hits the
   "`_ready` is set but client is gone" branch (~line 221) and raises
   *"persistent connection is down. It will reconnect automatically."* — a promise
   nothing can keep: the auto-reconnect/health monitor belongs to the NEW
   connection entry; the stale closure references the dead one forever.
6. Each further restart mounts another duplicate provider and changes nothing.

Why `status` lied: the status builder reads the **connection manager** (the new
connection is genuinely connected/ready). Routing goes through the **provider
list** (stale). Two sources of truth, diverged.

Why disable→enable recovered: `disable_server`'s drop is equally blind, but the
disable+enable cycle re-runs the full mount + `tools/list_changed` notification
path, after which resolution lands on a live provider (observably, the client
session's tools dropped and re-announced during it). It works by side effect, not
by cleaning up — the stale providers are presumably still in the list.

## Recommended fix

Stop repr-sniffing; make mount bookkeeping explicit:

1. Keep a registry `_mounted: dict[str, list[object]]` populated in
   `_mount_server` (the wrapper is `combiner.providers[-1]` immediately after
   `combiner.mount(...)`, or capture `mount()`'s return value if this fastmcp
   returns it), and have `_drop_providers` remove those exact objects from
   `combiner.providers` by identity, then clear the registry entry.
2. Interim/back-compat shim if you want one while migrating:
   `any(getattr(t, "_prefix", None) == server_name for t in
   getattr(p, "transforms", None) or [])` — but that's private-attr sniffing with
   the same drift risk; the registry is the durable fix.
3. Guard against recurrence: log a WARNING (or fail the restart) when
   `_drop_providers` removes 0 for a server the combiner believes is mounted —
   that asymmetry is exactly how this bug stayed invisible while `restart_server`
   reported success.

## Tests worth adding

- Mount a server via the real `_mount_server` path, then assert
  `_drop_providers(name) == 1` — this is the regression test that would have
  caught the fastmcp drift (existing tests apparently never assert a nonzero
  drop against a real mount).
- Restart flow: after `restart_server`, assert exactly ONE provider exists for
  the namespace and that a proxied call round-trips (no stale-factory error).
- Duplicate-mount invariant: calling `_mount_server` twice for one name must not
  leave two providers resolving the same tool names.

## Repro (live, no harness needed)

```python
from fastmcp import FastMCP
parent, child = FastMCP('parent'), FastMCP('child')
parent.mount(child, namespace='x')
p = parent.providers[-1]
assert "namespace='x'" not in repr(p)          # matcher branch 1 dead
assert getattr(p, '_namespace', None) is None  # matcher branch 2 dead
```

---

## Follow-up (2026-10-15): a SECOND wedge mode — restart hangs, Ctrl-C needed

The stale-provider root cause above is **fixed** (mount registry +
`drop_server_providers` by object identity, with the "dropped 0 providers"
warning). What wedged next was a different beast, hit by `restart clickup`:

## Symptom

`mcp-combiner restart-server clickup` (the ctl CLI, same path as
`:MCPRestartServer`) **hung indefinitely** — no output until the user Ctrl-C'd.
Interrupted, the traceback ends in:

```text
fastmcp/client/client.py: _await_with_session_monitoring → asyncio.wait → await waiter
ctl.py: asyncio.run(args.func(args)) → KeyboardInterrupt
```

Two unbounded/long awaits make this reachable:

1. **ctl side** — `_call_meta` awaits `client.call_tool` with no timeout at all;
   the only bound fastmcp applies client-side is a 300s per-request read
   timeout, and a server-side handler stuck in a non-request await is never
   timed out by it. So a wedged combiner = a visibly hung CLI.
2. **Combiner side** — `combiner__restart_server` itself awaits several steps
   with no bound: `conn_manager.disconnect` → `_teardown` →
   `conn.stack.aclose()` (awaits the session task, which can be wedged inside
   a hung upstream POST) and `_open` → `enter_async_context` (the initialize
   handshake, fastmcp default read timeout 300s). A wedged upstream made every
   such await hang ~5 minutes — or forever on the aclose path.

(Also note: Ctrl-C kills the ctl's MCP session, which cancels the in-flight
server-side `restart_server` mid-flight — possibly between disconnect and
remount, leaving the server unmounted. Another route to "it wedged".)

## Fix (2026-10-15)

Stop guessing where it hangs; bound every await on the restart path:

- `connections.py` — `_CONNECT_TIMEOUT` (`MCP_COMBINER_CONNECT_TIMEOUT`, 30s):
  bounds `enter_async_context` for **non-OAuth** upstreams (OAuth is exempt —
  its first connect legitimately blocks in the interactive browser flow).
  `_TEARDOWN_TIMEOUT` (`MCP_COMBINER_TEARDOWN_TIMEOUT`, 15s): bounds
  `stack.aclose()` in `_teardown`; on expiry the fastmcp session task is
  force-cancelled (`_hard_kill_session`) so disconnect/restart cannot wait on
  a wedged POST.
- `ctl.py` — `_call_meta` bounds `client.call_tool` at 120s
  (`MCP_COMBINER_CTL_META_TIMEOUT`), raising a clean
  `error: meta call '...' got no answer ... try \`mcp-combiner restart --force\``
  via the existing catch-all in `run()`.

With both, an unresponsive upstream now fails a restart in bounded time — the
restart returns an error, the health monitor retries with backoff, and the CLI
reports instead of hanging. Verified live against a black-hole (accepts TCP,
never answers) upstream for both bounded paths.

