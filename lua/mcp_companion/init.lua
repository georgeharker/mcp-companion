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

    -- Check for config file — warn but don't block (combiner will error later)
    if not config.get().combiner.config then
        log.warn("No servers.json found. Create one or set combiner.config in setup()")
    end

    -- Ensure the combiner is installed in the target venv (combiner.venv if set, else
    -- the plugin-local combiner/.venv), unless the user pinned a custom python_cmd.
    -- Async + idempotent (no-op if the current version is already installed); on
    -- success we re-resolve python_cmd so the (later) combiner start prefers it.
    -- combiner.command supplies a ready-to-run executable (e.g. a Nix-built binary),
    -- so there is nothing for uv to install — skip the ensure step entirely.
    if not config.get().combiner._custom_python and not config.get().combiner.command then
        require("mcp_companion.install").ensure(nil, function(ok, err, installed)
            if ok then
                config.refresh_python_cmd()
                if installed then
                    vim.notify("[mcp-companion] combiner installed", vim.log.levels.INFO)
                end
            else
                vim.notify("[mcp-companion] combiner install failed: " .. tostring(err), vim.log.levels.WARN)
            end
        end)
    end

    -- Initialize native servers
    local native = require("mcp_companion.native")
    native.setup(config.get())

    -- Setup combiner lifecycle
    local combiner = require("mcp_companion.combiner")
    combiner.setup(config.get())

    -- NOTE: CC extension registration is handled via CC's extensions config:
    --   extensions = { mcp_companion = { callback = "mcp_companion.cc", opts = {...} } }
    -- We do NOT call cc.register_extension() here — CC calls M.init(schema) on our module.

    -- Open the Neovim back-channel and register with the combiner so external
    -- agents can call `neovim_*` tools back into this instance. We reconcile via
    -- channel.sync() (boot-id aware) on combiner connect AND on every SSE reconnect,
    -- so a combiner *restart* (which wipes the combiner's registry) is recovered.
    local channel = require("mcp_companion.native.channel")
    if channel.enabled() then
        state.on("combiner_ready", function()
            channel.sync()
        end)
        state.on("combiner_stream_connected", function()
            channel.sync()
        end)
        if state.get().combiner.status == "connected" then
            channel.sync()
        end
    end

    -- Autocmds
    local group = vim.api.nvim_create_augroup("MCPCompanion", { clear = true })
    vim.api.nvim_create_autocmd("VimLeavePre", {
        group = group,
        callback = function()
            pcall(function()
                require("mcp_companion.native.channel").deregister()
            end)
            combiner.stop()
        end,
    })

    -- Combiner is started by CC extension setup (cc/init.lua) when CodeCompanion loads,
    -- ensuring it's healthy before any ACP session is created.
    -- Manual start available via :MCPStart command.

    -- User commands
    vim.api.nvim_create_user_command("MCPStatus", function()
        local ui = require("mcp_companion.ui")
        ui.toggle()
    end, { desc = "Toggle MCP Companion status window" })

    vim.api.nvim_create_user_command("MCPRestart", function(args)
        combiner.restart({ force = args.bang })
    end, { bang = true, desc = "Restart MCP combiner (use ! to force when other clients attached)" })

    vim.api.nvim_create_user_command("MCPInstall", function(args)
        local venv = args.args ~= "" and args.args or nil
        vim.notify("[mcp-companion] installing combiner…", vim.log.levels.INFO)
        require("mcp_companion.install").ensure(venv, function(ok, err)
            if ok then
                require("mcp_companion.config").refresh_python_cmd()
                vim.notify("[mcp-companion] combiner installed", vim.log.levels.INFO)
            else
                vim.notify("[mcp-companion] install failed: " .. tostring(err), vim.log.levels.ERROR)
            end
        end, args.bang)
    end, {
        nargs = "?",
        bang = true,
        desc = "Install/refresh the Python combiner into a venv (default combiner.venv, else plugin-local combiner/.venv); ! forces reinstall",
    })

    vim.api.nvim_create_user_command("MCPRestartServer", function(args)
        local server_name = args.args
        if not server_name or server_name == "" then
            vim.notify("[mcp-companion] Usage: :MCPRestartServer <server_name>", vim.log.levels.WARN)
            return
        end
        local client = combiner.client
        if not client or not client.connected then
            vim.notify("[mcp-companion] Combiner not connected", vim.log.levels.WARN)
            return
        end
        vim.notify(string.format("[mcp-companion] Restarting %s...", server_name), vim.log.levels.INFO)
        client:restart_server(server_name, function(err, result)
            if err then
                vim.notify(string.format("[mcp-companion] Restart failed: %s", tostring(err)), vim.log.levels.ERROR)
            else
                vim.notify(string.format("[mcp-companion] %s", result or "done"), vim.log.levels.INFO)
            end
        end)
    end, {
        nargs = 1,
        desc = "Restart a single MCP server (stops + respawns its backing process; no full combiner restart)",
        complete = function()
            local srv_state = require("mcp_companion.state")
            local servers = srv_state.field("servers") or {}
            local names = {}
            for _, srv in ipairs(servers) do
                if srv.name ~= "_combiner" then
                    table.insert(names, srv.name)
                end
            end
            return names
        end,
    })

    vim.api.nvim_create_user_command("MCPReload", function()
        local client = combiner.client
        if not client or not client.connected then
            vim.notify("[mcp-companion] Combiner not connected", vim.log.levels.WARN)
            return
        end
        vim.notify("[mcp-companion] Reloading combiner config...", vim.log.levels.INFO)
        client:reload_config(function(err, result)
            if err then
                vim.notify(string.format("[mcp-companion] Reload failed: %s", tostring(err)), vim.log.levels.ERROR)
            else
                vim.notify(string.format("[mcp-companion] %s", result or "config reloaded"), vim.log.levels.INFO)
            end
        end)
    end, { desc = "Reload the combiner config file and apply server changes (no restart)" })

    vim.api.nvim_create_user_command("MCPLog", function()
        local log_path = log.get_log_path()
        if log_path then
            vim.cmd("edit " .. vim.fn.fnameescape(log_path))
        else
            vim.notify("[mcp-companion] File logging not enabled", vim.log.levels.WARN)
        end
    end, { desc = "Open MCP Companion log file" })

    vim.api.nvim_create_user_command("MCPToggleServer", function(args)
        local server_name = args.args
        if not server_name or server_name == "" then
            vim.notify("[mcp-companion] Usage: :MCPToggleServer <server_name>", vim.log.levels.WARN)
            return
        end
        local client = combiner.client
        if not client or not client.connected then
            vim.notify("[mcp-companion] Combiner not connected", vim.log.levels.WARN)
            return
        end
        vim.notify(string.format("[mcp-companion] Toggling %s...", server_name), vim.log.levels.INFO)
        client:toggle_server(server_name, function(err, result)
            if err then
                vim.notify(string.format("[mcp-companion] Toggle failed: %s", tostring(err)), vim.log.levels.ERROR)
            else
                vim.notify(string.format("[mcp-companion] %s", result or "done"), vim.log.levels.INFO)
            end
        end)
    end, {
        nargs = 1,
        desc = "Toggle an MCP server enabled/disabled",
        complete = function()
            -- Complete with known server names from state
            local srv_state = require("mcp_companion.state")
            local servers = srv_state.field("servers") or {}
            local names = {}
            for _, srv in ipairs(servers) do
                if srv.name ~= "_combiner" then
                    table.insert(names, srv.name)
                end
            end
            return names
        end,
    })

    vim.api.nvim_create_user_command("MCPSaveProjectConfig", function(args)
        local format = args.args ~= "" and args.args or "shortest"
        if format ~= "shortest" and format ~= "allowed" and format ~= "disabled" then
            vim.notify("[mcp-companion] Usage: :MCPSaveProjectConfig [shortest|allowed|disabled]", vim.log.levels.WARN)
            return
        end
        local cc = require("mcp_companion.cc")
        local chat = cc._current_chat_for_save()
        if not chat then
            vim.notify(
                "[mcp-companion] No active chat with an MCP session — open a CodeCompanion chat first",
                vim.log.levels.WARN
            )
            return
        end
        cc._save_project_config_interactive(chat, format)
    end, {
        nargs = "?",
        desc = "Snapshot current chat session's MCP server visibility to .mcp-companion.json",
        complete = function()
            return { "shortest", "allowed", "disabled" }
        end,
    })

    state.update("setup_state", "completed")
    _setup_done = true

    -- Register on_ready callback
    if config.get().on_ready then
        state.on("combiner_ready", function()
            config.get().on_ready(combiner)
        end)
    end

    -- Register on_error callback
    if config.get().on_error then
        state.on("combiner_error", function(err)
            config.get().on_error(err)
        end)
    end

    log.info("Setup complete (config: %s)", config.get().combiner.config or "none")
end

--- Get current state module
--- @return table
function M.get_state()
    return require("mcp_companion.state")
end

--- Get combiner module (lifecycle + client)
--- @return table
function M.get_combiner()
    return require("mcp_companion.combiner")
end

--- Alias for compatibility
--- @return table
function M.get_hub_instance()
    return require("mcp_companion.combiner")
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

-- Public native-server registration API. Registration-only and setup-time:
-- call these before any editor connects to the combiner, and register the same
-- tools across all instances (the combiner freezes the catalog once per process).
-- See docs/designs/native-neovim-server.md.
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
