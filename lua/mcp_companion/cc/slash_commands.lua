--- mcp-companion.nvim — CC Slash Commands (MCP prompts -> / commands)
--- Registers each MCP prompt as a CodeCompanion / slash command.
---
--- Per the MCP spec, prompts are user-controlled and designed to be invoked
--- explicitly (e.g. as slash commands). The callback receives the CC Chat
--- object and calls prompts/get on the bridge, then injects the result
--- messages into the chat.
---
--- CC API (v19+):
---   slash_commands live at config.interactions.chat.slash_commands
---   entries can use { callback = fn, description = "..." } for dynamic values
---   or { path = "module.path", description = "..." } for module-based providers
---   We use the callback form since our prompts are dynamic.
---
--- Usage: type /mcp:prompt_name in a CC chat buffer.
--- @module mcp_companion.cc.slash_commands

local M = {}

local log = require("mcp_companion.log")

--- Register MCP prompts as CC / slash commands.
--- Called on bridge_ready and servers_updated events.
function M.register()
    local bridge = require("mcp_companion.bridge")

    if not bridge.client or not bridge.client.connected then
        return
    end

    local cc_config_ok, cc_config = pcall(require, "codecompanion.config")
    if not cc_config_ok then
        log.debug("codecompanion.config not available, skipping slash command registration")
        return
    end

    local slash_cmds = cc_config.interactions
        and cc_config.interactions.chat
        and cc_config.interactions.chat.slash_commands
    if not slash_cmds then
        log.debug("CC slash_commands table not found, skipping")
        return
    end

    -- Remove previously registered mcp: commands before re-registering
    for key in pairs(slash_cmds) do
        if type(key) == "string" and key:match("^mcp:") then
            slash_cmds[key] = nil
        end
    end

    local prompts = bridge.client.prompts or {}
    local count = 0

    for _, prompt in ipairs(prompts) do
        local cmd_name = string.format("mcp:%s", prompt.name or "unknown")
        local captured_prompt = prompt

        slash_cmds[cmd_name] = {
            description = prompt.description or string.format("MCP prompt: %s", prompt.name),
            ---@param chat table CodeCompanion chat object
            callback = function(chat)
                -- Collect arguments if the prompt requires them
                local args = {}
                if captured_prompt.arguments and #captured_prompt.arguments > 0 then
                    for _, arg in ipairs(captured_prompt.arguments) do
                        local value = vim.fn.input(string.format(
                            "%s (%s): ",
                            arg.name,
                            arg.description or ""
                        ))
                        if value ~= "" then
                            args[arg.name] = value
                        elseif arg.required then
                            log.warn("Required argument '%s' not provided for prompt '%s'",
                                arg.name, captured_prompt.name)
                            return
                        end
                    end
                end

                -- Fetch prompt from bridge
                bridge.client:get_prompt(captured_prompt.name, args, function(err, result)
                    vim.schedule(function()
                        if err then
                            log.error("Prompt '%s' error: %s", captured_prompt.name, tostring(err))
                            return
                        end

                        if not result or not result.messages then
                            return
                        end

                        for _, msg in ipairs(result.messages) do
                            local role = msg.role or "user"
                            local text = ""
                            if msg.content then
                                if type(msg.content) == "table" and msg.content.type == "text" then
                                    text = msg.content.text or ""
                                elseif type(msg.content) == "string" then
                                    text = msg.content
                                end
                            end

                            if text ~= "" then
                                chat:add_message({ role = role, content = text }, {
                                    _meta = {
                                        source = "slash_command",
                                        tag = cmd_name,
                                    },
                                })
                            end
                        end
                    end)
                end)
            end,
        }
        count = count + 1
    end

    if count > 0 then
        log.info("Registered %d MCP prompts as CC slash commands", count)
    end
end

return M
