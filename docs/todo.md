# TODO

Outstanding work for mcp-companion.nvim, in priority order.

## Fix: Repeated re-registration on first connect

On first connect, `tool_list_changed`, `resource_list_changed`, and `prompt_list_changed`
events fire before `servers_updated`, causing three re-registration attempts. The fingerprint
cache in `cc/tools.lua` deduplicates them, but the extra work is unnecessary.

The fix is to suppress capability-change events during the initial connect sequence and
only emit `bridge_ready` + `servers_updated` once all lists are populated.

Files: `bridge/client.lua` (event emission), `cc/init.lua` (event subscription).

## Feature: Tool approval flow (cc/approval.lua)

`cc/approval.lua` is a stub. Currently all tool calls execute without confirmation.

The approval flow should:
- Check `config.auto_approve` (bool or function)
- If not auto-approved, prompt the user (floating window or `vim.ui.select`)
- Allow per-server and per-tool approval rules
- Integrate with the CC tool callback (`execute` function in `cc/tools.lua`)

Files: `cc/approval.lua`, `cc/tools.lua` (integrate approval into execute callback).

## Feature: Status UI (ui/init.lua → command)

`ui/init.lua` has a working floating window showing bridge status, servers, and tool
counts. It is not accessible from Neovim.

Needs:
- A `:MpcCompanionStatus` user command (or similar)
- Optionally a keybind in the default config
- Auto-refresh on `servers_updated` event

Files: `ui/init.lua`, `init.lua` (register command).

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
- **M12** — ACP forwarding: monkey-patch `Connection:_establish_session`, HTTP transport,
  confirmed working with OpenCode using tools in a real chat session
