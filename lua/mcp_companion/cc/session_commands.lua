--- mcp-companion.nvim — Session-scoped server toggle slash command
--- Registers /mcp-session as a static CC slash command that lets the user
--- enable or disable individual MCP servers for the current chat session only.
---
--- Unlike bridge__disable_server (global), the per-session toggle is invisible
--- to other sessions and is automatically cleaned up when the session ends.
---
--- Usage: type /mcp-session in a CC chat buffer, then pick a server and action.
--- @module mcp_companion.cc.session_commands

local M = {}

local log = require("mcp_companion.log")

-- Track per-chat session-disabled state locally so the picker shows current status
-- without needing an extra round-trip to the bridge.
-- Format: { [bufnr] = { [server_name] = true } }
--- @type table<integer, table<string, boolean>>
local _session_state = {}

--- Return the set of servers disabled for a given chat buffer.
--- @param bufnr integer
--- @return table<string, boolean>
local function _get_disabled(bufnr)
    _session_state[bufnr] = _session_state[bufnr] or {}
    return _session_state[bufnr]
end

--- Sync CC tool registry after a session toggle.
--- On disable: remove the server's tool group from the chat's tool_registry
--- and unregister from the global CC MCP registry.
--- On enable: re-register the server's tools and re-add the group.
--- @param chat table CC chat object
--- @param server_name string
--- @param is_disabling boolean true if disabling, false if re-enabling
local function _sync_cc_tool_group(chat, server_name, is_disabling)
    local cc_mcp_ok, cc_mcp = pcall(require, "codecompanion.mcp")
    if not cc_mcp_ok then return end

    local group_name = cc_mcp.tool_prefix() .. server_name
    local bridge_group_name = cc_mcp.tool_prefix() .. "bridge"

    if is_disabling then
        -- Remove the per-server group from this chat's tool_registry
        if chat.tool_registry and chat.tool_registry.remove then
            pcall(chat.tool_registry.remove, chat.tool_registry, group_name)
        end
        -- Also remove the aggregate bridge group — it contains tools from the
        -- now-disabled server and needs to be rebuilt
        if chat.tool_registry and chat.tool_registry.remove then
            pcall(chat.tool_registry.remove, chat.tool_registry, bridge_group_name)
        end
        -- Unregister from the global MCP registry so new chats don't see it
        if cc_mcp.unregister_tools then
            pcall(cc_mcp.unregister_tools, server_name)
        end
        log.debug("CC: removed tool group '%s' for session-disabled server", group_name)
    else
        -- Re-enabling: trigger a full re-registration which will pick up
        -- the re-enabled server from the bridge's tools/list
        local cc_init_ok, cc_init = pcall(require, "mcp_companion.cc")
        if cc_init_ok and cc_init._register_all then
            -- Clear tools fingerprint so re-registration isn't skipped
            local tools_ok, cc_tools = pcall(require, "mcp_companion.cc.tools")
            if tools_ok and cc_tools.clear_fingerprint then
                cc_tools.clear_fingerprint()
            end
            cc_init._register_all()
        end
        -- Re-add the per-server group to this chat
        if chat.tool_registry then
            chat.tool_registry:add(group_name, {
                config = chat.tools and chat.tools.tools_config,
            })
        end
        -- Re-add the aggregate bridge group
        if chat.tool_registry then
            chat.tool_registry:add(bridge_group_name, {
                config = chat.tools and chat.tools.tools_config,
            })
        end
        -- Refresh tools display
        if chat.tools and chat.tools.refresh then
            chat.tools:refresh({ adapter = chat.adapter })
        end
        log.debug("CC: re-added tool group '%s' for session-enabled server", group_name)
    end
end

--- Call the session toggle for the given chat via the token filter endpoint.
--- Uses chat._mcp_token as the stable identifier for both ACP and HTTP adapter chats.
--- @param chat table CC chat object
--- @param tool_name string "bridge__session_disable_server" or "bridge__session_enable_server"
--- @param server_name string
--- @param callback fun(err?: string, msg?: string)
local function _call_session_tool(chat, tool_name, server_name, callback)
    local token = chat._mcp_token
    if token then
        local cfg = require("mcp_companion.config").get()
        local host = (cfg.bridge and cfg.bridge.host) or "127.0.0.1"
        local port = (cfg.bridge and cfg.bridge.port) or 9741
        local http = require("mcp_companion.http")

        local is_disabling = tool_name == "bridge__session_disable_server"
        local action_key = is_disabling and "disable" or "enable"
        local body = vim.json.encode({ [action_key] = server_name })

        http.request({
            url = string.format("http://%s:%d/sessions/token/%s/filter", host, port, token),
            method = "post",
            headers = { ["Content-Type"] = "application/json" },
            body = body,
            timeout = 5000,
            callback = function(r)
                vim.schedule(function()
                    if r.status == 200 then
                        local ok, data = pcall(vim.json.decode, r.body)
                        if ok and data and data.disabled_servers and chat.bufnr then
                            local disabled_map = {}
                            for _, s in ipairs(data.disabled_servers) do
                                disabled_map[s] = true
                            end
                            _session_state[chat.bufnr] = disabled_map
                        end
                        local action = is_disabling and "disabled" or "enabled"
                        callback(nil, string.format("%s %s for this session", action, server_name))
                    else
                        callback(string.format("Bridge filter update failed (status %s)", r.status))
                    end
                end)
            end,
        })
        return
    end

    -- Fallback: no token — call bridge meta-tool via Neovim MCP client
    local bridge = require("mcp_companion.bridge")
    if not bridge.client then
        callback("Bridge not connected")
        return
    end

    local chat_id = chat and chat.bufnr and tostring(chat.bufnr) or nil

    bridge.client:call_tool(tool_name, { server_name = server_name, chat_id = chat_id }, function(err, result)
        vim.schedule(function()
            if err then
                callback(tostring(err))
                return
            end
            local text = result
                and result.content
                and result.content[1]
                and result.content[1].text
            if text then
                local ok, data = pcall(vim.json.decode, text)
                if ok and data and data.disabled_servers and chat and chat.bufnr then
                    local disabled_map = {}
                    for _, s in ipairs(data.disabled_servers) do
                        disabled_map[s] = true
                    end
                    _session_state[chat.bufnr] = disabled_map
                end
            end
            local is_disabling = tool_name == "bridge__session_disable_server"
            local action = is_disabling and "disabled" or "enabled"
            callback(nil, string.format("%s %s for this session", action, server_name))
        end)
    end)
end

--- Register /mcp-session as a static CC slash command.
--- Called once at setup — not driven by bridge_ready/servers_updated.
function M.register()
    local cc_config_ok, cc_config = pcall(require, "codecompanion.config")
    if not cc_config_ok then
        log.debug("codecompanion.config not available, skipping /mcp-session registration")
        return
    end

    local slash_cmds = cc_config.interactions
        and cc_config.interactions.chat
        and cc_config.interactions.chat.slash_commands
    if not slash_cmds then
        log.debug("CC slash_commands table not found, skipping /mcp-session")
        return
    end

    slash_cmds["mcp-session"] = {
        description = "Enable or disable MCP servers for this chat session",
        ---@param chat table CodeCompanion chat object
        callback = function(chat)
            local state = require("mcp_companion.state")
            local servers = state.field("servers") or {}

            -- Filter out the internal _bridge pseudo-server
            local names = {}
            for _, srv in ipairs(servers) do
                if srv.name ~= "_bridge" then
                    table.insert(names, srv.name)
                end
            end

            if #names == 0 then
                vim.notify("mcp-companion: no MCP servers available", vim.log.levels.WARN)
                return
            end

            local bufnr = chat.bufnr
            local disabled = _get_disabled(bufnr)

            -- Build picker items with current status
            local items = {}
            for _, name in ipairs(names) do
                table.insert(items, {
                    name = name,
                    label = string.format("[%s] %s", disabled[name] and "OFF" or " ON", name),
                })
            end

            vim.ui.select(items, {
                prompt = "Toggle MCP server for this session:",
                format_item = function(item) return item.label end,
            }, function(choice)
                if not choice then return end

                local currently_disabled = disabled[choice.name]
                local tool, action, new_state
                if currently_disabled then
                    tool = "bridge__session_enable_server"
                    action = "Enabling"
                    new_state = false
                else
                    tool = "bridge__session_disable_server"
                    action = "Disabling"
                    new_state = true
                end

                log.debug("Session toggle: %s %s", action:lower(), choice.name)

                _call_session_tool(chat, tool, choice.name, function(err, msg)
                    if err then
                        vim.notify(
                            string.format("mcp-companion: session toggle failed: %s", err),
                            vim.log.levels.ERROR
                        )
                        return
                    end

                    -- Update local state
                    if new_state then
                        disabled[choice.name] = true
                    else
                        disabled[choice.name] = nil
                    end

                    -- Update CC tool registry so @-mention and context block
                    -- reflect the session-disabled state (Part B).
                    _sync_cc_tool_group(chat, choice.name, new_state)

                    -- Refresh status window if open (it shows session state)
                    local ui_ok, ui = pcall(require, "mcp_companion.ui")
                    if ui_ok and ui.is_open() then
                        ui.render()
                    end

                    vim.notify(
                        string.format("mcp-companion: %s", msg),
                        vim.log.levels.INFO
                    )
                    log.info("Session toggle: %s %s — %s", action:lower(), choice.name, msg)
                end)
            end)
        end,
    }

    log.debug("Registered /mcp-session slash command")
end

--- Return the session-disabled set for a chat buffer (read-only view).
--- @param bufnr integer
--- @return table<string, boolean> server_name->true for session-disabled servers
function M.get_session_state(bufnr)
    return _session_state[bufnr] or {}
end

--- Set the session-disabled state for a chat buffer.
--- Used by ACP filter initialization to sync state for MCPStatus display.
--- @param bufnr integer
--- @param disabled_map table<string, boolean> server_name->true for disabled servers
function M.set_session_state(bufnr, disabled_map)
    _session_state[bufnr] = disabled_map
end

--- Fetch session status from the bridge (async).
--- Uses chat._mcp_token as the stable identifier for both ACP and HTTP adapter chats.
--- @param chat table|nil CC chat object (nil for non-chat context)
--- @param callback fun(err: string|nil, disabled: table<string, boolean>|nil)
function M.fetch_session_status(chat, callback)
    local token = chat and chat._mcp_token
    if token then
        local cfg = require("mcp_companion.config").get()
        local host = (cfg.bridge and cfg.bridge.host) or "127.0.0.1"
        local port = (cfg.bridge and cfg.bridge.port) or 9741
        local http = require("mcp_companion.http")
        log.debug("MCPStatus: fetching session filter via token (bufnr=%s token=%s)",
            tostring(chat and chat.bufnr), token)

        http.request({
            url = string.format("http://%s:%d/sessions/token/%s/filter", host, port, token),
            method = "get",
            timeout = 3000,
            callback = function(resp)
                vim.schedule(function()
                    if resp.status ~= 200 then
                        callback(string.format("HTTP %s", resp.status), nil)
                        return
                    end
                    local ok, data = pcall(vim.json.decode, resp.body)
                    if not ok or not data then
                        callback("JSON parse error", nil)
                        return
                    end
                    local disabled = {}
                    for _, name in ipairs(data.disabled_servers or {}) do
                        disabled[name] = true
                    end
                    if chat and chat.bufnr then
                        _session_state[chat.bufnr] = disabled
                    end
                    callback(nil, disabled)
                end)
            end,
        })
        return
    end

    -- Fallback: no token — call bridge meta-tool
    local bridge = require("mcp_companion.bridge")
    if not bridge.client then
        log.debug("MCPStatus: bridge not connected (bufnr=%s)", tostring(chat and chat.bufnr))
        callback("Bridge not connected", nil)
        return
    end

    local chat_id = chat and chat.bufnr and tostring(chat.bufnr) or nil
    bridge.client:call_tool("bridge__session_status", { chat_id = chat_id }, function(err, result)
        vim.schedule(function()
            if err then callback(tostring(err), nil); return end
            local text = result and result.content and result.content[1] and result.content[1].text
            if not text then callback("Empty response", nil); return end
            local ok, data = pcall(vim.json.decode, text)
            if not ok or not data then callback("JSON parse error", nil); return end
            local disabled = {}
            for _, name in ipairs(data.disabled_servers or {}) do
                disabled[name] = true
            end
            if chat and chat.bufnr then _session_state[chat.bufnr] = disabled end
            callback(nil, disabled)
        end)
    end)
end

--- Clear session state for a chat buffer (call on chat close).
--- @param bufnr integer
function M.clear(bufnr)
    _session_state[bufnr] = nil
end

return M
