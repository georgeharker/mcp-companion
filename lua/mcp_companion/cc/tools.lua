--- mcp-companion.nvim — CC Tool Registration
--- Registers MCP tools from the bridge into CodeCompanion's tool system.
---
--- Injects directly into config.interactions.chat.tools (the live CC config table),
--- following the same pattern as mcphub.nvim's dynamic tool registration.
---
--- Each MCP server becomes a tool group. Each tool within becomes an individual
--- CC tool entry with a callback() that returns the tool spec.
--- @module mcp_companion.cc.tools

local M = {}

local log = require("mcp_companion.log")

--- ID prefix for all tools/groups we own — used for cleanup
local _ID_PREFIX = "mcp_companion:"

--- Fingerprint of the last successful registration (tool count + sorted names).
--- Used to skip re-registration when nothing has changed.
local _last_fingerprint = nil

--- Compute a cheap fingerprint from the servers list.
--- @param servers table[] state.servers array
--- @return string
local function _fingerprint(servers)
    local names = {}
    for _, srv in ipairs(servers) do
        for _, t in ipairs(srv.tools or {}) do
            table.insert(names, t._namespaced or t.name)
        end
    end
    table.sort(names)
    return tostring(#names) .. ":" .. table.concat(names, ",")
end

--- Remove all previously registered tools and groups from CC config
local function _cleanup(tools_tbl)
    -- Remove individual tools
    for key, value in pairs(tools_tbl) do
        if type(value) == "table" and type(value.id) == "string" then
            if value.id:sub(1, #_ID_PREFIX) == _ID_PREFIX then
                tools_tbl[key] = nil
            end
        end
    end

    -- Remove groups
    local groups = tools_tbl.groups
    if type(groups) == "table" then
        for key, value in pairs(groups) do
            if type(value) == "table" and type(value.id) == "string" then
                if value.id:sub(1, #_ID_PREFIX) == _ID_PREFIX then
                    groups[key] = nil
                end
            end
        end
    end
end

--- Build the cmds handler for a bridge tool.
--- CC calls cmds[i](self, action, cmd_opts) where action is the parsed
--- tool input from the LLM and cmd_opts.output_cb is the result callback.
--- @param client table MCPCompanion.Client
--- @param namespaced_name string Full bridge tool name (e.g. "everything_echo")
--- @param display_name string Short name for logging
--- @param server_name string Server that owns this tool (for approval checks)
--- @return function
local function _make_bridge_cmd(client, namespaced_name, display_name, server_name)
    return function(_self, action, cmd_opts)
        -- action is the raw tool input table from the LLM
        local params = type(action) == "table" and action or {}

        -- Check approval before executing
        local approval = require("mcp_companion.cc.approval")
        approval.check(server_name, display_name, _self, function(approved)
            if not approved then
                vim.schedule(function()
                    cmd_opts.output_cb({ status = "error", data = "Tool call denied by user." })
                end)
                return
            end

            client:call_tool(namespaced_name, params, function(err, result)
                vim.schedule(function()
                    if err then
                        cmd_opts.output_cb({ status = "error", data = tostring(err) })
                        return
                    end
                    if not result then
                        cmd_opts.output_cb({ status = "success", data = "" })
                        return
                    end
                    -- Check for MCP-level error in result
                    if result.isError then
                        local parts = {}
                        for _, item in ipairs(result.content or {}) do
                            if item.type == "text" then
                                table.insert(parts, item.text or "")
                            end
                        end
                        cmd_opts.output_cb({ status = "error", data = table.concat(parts, "\n") })
                        return
                    end
                    -- Extract text content
                    local parts = {}
                    for _, item in ipairs(result.content or {}) do
                        if item.type == "text" then
                            table.insert(parts, item.text or "")
                        elseif item.type == "image" then
                            table.insert(parts, string.format("[image: %s]", item.mimeType or "unknown"))
                        elseif item.type == "resource" and item.resource and item.resource.text then
                            table.insert(parts, item.resource.text)
                        end
                    end
                    local text = table.concat(parts, "\n")
                    if text == "" then
                        text = string.format("Tool '%s' completed with no output.", display_name)
                    end
                    cmd_opts.output_cb({ status = "success", data = text })
                end)
            end)
        end)
    end
end

--- Build the output handlers table (error + success) for a CC tool.
--- output.error(self, stderr, meta) and output.success(self, stdout, meta)
--- where stdout/stderr are arrays and meta.tools.chat is the chat object.
--- @param display_name string
--- @return table
local function _make_output(display_name)
    return {
        error = function(_self, stderr, meta)
            local chat = meta and meta.tools and meta.tools.chat
            if not chat then return end
            local err_data = stderr and (stderr[#stderr] or {}) or {}
            local text = type(err_data) == "table" and (err_data.data or vim.inspect(err_data))
                or tostring(err_data)
            chat:add_tool_output(_self, string.format("**`%s` Tool**: Error:\n```\n%s\n```", display_name, text))
        end,
        success = function(_self, stdout, meta)
            local chat = meta and meta.tools and meta.tools.chat
            if not chat then return end
            local out = stdout and (stdout[#stdout] or {}) or {}
            local text = type(out) == "table" and (out.data or "") or tostring(out)
            if text == "" then
                chat:add_tool_output(_self, string.format("**`%s` Tool**: Completed with no output.", display_name))
            else
                chat:add_tool_output(
                    _self,
                    string.format("**`%s` Tool**: Returned:\n```\n%s\n```", display_name, text)
                )
            end
        end,
    }
end

--- Register all MCP tools from the bridge into CodeCompanion.
--- Reads state.servers (pre-grouped by client._update_server_state) and
--- injects tool entries + groups into config.interactions.chat.tools.
function M.register()
    local cc_config_ok, cc_config = pcall(require, "codecompanion.config")
    if not cc_config_ok then
        log.debug("codecompanion.config not available, skipping tool registration")
        return
    end

    -- Safely navigate to the tools table
    local tools_tbl = cc_config.interactions
        and cc_config.interactions.chat
        and cc_config.interactions.chat.tools
    if not tools_tbl then
        log.warn("codecompanion.config.interactions.chat.tools not found")
        return
    end

    -- Ensure groups subtable exists
    tools_tbl.groups = tools_tbl.groups or {}

    -- Clean up previous registrations
    _cleanup(tools_tbl)

    local state = require("mcp_companion.state")
    local bridge = require("mcp_companion.bridge")
    local client = bridge.client

    if not client then
        log.debug("No bridge client, skipping tool registration")
        return
    end

    local servers = state.field("servers") or {}

    -- Skip re-registration if nothing has changed since last time
    local fp = _fingerprint(servers)
    if fp == _last_fingerprint then
        log.debug("CC tools: capabilities unchanged, skipping re-registration")
        return
    end

    local registered_servers = 0
    local registered_tools = 0

    for _, server in ipairs(servers) do
        -- Skip the internal _bridge pseudo-server
        if server.name == "_bridge" then
            goto continue
        end

        local tool_keys = {}

        for _, tool in ipairs(server.tools or {}) do
            -- tool._display = short name (e.g. "echo")
            -- tool._namespaced = full bridge name (e.g. "everything_echo")
            local display = tool._display or tool.name
            local namespaced = tool._namespaced or tool.name
            -- Key into the tools table: server_name__tool_display_name
            local key = server.name .. "__" .. display

            local tool_id = _ID_PREFIX .. server.name .. ":" .. display

            -- Capture loop variables for the closure
            local captured_display = display
            local captured_namespaced = namespaced
            local captured_description = tool.description or ("MCP tool: " .. display)
            local captured_input_schema = tool.inputSchema or { type = "object", properties = {} }

            tools_tbl[key] = {
                id = tool_id,
                description = captured_description,
                hide_in_help_window = true,
                visible = false,
                callback = function()
                    return {
                        name = key,
                        cmds = {
                            _make_bridge_cmd(client, captured_namespaced, captured_display, server.name),
                        },
                        system_prompt = function(_group_config, _ctx)
                            return string.format(
                                "You can use the `%s` tool from the `%s` MCP server to: %s\n",
                                captured_display,
                                server.name,
                                captured_description
                            )
                        end,
                        output = _make_output(captured_display),
                        schema = {
                            type = "function",
                            ["function"] = {
                                name = key,
                                description = captured_description,
                                parameters = captured_input_schema,
                            },
                        },
                    }
                end,
            }

            table.insert(tool_keys, key)
            registered_tools = registered_tools + 1
        end

        -- Create a group for this server
        if #tool_keys > 0 then
            local group_key = server.name
            tools_tbl.groups[group_key] = {
                id = _ID_PREFIX .. "group:" .. server.name,
                description = string.format("All tools from the `%s` MCP server", server.name),
                hide_in_help_window = false,
                tools = tool_keys,
                system_prompt = function(_group_config, _ctx)
                    return string.format(
                        "You have access to the `%s` MCP server with %d tool(s).\n",
                        server.name,
                        #tool_keys
                    )
                end,
                opts = { collapse_tools = true },
            }
            registered_servers = registered_servers + 1
        end

        ::continue::
    end

    log.info("CC tools registered: %d tools across %d servers", registered_tools, registered_servers)
    _last_fingerprint = fp
end

--- Unregister all previously registered tools (cleanup only)
function M.unregister()
    local cc_config_ok, cc_config = pcall(require, "codecompanion.config")
    if not cc_config_ok then return end
    local tools_tbl = cc_config.interactions
        and cc_config.interactions.chat
        and cc_config.interactions.chat.tools
    if tools_tbl then
        _cleanup(tools_tbl)
    end
    -- Reset fingerprint so the next register() call does a full re-registration
    _last_fingerprint = nil
end

return M
