--- Operations facade: the single place combiner operations are invoked from.
---
--- Every user-facing surface (user commands in init.lua, the :MCPStatus UI,
--- CC integrations) calls these instead of reaching for the MCP client
--- directly, so the connected-check / feedback / callback shape exists once.
--- This is also the operations inventory a CLI binding mirrors (see the
--- Python `mcp-combiner` control CLI).
---
--- All operations take an optional `done(err, result)` callback; by default
--- they also emit vim.notify feedback (pass opts.quiet = true to suppress).

local M = {}

local NS = "[mcp-companion] "

--- @param opts? {quiet?: boolean}
local function notifier(opts)
    if opts and opts.quiet then
        return function() end
    end
    return function(msg, level)
        vim.notify(NS .. msg, level or vim.log.levels.INFO)
    end
end

--- Run `fn(client)` if the combiner client is connected, else report.
--- @param notify fun(msg: string, level?: integer)
--- @param done? fun(err: string|nil, result: string|nil)
--- @return boolean ran
local function with_client(notify, done, fn)
    local combiner = require("mcp_companion.combiner")
    local client = combiner.client
    if not client or not client.connected then
        notify("Combiner not connected", vim.log.levels.WARN)
        if done then
            done("combiner not connected", nil)
        end
        return false
    end
    fn(client)
    return true
end

--- Standard completion callback: notify + forward.
local function finish(notify, action, done)
    return function(err, result)
        if err then
            notify(string.format("%s failed: %s", action, tostring(err)), vim.log.levels.ERROR)
        else
            notify(tostring(result or (action .. " done")))
        end
        if done then
            done(err, result)
        end
    end
end

--- Known server names (excluding the combiner's own pseudo-entry) — used for
--- command completion and pickers.
--- @return string[]
function M.server_names()
    local state = require("mcp_companion.state")
    local servers = state.field("servers") or {}
    local names = {}
    for _, srv in ipairs(servers) do
        if srv.name and srv.name ~= "_combiner" then
            table.insert(names, srv.name)
        end
    end
    return names
end

--- Restart the whole combiner process.
--- @param opts? {force?: boolean}
function M.restart_combiner(opts)
    require("mcp_companion.combiner").restart(opts)
end

--- Restart a single server (hard bounce for combiner-owned processes).
--- @param server_name string
--- @param done? fun(err: string|nil, result: string|nil)
--- @param opts? {quiet?: boolean}
function M.restart_server(server_name, done, opts)
    local notify = notifier(opts)
    with_client(notify, done, function(client)
        notify(string.format("Restarting %s...", server_name))
        client:restart_server(server_name, finish(notify, "Restart", done))
    end)
end

--- Toggle a server enabled/disabled globally.
--- @param server_name string
--- @param done? fun(err: string|nil, result: string|nil)
--- @param opts? {quiet?: boolean}
function M.toggle_server(server_name, done, opts)
    local notify = notifier(opts)
    with_client(notify, done, function(client)
        notify(string.format("Toggling %s...", server_name))
        client:toggle_server(server_name, finish(notify, "Toggle", done))
    end)
end

--- Re-read the combiner config file and apply the diff (no restart).
--- @param done? fun(err: string|nil, result: string|nil)
--- @param opts? {quiet?: boolean}
function M.reload_config(done, opts)
    local notify = notifier(opts)
    with_client(notify, done, function(client)
        notify("Reloading combiner config...")
        client:reload_config(finish(notify, "Reload", done))
    end)
end

--- Toggle a server's visibility in the project's .mcp-companion.json.
--- @param server_name string
--- @param done? fun(err: string|nil, result: table|nil)
--- @param opts? {quiet?: boolean}
function M.toggle_in_project(server_name, done, opts)
    local notify = notifier(opts)
    if server_name == "_combiner" then
        return
    end
    local known = M.server_names()
    if #known == 0 then
        notify("No connected servers — combiner state not loaded yet", vim.log.levels.WARN)
        if done then
            done("no servers", nil)
        end
        return
    end
    local project = require("mcp_companion.project")
    local ok, result = pcall(project.toggle_in_project_file, server_name, known)
    if not ok then
        notify("Project toggle failed: " .. tostring(result), vim.log.levels.ERROR)
        if done then
            done(tostring(result), nil)
        end
        return
    end
    local label = result.now_visible and "visible" or "hidden"
    notify(string.format("%s %s in project (%s)", server_name, label, result.path))
    if done then
        done(nil, result)
    end
end

return M
