# CodeCompanion ACP Integration Design

## Status: Investigation Complete / Pending Upstream Request

## Problem

mcp-companion needs to inject a local MCP bridge server into CodeCompanion's ACP
sessions so that agents (Copilot, Claude Code, etc.) gain access to additional
MCP tools. The ACP spec explicitly supports this:

> Clients **MAY** use this ability to provide tools directly to the underlying
> language model by including their own MCP server.
>
> — [ACP Session Setup](https://agentclientprotocol.com/protocol/session-setup)

The bridge entry must be added to `adapter_modified.defaults.mcpServers` before
`session/new` is sent. The transport type (HTTP vs stdio/mcp-remote) depends on
the agent's `mcpCapabilities.http` capability, which is only available from
`_agent_info` after the INITIALIZE RPC completes.

There is no CodeCompanion hook at the point in the lifecycle where both pieces
of information are available.

---

## ACP Spec Summary

### MCP Server Transports (ACP `McpServer` type)

Defined as a discriminated union (`anyOf`) with three mutually exclusive variants.
No combined entry exists — you cannot specify both `command` and `url` on the same
entry. No preference/fallback mechanism within a single entry.

| Variant | Discriminator | Required Fields |
|---------|---------------|-----------------|
| `McpServerStdio` | *(none)* | `name`, `command`, `args` (string[]), `env` (EnvVariable[]) |
| `McpServerHttp` | `"type": "http"` | `name`, `url`, `headers` (HttpHeader[]) |
| `McpServerSse` | `"type": "sse"` | `name`, `url`, `headers` (HttpHeader[]) |

### Agent Capabilities (`mcpCapabilities`)

Returned in the INITIALIZE response at `agentCapabilities.mcpCapabilities`:

```json
{
  "mcpCapabilities": {
    "http": true,   // default: false
    "sse": true     // default: false (deprecated by MCP spec)
  }
}
```

> Before using HTTP or SSE transports, Clients **MUST** verify the Agent's
> capabilities during initialization.

**Note:** The ACP docs page on initialization inconsistently shows this field as
`agentCapabilities.mcp` rather than `agentCapabilities.mcpCapabilities`. The
official JSON schema at `schema.json` uses `mcpCapabilities`.

### References

- [ACP Session Setup](https://agentclientprotocol.com/protocol/session-setup) — mcpServers, transport types
- [ACP Initialization](https://agentclientprotocol.com/protocol/initialization) — capabilities exchange
- [ACP Schema](https://github.com/agentclientprotocol/agent-client-protocol/tree/main/schema) — `McpServer` anyOf definition

---

## CodeCompanion ACP Architecture

### Key Source Files

All paths relative to `~/.local/share/nvim/lazy/codecompanion.nvim/lua/codecompanion/`:

| File | Purpose |
|------|---------|
| `acp/init.lua` | `Connection` class — core ACP lifecycle (~1010 lines) |
| `interactions/chat/acp/handler.lua` | `ACPHandler` — creates connections from chat |
| `interactions/chat/helpers/init.lua` | `create_acp_connection(chat)` entry point |
| `adapters/acp/init.lua` | `Adapter.resolve()` / `Adapter.extend()` — config merging |
| `_extensions/init.lua` | Extension system (`setup` + `exports`, no lifecycle hooks) |

### Connection Lifecycle

```
Chat.new()
  → fire("ChatAdapter")                    ← no ACP connection yet
  → vim.schedule → helpers.create_acp_connection(chat)
    → ACPHandler.new(chat) → ensure_connection()
      → Connection.new({adapter})
      → connect_and_authenticate()
        → start_agent_process()
          → prepare_adapter()              ← adapter_modified created, mcpServers resolved
          → handlers.setup(adapter)        ← TOO EARLY: no _agent_info yet
          → spawn process (vim.system)
        → INITIALIZE RPC                   ← _agent_info NOW available
        → _authenticate()
                                           ← ⚠️ NO HOOK HERE ⚠️
      → _establish_session()
        → reads adapter_modified.defaults.mcpServers
        → sends session/new with mcpServers
      → apply_default_model()
      → apply_default_mode()
```

### `Connection:_establish_session()` (line 340)

This is the method that reads `mcpServers` and sends `session/new`:

```lua
function Connection:_establish_session()
  local session_args = {
    cwd = vim.fn.getcwd(),
    mcpServers = self.adapter_modified.defaults.mcpServers,
  }
  if self.adapter_modified.defaults.mcpServers == "inherit_from_config"
      and config.mcp.opts.acp_enabled then
    session_args.mcpServers = require("codecompanion.mcp").transform_to_acp()
  end
  -- ... sends SESSION_NEW or SESSION_LOAD with session_args
end
```

### Available Extension Points (None Sufficient)

| Mechanism | When it fires | Why it doesn't work |
|-----------|---------------|---------------------|
| `handlers.setup(adapter)` | In `start_agent_process()`, before INITIALIZE | No `_agent_info` — can't check `mcpCapabilities.http` |
| `ChatAdapter` event | In `Chat.new()`, before any ACP connection | No connection object exists yet |
| `ChatCreated` event | After chat UI creation | Before ACP connection |
| Extension `setup(opts)` | At plugin load time | One-shot, no per-connection lifecycle |
| Declarative `defaults.mcpServers` | Static adapter config | Can't dynamically choose HTTP vs stdio |

### Adapter Structure

ACP adapters (e.g. `copilot_acp.lua`) have:

```lua
{
  name = "copilot_acp",
  type = "acp",
  defaults = {
    mcpServers = {},      -- user-configurable via CC config
    timeout = 20000,
  },
  handlers = {
    setup = function(adapter) ... end,
    auth = function(adapter) ... end,
    form_messages = function(...) ... end,
    on_exit = function(adapter, code) ... end,
  },
}
```

`Adapter.extend()` merges user config via `vim.tbl_deep_extend("force", ...)`.
All ACP adapters ship with `defaults.mcpServers = {}`.

---

## Current Implementation (Monkey-Patch)

**File:** `lua/mcp_companion/cc/init.lua`, `_patch_acp()` at line 249.

Patches two methods on the `Connection` prototype:

### 1. `connect_and_initialize` — bridge warm-up

```lua
Connection.connect_and_initialize = function(self)
  M._start_bridge_async()  -- non-blocking, kicks off bridge if not running
  return original_connect(self)
end
```

### 2. `_establish_session` — bridge injection

```lua
Connection._establish_session = function(self)
  local bridge_entry = build_bridge_entry(self)
  if bridge_entry then
    local defaults = self.adapter_modified and self.adapter_modified.defaults
    if defaults then
      defaults.mcpServers = defaults.mcpServers or {}
      if type(defaults.mcpServers) ~= "table" then
        defaults.mcpServers = {}
      end
      -- Idempotency: check if already present
      local already = false
      for _, s in ipairs(defaults.mcpServers) do
        if s.name == "mcp-bridge" then already = true; break end
      end
      if not already then
        table.insert(defaults.mcpServers, bridge_entry)
      end
    end
  end
  return original_establish(self)
end
```

### `build_bridge_entry(conn)` — transport selection

Reads deterministic host/port from config, then checks agent capabilities:

```lua
local caps = conn._agent_info
    and conn._agent_info.agentCapabilities
    and conn._agent_info.agentCapabilities.mcpCapabilities

if caps and caps.http then
  return { type = "http", name = "mcp-bridge", url = bridge_url, headers = {} }
else
  return { name = "mcp-bridge", command = "npx", args = { "-y", "mcp-remote", bridge_url }, env = {} }
end
```

### Why this works

- `_establish_session` runs after `connect_and_authenticate()`, so `_agent_info` is populated
- `_establish_session` reads `adapter_modified.defaults.mcpServers`, so injecting there is picked up
- Idempotency via `Connection._mcp_companion_patched` flag on the prototype

### Why this is fragile

- Depends on internal method signatures (`_establish_session` is a private method)
- Depends on `adapter_modified.defaults.mcpServers` structure
- Prototype-level patch affects ALL ACP connections globally
- Any CC refactor of the connection lifecycle could break it silently

---

## Proposed Upstream Change

### Option A: `before_session` adapter handler (preferred)

A new adapter handler called after `connect_and_authenticate()` succeeds but before
`_establish_session()` runs. Receives the full Connection instance:

```lua
-- In Connection:connect_and_initialize(), between lines 167-171:
function Connection:connect_and_initialize()
  if self:is_connected() then return self end
  if not self:connect_and_authenticate() then return nil end

  -- NEW: allow adapters/extensions to modify session params
  if self.adapter_modified.handlers
      and self.adapter_modified.handlers.before_session then
    self.adapter_modified.handlers.before_session(self)
  end

  if not self:_establish_session() then return nil end
  self:apply_default_model()
  self:apply_default_mode()
  return self
end
```

Also needed in `ensure_session()` (~line 240) for the reconnection path.

**Impact:** ~5 lines added to CC. Extensions/plugins can then cleanly:
1. Read `conn._agent_info.agentCapabilities.mcpCapabilities.http`
2. Build appropriate MCP server entry
3. Insert into `conn.adapter_modified.defaults.mcpServers`

### Option B: User event (`CodeCompanionACPSessionPre`)

More general-purpose — any plugin can listen via autocmd:

```lua
-- In Connection:connect_and_initialize():
utils.fire("ACPSessionPre", { connection = self })
```

### Option C: Static stdio-only (simplify current code)

Always use stdio/mcp-remote transport, eliminating the need to check `_agent_info`:

```lua
-- Could be set declaratively in adapter config:
defaults = {
  mcpServers = {
    { name = "mcp-bridge", command = "npx", args = { "-y", "mcp-remote", "http://127.0.0.1:9741/mcp" }, env = {} }
  }
}
```

**Trade-off:** Loses HTTP transport optimisation. Adds npx/mcp-remote as a runtime
dependency. But eliminates the `_establish_session` patch entirely — only the
`connect_and_initialize` warm-up patch would remain.

### Option D: Patch `connect_and_authenticate` to fire custom event

Wrap only `connect_and_authenticate` to fire a custom User event after it returns,
then listen for that event to inject mcpServers. Still a monkey-patch but separates
concerns.

---

## Recommendation

1. **Post upstream request** for Option A (`before_session` handler) or Option B
   (User event). See draft below.
2. **Keep current monkey-patch** until upstream support lands — it's correct and
   the most robust approach given constraints.
3. **Consider Option C** as a fallback simplification if upstream is slow to respond.

---

## Draft Upstream Issue

**Title:** Feature request: adapter hook between `connect_and_authenticate()` and `_establish_session()`

### Context

I'm building [mcp-companion](https://github.com/georgeharker/mcp-companion), a
Neovim plugin that runs a local MCP bridge server and injects it into ACP sessions
so that agents like Copilot get access to additional MCP tools.

The ACP spec explicitly supports this pattern — from the
[Session Setup](https://agentclientprotocol.com/protocol/session-setup) docs:

> Clients **MAY** use this ability to provide tools directly to the underlying
> language model by including their own MCP server.

The bridge entry needs to be added to `adapter_modified.defaults.mcpServers`
before `session/new` is sent. However, the transport type (HTTP vs stdio/mcp-remote)
depends on the agent's `mcpCapabilities.http` capability, which is only available
from `_agent_info` after the INITIALIZE RPC completes.

### The problem

There's currently no hook point between authentication completing (when `_agent_info`
is populated) and session creation (when `mcpServers` is read). The lifecycle:

```
connect_and_authenticate()
  → start_agent_process()
    → prepare_adapter()        ← adapter_modified created
    → handlers.setup(adapter)  ← too early, no _agent_info yet
    → spawn process
  → INITIALIZE RPC             ← _agent_info now available
  → _authenticate()
                               ← ⚠️ NO HOOK HERE
_establish_session()
  → reads adapter_modified.defaults.mcpServers
  → sends session/new
```

### Current workaround

Monkey-patching `Connection._establish_session` on the prototype to inject the
bridge entry just before the original runs:

https://github.com/georgeharker/mcp-companion/blob/main/lua/mcp_companion/cc/init.lua#L249-L312

### Proposed solution

A `before_session` adapter handler, called after `connect_and_authenticate()`
succeeds but before `_establish_session()` runs, receiving the Connection instance:

```lua
if self.adapter_modified.handlers
    and self.adapter_modified.handlers.before_session then
  self.adapter_modified.handlers.before_session(self)
end
```

Same hook also needed in `ensure_session()` for the reconnection path.

Alternatively, a User event like `CodeCompanionACPSessionPre` would also work.

### Use case

With this hook, mcp-companion can cleanly:
1. Read `conn._agent_info.agentCapabilities.mcpCapabilities.http` to determine transport
2. Build the appropriate MCP server entry (HTTP or stdio)
3. Insert into `conn.adapter_modified.defaults.mcpServers`

No monkey-patching required.
