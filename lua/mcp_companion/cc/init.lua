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

--- Build bridge MCP server entry for ACP session/new.
--- The bridge URL is deterministic from config — we don't need to wait for
--- upstream servers or full MCP client connect. The bridge proxies everything.
--- @param agent_capabilities table|nil agentCapabilities from ACP INITIALIZE RPC
--- @return table|nil bridge_entry MCP server entry or nil if no bridge config
local function build_bridge_entry(agent_capabilities)
  local config = require("mcp_companion.config").get()

  -- Need bridge config to know host/port
  if not config.bridge or not config.bridge.config then
    return nil
  end

  local host = config.bridge.host or "127.0.0.1"
  local port = config.bridge.port or 9741
  local bridge_url = string.format("http://%s:%d/mcp", host, port)

  -- Check if agent supports HTTP MCP transport
  local caps = agent_capabilities and agent_capabilities.mcpCapabilities

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

--- Called by CodeCompanion when the extension is loaded.
--- Sets up event listeners that trigger (re)registration when the bridge
--- connects or capabilities change.
--- Also patches ACP to inject bridge as MCP server for ACP agents.
--- @param schema? table Extension schema from CC config
function M.setup(schema)
  local state = require("mcp_companion.state")

  -- Start bridge when any chat adapter is created.
  -- Block briefly to ensure tools are registered before first submit.
  -- With parallel requests and "healthy" state, this blocks for
  -- at most the MCP client connect time (~300ms if bridge already up).
  -- Use a generous timeout (30s) to accommodate OAuth browser flows on first
  -- connection — the wait resolves immediately once the bridge is healthy.
  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionChatAdapter",
    callback = function()
      M._wait_for_bridge(30000)
    end,
  })

  -- Auto-enable MCP tool groups when chat is created
  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionChatCreated",
    callback = function(args)
      M._auto_enable_tools(args.data)
    end,
  })

  -- Inject bridge into ACP session/new when the pre-session event fires.
  -- CodeCompanion fires CodeCompanionACPSessionPre after connect_and_authenticate()
  -- succeeds but before _establish_session() reads adapter_modified.defaults.mcpServers.
  -- Event data contains adapter_modified (mutable reference) and agent_capabilities.
  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionACPSessionPre",
    callback = function(args)
      local adapter_modified = args.data and args.data.adapter_modified
      local agent_capabilities = args.data and args.data.agent_capabilities
      if not adapter_modified then
        log.warn("CC ACP: CodeCompanionACPSessionPre fired but adapter_modified is nil")
        return
      end

      -- Kick off bridge warm-up (non-blocking) — bridge entry uses the
      -- deterministic config URL, so it doesn't need to wait for full connect.
      M._start_bridge_async()

      -- Build and inject bridge entry into adapter defaults
      local bridge_entry = build_bridge_entry(agent_capabilities)
      if bridge_entry then
        local defaults = adapter_modified.defaults
        if defaults then
          defaults.mcpServers = defaults.mcpServers or {}
          if type(defaults.mcpServers) ~= "table" then
            defaults.mcpServers = {}
          end

          -- Check if already present (idempotent)
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
    end,
  })

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

  -- Already connected, healthy, or connecting
  local bridge_status = state.get().bridge.status
  if bridge_status == "connected" or bridge_status == "connecting" or bridge_status == "healthy" then
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

--- Wait for bridge to be fully connected (tools registered).
--- Used by ChatAdapter to ensure tools are available before first submit.
--- With parallel requests, the healthy→connected gap is ~200ms.
--- @param timeout_ms? number Maximum time to wait (default 5000)
--- @return boolean success Whether bridge is connected
function M._wait_for_bridge(timeout_ms)
  timeout_ms = timeout_ms or 5000
  local state = require("mcp_companion.state")

  local function is_connected()
    return state.get().bridge.status == "connected"
  end

  -- Already connected
  if is_connected() then
    return true
  end

  -- Not even started - start it now
  local s = state.get().bridge.status
  if s ~= "connecting" and s ~= "healthy" then
    M._start_bridge_async()
  end

  -- Wait for full connect (tools registered)
  local ok = vim.wait(timeout_ms, is_connected, 50)

  if ok then
    log.info("CC: bridge connected")
    -- Register tools synchronously so they're available on this tick.
    -- The bridge_ready event also triggers _register_all() via vim.schedule,
    -- but that runs on the next event loop tick — too late for the first
    -- chat submit.
    M._register_all()
  else
    log.warn("CC: bridge did not connect in %dms", timeout_ms)
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
