--- mcp-companion.nvim — CC Tool Registration
--- Registers MCP tools from the bridge into CodeCompanion's MCP tool registry.
---
--- Uses CC's `codecompanion.mcp.register_tools()` API which persists across
--- config reloads. Tools are then merged via CC's filter.lua `pre_filter`.
---
--- Each bridge server becomes a registry entry with its tools and a group.
--- @module mcp_companion.cc.tools

local M = {}

local log = require("mcp_companion.log")

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

            -- Execute the tool via bridge client
            client:call_tool(namespaced_name, params, function(err, result)
                vim.schedule(function()
                    if err then
                        cmd_opts.output_cb({ status = "error", data = tostring(err) })
                    else
                        -- MCP tool results have a content array of content blocks
                        local content = result and result.content or {}
                        local text_parts = {}
                        for _, block in ipairs(content) do
                            if block.type == "text" then
                                table.insert(text_parts, block.text)
                            elseif block.type == "image" then
                                table.insert(text_parts, "[Image: " .. (block.mimeType or "unknown") .. "]")
                            elseif block.type == "resource" then
                                table.insert(text_parts, "[Resource: " .. (block.resource and block.resource.uri or "unknown") .. "]")
                            end
                        end
                        local output = table.concat(text_parts, "\n")
                        cmd_opts.output_cb({ status = "success", data = output })
                    end
                end)
            end)
        end)
    end
end

--- Build output handlers for tool results
--- @param display_name string Tool display name for output formatting
--- @return table
local function _make_output(display_name)
    return {
        rejected = function(_self, rejected_msg)
            return string.format("**`%s` Tool Rejected**: %s", display_name, rejected_msg or "No reason given")
        end,
        error = function(_self, error_msg)
            return string.format("**`%s` Tool Error**: %s", display_name, error_msg or "Unknown error")
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

--- Register all MCP tools from the bridge into CodeCompanion's MCP registry.
--- Uses CC's `mcp.register_tools()` API which persists across config reloads.
function M.register()
    local cc_mcp_ok, cc_mcp = pcall(require, "codecompanion.mcp")
    if not cc_mcp_ok then
        log.debug("codecompanion.mcp not available, skipping tool registration")
        return
    end

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

        local server_tools = {}  -- tool_name -> tool_config
        local tool_keys = {}

        for _, tool in ipairs(server.tools or {}) do
            -- tool._display = short name (e.g. "echo")
            -- tool._namespaced = full bridge name (e.g. "everything_echo")
            local display = tool._display or tool.name
            local namespaced = tool._namespaced or tool.name
            -- Use the namespaced name as the key (already unique and matches bridge)
            local key = namespaced

            -- Capture loop variables for the closure
            local captured_display = display
            local captured_namespaced = namespaced
            local captured_description = tool.description or ("MCP tool: " .. display)
            local captured_input_schema = tool.inputSchema or { type = "object", properties = {} }

            server_tools[key] = {
                description = captured_description,
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

        -- Register this server's tools with CC's MCP registry
        if #tool_keys > 0 then
            local group = {
                description = string.format("All tools from the `%s` MCP server", server.name),
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

            cc_mcp.register_tools(server.name, server_tools, group)
            registered_servers = registered_servers + 1
        end

        ::continue::
    end

    log.info("CC tools registered: %d tools across %d servers", registered_tools, registered_servers)
    _last_fingerprint = fp
end

--- Unregister all previously registered tools
function M.unregister()
    local cc_mcp_ok, cc_mcp = pcall(require, "codecompanion.mcp")
    if not cc_mcp_ok then return end

    local state = require("mcp_companion.state")
    local servers = state.field("servers") or {}

    for _, server in ipairs(servers) do
        if server.name ~= "_bridge" then
            cc_mcp.unregister_tools(server.name)
        end
    end

    _last_fingerprint = nil
    log.debug("CC tools unregistered")
end

return M
