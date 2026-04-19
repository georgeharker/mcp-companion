# Investigation: "Connection closed before response completed"

## Symptom

When Neovim starts and finds a pre-existing bridge process already running, connection fails:

```
[mcp-companion] Initialize failed: Connection closed before response completed
[mcp-companion] MCP client connection failed: Connection closed before response completed
```

## Error chain

1. `lua/mcp_companion/bridge/client.lua:336` — TCP EOF with incomplete chunked body
2. `lua/mcp_companion/bridge/client.lua:547` — `initialize` request callback receives the error
3. `lua/mcp_companion/bridge/init.lua:260` — `_create_client()` propagates to caller

## Root cause

The bridge Python process enters an unrecoverable state where every new `POST /mcp`
(initialize) request returns:

```
HTTP/1.1 200 OK
Transfer-Encoding: chunked
Content-Type: text/event-stream
mcp-session-id: <id>
<empty body, immediate close>
```

This was confirmed with raw TCP, curl, and httpx against the live process. The response
headers arrive, an `mcp-session-id` is issued, but no SSE events are ever written and the
connection closes within ~1 ms.

Existing sessions (established before the process entered this state) continue to work —
`tools/list` with a valid session ID returns 200 with cached results.

## Bridge-side failure mode

Bridge log (`~/.local/state/nvim/mcp-bridge.log`) showed:

- ~185 ASGI errors per minute from process startup onward:
  ```
  ERROR: ASGI callable returned without completing response.
  ```
- Tracebacks pointing into `mcp.server.streamable_http_manager` →
  `run_stateless_server` (or `run_server`) → `anyio.ClosedResourceError`

### MCP SDK session manager internals

The bridge uses `StreamableHTTPSessionManager(stateless=False)` (stateful mode).

For a new session `initialize` request the path is:

1. `handle_request()` → `_handle_stateful_request()`
2. Creates a `StreamableHTTPServerTransport`; starts `run_server` task via
   `await self._task_group.start(run_server)`
3. `run_server`: `async with http_transport.connect() as streams:` →
   `task_status.started()` → `await self.app.run(...)`
4. `http_transport.handle_request()` → `_handle_post_request()`:
   - Creates `sse_stream_writer / sse_stream_reader` (anyio MemoryObjectStream, buffer=0)
   - Creates `EventSourceResponse(content=sse_stream_reader, data_sender_callable=sse_writer)`
   - Task group: `tg.start_soon(response, scope, receive, send)` +
     `await writer.send(session_message)`

### sse_starlette task group (EventSourceResponse)

`EventSourceResponse.__call__` spawns five tasks, all wrapped in `cancel_on_finish`:

| Task | Behaviour |
|---|---|
| `_stream_response` | Sends HTTP headers, iterates `sse_stream_reader`, sends final empty body |
| `_ping` | Periodic keepalive pings |
| `_listen_for_exit_signal_with_grace` | Server shutdown signal |
| `data_sender_callable` (`sse_writer`) | Reads `request_stream_reader`, writes SSE events |
| `_listen_for_disconnect` | Calls ASGI `receive()` in a loop; cancels group on disconnect |

**Critical**: `cancel_on_finish` means the FIRST task to finish cancels the whole group.

### Why `_stream_response` finishes immediately

`_stream_response` iterates `sse_stream_reader`. If `sse_stream_reader` is closed or empty
before any data arrives, the iterator exits immediately → task completes → group is
cancelled → ASGI callable returns without sending body → uvicorn logs
`ASGI callable returned without completing response`.

`sse_stream_reader` only gets data when `sse_writer` pushes to it. `sse_writer` reads from
`request_stream_reader`, which only gets data when the MCP `message_router` routes a server
response. If `_write_stream` (the stream into the running MCP `app`) closes unexpectedly
(e.g. `anyio.ClosedResourceError`), the entire pipeline stalls and `sse_stream_reader`
is closed with no data.

### What triggers the initial corruption

The bridge log showed the first ASGI errors appeared immediately after a `Basic Memory`
MCP server reconnection event. The reconnect likely caused a `ClosedResourceError` in the
shared anyio task group of the session manager, leaving the internal streams in a broken
state for all subsequent new-session requests.

## What does NOT cause this

- The Lua client's `Connection: close` header — `Connection: keep-alive` produces the
  same empty-body response
- Missing session ID in the request — that returns `400 Bad Request: Missing session ID`
  (a different, healthy code path)
- The bridge health endpoint — `GET /health` returns 200 throughout

## Scope of failure

Once the bridge enters this state:

- New MCP sessions cannot be established (initialize always fails)
- Existing sessions remain fully functional
- The only recovery is restarting the bridge process

## Affected code paths

| File | Lines | Description |
|---|---|---|
| `lua/mcp_companion/bridge/init.lua` | 50–61 | Pre-existing bridge detection → `_create_client()` |
| `lua/mcp_companion/bridge/init.lua` | 239–266 | `_create_client()` — no retry on failure |
| `lua/mcp_companion/bridge/client.lua` | 533–614 | `Client:connect()` — single initialize attempt |
| `lua/mcp_companion/bridge/client.lua` | 320–340 | TCP EOF handler → "Connection closed" error |

## Potential mitigations (not yet implemented)

### Option A — Retry in `_create_client()`

Retry `_create_client()` up to N times (e.g. 3) with a short delay (e.g. 2 s) before
declaring failure. Transient failures (race on startup, brief process churn) would recover
automatically. Persistent failures (bridge stuck in broken state) would still fail after
N×delay seconds, but with a more informative error message.

Pros: simple, non-destructive, no process management  
Cons: does not recover a persistently broken bridge; adds latency before final failure

### Option B — Force-restart on exhausted retries (pre-existing bridge)

After N failed retries, if the bridge was found pre-existing (not spawned by this Neovim
instance), force-kill the process on the configured port (e.g. via `lsof -ti tcp:<port>`)
and start a fresh bridge.

Pros: fully automatic recovery  
Cons: kills a process we did not start; other Neovim instances sharing the bridge lose
their connections; platform-specific (`lsof`)

### Option C — Improve bridge Python-side resilience

Patch `StreamableHTTPSessionManager` (or the FastMCP wrapper) to catch
`anyio.ClosedResourceError` inside `run_server` / `_handle_stateful_request` and reset
the internal task group + streams rather than leaving them in a broken state.

Pros: fixes the root cause; no Lua-side changes  
Cons: requires changes to vendored/upstream Python dependencies (mcp SDK or FastMCP)

### Option D — Expose a `/reset` or `/restart` bridge endpoint

Add a bridge HTTP endpoint (e.g. `POST /admin/restart`) that reinitialises the
`StreamableHTTPSessionManager` in-process without restarting the OS process.

Pros: clean, avoids process kill; cross-platform  
Cons: requires bridge Python changes; existing sessions still drop
