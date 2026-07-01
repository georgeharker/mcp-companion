--- mcp-companion.nvim — Native `neovim` server: read-only resources
--- Structured JSON context an LLM can pull (buffers, diagnostics, workspace).
--- @module mcp_companion.native.neovim.resources

local util = require("mcp_companion.native.util")

local M = {}

M.resources = {
    {
        name = "Buffers",
        uri = "neovim://buffers",
        mimeType = "application/json",
        description = "Open buffers with active/modified flags.",
        handler = function(_ctx)
            -- Reuse the list_buffers tool handler for a single source of truth.
            local buffers = require("mcp_companion.native.neovim.buffers")
            for _, t in ipairs(buffers.tools) do
                if t.name == "list_buffers" then
                    return t.handler({}, _ctx)
                end
            end
            return util.json({ buffers = {} })
        end,
    },

    {
        name = "Workspace",
        uri = "neovim://workspace",
        mimeType = "application/json",
        description = "Working directory, git branch, and a shallow directory listing.",
        handler = function(_ctx)
            local cwd = vim.fn.getcwd()
            local branch = vim.fn.systemlist({ "git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD" })[1]
            if vim.v.shell_error ~= 0 then
                branch = nil
            end
            local entries = {}
            for name, kind in vim.fs.dir(cwd) do
                table.insert(entries, { name = name, type = kind })
            end
            return util.json({ cwd = cwd, git_branch = branch, entries = entries })
        end,
    },

    {
        name = "Diagnostics (workspace)",
        uri = "neovim://diagnostics/workspace",
        mimeType = "application/json",
        description = "Structured diagnostics across all open buffers.",
        handler = function(ctx)
            local diag = require("mcp_companion.native.neovim.diagnostics")
            for _, t in ipairs(diag.tools) do
                if t.name == "get_diagnostics" then
                    return t.handler({ scope = "workspace" }, ctx)
                end
            end
            return util.json({ diagnostics = {} })
        end,
    },
}

return M
