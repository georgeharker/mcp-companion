# mcp-companion.nvim

A Neovim plugin that connects the [Model Context Protocol (MCP)](https://modelcontextprotocol.io)
ecosystem to [CodeCompanion.nvim](https://github.com/olimorris/codecompanion.nvim).

MCP servers (tools, resources, prompts) are exposed as CodeCompanion tools for use in chat,
and forwarded to ACP agents (OpenCode, Claude Code) so they can call them autonomously.

## Features

- **MCP tools → CC tools**: All tools from all configured MCP servers are registered as
  CodeCompanion tools, usable in chat with `@` mentions or by the LLM directly.
- **ACP forwarding**: When using an ACP adapter (OpenCode), the bridge is automatically
  injected into the ACP session so the agent can call MCP tools autonomously.
- **Bridge lifecycle**: A Python FastMCP bridge process is managed automatically via
  [sharedserver](https://github.com/georgeharker/sharedserver) (shared across Neovim instances).
- **Hot reload**: Capabilities are polled and re-registered when MCP servers change.

## Architecture

```
Neovim
├── mcp_companion (Lua plugin)
│   ├── bridge/         HTTP client → FastMCP bridge process
│   ├── cc/             CodeCompanion extension (tools, resources, prompts, ACP)
│   ├── native/         [TODO] Pure-Lua MCP server registration
│   └── ui/             [TODO] Status floating window
│
└── CodeCompanion
    └── ACP adapter     → OpenCode (session/new injects bridge as mcpServers)

bridge/ (Python)
└── FastMCP proxy       → your MCP servers (todoist, github, clickup, etc.)
```

The bridge process is a [FastMCP](https://github.com/jlowin/fastmcp) server that proxies
all configured MCP servers. The Lua plugin communicates with it over HTTP on localhost.

## Requirements

- Neovim 0.10+
- Python 3.12+ with `uv`
- [CodeCompanion.nvim](https://github.com/olimorris/codecompanion.nvim)
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
        "georgeharker/sharedserver",  -- optional
    },
    build = "cd bridge && uv venv .venv --python 3.12 && uv pip install -e . --python .venv/bin/python",
    config = function()
        require("mcp_companion").setup({
            bridge = {
                port = 9741,
                config = vim.fn.expand("~/.config/mcp/servers.json"),
            },
            log = { level = "info" },
        })
    end,
},
```

Then register the CC extension:

```lua
-- In your codecompanion setup:
require("codecompanion").setup({
    extensions = {
        mcp_companion = {
            callback = "mcp_companion.cc",
            opts = {},
        },
    },
    mcp = {
        opts = {
            acp_enabled = true,  -- required for ACP forwarding
        },
    },
})
```

## MCP Server Config

The bridge reads a standard MCP servers JSON file. VS Code and Claude Desktop format
is supported, including `${env:VAR}` interpolation:

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
    }
  }
}
```

## Usage

### In CodeCompanion chat

All MCP tools are available as CC tools. The LLM can call them automatically, or you
can reference them with `@tool_name` in chat.

### With OpenCode (ACP)

When you use the `opencode` adapter in CodeCompanion, the bridge is automatically
forwarded to OpenCode via `session/new`. OpenCode connects to the bridge directly
and can call all MCP tools autonomously without any extra configuration.

### Manual bridge control

```lua
-- Start/stop bridge explicitly
require("mcp_companion").start_bridge()
require("mcp_companion").stop_bridge()

-- Check status
local status = require("mcp_companion").status()

-- Listen to events
require("mcp_companion").on("bridge_ready", function()
    print("Bridge connected with " .. #status().servers .. " servers")
end)
```

## Events

| Event | When |
|---|---|
| `bridge_ready` | Bridge connected and all capabilities loaded |
| `bridge_stopped` | Bridge disconnected |
| `servers_updated` | Server list or capabilities changed |
| `tool_list_changed` | Tool list changed on a server |
| `resource_list_changed` | Resource list changed |
| `prompt_list_changed` | Prompt list changed |

## Configuration

```lua
require("mcp_companion").setup({
    bridge = {
        port = 9741,                    -- bridge HTTP port
        config = nil,                   -- path to MCP servers JSON (auto-detected)
        python_cmd = nil,               -- path to Python (auto-resolved from .venv)
        poll_interval = 30000,          -- capability polling in ms
    },
    log = {
        level = "info",                 -- "debug", "info", "warn", "error"
        file = true,                    -- write to ~/.local/state/nvim/mcp-companion.log
    },
    auto_approve = false,               -- auto-approve all tool calls (or function(server, tool))
})
```

## Development

```bash
cd bridge
uv venv .venv --python 3.12
uv pip install -e ".[dev]"
pytest tests/ -v
```

To test the Lua side, with the bridge running on port 9742:

```vim
:luafile lua/mcp_companion/test_cc_tools.lua
:luafile lua/mcp_companion/test_real_servers.lua
```

## License

MIT
