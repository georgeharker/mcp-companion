--- mcp-companion.nvim — CC Extension entry point
--- Bridges MCP capabilities into CodeCompanion:
---   - MCP tools → CC tools (function calling)
---   - MCP resources → CC # variables
---   - MCP prompts → CC / slash commands
---
--- Registered via CodeCompanion.register_extension("mcp_companion", M)
--- @module mcp_companion.cc

local M = {}

local log = require("mcp_companion.log")

--- Called by CodeCompanion when the extension is loaded.
--- Sets up event listeners that trigger (re)registration when the bridge
--- connects or capabilities change.
--- Also injects the bridge as an MCP server entry into CC's live config
--- so that transform_to_acp() can export it to ACP agents.
--- @param schema? table Extension schema from CC config
function M.setup(schema)
  local state = require("mcp_companion.state")

  -- Inject bridge into CC's live mcp config for ACP forwarding.
  -- CC extensions routinely modify the live config — this is the standard pattern.
  -- ACP agents (OpenCode, Claude Code) can connect to the bridge as a remote MCP server.
  M._inject_bridge_config()

  -- When bridge connects and capabilities are populated, register everything
  state.on("bridge_ready", function()
    log.debug("CC extension: bridge_ready — registering all")
    M._register_all()
  end)

  -- Re-register when servers change (capabilities polling fires servers_updated
  -- after all tool/resource/prompt lists have been refreshed — no need to also
  -- subscribe to the individual list_changed events, which would cause triple
  -- re-registration on every poll cycle).
  state.on("servers_updated", function()
    log.debug("CC extension: servers_updated — re-registering all")
    M._register_all()
  end)

  log.info("CC extension initialized")
end

function M._register_all()
  M._register_tools()
  M._register_resources()
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

function M._register_resources()
  local ok, vars = pcall(require, "mcp_companion.cc.variables")
  if ok then
    vars.register()
  else
    log.warn("Failed to load cc.variables: %s", tostring(vars))
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

--- Inject bridge into ACP session/new by monkey-patching Connection:_establish_session.
---
--- IMPORTANT: We do NOT add "mcp-bridge" to cc_config.mcp.opts.default_servers.
--- CC auto-starts every default_server as a stdio MCP client and prefixes all its tools
--- with "server_name_" — double-registering all 180 bridge tools with "mcp-bridge_" prefix.
--- Our cc/tools.lua handles tool registration via the CC tools API instead.
---
--- ACP forwarding: we wrap Connection:_establish_session to append our bridge server
--- to session_args.mcpServers immediately before SESSION_NEW is sent. Transport:
---   - HTTP if agent advertises mcpCapabilities.http in initialize response
---   - stdio via mcp-remote proxy otherwise
---
--- This runs once per Connection setup (idempotent — guarded by _patched flag).
function M._inject_bridge_config()
  local ok, Connection = pcall(require, "codecompanion.acp")
  if not ok then
    log.warn("CC ACP module not available — skipping bridge ACP injection: %s", tostring(Connection))
    return
  end

  -- Idempotency guard: only patch once across all reloads
  if Connection._mcp_companion_patched then
    return
  end
  Connection._mcp_companion_patched = true

  local original = Connection._establish_session

  Connection._establish_session = function(self)
    -- Wrap send_rpc_request on this instance for the duration of _establish_session.
    -- This intercepts both SESSION_LOAD (first attempt) and SESSION_NEW (fallback)
    -- to inject the bridge into mcpServers before either is sent.
    -- We always defer to original_send — no early restore needed.
    local original_send = self.send_rpc_request

    self.send_rpc_request = function(conn, method, params)
      -- Inject bridge into SESSION_NEW and SESSION_LOAD
      if method == "session/new" or method == "session/load" then
        local servers = params.mcpServers
        if type(servers) ~= "table" then
          servers = {}
        end

        -- Determine transport based on agent capabilities
        local bridge_entry
        local caps = conn._agent_info
            and conn._agent_info.agentCapabilities
            and conn._agent_info.agentCapabilities.mcpCapabilities

        local bridge_url = "http://127.0.0.1:9741/mcp"

        if caps and caps.http then
          log.debug("CC ACP: injecting bridge via HTTP transport")
          bridge_entry = {
            type = "http",
            name = "mcp-bridge",
            url = bridge_url,
            headers = {},
          }
        else
          -- Fallback: stdio via mcp-remote (bridges any HTTP MCP server to stdio)
          log.debug("CC ACP: injecting bridge via stdio mcp-remote fallback")
          bridge_entry = {
            name = "mcp-bridge",
            command = "npx",
            args = { "-y", "mcp-remote", bridge_url },
            env = {},
          }
        end

        -- Append only if not already present (idempotent across session reloads)
        local already = false
        for _, s in ipairs(servers) do
          if s.name == "mcp-bridge" then
            already = true
            break
          end
        end
        if not already then
          table.insert(servers, bridge_entry)
        end

        params.mcpServers = servers
        log.info("CC ACP: bridge injected into %s (%d total mcp servers)", method, #servers)
      end

      return original_send(conn, method, params)
    end

    local result = original(self)

    -- Restore send_rpc_request — intercept only needed during session establishment
    self.send_rpc_request = original_send

    return result
  end

  log.debug("CC ACP: Connection._establish_session patched for bridge injection")
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
    if bridge.client and bridge.client.connected then
      bridge.client:refresh_capabilities()
    end
  end,
}

return M
