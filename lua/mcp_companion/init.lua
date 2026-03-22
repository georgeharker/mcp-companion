--- mcp-companion.nvim — Plugin entry point
--- @module mcp_companion

local M = {}

--- @type boolean
local _setup_done = false

--- Setup the plugin
--- @param opts? table User configuration
function M.setup(opts)
  if _setup_done then
    return
  end

  local config = require("mcp_companion.config")
  local issues = config.setup(opts or {})

  local state = require("mcp_companion.state")
  state.reset()
  state.update("setup_state", "in_progress")

  local log = require("mcp_companion.log")
  log.setup(config.get().log)

  -- Report config issues
  if #issues > 0 then
    for _, issue in ipairs(issues) do
      log.warn("Config: %s", issue)
      state.add_error("Config: " .. issue)
    end
  end

  -- Check for config file — warn but don't block (bridge will error later)
  if not config.get().bridge.config then
    log.warn("No servers.json found. Create one or set bridge.config in setup()")
  end

  -- Initialize native servers
  local native = require("mcp_companion.native")
  native.setup(config.get())

  -- Setup bridge lifecycle
  local bridge = require("mcp_companion.bridge")
  bridge.setup(config.get())

  -- NOTE: CC extension registration is handled via CC's extensions config:
  --   extensions = { mcp_companion = { callback = "mcp_companion.cc", opts = {...} } }
  -- We do NOT call cc.register_extension() here — CC calls M.init(schema) on our module.

  -- Autocmds
  local group = vim.api.nvim_create_augroup("MCPCompanion", { clear = true })
  vim.api.nvim_create_autocmd("VimLeavePre", {
    group = group,
    callback = function()
      bridge.stop()
    end,
  })

  -- Start bridge lazily on first CodeCompanion chat (only if config file exists)
  if config.get().bridge.config then
    vim.api.nvim_create_autocmd("User", {
      group = group,
      pattern = "CodeCompanionChatCreated",
      once = true,
      callback = function()
        bridge.start()
      end,
    })
  end

  -- User commands
  vim.api.nvim_create_user_command("MCPStatus", function()
    local ui = require("mcp_companion.ui")
    ui.toggle()
  end, { desc = "Toggle MCP Companion status window" })

  vim.api.nvim_create_user_command("MCPRestart", function()
    bridge.restart()
  end, { desc = "Restart MCP bridge" })

  vim.api.nvim_create_user_command("MCPLog", function()
    local log_path = log.get_log_path()
    if log_path then
      vim.cmd("edit " .. vim.fn.fnameescape(log_path))
    else
      vim.notify("[mcp-companion] File logging not enabled", vim.log.levels.WARN)
    end
  end, { desc = "Open MCP Companion log file" })

  state.update("setup_state", "completed")
  _setup_done = true

  -- Register on_ready callback
  if config.get().on_ready then
    state.on("bridge_ready", function()
      config.get().on_ready(bridge)
    end)
  end

  -- Register on_error callback
  if config.get().on_error then
    state.on("bridge_error", function(err)
      config.get().on_error(err)
    end)
  end

  log.info("Setup complete (config: %s)", config.get().bridge.config or "none")
end

--- Get current state module
--- @return table
function M.get_state()
  return require("mcp_companion.state")
end

--- Get bridge module (lifecycle + client)
--- @return table
function M.get_bridge()
  return require("mcp_companion.bridge")
end

--- Alias for compatibility
--- @return table
function M.get_hub_instance()
  return require("mcp_companion.bridge")
end

--- Subscribe to events
--- @param event string Event name
--- @param callback function Handler
--- @return function unsubscribe
function M.on(event, callback)
  return require("mcp_companion.state").on(event, callback)
end

--- Unsubscribe from events
--- @param event string
--- @param callback function
function M.off(event, callback)
  require("mcp_companion.state").off(event, callback)
end

-- Re-export native server public API
M.add_server = function(...)
  return require("mcp_companion.native").add_server(...)
end
M.add_tool = function(...)
  return require("mcp_companion.native").add_tool(...)
end
M.add_resource = function(...)
  return require("mcp_companion.native").add_resource(...)
end
M.add_resource_template = function(...)
  return require("mcp_companion.native").add_resource_template(...)
end
M.add_prompt = function(...)
  return require("mcp_companion.native").add_prompt(...)
end

return M
