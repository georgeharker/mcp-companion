# mcp-companion.nvim — Design Notes

## Background

[mcphub.nvim](https://github.com/ravibrock/mcphub.nvim) was abandoned. It used a Node.js
`mcp-hub` process as a bridge and a dedicated Lua plugin. This project replaces both.

The goal: a modern, maintainable MCP integration for Neovim that works with CodeCompanion,
supports ACP agents like OpenCode, and doesn't depend on abandoned Node.js infrastructure.

## Option B: Python FastMCP Bridge

We chose a Python [FastMCP](https://github.com/jlowin/fastmcp) bridge over maintaining a
Node.js hub, because:

- FastMCP has a stable, maintained Python SDK
- `stateless_http=True` mode avoids session corruption issues (FastMCP #823, #945)
- uv makes the venv trivially reproducible
- Python is easier to extend than the abandoned mcp-hub

The bridge is a FastMCP server that proxies all configured MCP servers via the
`everything` mount pattern. It exposes them all on a single HTTP endpoint at
`http://127.0.0.1:<port>/mcp`.

## Bridge Process Lifecycle

The bridge is a long-running process. Multiple Neovim instances should share one bridge
rather than each starting their own.

We use [sharedserver](https://github.com/georgeharker/sharedserver) for this. It is a
Neovim Lua plugin with a Rust CLI backend that manages process lifecycle with reference
counting: the process starts when the first instance registers, and stops after the last
instance deregisters plus an idle timeout.

Fallback: if sharedserver is not available, the bridge starts directly via `vim.uv` and
lives for the lifetime of the Neovim instance.

## HTTP Client Design

The Lua HTTP client (`bridge/client.lua`) uses `vim.uv.new_tcp()` — one TCP connection
per request, not persistent. This is because:

- FastMCP's HTTP endpoint keeps connections alive for SSE notifications by default
- `Connection: close` on every request forces immediate body delivery
- `stateless_http=True` on the server eliminates session state corruption

No SSE notification stream is used. Instead, capabilities are polled via `vim.uv.new_timer()`
at a configurable interval (default 30s). This avoids an SSE disconnect bug that corrupts
the FastMCP proxy state.

## Tool Naming

FastMCP uses `_` as the namespace separator when mounting servers. A tool named `get_me`
on a server named `github` becomes `github_get_me` in the bridge namespace.

The Lua client splits on the first `_` to recover the server name and display name:
- `github_get_me` → server `github`, display `get_me`
- `clickup_clickup_search` → server `clickup`, display `clickup_search`

Both names are stored per tool:
- `tool._namespaced`: full bridge name (used for MCP calls)
- `tool._display`: stripped name (used for CC tool key suffix)

CC tool keys use double underscore: `server__display` (e.g. `github__get_me`,
`clickup__clickup_search`).

## CodeCompanion Integration

CC has a tools API (`config.interactions.chat.tools`) where tools can be registered
with an `id`, `description`, and `callback`. The callback returns a command spec that
CC executes and streams back to the chat.

Our `cc/tools.lua` writes directly into the live `cc_config.interactions.chat.tools`
table. A fingerprint cache (tool count + sorted namespaced names) prevents redundant
re-registration on every poll cycle.

### Why not CC's native MCP client?

CC has its own MCP client subsystem. If we added the bridge to `cc_config.mcp.servers`
and `default_servers`, CC would:

1. Auto-start the bridge as a stdio MCP client on every Neovim startup
2. Prefix all tool names with `mcp-bridge_` (e.g. `mcp-bridge_github_get_me`)
3. Double-register all 180 tools alongside our correctly-named registrations

So we bypass CC's MCP client entirely and register tools ourselves via the tools API.

## ACP Forwarding

ACP (Agent Client Protocol) is the protocol CodeCompanion uses to communicate with
external AI agents like OpenCode and Claude Code. The `session/new` method accepts a
`mcpServers` array of MCP server connection details.

When an ACP session is established, we inject the bridge into `mcpServers` so the
agent can connect to it directly and call tools autonomously. The agent discovers all
tools by querying the bridge's MCP endpoint.

### Transport

The ACP spec supports three transports in `mcpServers`:
- **stdio** (required): `{ name, command, args, env[] }`
- **HTTP** (optional, requires `mcpCapabilities.http: true` in initialize): `{ type: "http", name, url, headers[] }`
- **SSE** (deprecated)

OpenCode advertises `mcpCapabilities: { http: true, sse: true }`, so we use HTTP:
`{ type = "http", name = "mcp-bridge", url = "http://127.0.0.1:9741/mcp", headers = {} }`.

If the agent does not support HTTP, we fall back to stdio via `mcp-remote`:
`{ name = "mcp-bridge", command = "npx", args = { "-y", "mcp-remote", url }, env = {} }`.

### Monkey-patch approach

CC's `transform_to_acp()` function only handles stdio servers and requires them to be
in `default_servers`. We cannot use it.

Instead, `_inject_bridge_config()` monkey-patches `Connection:_establish_session` on the
CC ACP `Connection` class. The patch:

1. Runs once (idempotent via `Connection._mcp_companion_patched` flag)
2. Per session: wraps `send_rpc_request` on the instance
3. Intercepts `session/new` and `session/load` to inject the bridge entry
4. Restores `send_rpc_request` after `_establish_session` returns

This is safe because `_agent_info` (containing `mcpCapabilities`) is populated from the
`initialize` response before `_establish_session` is called.

## Data Flow

### CC chat tool call

```
User types prompt → LLM decides to call tool
  → CC calls tool callback in cc/tools.lua
    → callback invokes bridge.client:call_tool(namespaced_name, args)
      → HTTP POST /mcp (JSON-RPC tools/call)
        → bridge proxies to real MCP server
          → result returned to CC → shown in chat
```

### ACP (OpenCode) tool call

```
User types prompt in OpenCode chat
  → OpenCode's LLM decides to call tool
    → OpenCode makes HTTP call directly to bridge
      → bridge proxies to real MCP server
        → result returned to OpenCode
          → shown in OpenCode chat
```

Note: for ACP sessions, tool calls bypass CC entirely. CC only establishes the session
and forwards prompts; OpenCode handles tool execution independently.

## State Management

`state.lua` maintains the canonical view of bridge state:
- `connected`: bool
- `servers`: array of server objects, each with `name`, `tools[]`, `resources[]`, `prompts[]`
- Each tool has `_namespaced`, `_display`, `name`, `description`, `inputSchema`

State is updated by `bridge/client.lua` after each capability refresh. Subscribers
receive events via `state.on(event, callback)`.

## File Structure

```
mcp-companion.nvim/
├── bridge/                     Python FastMCP bridge
│   ├── pyproject.toml
│   └── mcp_bridge/
│       ├── server.py           FastMCP proxy server
│       ├── config.py           MCP server config loader (VS Code format)
│       ├── meta_tools.py       Bridge meta-tools (_status, _list_servers, _reload_config)
│       └── __main__.py         CLI entry point
├── lua/mcp_companion/
│   ├── init.lua                Public API + setup()
│   ├── config.lua              Config schema + defaults + auto-detection
│   ├── state.lua               Shared state + event bus
│   ├── log.lua                 Logger
│   ├── bridge/
│   │   ├── init.lua            Bridge process lifecycle
│   │   └── client.lua          HTTP client (vim.uv TCP)
│   ├── cc/
│   │   ├── init.lua            CC extension entry point + ACP injection
│   │   ├── tools.lua           MCP tools → CC tools registration
│   │   ├── variables.lua       [STUB] MCP resources → CC # variables
│   │   ├── slash_commands.lua  [STUB] MCP prompts → CC / slash commands
│   │   └── approval.lua        [STUB] Tool approval flow
│   ├── native/
│   │   └── init.lua            [STUB] Pure-Lua MCP server registration
│   └── ui/
│       └── init.lua            Status floating window (not wired)
└── tests/
    └── (pytest suite for bridge Python code)
```

## Known Limitations

- No tool approval flow (all tools auto-execute)
- Resources and prompts are not yet registered with CC
- Status UI exists but is not accessible via any command or keybind
- Native (pure-Lua) MCP server registration is not implemented
- Bridge must be running before CC connects; there is no retry on connection failure
- 180 tools on session/new means OpenCode's first connect takes a few seconds
