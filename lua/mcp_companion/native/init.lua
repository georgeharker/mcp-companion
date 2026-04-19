--- mcp-companion.nvim — Native server registry
--- @module mcp_companion.native
-- luacheck: ignore 212 (unused arguments in interface stubs)

local M = {}

--- @type table<string, table> Registered native servers
local _servers = {}

--- Setup native servers from config
--- @param config table Plugin config
function M.setup(config)
  -- Will initialize built-in native servers (neovim, etc.)
  -- based on config.native_servers settings
end

--- Get all registered native servers
--- @return table[]
function M.get_servers()
  local result = {}
  for _, server in pairs(_servers) do
    table.insert(result, server)
  end
  return result
end

--- Check if a server name is a native server
--- @param name string
--- @return boolean
function M.is_native_server(name)
  return _servers[name] ~= nil
end

--- Add a native server
--- @param name string Server name
--- @param definition table Server definition
function M.add_server(name, definition)
  -- TODO: Implement (port from mcphub NativeServer)
  _servers[name] = definition
end

--- Add a tool to a native server
--- @param server_name string
--- @param tool table Tool definition
function M.add_tool(server_name, tool)
  -- TODO: Implement
end

--- Add a resource to a native server
--- @param server_name string
--- @param resource table Resource definition
function M.add_resource(server_name, resource)
  -- TODO: Implement
end

--- Add a resource template to a native server
--- @param server_name string
--- @param template table Resource template definition
function M.add_resource_template(server_name, template)
  -- TODO: Implement
end

--- Add a prompt to a native server
--- @param server_name string
--- @param prompt table Prompt definition
function M.add_prompt(server_name, prompt)
  -- TODO: Implement
end

return M
