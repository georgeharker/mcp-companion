--- mcp-companion.nvim — Auto-approval logic
--- Determines whether a tool call should proceed immediately or require
--- user confirmation before execution.
---
--- Approval chain:
---   1. Global auto_approve (boolean or function)
---   2. Native servers auto-approve by default
---   3. Per-server autoApprove from bridge config
---   4. Prompt user via vim.ui.select
--- @module mcp_companion.cc.approval

local M = {}

--- Check if a tool call should be auto-approved.
--- If not auto-approved, prompts the user via vim.ui.select.
--- @param server_name string Server that owns the tool
--- @param tool_name string Tool being called
--- @param tool_ctx table CC tool context (self from handler)
--- @param callback fun(approved: boolean) Called with result
function M.check(server_name, tool_name, tool_ctx, callback)
  local config = require("mcp_companion.config").get()

  -- 1. Global auto_approve
  if config.auto_approve == true then
    return callback(true)
  end

  if type(config.auto_approve) == "function" then
    local fn = config.auto_approve --[[@as fun(tool_name: string, server_name: string, tool_ctx: table): boolean]]
    local ok, result = pcall(fn, tool_name, server_name, tool_ctx)
    if ok then
      return callback(result)
    end
    -- If the function errors, fall through to other checks
  end

  -- 2. Native servers auto-approve by default (they run in-process)
  local native_ok, native = pcall(require, "mcp_companion.native")
  if native_ok and native.is_native_server(server_name) then
    return callback(true)
  end

  -- 3. Per-server autoApprove patterns from state
  local state = require("mcp_companion.state")
  local servers = state.field("servers") or {}
  for _, srv in ipairs(servers) do
    if srv.name == server_name and srv.auto_approve then
      if type(srv.auto_approve) == "table" then
        for _, pattern in ipairs(srv.auto_approve) do
          if tool_name:match(pattern) then
            return callback(true)
          end
        end
      elseif srv.auto_approve == true then
        return callback(true)
      end
    end
  end

  -- 4. Prompt user
  vim.schedule(function()
    vim.ui.select(
      { "Allow", "Deny" },
      {
        prompt = string.format("MCP tool call: %s/%s", server_name, tool_name),
        kind = "mcp_approval",
      },
      function(choice)
        callback(choice == "Allow")
      end
    )
  end)
end

return M
