--- mcp-companion.nvim — CC Variables (MCP resources → # variables)
--- Registers each MCP resource as a CodeCompanion # variable so users
--- can type #server:resource_name to include resource content in chat.
--- @module mcp_companion.cc.variables

local M = {}

local log = require("mcp_companion.log")

--- Register MCP resources as CC # variables.
--- Called on bridge_ready and resource_list_changed events.
function M.register()
  local state = require("mcp_companion.state")
  local bridge = require("mcp_companion.bridge")

  if not bridge.client or not bridge.client.connected then
    return
  end

  -- CC variables are registered by injecting into the config table.
  -- codecompanion.config.strategies.chat.variables
  local cc_config_ok, cc_config = pcall(require, "codecompanion.config")
  if not cc_config_ok then
    log.debug("codecompanion.config not available, skipping variable registration")
    return
  end

  local servers = state.field("servers") or {}
  local count = 0

  for _, server in ipairs(servers) do
    if server.name ~= "_bridge" then
      for _, resource in ipairs(server.resources or {}) do
        local var_name = string.format("mcp:%s", resource.name or resource.uri or "unknown")

        -- Register as a CC variable
        if cc_config.strategies and cc_config.strategies.chat and cc_config.strategies.chat.variables then
          cc_config.strategies.chat.variables[var_name] = {
            callback = function(_self)
              -- Read resource synchronously
              local ok, result = pcall(function()
                return bridge.client:read_resource(resource.uri)
              end)

              if ok and result and result.contents then
                local parts = {}
                for _, content in ipairs(result.contents) do
                  if content.text then
                    table.insert(parts, content.text)
                  end
                end
                return table.concat(parts, "\n")
              else
                return string.format("[Error reading resource %s: %s]", resource.uri, tostring(result))
              end
            end,
            description = resource.description or string.format("MCP resource: %s", resource.uri),
          }
          count = count + 1
        end
      end
    end
  end

  if count > 0 then
    log.info("Registered %d MCP resources as CC variables", count)
  end
end

return M
