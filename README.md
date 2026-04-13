# mcp-companion

An MCP proxy bridge and editor integration that aggregates multiple
[Model Context Protocol](https://modelcontextprotocol.io) servers behind a
single HTTP endpoint, with first-class
[CodeCompanion.nvim](https://github.com/olimorris/codecompanion.nvim) support.

The bridge runs standalone as a Python process — any MCP-aware client can
connect to it over HTTP. The Lua plugin layer adds Neovim-specific features:
tool registration, editor context, slash commands, ACP forwarding, and a status
UI.

## Overview

```
┌─────────────────────────────────────────────────────┐
│  MCP Bridge (Python, standalone)                    │
│  Aggregates N MCP servers → single HTTP endpoint    │
│  Auth, env interpolation, meta-tools, health API    │
└────────────────────┬────────────────────────────────┘
                     │ HTTP :9741
        ┌────────────┼────────────────┐
        ▼            ▼                ▼
   Neovim plugin   OpenCode     Any HTTP client
   (CodeCompanion) (ACP agent)  (curl, scripts)
```

---

## MCP Bridge (standalone)

The bridge is a [FastMCP](https://github.com/jlowin/fastmcp) server that
proxies all configured MCP servers through a single HTTP endpoint. It works
independently of Neovim — any MCP client that speaks HTTP can use it.

### Quick start

```bash
# Install dependencies
cd bridge
uv sync --frozen

# Run the bridge
uv run python -m mcp_bridge --config ~/.config/mcp/servers.json --port 9741

# Health check
curl http://127.0.0.1:9741/health
```

### What the bridge does

- Reads a standard `mcpServers` JSON config (VS Code / Claude Desktop format)
- Spawns and manages stdio servers, connects to HTTP/SSE servers
- Exposes all tools, resources, and prompts through one HTTP endpoint
- Handles environment variable interpolation, OAuth 2.1 auth, schema sanitization
- Provides meta-tools (`bridge__status`, `bridge__enable_server`, `bridge__disable_server`)
- Serves a `/health` endpoint with server status

### Using with other MCP clients

Any MCP client that supports HTTP transport can connect directly:

```bash
# OpenCode, Claude Code, or any ACP agent
# Point it at http://127.0.0.1:9741

# Or use curl to call tools directly
curl -X POST http://127.0.0.1:9741/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

---

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
| `env` | `object` | Environment variables (supports interpolation) |
| `url` | `string` | URL for http/sse transport |
| `headers` | `object` | HTTP headers (supports interpolation) |
| `transport` | `string` | `"stdio"`, `"http"`, or `"sse"` (auto-detected from presence of `url`) |
| `disabled` | `boolean` | Skip this server |
| `autoApprove` | `string[]` | Tool name patterns to auto-approve |
| `auth` | `string\|object` | Authentication config (see below) |
| `sharedServer` | `string` | Name of a `sharedServers` entry to start before connecting (see below) |

### sharedServer — per-server process management

Many MCP servers that expose an HTTP endpoint (as opposed to stdio) need to run as
standalone processes: started before the bridge connects, kept alive during the session,
and shut down when no longer needed. Managing this manually is tedious — you have to
remember to start them before your editor, keep them running, and clean them up
afterward.

The `sharedServer` field solves this. It links a server entry to a process definition in
the top-level `sharedServers` dict. The bridge delegates lifecycle to
[sharedserver](https://github.com/georgeharker/sharedserver), a reference-counted
process supervisor:

- On bridge startup, sharedserver **starts** the process (or increments a refcount if
  it is already running from another client)
- The process stays alive as long as any client holds a reference — multiple bridge
  instances, Neovim windows, or scripts share the same process transparently
- After the last client detaches, the process remains alive for `grace_period` before
  stopping — so a quick restart or a second Neovim window opening does not cause an
  unnecessary restart
- On bridge shutdown, sharedserver **decrements the refcount**; the process stops only
  when the grace period expires with no remaining clients

The result is ephemeral-but-shared server processes: they start on demand, are shared
across all clients that need them, and stop themselves when idle. You never need to
manually start or stop them.

The bridge waits up to `health_timeout` seconds for the process to become reachable
after starting before mounting the proxy. If the process was already running, this
passes immediately.

A complete example — a Google Workspace MCP server that needs OAuth and is managed via
sharedserver:

```json
{
  "sharedServers": {
    "google-workspace-proc": {
      "command": "uvx",
      "args": ["workspace-mcp", "--transport", "streamable-http"],
      "env": {
        "WORKSPACE_MCP_PORT": "8002",
        "MCP_ENABLE_OAUTH21": "true",
        "GOOGLE_OAUTH_CLIENT_ID": "${env:GOOGLE_OAUTH_CLIENT_ID}",
        "GOOGLE_OAUTH_CLIENT_SECRET": "${env:GOOGLE_OAUTH_CLIENT_SECRET}"
      },
      "grace_period": "30m",
      "health_timeout": 30
    }
  },
  "mcpServers": {
    "google-workspace": {
      "url": "http://localhost:8002/mcp",
      "auth": "oauth",
      "sharedServer": "google-workspace-proc"
    }
  }
}
```

The `sharedServers` key is separate from `mcpServers` — it describes *how to run* the
process; the `mcpServers` entry describes *how to connect* to it.  Multiple server
entries can reference the same `sharedServers` entry.

**`sharedServers` entry fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `command` | `string` | **required** | Executable to run (e.g. `"uvx"`) |
| `args` | `string[]` | `[]` | Arguments to the command (supports interpolation) |
| `env` | `object` | `{}` | Extra environment variables (supports interpolation) |
| `grace_period` | `string` | — | How long to keep the process alive after the last client detaches (e.g. `"30m"`) |
| `health_timeout` | `integer` | `30` | Seconds to poll the server URL after start before giving up |

### Environment variable interpolation

All config fields support `${VAR}` interpolation with optional defaults:

| Syntax | Description |
|---|---|
| `${VAR}` | Expands to `$VAR` value, empty string if unset |
| `${env:VAR}` | Same as `${VAR}` (VS Code / Claude Desktop compat) |
| `${VAR:-default}` | Expands to `$VAR` if set, otherwise `default` |
| `${env:VAR:-default}` | Same with `env:` prefix |

Expansion applies to: `command`, `args`, `env`, `url`, and `headers` fields.
Interpolation happens at runtime (when connecting to servers), not at config
load time.

---

## Authentication

MCP servers that require authentication are supported via the `auth` field.
Three modes are available:

### Bearer token

```json
{
  "mcpServers": {
    "my-api": {
      "url": "https://api.example.com/mcp",
      "auth": { "bearer": "${env:MY_API_TOKEN}" }
    }
  }
}
```

### OAuth 2.1 — auto-discovery

```json
{
  "mcpServers": {
    "my-api": {
      "url": "https://api.example.com/mcp",
      "auth": "oauth"
    }
  }
}
```

This triggers the full [MCP OAuth 2.1](https://spec.modelcontextprotocol.io/specification/2025-03-26/basic/authorization/)
flow: metadata discovery, dynamic client registration, PKCE authorization code
grant via browser redirect, and token exchange.

### OAuth 2.1 — explicit client

```json
{
  "mcpServers": {
    "my-api": {
      "url": "https://api.example.com/mcp",
      "auth": {
        "oauth": {
          "client_id": "my-app",
          "client_secret": "${env:OAUTH_SECRET}",
          "scopes": "read write"
        }
      }
    }
  }
}
```

When `client_id` is provided, dynamic client registration is skipped.

### OAuth options

| Field | Type | Default | Description |
|---|---|---|---|
| `client_id` | `string` | — | Pre-registered OAuth client ID (skips dynamic registration) |
| `client_secret` | `string` | — | Client secret (used with `client_id`) |
| `scopes` | `string\|string[]` | — | OAuth scopes to request |
| `client_metadata_url` | `string` | — | CIMD URL (alternative to dynamic registration) |
| `cache_tokens` | `boolean` | `true` | Persist tokens to disk for this server (overrides global setting) |
| `callback_port` | `integer` | — | Local port for the OAuth redirect callback (e.g. `9876`). Required when the auth provider validates redirect URIs strictly (Google, GitHub, etc.) — must match the URI registered in your OAuth app. |

### OAuth token caching

By default, tokens are persisted to `~/.cache/mcp-companion/oauth-tokens/<server>/`
and reused across sessions. Refresh tokens are handled automatically.

**Global caching settings** live in the top-level `oauth` section of your config:

```json
{
  "oauth": {
    "cache_tokens": true,
    "token_dir": "~/.cache/mcp-companion/oauth-tokens"
  },
  "mcpServers": { ... }
}
```

**Per-server override** — disable caching for one server while keeping it globally:

```json
{
  "mcpServers": {
    "my-api": {
      "url": "https://api.example.com/mcp",
      "auth": {
        "oauth": {
          "cache_tokens": false
        }
      }
    }
  }
}
```

**CLI flags** — override everything at startup (highest priority):

```bash
# Disable disk caching entirely (tokens lost on restart)
python -m mcp_bridge --config servers.json --no-oauth-cache

# Use a custom token directory
python -m mcp_bridge --config servers.json --oauth-token-dir /secure/tokens

# Re-enable caching if config file says otherwise
python -m mcp_bridge --config servers.json --oauth-cache
```

Priority order (highest to lowest): CLI flag → config `oauth` section → built-in default.

### External OAuth provider mode

Some MCP servers support an "external OAuth provider" mode where the server
does **not** run its own OAuth flow — it simply validates bearer tokens issued
by the upstream identity provider (e.g. Google). In this mode the bridge holds
the real OAuth token and passes it on every request. The server is stateless: it
can restart freely without invalidating any sessions.

**How it works:**

1. The MCP server is configured to advertise the identity provider (e.g.
   Google) via RFC 9728 `/.well-known/oauth-protected-resource` and returns
   `401` on unauthenticated requests.
2. The bridge's OAuth client follows the discovery document, performs the PKCE
   authorization code flow **directly against the identity provider**, and
   caches the resulting access + refresh token in the bridge's encrypted token
   store.
3. Every subsequent request to the MCP server carries
   `Authorization: Bearer <real-token>`. The MCP server validates it against
   the provider's API — no local state required.
4. When the access token expires, the bridge silently refreshes it using the
   cached refresh token. No re-authentication required unless the refresh token
   itself expires.

**When to use this vs. standard OAuth 2.1:**

| | Standard OAuth 2.1 | External provider mode |
|---|---|---|
| Token issued by | MCP server (JWT) | Identity provider directly |
| MCP server restart | Loses client registrations → re-auth needed | Transparent (stateless) |
| Requires `client_id` | Only if provider doesn't support DCR | Yes (Google/GitHub don't support DCR) |
| Redirect URI to register | Automatically negotiated | Must match `callback_port` |

**Configuration example — Google Workspace MCP:**

Enable external provider mode on the GWS server:

```json
{
  "sharedServers": {
    "goog_ws": {
      "command": "uvx",
      "args": ["workspace-mcp", "--transport", "streamable-http"],
      "env": {
        "WORKSPACE_MCP_PORT": "8002",
        "MCP_ENABLE_OAUTH21": "true",
        "EXTERNAL_OAUTH21_PROVIDER": "true",
        "WORKSPACE_MCP_STATELESS_MODE": "true",
        "GOOGLE_OAUTH_CLIENT_ID": "${env:GOOGLE_OAUTH_CLIENT_ID}",
        "GOOGLE_OAUTH_CLIENT_SECRET": "${env:GOOGLE_OAUTH_CLIENT_SECRET}"
      },
      "grace_period": "30m",
      "health_timeout": 30
    }
  },
  "mcpServers": {
    "gws": {
      "url": "http://localhost:8002/mcp",
      "sharedServer": "goog_ws",
      "auth": {
        "oauth": {
          "client_id": "${env:GOOGLE_OAUTH_CLIENT_ID}",
          "client_secret": "${env:GOOGLE_OAUTH_CLIENT_SECRET}",
          "callback_port": 9876
        }
      }
    }
  }
}
```

**Google Console setup** (one-time):

1. Go to [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials)
2. Create an **OAuth 2.0 Client ID** of type **Web application**
3. Under "Authorized redirect URIs" add: `http://localhost:9876/callback`
   (use `localhost`, not `127.0.0.1`)
4. Add your Google account as a test user on the OAuth consent screen

On first connection the bridge opens a browser tab for the Google consent
screen. After you approve it, the access and refresh tokens are cached
in `~/.cache/mcp-companion/oauth-tokens/gws/`. Subsequent restarts of
GWS (or even the bridge) will silently re-use the cached token without
prompting again.

**Notes:**

- `callback_port` must match the redirect URI registered in your OAuth app
  exactly. Google and most providers reject unregistered URIs.
- The `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` env vars are
  needed by both GWS (for token validation) and the bridge (for the OAuth
  flow). Use your shell environment or a secrets manager such as 1Password
  CLI (`op run --`) to supply them.
- The `OAUTHLIB_INSECURE_TRANSPORT=1` env var is only needed when GWS itself
  runs over plain HTTP (the default in local development) — it is not needed
  by the bridge.

---

### Token encryption

Cached OAuth tokens are encrypted at rest using Fernet symmetric encryption. By default,
the encryption key is derived from machine-specific identifiers (hostname + username).
This provides obfuscation but not strong security — anyone with access to your home
directory can derive the same key.

For stronger security, set a custom encryption key:

```bash
# Via environment variable
export MCP_BRIDGE_TOKEN_KEY="your-secret-key-here"
python -m mcp_bridge --config servers.json

# Or in Neovim config
require("mcp_companion").setup({
    bridge = {
        token_key = "your-secret-key-here",
    },
})
```

When you change the encryption key, existing cached tokens become unreadable and you'll
need to re-authenticate with OAuth servers.

---

## Neovim Integration

The Lua plugin connects the bridge to
[CodeCompanion.nvim](https://github.com/olimorris/codecompanion.nvim), exposing
MCP capabilities as native editor features.

### Requirements

- Neovim 0.10+
- Python 3.12+ with [`uv`](https://github.com/astral-sh/uv)
- [CodeCompanion.nvim](https://github.com/olimorris/codecompanion.nvim) v19+
- [sharedserver](https://github.com/georgeharker/sharedserver) — manages the bridge
  process lifecycle across multiple Neovim instances

### Installing sharedserver

The plugin uses sharedserver to share one bridge process across all Neovim instances,
with automatic startup, health polling, idle timeout, and graceful shutdown.

**Install via cargo:**

```bash
cargo install sharedserver
```

**Or let lazy.nvim build it** — list `georgeharker/sharedserver` as a plugin entry with
a `build` step (see the lazy.nvim spec below). lazy.nvim will compile and install the
binary automatically on first sync.

### Installation (lazy.nvim)

Install sharedserver and mcp-companion as separate top-level plugin entries so
lazy.nvim runs the build steps independently, then declare sharedserver as a
dependency of mcp-companion so load order is correct:

```lua
-- sharedserver: builds the Rust binary that manages bridge process lifecycle
{
    "georgeharker/sharedserver",
    build = "cargo install --path rust",
    lazy = false,
},

-- mcp-companion: the bridge + Neovim plugin
{
    "georgeharker/mcp-companion",
    lazy = false,
    dependencies = {
        "olimorris/codecompanion.nvim",
        "georgeharker/sharedserver",
    },
    build = "cd bridge && uv sync --frozen",
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

### Features

#### MCP tools as CC tools

Every tool from every configured MCP server is registered as a CodeCompanion
tool. The LLM can call them directly during chat, and they appear in the tool
picker. Tools are grouped by server (`@github`, `@todoist`, etc.) and
individually addressable.

#### MCP resources as editor context

MCP resources are registered as CC editor context entries. Type
`#mcp:resource_name` in a chat buffer to inline a resource's content.
Optionally, resources can be auto-injected into every new chat's system prompt
(useful for guidance documents like basic-memory's "ai assistant guide").

#### MCP prompts as slash commands

MCP prompts become CC slash commands. Type `/mcp:prompt_name` in a chat buffer
to invoke a prompt. If the prompt defines arguments, you are prompted to fill
them in before the prompt messages are injected into the chat.

#### ACP forwarding

When using an ACP adapter (OpenCode, Claude Code), the bridge is automatically
injected into the ACP session via `session/new` and `session/load`. The agent
connects to the bridge directly over HTTP (or via `mcp-remote` stdio fallback)
and can call all MCP tools autonomously without extra configuration.

#### Tool approval flow

Tool calls go through a configurable approval chain before execution:

1. **Global auto-approve** — `auto_approve = true` or a custom function
2. **Native servers** — auto-approved (they run in-process)
3. **Per-server patterns** — `autoApprove` list in your servers.json
4. **User prompt** — `vim.ui.select` ("Allow" / "Deny")

#### Bridge lifecycle

When sharedserver is available, the Neovim plugin calls:

```
sharedserver use mcp-bridge --grace-period <idle_timeout> --pid <nvim-pid> \
  -- python -m mcp_bridge --config <path> --port <port>
```

Multiple Neovim instances share the same bridge on `127.0.0.1:9741`. When
the last Neovim instance exits (or calls `stop_bridge()`), the bridge stays
alive for `idle_timeout` in case another instance reconnects, then shuts down.

Without sharedserver, the bridge starts directly via `vim.uv` and lives for
the lifetime of the Neovim instance.

#### Hot reload

Capabilities are polled at a configurable interval. When MCP servers add,
remove, or change tools/resources/prompts, the plugin re-registers everything
in CodeCompanion automatically.

#### Status UI

`:MCPStatus` opens a floating window showing bridge state, connected servers,
and tool/resource/prompt counts. Servers can be expanded/collapsed, and a log
view is available. `:MCPRestart` restarts the bridge. `:MCPLog` opens the log
file.

#### Meta-tools

The bridge exposes management tools that the LLM can call:

- `bridge__status` — list all configured servers and their state
- `bridge__enable_server` / `bridge__disable_server` — toggle servers at runtime

### Usage

#### In CodeCompanion chat

All MCP tools are available as CC tools. The LLM can call them automatically,
or you can reference them with `@server_name` to include all tools from a
server:

```
@github Create an issue titled "Bug report" in my repo
```

Individual tools are also accessible by their full key (`server__tool_name`).

#### Editor context (resources)

```
#mcp:basic-memory://ai-assistant-guide  Tell me about the codebase
```

#### Slash commands (prompts)

```
/mcp:summarize-project
```

If the prompt requires arguments, you will be prompted to enter them.

#### With ACP agents (OpenCode, Claude Code)

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

### Logging

MCP companion writes logs to two locations:

| Log | Default path | Purpose |
|---|---|---|
| Plugin log | `~/.local/state/nvim/mcp-companion.log` | Lua-side events (bridge lifecycle, server connections, errors) |
| Bridge log | set via `bridge.log_file` | Python bridge output (server communication, OAuth, tool calls) |
| sharedserver logs | `$XDG_RUNTIME_DIR/sharedserver` or `/tmp/sharedserver` | All processes managed by sharedserver |

Use `:MCPLog` to open the plugin log directly in a Neovim buffer.

By default the bridge produces no log file. To enable bridge logging, set `bridge.log_file` in
your [plugin configuration](#plugin-configuration).

When the bridge is managed by [sharedserver](https://github.com/georgeharker/sharedserver.nvim),
sharedserver writes its own logs to `$XDG_RUNTIME_DIR/sharedserver` (or `/tmp/sharedserver` if
`XDG_RUNTIME_DIR` is not set).

OAuth tokens are cached at `~/.cache/mcp-companion/oauth-tokens/<server>/`.

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

### Events

| Event | When |
|---|---|
| `bridge_ready` | Bridge connected and all capabilities loaded |
| `bridge_stopped` | Bridge disconnected |
| `bridge_error` | Bridge encountered an error |
| `servers_updated` | Server list or capabilities changed |
| `tool_list_changed` | Tool list changed on a server |
| `resource_list_changed` | Resource list changed |
| `prompt_list_changed` | Prompt list changed |

### Plugin Configuration

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
        token_key = nil,                -- encryption key for OAuth tokens (or use MCP_BRIDGE_TOKEN_KEY env)
        log_file = nil,                 -- path to write bridge stdout/stderr (e.g. vim.fn.stdpath("log") .. "/mcp-bridge.log")
        global_env = {},                -- extra environment variables passed to the bridge process
    },
    log = {
        level = "warn",                 -- file log level: "debug", "info", "warn", "error"
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

#### Auto-approve examples

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

#### System prompt resource injection

```lua
-- Inject all MCP resources into every new chat's system prompt
system_prompt_resources = true

-- Inject only matching resources
system_prompt_resources = { "ai%-assistant%-guide", "project%-context" }
```

---

## Architecture

```
┌─────────────────────────────────────────────┐
│ MCP Bridge (Python, FastMCP)                │
│                                             │
│  server.py      Proxy + middleware + health  │
│  config.py      Pydantic models, env interp  │
│  auth.py        OAuth 2.1, bearer tokens     │
│  sharedserver.py  sharedserver lifecycle     │
│  meta_tools.py  bridge__status, enable/disable│
└────────────────────┬────────────────────────┘
                     │ HTTP :9741
┌────────────────────┴────────────────────────┐
│ Neovim Plugin (Lua)                         │
│                                             │
│  bridge/       HTTP client -> bridge process │
│  cc/           CodeCompanion extension       │
│    tools       MCP tools -> CC tools         │
│    editor_context  MCP resources -> #context │
│    slash_commands   MCP prompts -> /commands  │
│    approval    Tool approval flow            │
│  native/       Pure-Lua MCP servers (stub)   │
│  ui/           Status floating window        │
└─────────────────────────────────────────────┘
```

The bridge aggregates N MCP servers through a single HTTP endpoint. A
`SanitizeSchemaMiddleware` handles servers with circular `$ref` schemas
(e.g. Todoist) that would otherwise crash Pydantic serialization.

---

## Development

### Python bridge

```bash
cd bridge
uv sync --frozen
pytest tests/ -v
mypy --strict mcp_bridge/ tests/
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

### Type safety

- **Lua**: Full LuaLS type annotations. Zero warnings under `lua-language-server --check --checklevel=Warning`.
- **Python**: Pydantic models throughout. Zero errors under `mypy --strict`.

## License

MIT
