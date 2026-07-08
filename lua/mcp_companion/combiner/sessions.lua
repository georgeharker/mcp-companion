--- Token-filter REST client — the single home for the combiner's
--- ``/sessions/token/<token>/filter`` endpoints.
---
--- Per-chat server filters are keyed by the chat's token (the stable
--- identifier for HTTP, ACP and CLI chats alike). This module replaces the
--- hand-rolled http.request calls previously duplicated across cc/init.lua
--- and cc/session_commands.lua.
---
--- All callbacks are invoked via vim.schedule as (err, data) where data is
--- the decoded JSON response ({disabled_servers = ..., pending = ...,
--- session_id = ...}).

local M = {}

--- @param token string
--- @return string
local function _filter_url(token)
    return require("mcp_companion.config").combiner_url() .. "/sessions/token/" .. token .. "/filter"
end

--- @param done? fun(err: string|nil, data: table|nil)
--- @return fun(r: table)
local function _handle(done)
    return function(r)
        vim.schedule(function()
            if not done then
                return
            end
            if r.status ~= 200 then
                done(string.format("HTTP %s: %s", tostring(r.status), r.body or ""), nil)
                return
            end
            local ok, data = pcall(vim.json.decode, r.body)
            if not ok or type(data) ~= "table" then
                done("combiner returned malformed JSON", nil)
                return
            end
            done(nil, data)
        end)
    end
end

--- Set (or amend) a token's server filter.
--- @param token string
--- @param body table  `{allowed_servers = {...}}` | `{enable = name}` | `{disable = name}`
--- @param done? fun(err: string|nil, data: table|nil)
function M.set_filter(token, body, done)
    require("mcp_companion.http").request({
        url = _filter_url(token),
        method = "post",
        headers = { ["Content-Type"] = "application/json" },
        body = vim.json.encode(body),
        timeout = 5000,
        callback = _handle(done),
    })
end

--- Read a token's current (or pending) filter state.
--- @param token string
--- @param done fun(err: string|nil, data: table|nil)
function M.get_filter(token, done)
    require("mcp_companion.http").request({
        url = _filter_url(token),
        method = "get",
        timeout = 5000,
        callback = _handle(done),
    })
end

--- Clear a token's filter (and any pending state).
--- @param token string
--- @param done? fun(err: string|nil, data: table|nil)
function M.clear_filter(token, done)
    require("mcp_companion.http").request({
        url = _filter_url(token),
        method = "delete",
        timeout = 3000,
        callback = _handle(done),
    })
end

--- Convert a response's disabled_servers list into a set keyed by name.
--- @param data table|nil
--- @return table<string, boolean>
function M.disabled_set(data)
    local out = {}
    for _, name in ipairs(data and data.disabled_servers or {}) do
        out[name] = true
    end
    return out
end

return M
