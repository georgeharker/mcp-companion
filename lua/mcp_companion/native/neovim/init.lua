--- mcp-companion.nvim — Built-in `neovim` native server definition
--- Assembles the fixed tool/resource set. There is no public registration DSL;
--- this module simply returns the server definition that native/init.lua installs.
--- @module mcp_companion.native.neovim

local M = {}

--- Build the `neovim` server definition.
--- @param opts? table native_servers.neovim config (e.g. { enabled, expose_exec })
--- @return table server definition { name, displayName, description, tools, resources }
function M.build(opts)
    opts = opts or {}

    local tools = {}
    vim.list_extend(tools, require("mcp_companion.native.neovim.files").tools)
    vim.list_extend(tools, require("mcp_companion.native.neovim.buffers").tools)
    vim.list_extend(tools, require("mcp_companion.native.neovim.diagnostics").tools)

    -- exec-tier tools are gated by exposure: off unless explicitly enabled.
    -- (run_command / exec_lua are not implemented in this phase.)
    if opts.expose_exec then
        -- vim.list_extend(tools, require("mcp_companion.native.neovim.exec").tools)
    end

    return {
        name = "neovim",
        displayName = "Neovim",
        description = "Control the running Neovim instance: read/edit buffers, files, "
            .. "navigation, and diagnostics.",
        tools = tools,
        resources = require("mcp_companion.native.neovim.resources").resources,
    }
end

return M
