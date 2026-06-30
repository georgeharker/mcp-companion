--- mcp-companion.nvim — Native `neovim` server: diagnostics tools
--- @module mcp_companion.native.neovim.diagnostics

local util = require("mcp_companion.native.util")
local winpick = require("mcp_companion.native.winpick")

--- Map a numeric severity to its name.
local SEVERITY = {
    [vim.diagnostic.severity.ERROR] = "ERROR",
    [vim.diagnostic.severity.WARN] = "WARN",
    [vim.diagnostic.severity.INFO] = "INFO",
    [vim.diagnostic.severity.HINT] = "HINT",
}

--- Convert a vim.Diagnostic into a structured entry.
--- @param d vim.Diagnostic
--- @return table
local function to_entry(d)
    return {
        severity = SEVERITY[d.severity] or tostring(d.severity),
        range = { start_line = d.lnum + 1, start_col = d.col, end_line = d.end_lnum + 1, end_col = d.end_col },
        code = d.code,
        source = d.source,
        message = d.message,
    }
end

local M = {}

M.tools = {
    {
        name = "get_diagnostics",
        tier = "read",
        description = "Get structured LSP diagnostics for the current buffer or whole workspace.",
        inputSchema = {
            type = "object",
            properties = {
                scope = { type = "string", enum = { "buffer", "workspace" }, description = "default: buffer" },
                buffer = { type = "integer", description = "Buffer id when scope=buffer (default: active)" },
            },
        },
        handler = function(args, ctx)
            local scope = args.scope or "buffer"
            if scope == "workspace" then
                local grouped = {}
                for _, d in ipairs(vim.diagnostic.get(nil)) do
                    local name = vim.api.nvim_buf_get_name(d.bufnr)
                    grouped[name] = grouped[name] or {}
                    table.insert(grouped[name], to_entry(d))
                end
                return util.json({ scope = "workspace", diagnostics = grouped })
            end
            local buf = util.resolve_buf(args, ctx)
            local entries = {}
            for _, d in ipairs(vim.diagnostic.get(buf)) do
                table.insert(entries, to_entry(d))
            end
            return util.json({ scope = "buffer", buffer = buf, diagnostics = entries })
        end,
    },

    {
        name = "goto_diagnostic",
        tier = "navigate",
        description = "Jump the cursor to the next/prev/first diagnostic, optionally filtered by severity.",
        inputSchema = {
            type = "object",
            properties = {
                direction = { type = "string", enum = { "next", "prev", "first" }, description = "default: next" },
                severity = { type = "string", enum = { "ERROR", "WARN", "INFO", "HINT" } },
            },
        },
        handler = function(args, _ctx)
            -- Jump in the user's code window, not the focused window (often a chat).
            local win = winpick.code_win()
            if not win then
                return util.err("no code window is open to navigate diagnostics in")
            end
            local sev = args.severity and vim.diagnostic.severity[args.severity] or nil
            local dir = args.direction or "next"
            local count = (dir == "prev" and -1) or (dir == "first" and math.huge * -1) or 1
            vim.api.nvim_win_call(win, function()
                vim.diagnostic.jump({ count = count, severity = sev })
            end)
            winpick.maybe_focus(win)
            local pos = vim.api.nvim_win_get_cursor(win)
            local buf = vim.api.nvim_win_get_buf(win)
            return util.json({ direction = dir, buffer = buf, line = pos[1], col = pos[2] })
        end,
    },
}

return M
