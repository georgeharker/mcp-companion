--- mcp-companion.nvim — CC Extension entry point
--- Bridges MCP capabilities into CodeCompanion:
---   - MCP tools → CC tools (function calling)
---   - MCP resources → CC #editor_context entries
---   - MCP prompts → CC / slash commands
---
--- Registered via CodeCompanion.register_extension("mcp_companion", M)
--- @module mcp_companion.cc

local M = {}

local log = require("mcp_companion.log")

--- Called by CodeCompanion when the extension is loaded.
--- Sets up event listeners that trigger (re)registration when the bridge
--- connects or capabilities change.
--- Also patches ACP to inject bridge as MCP server for ACP agents.
--- @param schema? table Extension schema from CC config
function M.setup(schema)
  local state = require("mcp_companion.state")

  -- Start bridge when any chat adapter is created.
  -- Currently blocks briefly to ensure tools are registered before first submit.
  -- TODO: For HTTP adapters, could use on_before_submit callback instead to
  -- avoid blocking chat UI. ACP must block in _establish_session regardless
  -- since session/new requires MCP server list upfront.
  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionChatAdapter",
    callback = function()
      M._wait_for_bridge(5000)
    end,
  })

  -- Auto-enable MCP tool groups when chat is created
  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionChatCreated",
    callback = function(args)
      M._auto_enable_tools(args.data)
    end,
  })

  -- Patch ACP to inject bridge into session/new (blocks there if needed)
  M._patch_acp()

  -- When bridge connects and capabilities are populated, register everything
  state.on("bridge_ready", function()
    log.debug("CC extension: bridge_ready — registering all")
    M._register_all()
  end)

  -- Re-register when servers change
  state.on("servers_updated", function()
    log.debug("CC extension: servers_updated — re-registering all")
    M._register_all()
  end)

  log.info("CC extension initialized")
end

--- Auto-enable MCP tool groups in a newly created chat.
--- Called on ChatCreated event to add our server groups to the chat's tool registry.
--- @param event_data table Event data with bufnr and id
function M._auto_enable_tools(event_data)
  if not event_data or not event_data.bufnr then
    return
  end

  local state = require("mcp_companion.state")
  if state.get().bridge.status ~= "connected" then
    log.debug("CC: bridge not connected, skipping auto-enable")
    return
  end

  -- Get the chat instance via bufnr
  local cc_ok, codecompanion = pcall(require, "codecompanion")
  if not cc_ok then return end

  local chat = codecompanion.buf_get_chat(event_data.bufnr)
  if not chat or not chat.tool_registry then
    log.debug("CC: chat or tool_registry not found for bufnr %s", event_data.bufnr)
    return
  end

  -- Get our registered servers and add their tool groups
  local mcp_ok, cc_mcp = pcall(require, "codecompanion.mcp")
  if not mcp_ok then return end

  local servers = state.field("servers") or {}
  local enabled_count = 0

  for _, server in ipairs(servers) do
    if server.name ~= "_bridge" then
      local group_name = cc_mcp.tool_prefix() .. server.name
      -- Refresh tools config and add the group
      chat.tools:refresh({ adapter = chat.adapter })
      chat.tool_registry:add(group_name, { config = chat.tools.tools_config })
      enabled_count = enabled_count + 1
    end
  end

  if enabled_count > 0 then
    log.info("CC: auto-enabled %d MCP server tool groups", enabled_count)
  end
end

--- Start bridge asynchronously (non-blocking).
--- Called on ChatAdapter event so bridge starts warming up while UI loads.
function M._start_bridge_async()
  local state = require("mcp_companion.state")
  local config = require("mcp_companion.config")

  -- Already connected or connecting
  local bridge_status = state.get().bridge.status
  if bridge_status == "connected" or bridge_status == "connecting" then
    return
  end

  -- No bridge config
  if not config.get().bridge.config then
    log.debug("CC: no bridge config, skipping bridge start")
    return
  end

  log.info("CC: starting bridge async on ChatAdapter event")
  require("mcp_companion.bridge").start()
end

--- Wait for bridge to be ready (blocking).
--- Call this before operations that need MCP tools.
--- @param timeout_ms? number Maximum time to wait (default 5000)
--- @return boolean success Whether bridge is ready
function M._wait_for_bridge(timeout_ms)
  timeout_ms = timeout_ms or 5000
  local state = require("mcp_companion.state")

  -- Already connected
  if state.get().bridge.status == "connected" then
    return true
  end

  -- Not even started - start it now
  if state.get().bridge.status ~= "connecting" then
    M._start_bridge_async()
  end

  -- Wait for bridge to become healthy
  local ok = vim.wait(timeout_ms, function()
    return state.get().bridge.status == "connected"
  end, 50)

  if ok then
    log.info("CC: bridge ready")
  else
    log.warn("CC: bridge did not become ready in %dms", timeout_ms)
  end

  return ok
end

function M._register_all()
  M._register_tools()
  M._register_editor_context()
  M._register_prompts()
end

function M._register_tools()
  local ok, tools = pcall(require, "mcp_companion.cc.tools")
  if ok then
    tools.register()
  else
    log.warn("Failed to load cc.tools: %s", tostring(tools))
  end
end

function M._register_editor_context()
  local ok, editor_ctx = pcall(require, "mcp_companion.cc.editor_context")
  if ok then
    editor_ctx.register()
  else
    log.warn("Failed to load cc.editor_context: %s", tostring(editor_ctx))
  end
end

function M._register_prompts()
  local ok, cmds = pcall(require, "mcp_companion.cc.slash_commands")
  if ok then
    cmds.register()
  else
    log.warn("Failed to load cc.slash_commands: %s", tostring(cmds))
  end
end

--- Build bridge MCP server entry for ACP session/new
--- @param conn table ACP Connection instance (has _agent_info after initialize)
--- @return table|nil bridge_entry MCP server entry or nil if bridge not ready
local function build_bridge_entry(conn)
  local state = require("mcp_companion.state")
  local bridge_state = state.get().bridge

  if bridge_state.status ~= "connected" then
    return nil
  end

  local bridge_url = string.format("http://%s:%d/mcp",
    bridge_state.host or "127.0.0.1",
    bridge_state.port or 9741)

  -- Check if agent supports HTTP MCP transport
  local caps = conn._agent_info
      and conn._agent_info.agentCapabilities
      and conn._agent_info.agentCapabilities.mcpCapabilities

  if caps and caps.http then
    log.debug("CC ACP: using HTTP transport for bridge")
    return {
      type = "http",
      name = "mcp-bridge",
      url = bridge_url,
      headers = {},
    }
  else
    -- Fallback: stdio via mcp-remote
    log.debug("CC ACP: using stdio mcp-remote transport for bridge")
    return {
      name = "mcp-bridge",
      command = "npx",
      args = { "-y", "mcp-remote", bridge_url },
      env = {},
    }
  end
end

--- Patch ACP Connection to:
--- 1. Start bridge before connect_and_initialize
--- 2. Inject bridge into session/new mcpServers
function M._patch_acp()
  local ok, Connection = pcall(require, "codecompanion.acp")
  if not ok then
    log.warn("CC ACP module not available — skipping bridge injection: %s", tostring(Connection))
    return
  end

  -- Idempotency guard
  if Connection._mcp_companion_patched then
    return
  end
  Connection._mcp_companion_patched = true

  -- Patch connect_and_initialize to start bridge first
  local original_connect = Connection.connect_and_initialize

  Connection.connect_and_initialize = function(self)
    -- Ensure bridge is ready before connecting to ACP agent
    -- ACP requires MCP servers at session/new time, so we must block here
    -- (bridge likely already started from ChatAdapter event, this just waits)
    M._wait_for_bridge(10000)

    -- Call original connect_and_initialize
    return original_connect(self)
  end

  -- Patch _establish_session to inject bridge into mcpServers
  local original_establish = Connection._establish_session

  Connection._establish_session = function(self)
    -- Build bridge entry (needs _agent_info which is set after initialize)
    local bridge_entry = build_bridge_entry(self)

    if bridge_entry then
      -- Inject into adapter defaults so it gets picked up by session_args
      local defaults = self.adapter_modified and self.adapter_modified.defaults
      if defaults then
        defaults.mcpServers = defaults.mcpServers or {}
        if type(defaults.mcpServers) ~= "table" then
          defaults.mcpServers = {}
        end

        -- Check if already present
        local already = false
        for _, s in ipairs(defaults.mcpServers) do
          if s.name == "mcp-bridge" then
            already = true
            break
          end
        end

        if not already then
          table.insert(defaults.mcpServers, bridge_entry)
          log.info("CC ACP: bridge injected into adapter defaults")
        end
      end
    end

    return original_establish(self)
  end

  log.debug("CC ACP: Connection patched for bridge injection")
end

--- Extension exports (accessible via CodeCompanion.extensions.mcp_companion)
M.exports = {
  --- Get current plugin state
  status = function()
    return require("mcp_companion.state").get()
  end,

  --- Get bridge client (for direct MCP calls if needed)
  client = function()
    local bridge = require("mcp_companion.bridge")
    return bridge.client
  end,

  --- Force refresh all capabilities
  refresh = function()
    local bridge = require("mcp_companion.bridge")
    local client = bridge.client
    if client and client.connected then
      client:refresh_capabilities()
    end
  end,
}

return M
