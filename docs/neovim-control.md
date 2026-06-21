# Controlling Neovim from an agent

mcp-companion can expose your **live Neovim instance** to an AI agent as a set of
`neovim_*` MCP tools — so an agent (in a CodeCompanion chat, or an external agent like
Claude Code / OpenCode connected through the bridge) can read and edit your buffers, open
files, jump to diagnostics, and more, acting on the editor you're actually working in.

This is a built-in **native server** named `neovim`: the tools run as pure Lua inside your
editor, and the bridge routes an agent's tool calls back into the right instance over a
private channel.

## Enabling

It's on by default. The relevant config (defaults shown):

```lua
require("mcp_companion").setup({
  native_servers = {
    neovim = {
      enabled = true,       -- expose the neovim server
      -- expose_exec = false, -- opt in to run_command / exec_lua (see Safety)
    },
  },
})
```

To turn it off, set `enabled = false`. Nothing else is required — when an editor with the
plugin is running, the bridge advertises the `neovim_*` tools to every connected client.

## How an agent uses it

Once at least one editor is connected, the agent sees tools named `neovim_<tool>` (e.g.
`neovim_open_file`, `neovim_read_buffer`). Just ask the agent in natural language:

> use open_file to open ~/.zshrc, then read it

The agent picks the matching `neovim_*` tool and calls it; the change happens in your live
editor.

### Choosing which editor (multiple instances)

If you run **more than one** Neovim, the agent must know which to target. Every `neovim_*`
tool accepts an optional `nvim_instance` argument, and there's a discovery tool:

- **`neovim_list_instances`** — returns the connected editors with their `instance_id`,
  `cwd`, and `name`.
- The default, when `nvim_instance` is omitted, is **the editor that started the chat**.
  A CodeCompanion/ACP chat is automatically associated with its own editor, so you never
  need the argument there.
- A directly-configured agent (e.g. Claude Code pointed at the bridge on its own) has **no
  association** — it must pass `nvim_instance`. If it doesn't, the tool returns an error
  telling it to call `neovim_list_instances` and choose one. (With a single editor you can
  still just name it.)

## The tool catalog

Tools are grouped into **risk tiers**:

| Tier | Tools | Notes |
|------|-------|-------|
| **read** | `read_file`, `read_files`, `read_buffer`, `list_buffers`, `list_directory`, `find_files`, `get_diagnostics`, `get_cursor`, `get_selection` | observation only |
| **navigate** | `open_file`, `set_cursor`, `goto_diagnostic` | low-risk cursor/window moves |
| **write** | `edit_buffer`, `set_buffer_lines`, `write_file`, `save_buffer`, `move_item`, `delete_items` | mutates buffers/files |
| **exec** | `run_command`, `exec_lua` | arbitrary shell / Lua — **off by default** |

Edits are **buffer-oriented**: `edit_buffer` (SEARCH/REPLACE blocks) and `set_buffer_lines`
operate on the live buffer, so open, unsaved files don't get clobbered by disk writes.

There are also read-only **resources** an agent can pull for context:
`neovim://buffers`, `neovim://workspace`, `neovim://diagnostics/workspace`.

### Where things open (window placement)

Navigate/display tools (`open_file`, `goto_diagnostic`, `set_cursor`) and the default
buffer for `get_cursor`/read/edit tools target your **code window** — the first normal
window in the current tabpage — never the focused window, which during a chat is the
CodeCompanion buffer. So an agent calling `open_file` won't open over your chat. Tunable:

```lua
native_servers = {
  neovim = {
    window = {
      no_code_window = "tab",   -- if no code window exists: "tab"|"split"|"vsplit"|"replace"
      focus = "file",           -- after open/navigate: "file" (go to it) | "chat" (stay)
      reuse_visible = true,     -- reuse a window already showing the file
      ignore_filetypes = { "codecompanion", "neo-tree", ... },  -- what counts as "not code"
      ignore_buftypes = { "nofile", "prompt", "terminal", "quickfix", "help" },
    },
  },
}
```

## Safety & permissions

Who approves a tool call depends on how the agent is connected:

- **In a CodeCompanion chat (in-process):** approval follows the per-server
  **auto-approve spec** — the same spec proxied servers use. The `neovim` server defaults
  to `auto_approve = { "tier:read", "tier:navigate" }`, so reads/navigation auto-approve
  while writes/exec prompt (`vim.ui.select`). Customise it in setup:

  ```lua
  native_servers = {
    neovim = {
      enabled = true,
      -- tool-name globs and/or tier:<read|navigate|write|exec> aliases, or true/false/function
      auto_approve = { "tier:read", "tier:navigate", "edit_buffer" },
    },
  },
  ```

  (`auto_approve` here, plus the global `auto_approve`, are documented in the README's
  [Auto-approve spec](../README.md#auto-approve-spec).)
- **External ACP / CLI agents (Claude Code, OpenCode, Copilot):** the agent's **own** host
  approval governs tool calls — the bridge does not approve (standard MCP: the host gives
  consent, not the server). Configure permissions in the agent itself; this covers the
  `neovim_*` tools like any other. See
  [Approval for external agents](../README.md#approval-for-external-agents) for per-host
  setup and doc links.
- **The `exec` tier is opt-in.** `run_command` and `exec_lua` aren't even advertised unless
  you set `native_servers.neovim.expose_exec = true`. Leave it off unless you want the
  agent to run arbitrary shell/Lua in your editor.

You can also disable the whole `neovim` server for a single chat via the per-session server
gating (`/mcp-session`), the same as any other server.

## Extending it (plugin authors)

You can register **additional** native servers/tools from your own config or plugin:

```lua
local mcpc = require("mcp_companion")

mcpc.add_tool("myserver", {
  name = "current_branch",
  description = "Return the current git branch",
  tier = "read",
  inputSchema = { type = "object", properties = {} },
  handler = function(args, ctx)
    local util = require("mcp_companion.native.util")
    local branch = vim.fn.systemlist({ "git", "rev-parse", "--abbrev-ref", "HEAD" })[1]
    return util.text(branch or "(not a git repo)")
  end,
})
```

API: `add_server`, `add_tool`, `add_resource`, `add_resource_template`, `add_prompt`. A
handler receives `(args, ctx)` and returns an MCP result — use the helpers in
`mcp_companion.native.util` (`text`, `json`, `err`).

**Important — registration is setup-time and must be identical across instances.** The
bridge captures the tool catalog **once per bridge process** (from the first editor to
connect) and freezes it. So:

- Register your tools during `setup()`, before an editor connects to the bridge.
- Register the **same** tools in every editor that shares the bridge.

Tools added late, or that differ between instances, still work for **in-process**
CodeCompanion chats but won't appear in the bridge's advertised catalog for external
agents. (Divergent per-editor toolsets would need per-connection manifests, which aren't
implemented.)

## Troubleshooting

**The agent doesn't see any `neovim_*` tools.**
The bridge only advertises them once an editor has registered. Check:

1. A Neovim with the plugin is running and the bridge is connected (`:MCPStatus`).
2. In a fresh chat, ask the agent to call `neovim_list_instances` — your editor should
   appear with its `cwd`/`name`.
3. If you just restarted the bridge, the plugin re-registers automatically (it detects the
   restart via the bridge's boot id). An **already-open** agent may have cached an empty
   tool list — start a new chat, which re-lists.

**"this connection is not associated with a specific Neovim instance".**
You're using a directly-configured agent with multiple (or zero) editors connected. Call
`neovim_list_instances` and pass the chosen `instance_id` as `nvim_instance`. (Connecting
through CodeCompanion binds your editor automatically, avoiding this.)

**An edit didn't land where expected.**
The default buffer is the one in your code window (the first non-chat/tree/terminal window).
With multiple code splits it picks the first — prefer passing an explicit `buffer` id, or
`open_file` first. See *Where things open* above to tune what counts as a code window.

For the architecture behind all this (the back-channel, instance/token association, restart
recovery), see the design record in the repository (`docs/designs/native-neovim-server.md`).
