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

When using ACP adapters (OpenCode, Claude Code) through CodeCompanion, no
manual configuration is needed — the bridge is automatically injected into
the agent's session.

Any MCP client that supports HTTP transport can also connect directly for
standalone use outside CodeCompanion:

```bash
# Direct HTTP access (standalone / debugging)
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
injected into the ACP session via `session/new`. The agent connects to the
bridge directly over HTTP (or via `mcp-remote` stdio fallback) and can call
all MCP tools autonomously without extra configuration.

The injection adapts to however the adapter's `mcpServers` is configured in
CodeCompanion:

| `defaults.mcpServers` value | How bridge is injected |
|---|---|
| `"inherit_from_config"` | CC calls `transform_to_acp()` to build the server list from `config.mcp.servers`. Our patch wraps that function to also include HTTP servers (upstream only handles stdio) and appends the bridge entry. |
| `{}` (empty table) | Bridge entry is inserted directly into the table during `ACPSessionPre`, before `_establish_session` reads it. |
| `{ ... }` (table with entries) | Same as empty table — bridge entry is appended if not already present. User-configured servers are preserved. |

Most ACP adapters ship with `defaults.mcpServers = {}`. Some (e.g. Copilot ACP)
use `"inherit_from_config"` to pick up servers from the global CC MCP config.
Both paths are handled automatically — no adapter-specific configuration is
needed.

> **Note:** Upstream `transform_to_acp()` only translates stdio-type servers.
> Our patch also translates HTTP servers from `config.mcp.servers` that are
> listed in `config.mcp.opts.default_servers`, so they are not silently dropped
> for ACP agents.

Individual servers can be hidden from the agent for the current chat session
using `/mcp-session` — see [Per-session server gating](#per-session-server-gating).

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
- `bridge__enable_server` / `bridge__disable_server` — toggle servers globally (all sessions)
- `bridge__session_disable_server` / `bridge__session_enable_server` — toggle a server for the calling session only

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
| `:MCPToggleServer <name>` | Globally enable/disable a server |
| `:MCPSaveProjectConfig [shortest\|allowed\|disabled]` | Snapshot the current chat session's MCP server visibility to `.mcp-companion.json` (see [Per-project defaults](#per-project-defaults-mcp-companionjson)) |

```lua
vim.keymap.set("n", "<leader>ms", "<cmd>MCPStatus<cr>", { desc = "MCP status" })
```

The status window shows bridge state, connected servers, and tool/resource/prompt
counts. Key bindings:

| Key | Action |
|---|---|
| `<CR>` | Expand/collapse the server under the cursor |
| `e` | Toggle **global** enable/disable (calls `bridge__enable_server` / `bridge__disable_server`) |
| `p` | Toggle the server's visibility in `.mcp-companion.json` (creates the file if absent; preserves the existing `allowed_servers` / `disabled_servers` shape) |
| `S` | Toggle the server for **this chat session only** — equivalent to `/mcp-session` on the chat the status window was opened from |
| `r` | Refresh from the bridge |
| `R` | Restart the bridge |
| `l` / `s` | Switch to logs / status view |
| `q` | Close the window |

The three toggle keys (`e`, `p`, `S`) form a hierarchy from broadest to
narrowest scope:

- `e` — global, persists in the bridge for every session.
- `p` — per-project, persists across Neovim restarts via `.mcp-companion.json`.
- `S` — per-chat, lives only as long as the chat session.

A server hidden by the project file shows `[project off]`; one hidden by a
session toggle shows `[session off]`. The same key that hid it (`p` or `S`)
restores it. `S` requires `:MCPStatus` to have been opened from a
CodeCompanion chat buffer (so it knows which chat to scope the toggle to).

### Logging

MCP companion writes logs to two locations:

| Log | Default path | Purpose |
|---|---|---|
| Plugin log | `~/.local/state/nvim/mcp-companion.log` | Lua-side events (bridge lifecycle, server connections, errors) |
| Bridge file log | `~/.local/state/nvim/mcp-bridge-py.log` | Python file logger (formatted, level set by `bridge.log_level`) |
| Bridge stderr capture | `~/.local/state/nvim/mcp-bridge.log` | sharedserver-captured stderr from the Python bridge process |
| sharedserver logs | `$XDG_RUNTIME_DIR/sharedserver` or `/tmp/sharedserver` | All processes managed by sharedserver |

Use `:MCPLog` to open the plugin log directly in a Neovim buffer.

The bridge file log is enabled by default. Configure it via `bridge.log` —
same shape as the top-level `log` table:

```lua
require("mcp_companion").setup({
  log = { level = "warn", file = true },        -- top-level (Lua side)
  bridge = {
    log = {
      level = "debug",                          -- trace | debug | info (default) | warn | error
      file = "/path/to/mcp-bridge.log",         -- true (default path), string (explicit), false (disabled)
    },
  },
})
```

Defaults are `level = "info"` and `file = true` (resolves to
`stdpath("log")/mcp-bridge-py.log`). At `level = "debug"` the upstream
`httpx`, `httpcore`, `mcp.client.auth`, and `fastmcp.client.auth` loggers
also flip to DEBUG so refresh requests, metadata-discovery URLs, and HTTP
request/response detail are captured. Restart the bridge after changing
either setting (`:MCPRestart!`).

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
        log = {
          level = "info",               -- "trace" | "debug" | "info" | "warn" | "error"
          file = true,                  -- true = default path, string = explicit path, false = disabled
        },
        token_in_url = false,           -- embed session token in URL path; see Troubleshooting below
        global_env = {},                -- extra environment variables passed to the bridge process
    },
    log = {
        level = "warn",                 -- file log level: "debug", "info", "warn", "error"
        notify = "error",               -- vim.notify level (default: errors only)
        file = true,                    -- write to ~/.local/state/nvim/mcp-companion.log
    },
    auto_approve = false,               -- true, false, or function(tool, server, ctx) -> bool
    system_prompt_resources = nil,       -- true (all), or {"pattern1", "pattern2"} to match
    cc = {
        -- Controls which MCP tool groups are added to new chats automatically.
        -- true (default): add the aggregate @mcp-bridge group (all servers, one context entry)
        -- false: do not auto-add; user manually @-mentions groups in each chat
        -- string[]: add only the named per-server groups, e.g. {"github", "filesystem"}
        auto_http_tools = true,
        -- true (default): inject bridge as MCP server into ACP agent sessions
        -- false or {}: inject bridge but disable all servers by default (use /mcp-session to enable)
        -- string[]: inject bridge but only expose the named servers, e.g. {"github"}
        auto_acp_tools = true,
        -- Whether to add per-tool natural-language system messages alongside the tools array.
        -- true (default): helps models that ignore JSON-Schema descriptions.
        -- false: saves tokens (descriptions duplicate the schema's `description` fields).
        -- Overridden per-project by .mcp-companion.json.
        tool_system_prompts = true,
        -- Normalize tool JSON schemas to fix providers (e.g. moonshot-ai/kimi) that reject
        -- schemas where `type` and `anyOf` coexist at the same level with a 400 error.
        -- The transformation is semantically equivalent and accepted by lenient validators.
        -- Passed to the bridge as --normalize-schema; applies at cache-fill time. Default false.
        normalize_schema = false,
        -- Per-adapter overrides for auto_http_tools and auto_acp_tools.
        -- Keys are adapter names as returned by chat.adapter.name (e.g. "moonshot-ai", "claude").
        -- Values override the corresponding top-level setting for sessions using that adapter.
        -- Further overridden per-project by .mcp-companion.json#/adapters/<name>.
        adapters = {
            -- ["moonshot-ai"] = { auto_http_tools = { "github" }, auto_acp_tools = { "github" } },
        },
    },
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

#### MCP tool group addressing (non-ACP chats)

When using a standard HTTP/LLM adapter (not ACP), MCP tools are available via
`@`-mention in CodeCompanion chats. Two levels of granularity are supported:

| Mention | Effect |
|---|---|
| `@mcp-bridge` | Enable **all** MCP tools from all connected servers (one context block entry) |
| `@mcp__github` | Enable tools from a single server only (replace `github` with any server name) |

With `cc.auto_http_tools = true` (the default), `@mcp-bridge` is added
automatically to every new chat and all servers are enabled on the bridge for
that session. With `false`, no tool groups are added and all servers are
disabled on the bridge — use `/mcp-session` or type `@mcp__<server>` manually
to enable tools on demand.

```lua
-- Default: all servers enabled automatically as a single group
cc = { auto_http_tools = true }

-- Opt-in only: type @mcp-bridge or @mcp__github manually in each chat
cc = { auto_http_tools = false }

-- Selective: auto-enable specific servers only
cc = { auto_http_tools = { "github", "filesystem" } }
```

You can also hide individual servers mid-conversation with `/mcp-session` —
see [Per-session server gating](#per-session-server-gating) below — or commit
per-project defaults to a `.mcp-companion.json` file (see
[Per-project defaults](#per-project-defaults-mcp-companionjson) below).

#### MCP tool availability in ACP chats

When using an ACP adapter (OpenCode, Claude Code, Cline), the bridge is
injected as a single MCP server entry into the agent's `session/new` call.
The agent discovers tools directly from the bridge — `@`-mention and tool
groups are not used.

`cc.auto_acp_tools` controls whether and which servers the bridge exposes to ACP
agents:

```lua
cc = {
  auto_acp_tools = true,                          -- (default) all servers visible
  auto_acp_tools = false,                         -- bridge injected, but no servers enabled by default
  auto_acp_tools = {},                            -- same as false
  auto_acp_tools = { "github", "filesystem" },    -- only these servers visible
}
```

When `auto_acp_tools` is `false`, `{}`, or a list, the bridge is still injected but unlisted
servers are automatically session-disabled for the ACP agent's bridge
connection once it is established. The filter is applied via the bridge's
REST session API and cleaned up when the chat closes.

**Per-session server gating** allows selectively hiding individual upstream
MCP servers from the ACP agent mid-conversation, without affecting other open
chats — see [Per-session server gating](#per-session-server-gating) below.
Per-project defaults can also be checked into a `.mcp-companion.json` file
(see [Per-project defaults](#per-project-defaults-mcp-companionjson)).

#### Per-session server gating

`/mcp-session` lets you hide or restore individual MCP servers for the current
chat session only. It works identically with both ACP and non-ACP (HTTP/LLM)
adapters:

```
/mcp-session
```

A picker lists all connected servers with their current session status
(`[ON]` / `[OFF]`). Selecting a server toggles it for the current chat only.

**What happens on toggle:**

- **ACP chats** — the agent receives a `notifications/tools/list_changed`
  signal and sees the updated tool list immediately.
- **Non-ACP chats** — the server's tool group is removed from (or re-added
  to) the CC tool registry and context block. Tools from a hidden server
  disappear from `@`-mention suggestions and the LLM's available tools.
- **`:MCPStatus`** — shows `[session off]` next to servers hidden in the
  currently focused chat.

When the chat session ends the state is automatically cleaned up.

##### Per-project defaults (`.mcp-companion.json`)

The global `cc.auto_http_tools` / `cc.auto_acp_tools` settings can be overridden
per-project by dropping a `.mcp-companion.json` file at (or above) the
project's working directory. When a new chat starts, the plugin walks upward
from `vim.fn.getcwd()` looking for this file; if found, it controls which
servers the chat session sees, regardless of the global default.

The intended workflow is "default off, opt in per project": set
`auto_http_tools = false` (and/or `auto_acp_tools = false`) globally, then
list the servers each project actually needs.

```json
{
  "$schema": "https://geohar.github.io/mcp-companion/project.schema.json",
  "allowed_servers": ["github", "gws"]
}
```

Or hide specific servers from an otherwise-default project:

```json
{
  "$schema": "https://geohar.github.io/mcp-companion/project.schema.json",
  "disabled_servers": ["clickup"]
}
```

| Field | Type | Effect |
|---|---|---|
| `allowed_servers` | `string[]` | Whitelist — only these servers are visible. |
| `disabled_servers` | `string[]` | Blacklist — every other configured server is visible. |
| `tool_system_prompts` | `boolean` | Override the plugin-level `cc.tool_system_prompts` setting (default `true`). Set `false` here to suppress per-tool natural-language system messages just for this project. |
| `adapters` | `object` | Per-adapter server filter overrides. Keys are adapter names (e.g. `"moonshot-ai"`, `"claude"`). Each value is an object with the same `allowed_servers` / `disabled_servers` shape as the top level, and overrides the top-level filter for sessions using that adapter. Useful when different models need to see different server subsets within the same project. |

Example with per-adapter overrides:

```json
{
  "$schema": "https://geohar.github.io/mcp-companion/project.schema.json",
  "allowed_servers": ["github", "gws"],
  "adapters": {
    "moonshot-ai": {
      "allowed_servers": ["github"]
    }
  }
}
```

The two server-list fields are mutually exclusive. Server names must match entries in
your `servers.json` / `mcpServers` config; unknown names are dropped with a
warning, so a stale project file never breaks chat creation. Malformed JSON
or schema violations log a warning and fall back to the global
`auto_*_tools` setting — they don't lock you out of MCP tools.

The file is re-read every time a chat session starts; no Neovim reload is
needed when you edit it. The schema is published at
[`docs/schemas/project.schema.json`](docs/schemas/project.schema.json) for
editor autocomplete (e.g., VSCode `json.schemas`, Neovim `jsonls`).

###### Saving from current session state

If you've reached the right per-project setup with `/mcp-session` toggles,
two surfaces snapshot it back to disk:

- `:MCPSaveProjectConfig [shortest|allowed|disabled]` — works from any
  buffer; resolves the active chat automatically.
- `/mcp-session-save` — slash command in a CodeCompanion chat; prompts for
  the format.

The default `shortest` writes whichever list (`allowed_servers` or
`disabled_servers`) is smaller, with a tie going to `allowed_servers` to
match the documented "default off, opt in per project" workflow. `allowed`
or `disabled` force a specific shape.

The save target is the existing `.mcp-companion.json` walked up from cwd if
one exists (so a save updates the same file the chat is already reading);
otherwise the file is created at `cwd/.mcp-companion.json`. If the existing
file would be overwritten with different contents, the command prompts
before writing.

##### How it works

Filtering is enforced at two layers:

1. **Bridge-side** (source of truth) — each chat gets a unique session token.
   When you toggle a server, the plugin calls the bridge's REST filter API
   (`/sessions/token/<token>/filter`) which controls which servers the session
   can execute tools on. This prevents tool calls from reaching a disabled
   server regardless of what the client sends.

2. **Neovim-side** (mirrors bridge state) — for non-ACP chats, the plugin also
   adds or removes tool groups from the CC `tool_registry` so the LLM's
   available tools stay in sync. For ACP chats, the bridge sends a
   `notifications/tools/list_changed` notification and the agent re-fetches
   tools directly.

The initial filter for a new chat is derived in this order of precedence:

1. **`.mcp-companion.json`** found by walking up from the cwd
   (see [Per-project defaults](#per-project-defaults-mcp-companionjson) above)
2. **`cc.auto_http_tools`** (or `cc.auto_acp_tools` for ACP chats):
   - `false` → all servers disabled on the bridge for that session
   - `{"github"}` → only `github` enabled; all others disabled
   - `true` → no filter; all servers enabled

##### Bridge meta-tools

The underlying bridge tools are callable by the agent directly (e.g., in an
ACP session where the agent has autonomous tool access):

**`bridge__session_disable_server`** — hide a server from this session

| Parameter | Type | Description |
|---|---|---|
| `server_name` | `string` (required) | Name of the server to disable |
| `chat_id` | `string` (optional) | Chat identifier for per-chat filtering when multiple chats share one MCP connection |

Returns JSON: `{ "session_id": "...", "action": "disabled", "server": "...", "disabled_servers": [...] }`

**`bridge__session_enable_server`** — restore a hidden server for this session

| Parameter | Type | Description |
|---|---|---|
| `server_name` | `string` (required) | Name of the server to re-enable |
| `chat_id` | `string` (optional) | Same as above |

Returns JSON: `{ "session_id": "...", "action": "enabled", "server": "...", "disabled_servers": [...] }`

**`bridge__session_status`** — get the current session's disabled server list

| Parameter | Type | Description |
|---|---|---|
| `chat_id` | `string` (optional) | Same as above |

Returns JSON: `{ "session_id": "...", "disabled_servers": [...] }`

These complement the global `bridge__enable_server` / `bridge__disable_server`
tools, which affect all sessions simultaneously.

##### Example workflow

```
1. Open a chat with auto_http_tools = { "github" }
   → only github tools available; other servers disabled on the bridge

2. Mid-conversation, run /mcp-session
   → picker shows:  [ ON] github   [OFF] todoist   [OFF] filesystem

3. Select todoist to toggle it ON
   → bridge enables todoist for this session
   → todoist tools appear in tool suggestions
   → other chats are unaffected

4. Close the chat
   → session filter is automatically cleaned up on the bridge
```

---

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

## Troubleshooting

### ACP agent cannot call MCP tools (per-chat session not established)

Per-chat sessions rely on the `X-MCP-Bridge-Session` header to map a token to
an MCP session on the bridge. The ACP spec requires HTTP MCP transports to
forward custom headers, but some agent SDKs may strip them.

**Symptom:** Tools fail or the bridge logs show no `Token mapped` entry for
the agent's session.

**Fix:** Enable the URL-path fallback so the token is embedded in the URL
itself:

```lua
bridge = {
    token_in_url = true,
}
```

If you need this workaround, please open an issue at
<https://github.com/geohar/mcp-companion/issues> with the name and version of
the ACP agent so we can track which SDKs need it.

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
