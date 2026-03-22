# mcp-companion.nvim

A Neovim plugin that connects the [Model Context Protocol (MCP)](https://modelcontextprotocol.io)
ecosystem to [CodeCompanion.nvim](https://github.com/olimorris/codecompanion.nvim).

MCP servers (tools, resources, prompts) are exposed as native CodeCompanion features
and forwarded to ACP agents (OpenCode, Claude Code) so they can call them autonomously.

## Features

### MCP tools as CC tools

Every tool from every configured MCP server is registered as a CodeCompanion tool.
The LLM can call them directly during chat, and they appear in the tool picker.
Tools are grouped by server (`@github`, `@todoist`, etc.) and individually addressable.

### MCP resources as editor context (`#variables`)

MCP resources are registered as CC editor context entries.
Type `#mcp:resource_name` in a chat buffer to inline a resource's content.
Optionally, resources can be auto-injected into every new chat's system prompt
(useful for guidance documents like basic-memory's "ai assistant guide").

### MCP prompts as slash commands

MCP prompts become CC slash commands.
Type `/mcp:prompt_name` in a chat buffer to invoke a prompt.
If the prompt defines arguments, you are prompted to fill them in before the prompt
messages are injected into the chat.

### ACP forwarding

When using an ACP adapter (OpenCode, Claude Code), the bridge is automatically
injected into the ACP session via `session/new` and `session/load`.
The agent connects to the bridge directly over HTTP (or via `mcp-remote` stdio
fallback) and can call all MCP tools autonomously without extra configuration.
Transport is chosen based on agent capabilities: HTTP if the agent advertises
`mcpCapabilities.http`, stdio via `mcp-remote` otherwise.

### Tool approval flow

Tool calls go through a configurable approval chain before execution:

1. **Global auto-approve** -- `auto_approve = true` or a custom function
2. **Native servers** -- auto-approved (they run in-process)
3. **Per-server patterns** -- `autoApprove` list in your servers.json
4. **User prompt** -- `vim.ui.select` ("Allow" / "Deny")

### Bridge lifecycle management

A Python [FastMCP](https://github.com/jlowin/fastmcp) bridge process is managed
automatically via [sharedserver](https://github.com/georgeharker/sharedserver).
The bridge is shared across Neovim instances on the same port, with automatic
startup, health polling, idle timeout, and graceful shutdown.

### Shared server support

The bridge runs as a shared process via
[sharedserver](https://github.com/georgeharker/sharedserver).
Multiple Neovim instances connect to the same bridge on `127.0.0.1:9741`,
avoiding duplicate MCP server processes. The bridge stays alive for the configured
idle timeout (`30m` default) after the last Neovim instance disconnects.

### Hot reload

Capabilities are polled at a configurable interval (`poll_interval`, default 30s).
When MCP servers add, remove, or change tools/resources/prompts, the plugin
re-registers everything in CodeCompanion automatically.

### Status UI

`:MCPStatus` opens a floating window showing bridge state, connected servers,
and tool/resource/prompt counts. Servers can be expanded/collapsed, and a log view
is available. `:MCPRestart` restarts the bridge. `:MCPLog` opens the log file.

### Meta-tools

The bridge exposes management tools that the LLM can call:

- `bridge__status` -- list all configured servers and their state
- `bridge__enable_server` / `bridge__disable_server` -- toggle servers at runtime

### Environment variable interpolation

Server configs support `${env:VAR}` and `${VAR}` interpolation in `env` and
`headers` fields, matching VS Code and Claude Desktop format.

### OAuth 2.1 (work in progress)

MCP OAuth 2.1 authentication ([spec](https://gofastmcp.com/clients/auth/oauth))
is planned. This will enable authenticated access to MCP servers that require
OAuth flows, potentially integrating with editor context for token management.

## Architecture

```
Neovim
├── mcp_companion (Lua plugin)
│   ├── bridge/         HTTP client → FastMCP bridge process
│   ├── cc/             CodeCompanion extension
│   │   ├── tools       MCP tools → CC tools (function calling)
│   │   ├── editor_context  MCP resources → CC #editor_context
│   │   ├── slash_commands  MCP prompts → CC /slash_commands
│   │   └── approval    Tool approval flow (auto-approve + vim.ui.select)
│   ├── native/         Pure-Lua MCP server registration (stub)
│   └── ui/             Status floating window (:MCPStatus)
│
└── CodeCompanion
    └── ACP adapter     → OpenCode / Claude Code (session/new injects bridge)

bridge/ (Python, FastMCP)
├── server.py           Proxy server with middleware + health endpoint
├── config.py           Pydantic models, env interpolation, transport config
└── meta_tools.py       bridge__status, bridge__enable/disable_server
```

The bridge is a [FastMCP](https://github.com/jlowin/fastmcp) server that proxies
all configured MCP servers through a single HTTP endpoint. The Lua plugin
communicates with it over HTTP on localhost. A `SanitizeSchemaMiddleware` handles
servers with circular `$ref` schemas (e.g. Todoist) that would otherwise crash
Pydantic serialization.

## Requirements

- Neovim 0.10+
- Python 3.12+ with [`uv`](https://github.com/astral-sh/uv)
- [CodeCompanion.nvim](https://github.com/olimorris/codecompanion.nvim) v19+
- [sharedserver](https://github.com/georgeharker/sharedserver) (optional, for bridge lifecycle)
- An MCP server config file (same format as VS Code / Claude Desktop)

## Installation

### lazy.nvim

```lua
{
    "georgeharker/mcp-companion.nvim",
    lazy = false,
    dependencies = {
        "olimorris/codecompanion.nvim",
        "georgeharker/sharedserver",  -- optional, manages bridge lifecycle
    },
    build = "cd bridge && uv venv --python 3.14 .venv && uv sync --frozen",
    config = function()
        require("mcp_companion").setup({
            bridge = {
                port = 9741,
                config = vim.fn.expand("~/.config/mcp/servers.json"),
            },
            log = { level = "info", notify = "error" },
        })
    end,
},
```

Then register the CC extension in your CodeCompanion config:

```lua
require("codecompanion").setup({
    extensions = {
        mcp_companion = {
            callback = "mcp_companion.cc",
            opts = {},
        },
    },
})
```

## MCP Server Config

The bridge reads a standard MCP servers JSON file. VS Code and Claude Desktop
format is supported:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_TOKEN": "${env:GITHUB_TOKEN}"
      }
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"]
    },
    "remote-api": {
      "url": "https://api.example.com/mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer ${env:API_TOKEN}"
      }
    }
  }
}
```

### Supported transport types

| Transport | Config | Description |
|---|---|---|
| `stdio` | `command` + `args` | Spawns a local process (default) |
| `http` | `url` | Connects to a remote HTTP MCP endpoint |
| `sse` | `url` | Connects via Server-Sent Events |

### Per-server options

| Field | Type | Description |
|---|---|---|
| `command` | `string` | Executable for stdio transport |
| `args` | `string[]` | Arguments for the command |
| `env` | `object` | Environment variables (supports `${env:VAR}` interpolation) |
| `url` | `string` | URL for http/sse transport |
| `headers` | `object` | HTTP headers (supports `${env:VAR}` interpolation) |
| `transport` | `string` | `"stdio"`, `"http"`, or `"sse"` (auto-detected from presence of `url`) |
| `disabled` | `boolean` | Skip this server |
| `autoApprove` | `string[]` | Tool name patterns to auto-approve (Lua patterns) |

## Usage

### In CodeCompanion chat

All MCP tools are available as CC tools. The LLM can call them automatically, or you
can reference them with `@server_name` to include all tools from a server:

```
@github Create an issue titled "Bug report" in my repo
```

Individual tools are also accessible by their full key (`server__tool_name`).

### Editor context (resources)

MCP resources are available as `#mcp:resource_name` variables:

```
#mcp:basic-memory://ai-assistant-guide  Tell me about the codebase
```

### Slash commands (prompts)

MCP prompts are available as `/mcp:prompt_name` slash commands:

```
/mcp:summarize-project
```

If the prompt requires arguments, you will be prompted to enter them.

### With ACP agents (OpenCode, Claude Code)

When you use an ACP adapter in CodeCompanion, the bridge is automatically
forwarded to the agent via `session/new`. The agent connects to the bridge
directly and can call all MCP tools autonomously:

```
You: Use the todoist tool to list my tasks for today
Agent: [calls todoist_get_tasks autonomously via bridge]
```

### Commands

| Command | Description |
|---|---|
| `:MCPStatus` | Toggle the status floating window |
| `:MCPRestart` | Restart the MCP bridge |
| `:MCPLog` | Open the log file in a buffer |

```lua
vim.keymap.set("n", "<leader>ms", "<cmd>MCPStatus<cr>", { desc = "MCP status" })
```

The status window shows bridge state, connected servers, and tool/resource/prompt
counts. Press `<CR>` on a server to expand/collapse it. Press `l` for the logs
view, `q` to close.

### Manual bridge control

```lua
-- Start/stop bridge explicitly
require("mcp_companion").start_bridge()
require("mcp_companion").stop_bridge()

-- Check status
local status = require("mcp_companion").status()

-- Listen to events
require("mcp_companion").on("bridge_ready", function()
    print("Bridge connected!")
end)
```

## Events

| Event | When |
|---|---|
| `bridge_ready` | Bridge connected and all capabilities loaded |
| `bridge_stopped` | Bridge disconnected |
| `bridge_error` | Bridge encountered an error |
| `servers_updated` | Server list or capabilities changed |
| `tool_list_changed` | Tool list changed on a server |
| `resource_list_changed` | Resource list changed |
| `prompt_list_changed` | Prompt list changed |

## Configuration

```lua
require("mcp_companion").setup({
    bridge = {
        port = 9741,                    -- bridge HTTP port
        host = "127.0.0.1",            -- bridge host
        config = nil,                   -- path to MCP servers JSON (auto-detected)
        python_cmd = nil,               -- path to Python (auto-resolved from .venv)
        idle_timeout = "30m",           -- sharedserver grace period
        startup_timeout = 30,           -- seconds to wait for bridge health
        request_timeout = 60,           -- default MCP request timeout in seconds
    },
    log = {
        level = "info",                 -- file log level: "debug", "info", "warn", "error"
        notify = "error",               -- vim.notify level (default: errors only)
        file = true,                    -- write to ~/.local/state/nvim/mcp-companion.log
    },
    auto_approve = false,               -- true, false, or function(tool, server, ctx) -> bool
    system_prompt_resources = nil,       -- true (all), or {"pattern1", "pattern2"} to match
    ui = {
        enabled = true,
        width = 0.8,                    -- fraction of screen
        height = 0.7,
        border = "rounded",
    },
    on_ready = nil,                     -- fun(bridge) called when bridge connects
    on_error = nil,                     -- fun(err) called on bridge errors
})
```

### Auto-approve examples

```lua
-- Approve everything
auto_approve = true

-- Approve specific tools
auto_approve = function(tool_name, server_name, ctx)
    -- Auto-approve all read-only tools
    if tool_name:match("^get_") or tool_name:match("^list_") then
        return true
    end
    return false  -- prompt for everything else
end
```

### System prompt resource injection

```lua
-- Inject all MCP resources into every new chat's system prompt
system_prompt_resources = true

-- Inject only matching resources
system_prompt_resources = { "ai%-assistant%-guide", "project%-context" }
```

## Development

### Python bridge

```bash
cd bridge
uv venv --python 3.14 .venv && uv sync --frozen --extra dev
pytest tests/ -v
mypy --strict
```

### Lua plugin

```bash
lua-language-server --check=. --checklevel=Warning
```

Integration tests (requires a running bridge):

```vim
:luafile tests/test_cc_tools.lua
:luafile tests/test_real_servers.lua
```

## Type safety

- **Lua**: Full LuaLS type annotations. Zero warnings under `lua-language-server --check --checklevel=Warning`.
- **Python**: Pydantic models throughout. Zero errors under `mypy --strict`.

## License

MIT
