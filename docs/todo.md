# TODO

Outstanding work for mcp-companion.nvim, in priority order.

## Feature: E2E test suite (M11)

No automated end-to-end tests. Current tests:
- `test_cc_tools.lua`: CC tool registration against test bridge on port 9742
- `test_real_servers.lua`: integration test against production bridge on port 9741

Needs a proper E2E suite that can run in CI without a live Neovim instance, covering:
- Bridge lifecycle (start, connect, poll, stop)
- CC tool registration and callback execution
- ACP session injection (mock CC ACP Connection)

## Feature: MCP resources → CC variables (cc/variables.lua)

`cc/variables.lua` is a stub. MCP resources should be registered as CodeCompanion
`#variable` completions so users can insert resource content into chat with `#resource_name`.

CC variables API: `config.strategies.chat.variables[name] = { callback = fn, description }`.

Files: `cc/variables.lua`.

## Feature: MCP prompts → CC slash commands (cc/slash_commands.lua)

`cc/slash_commands.lua` is a stub. MCP prompts should be registered as CodeCompanion
`/slash_command` completions.

CC slash commands API: `config.strategies.chat.slash_commands[name] = { callback = fn, description }`.

Files: `cc/slash_commands.lua`.

## TODO: Native Lua MCP server registration (M9)

`native/init.lua` is a stub. The original plan included an API for registering MCP
servers, tools, resources, and prompts directly from Lua without going through the bridge.

Given that all real use cases go through the bridge (and the bridge handles arbitrary
MCP servers), this is low priority. The API surface is preserved but unimplemented.

If implemented, it would allow plugins to register tools directly:
```lua
require("mcp_companion").add_tool({
    name = "my_tool",
    description = "...",
    inputSchema = { ... },
    execute = function(args) return "result" end,
})
```

Files: `native/init.lua`, `init.lua` (wire up public API).

## Closed

- **M0** — Scaffold: all files, Lua modules load
- **M1** — Python bridge: FastMCP proxy, health endpoint, 19 test tools, 6 pytest passing
- **M3** — Lua config/state/log: fully implemented, 17 tests passing
- **M5** — Lua MCP HTTP client: vim.uv TCP, multi-session, 3/3 passing
- **M6** — CC tool registration: direct CC tools API, fingerprint dedup, 32/32 tests passing
- **M10** — Status UI: `:MCPStatus` / `:MCPRestart` / `:MCPLog` commands, floating window
  with server expand/collapse, live state updates, logs view
- **Fix** — Repeated re-registration: removed individual `tool_list_changed` /
  `resource_list_changed` / `prompt_list_changed` emits from `refresh_capabilities()`;
  only `servers_updated` fires once after all lists are fetched
- **Approval** — Tool approval flow: `vim.ui.select` prompt with global/per-server/
  per-tool auto_approve config; wired into `cc/tools.lua` execute callback
- **M12** — ACP forwarding: monkey-patch `Connection:_establish_session`, HTTP transport,
  confirmed working with OpenCode using tools in a real chat session
