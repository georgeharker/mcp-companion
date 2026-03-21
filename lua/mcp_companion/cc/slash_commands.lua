--- mcp-companion.nvim — CC Slash Commands (MCP prompts → / commands)
--- Registers each MCP prompt as a CodeCompanion / slash command so users
--- can type /mcp:prompt_name to invoke MCP prompts in chat.
--- @module mcp_companion.cc.slash_commands

local M = {}

local log = require("mcp_companion.log")

--- Register MCP prompts as CC / slash commands.
--- Called on bridge_ready and prompt_list_changed events.
function M.register()
  local state = require("mcp_companion.state")
  local bridge = require("mcp_companion.bridge")

  if not bridge.client or not bridge.client.connected then
    return
  end

  -- CC slash commands are registered via config.strategies.chat.slash_commands
  local cc_config_ok, cc_config = pcall(require, "codecompanion.config")
  if not cc_config_ok then
    log.debug("codecompanion.config not available, skipping slash command registration")
    return
  end

  local prompts = bridge.client.prompts or {}
  local count = 0

  for _, prompt in ipairs(prompts) do
    local cmd_name = string.format("mcp:%s", prompt.name or "unknown")

    if cc_config.strategies and cc_config.strategies.chat and cc_config.strategies.chat.slash_commands then
      cc_config.strategies.chat.slash_commands[cmd_name] = {
        callback = function(self)
          -- Collect arguments if the prompt has them
          local args = {}
          if prompt.arguments and #prompt.arguments > 0 then
            for _, arg in ipairs(prompt.arguments) do
              local value = vim.fn.input(string.format("%s (%s): ", arg.name, arg.description or ""))
              if value ~= "" then
                args[arg.name] = value
              elseif arg.required then
                vim.notify(
                  string.format("[mcp-companion] Required argument '%s' not provided", arg.name),
                  vim.log.levels.WARN
                )
                return
              end
            end
          end

          -- Get prompt from bridge
          bridge.client:get_prompt(prompt.name, args, function(err, result)
            vim.schedule(function()
              if err then
                vim.notify(
                  string.format("[mcp-companion] Prompt error: %s", tostring(err)),
                  vim.log.levels.ERROR
                )
                return
              end

              if result and result.messages then
                for _, msg in ipairs(result.messages) do
                  local role = msg.role or "user"
                  local text = ""
                  if msg.content and msg.content.type == "text" then
                    text = msg.content.text or ""
                  elseif type(msg.content) == "string" then
                    text = msg.content
                  end

                  if text ~= "" then
                    -- Add message to the CC chat
                    if self.add_message then
                      self:add_message({ role = role, content = text })
                    end
                  end
                end
              end
            end)
          end)
        end,
        description = prompt.description or string.format("MCP prompt: %s", prompt.name),
      }
      count = count + 1
    end
  end

  if count > 0 then
    log.info("Registered %d MCP prompts as CC slash commands", count)
  end
end

return M
