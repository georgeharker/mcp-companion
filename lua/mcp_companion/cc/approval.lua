--- mcp-companion.nvim — Auto-approval logic
--- Determines whether a tool call should proceed immediately or require
--- user confirmation before execution.
---
--- Approval chain:
---   1. Global auto_approve (boolean or function)
---   2. Native servers auto-approve by default
---   3. Per-server autoApprove from bridge config (TODO)
---   4. Default: approve (TODO: implement confirmation UI)
--- @module mcp_companion.cc.approval

local M = {}

--- Check if a tool call should be auto-approved.
--- @param server_name string Server that owns the tool
--- @param tool_name string Tool being called
--- @param tool_ctx table CC tool context (self from handler)
--- @return boolean approved True to proceed, false to deny
function M.check(server_name, tool_name, tool_ctx)
  local config = require("mcp_companion.config").get()

  -- 1. Global auto_approve
  if config.auto_approve == true then
    return true
  end

  if type(config.auto_approve) == "function" then
    local ok, result = pcall(config.auto_approve, tool_name, server_name, tool_ctx)
    if ok then
      return result
    end
    -- If the function errors, fall through to other checks
  end

  -- 2. Native servers auto-approve by default (they run in-process)
  local native_ok, native = pcall(require, "mcp_companion.native")
  if native_ok and native.is_native_server(server_name) then
    return true
  end

  -- 3. Per-server autoApprove patterns from bridge config
  -- TODO: Query bridge__status for server autoApprove lists
  -- For now, check state.servers for any server-level auto_approve flag
  local state = require("mcp_companion.state")
  local servers = state.field("servers") or {}
  for _, srv in ipairs(servers) do
    if srv.name == server_name and srv.auto_approve then
      if type(srv.auto_approve) == "table" then
        -- Check if tool_name matches any pattern in the auto_approve list
        for _, pattern in ipairs(srv.auto_approve) do
          if tool_name:match(pattern) then
            return true
          end
        end
      elseif srv.auto_approve == true then
        return true
      end
    end
  end

  -- 4. Default: approve for now
  -- TODO: Implement confirmation UI (vim.ui.select / floating window)
  -- When implemented, this should show a confirmation dialog and return
  -- the user's choice. For now, auto-approve everything.
  return true
end

return M
