--- mcp-companion.nvim — Singleton state + subscriber system
--- @module mcp_companion.state

local M = {}

--- @class MCPCompanion.ServerInfo
--- @field name string Server name (namespace prefix)
--- @field status string "connected"|"disconnected"|"error"|"disabled"
--- @field disabled boolean Whether server is disabled (tools unmounted)
--- @field tools table[] MCP tool definitions
--- @field resources table[] MCP resource definitions
--- @field resource_templates table[] MCP resource template definitions
--- @field prompts table[] MCP prompt definitions

--- @class MCPCompanion.BridgeState
--- @field status string "disconnected"|"connecting"|"healthy"|"connected"|"error"
--- @field port? number
--- @field pid? number
--- @field clients? number Number of sharedserver clients
--- @field error? string Last error message

--- Internal state table
--- @type table
local _state = {
  setup_state = "not_started", -- not_started | in_progress | completed | failed
  bridge = {
    status = "disconnected",
    port = nil,
    pid = nil,
    clients = 0,
    error = nil,
  },
  servers = {}, --- @type MCPCompanion.ServerInfo[]
  native_servers = {}, --- @type MCPCompanion.ServerInfo[]
  errors = {}, -- Recent errors (max 50)
  logs = {}, -- Recent log entries (max 500)
}

--- Valid subscriber channels
local _valid_channels = { bridge = true, servers = true, ui = true, all = true, errors = true, logs = true }

--- Subscribers by channel
--- @type table<string, function[]>
local _subscribers = {}

--- Event subscribers
--- @type table<string, function[]>
local _event_subs = {}

--- Reset state to initial values
function M.reset()
  _state = {
    setup_state = "not_started",
    bridge = { status = "disconnected", port = nil, pid = nil, clients = 0, error = nil },
    servers = {},
    native_servers = {},
    errors = {},
    logs = {},
  }
  _subscribers = {}
  _event_subs = {}
end

--- Simple shallow equality check for tables
--- @param a any
--- @param b any
--- @return boolean
local function _shallow_eq(a, b)
  if type(a) ~= type(b) then
    return false
  end
  if type(a) ~= "table" then
    return a == b
  end
  -- Compare keys in a
  for k, v in pairs(a) do
    if b[k] ~= v then
      return false
    end
  end
  -- Check b has no extra keys
  for k in pairs(b) do
    if a[k] == nil then
      return false
    end
  end
  return true
end

--- Update a state field and notify subscribers
--- @param channel string The channel/field to update
--- @param value any New value
function M.update(channel, value)
  local old = _state[channel]

  -- Apply update
  if type(value) == "table" and type(old) == "table" and not vim.islist(value) then
    _state[channel] = vim.tbl_deep_extend("force", old, value)
  else
    _state[channel] = value
  end

  -- Skip notifications if nothing changed (shallow check for non-table values)
  if _shallow_eq(old, _state[channel]) then
    return
  end

  -- Notify channel subscribers
  local new_val = _state[channel]
  for _, cb in ipairs(_subscribers[channel] or {}) do
    vim.schedule(function()
      cb(new_val, channel)
    end)
  end

  -- Notify "all" subscribers
  for _, cb in ipairs(_subscribers.all or {}) do
    vim.schedule(function()
      cb(new_val, channel)
    end)
  end

  -- Notify "ui" on any non-ui change
  if channel ~= "ui" then
    for _, cb in ipairs(_subscribers.ui or {}) do
      vim.schedule(function()
        cb(_state, "ui")
      end)
    end
  end
end

--- Subscribe to a channel
--- @param channel string
--- @param callback function
--- @return function unsubscribe Call this to remove the subscription
function M.subscribe(channel, callback)
  _subscribers[channel] = _subscribers[channel] or {}
  table.insert(_subscribers[channel], callback)

  -- Return unsubscribe function
  return function()
    M.unsubscribe(channel, callback)
  end
end

--- Unsubscribe from a channel
--- @param channel string
--- @param callback function
function M.unsubscribe(channel, callback)
  local subs = _subscribers[channel]
  if not subs then
    return
  end
  for i = #subs, 1, -1 do
    if subs[i] == callback then
      table.remove(subs, i)
    end
  end
end

--- Emit a named event
--- @param event string
--- @param ... any Event data
function M.emit(event, ...)
  local args = { ... }
  for _, cb in ipairs(_event_subs[event] or {}) do
    vim.schedule(function()
      cb(unpack(args))
    end)
  end
end

--- Subscribe to a named event
--- @param event string
--- @param callback function
--- @return function unsubscribe
function M.on(event, callback)
  _event_subs[event] = _event_subs[event] or {}
  table.insert(_event_subs[event], callback)
  return function()
    M.off(event, callback)
  end
end

--- Unsubscribe from a named event
--- @param event string
--- @param callback function
function M.off(event, callback)
  local subs = _event_subs[event]
  if not subs then
    return
  end
  for i = #subs, 1, -1 do
    if subs[i] == callback then
      table.remove(subs, i)
    end
  end
end

--- Add an error to the error list
--- @param err table|string
function M.add_error(err)
  table.insert(_state.errors, 1, {
    message = type(err) == "string" and err or (type(err) == "table" and err.message) or tostring(err),
    timestamp = os.time(),
  })
  while #_state.errors > 50 do
    table.remove(_state.errors)
  end
  M.update("errors", _state.errors)
end

--- Add a log entry
--- @param entry table|string
function M.add_log(entry)
  table.insert(_state.logs, {
    message = type(entry) == "string" and entry or (type(entry) == "table" and entry.message) or tostring(entry),
    level = type(entry) == "table" and entry.level or "info",
    timestamp = os.time(),
  })
  while #_state.logs > 500 do
    table.remove(_state.logs, 1)
  end
end

--- Get current state (read-only reference)
--- @return table
function M.get()
  return _state
end

--- Get a specific state field
--- @param key string
--- @return any
function M.field(key)
  return _state[key]
end

return M
