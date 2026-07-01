--- mcp-companion.nvim — Native `neovim` server: buffer & navigation tools
--- @module mcp_companion.native.neovim.buffers

local util = require("mcp_companion.native.util")
local winpick = require("mcp_companion.native.winpick")

--- Read a buffer's lines (0-based exclusive end via nvim API; we expose 1-based).
--- @param buf integer
--- @return string[]
local function buf_lines(buf)
    return vim.api.nvim_buf_get_lines(buf, 0, -1, false)
end

local M = {}

M.tools = {
    {
        name = "list_buffers",
        tier = "read",
        description = "List open buffers with id, name, active/modified flags, filetype and line count.",
        inputSchema = { type = "object", properties = {} },
        handler = function(_args, ctx)
            local current = util.resolve_buf({}, ctx)
            local out = {}
            for _, buf in ipairs(vim.api.nvim_list_bufs()) do
                if vim.api.nvim_buf_is_loaded(buf) and vim.bo[buf].buflisted then
                    table.insert(out, {
                        id = buf,
                        name = vim.api.nvim_buf_get_name(buf),
                        active = buf == current,
                        modified = vim.bo[buf].modified,
                        filetype = vim.bo[buf].filetype,
                        lines = vim.api.nvim_buf_line_count(buf),
                    })
                end
            end
            return util.json({ buffers = out })
        end,
    },

    {
        name = "read_buffer",
        tier = "read",
        description = "Read a buffer's contents with line numbers. Defaults to the active file buffer.",
        inputSchema = {
            type = "object",
            properties = {
                buffer = { type = "integer", description = "Buffer id (default: active file buffer)" },
                start_line = { type = "integer", description = "1-based first line (default 1)" },
                end_line = { type = "integer", description = "1-based last line, inclusive (default: last)" },
            },
        },
        handler = function(args, ctx)
            local buf = util.resolve_buf(args, ctx)
            local lines = buf_lines(buf)
            local first = math.max(1, args.start_line or 1)
            local last = args.end_line and math.min(#lines, args.end_line) or #lines
            local rendered = {}
            for i = first, last do
                table.insert(rendered, string.format("%d\t%s", i, lines[i] or ""))
            end
            local name = vim.api.nvim_buf_get_name(buf)
            local header = string.format("# buffer %d %s (%d lines)\n", buf, name ~= "" and name or "[No Name]", #lines)
            return util.text(header .. table.concat(rendered, "\n"))
        end,
    },

    {
        name = "get_cursor",
        tier = "read",
        description = "Get the cursor position {buffer, line (1-based), col (0-based)} in the "
            .. "user's code window (not a chat/tree window).",
        inputSchema = { type = "object", properties = {} },
        handler = function(_args, _ctx)
            local win = winpick.code_win()
            if not win then
                return util.err("no code window is open")
            end
            local buf = vim.api.nvim_win_get_buf(win)
            local pos = vim.api.nvim_win_get_cursor(win)
            return util.json({ buffer = buf, line = pos[1], col = pos[2] })
        end,
    },

    {
        name = "get_selection",
        tier = "read",
        description = "Get the current/last visual selection {buffer, range, text}.",
        inputSchema = { type = "object", properties = {} },
        handler = function(_args, ctx)
            local buf = util.resolve_buf({}, ctx)
            local s = vim.api.nvim_buf_get_mark(buf, "<")
            local e = vim.api.nvim_buf_get_mark(buf, ">")
            if s[1] == 0 and e[1] == 0 then
                return util.json({ buffer = buf, range = vim.NIL, text = "" })
            end
            local lines = vim.api.nvim_buf_get_lines(buf, s[1] - 1, e[1], false)
            return util.json({
                buffer = buf,
                range = { start_line = s[1], start_col = s[2], end_line = e[1], end_col = e[2] },
                text = table.concat(lines, "\n"),
            })
        end,
    },

    {
        name = "open_file",
        tier = "navigate",
        description = "Open (or focus) a file in the user's code window and optionally jump to a "
            .. "line. Never opens over a chat/tree/terminal window; opens a new tab if no code "
            .. "window exists (configurable via native_servers.neovim.window).",
        inputSchema = {
            type = "object",
            properties = {
                path = { type = "string", description = "File path to open" },
                line = { type = "integer", description = "1-based line to jump to" },
            },
            required = { "path" },
        },
        handler = function(args, _ctx)
            if not args.path or args.path == "" then
                return util.err("open_file requires 'path'")
            end
            local win, err = winpick.open(args.path, args.line)
            if not win then
                return util.err("open_file failed: " .. tostring(err))
            end
            local buf = vim.api.nvim_win_get_buf(win)
            return util.json({ buffer = buf, window = win, path = vim.api.nvim_buf_get_name(buf) })
        end,
    },

    {
        name = "set_cursor",
        tier = "navigate",
        description = "Move the cursor to {line (1-based), col (0-based)} in a buffer's window.",
        inputSchema = {
            type = "object",
            properties = {
                buffer = { type = "integer", description = "Buffer id (default: active file buffer)" },
                line = { type = "integer", description = "1-based line" },
                col = { type = "integer", description = "0-based column (default 0)" },
            },
            required = { "line" },
        },
        handler = function(args, ctx)
            local buf = util.resolve_buf(args, ctx)
            local win = util.win_for_buf(buf)
            if not win then
                -- Not visible — reveal it in the code window (never a chat window).
                local cw = winpick.code_win()
                if cw then
                    local ok = pcall(function()
                        vim.api.nvim_win_call(cw, function()
                            vim.api.nvim_set_current_buf(buf)
                        end)
                    end)
                    if ok then
                        win = cw
                    end
                end
            end
            if not win then
                return util.err("buffer " .. buf .. " is not visible and no code window is open")
            end
            local ok, e = pcall(vim.api.nvim_win_set_cursor, win, { args.line, args.col or 0 })
            if not ok then
                return util.err("set_cursor failed: " .. tostring(e))
            end
            winpick.maybe_focus(win)
            return util.json({ buffer = buf, window = win, line = args.line, col = args.col or 0 })
        end,
    },

    {
        name = "set_buffer_lines",
        tier = "write",
        description = "Replace a 1-based inclusive line range in a buffer with new lines.",
        inputSchema = {
            type = "object",
            properties = {
                buffer = { type = "integer", description = "Buffer id (default: active file buffer)" },
                start = { type = "integer", description = "1-based first line to replace" },
                ["end"] = { type = "integer", description = "1-based last line to replace, inclusive" },
                lines = { type = "array", items = { type = "string" }, description = "Replacement lines" },
            },
            required = { "start", "end", "lines" },
        },
        handler = function(args, ctx)
            local buf = util.resolve_buf(args, ctx)
            local total = vim.api.nvim_buf_line_count(buf)
            local s = math.max(1, args.start)
            local e = math.min(total, args["end"])
            if e < s then
                return util.err("invalid range: end < start")
            end
            local ok, err = pcall(vim.api.nvim_buf_set_lines, buf, s - 1, e, false, args.lines)
            if not ok then
                return util.err("set_buffer_lines failed: " .. tostring(err))
            end
            return util.json({ buffer = buf, replaced = { s, e }, new_line_count = vim.api.nvim_buf_line_count(buf) })
        end,
    },

    {
        name = "edit_buffer",
        tier = "write",
        description = "Apply SEARCH/REPLACE blocks to the live buffer. Each block: "
            .. "<<<<<<< SEARCH / =======  / >>>>>>> REPLACE. First exact match per block.",
        inputSchema = {
            type = "object",
            properties = {
                buffer = { type = "integer", description = "Buffer id (default: active file buffer)" },
                path = { type = "string", description = "Path of an open buffer (alternative to buffer id)" },
                diff = { type = "string", description = "One or more SEARCH/REPLACE blocks" },
            },
            required = { "diff" },
        },
        handler = function(args, ctx)
            local buf = args.buffer
            if not buf and args.path then
                buf = vim.fn.bufnr(args.path)
                if buf == -1 then
                    return util.err("no open buffer for path: " .. args.path)
                end
            end
            buf = util.resolve_buf({ buffer = buf }, ctx)

            local blocks = M._parse_search_replace(args.diff or "")
            if #blocks == 0 then
                return util.err("no SEARCH/REPLACE blocks found in diff")
            end

            local text = table.concat(vim.api.nvim_buf_get_lines(buf, 0, -1, false), "\n")
            local applied, failed = 0, {}
            for i, block in ipairs(blocks) do
                local from = text:find(block.search, 1, true)
                if from then
                    text = text:sub(1, from - 1) .. block.replace .. text:sub(from + #block.search)
                    applied = applied + 1
                else
                    table.insert(failed, i)
                end
            end

            if applied > 0 then
                vim.api.nvim_buf_set_lines(buf, 0, -1, false, vim.split(text, "\n", { plain = true }))
            end
            return util.json({
                buffer = buf,
                blocks = #blocks,
                applied = applied,
                unmatched = failed,
            })
        end,
    },

    {
        name = "save_buffer",
        tier = "write",
        description = "Write a modified buffer to disk.",
        inputSchema = {
            type = "object",
            properties = {
                buffer = { type = "integer", description = "Buffer id (default: active file buffer)" },
            },
        },
        handler = function(args, ctx)
            local buf = util.resolve_buf(args, ctx)
            local ok, err = pcall(function()
                vim.api.nvim_buf_call(buf, function()
                    vim.cmd("write")
                end)
            end)
            if not ok then
                return util.err("save_buffer failed: " .. tostring(err))
            end
            return util.json({ buffer = buf, saved = true })
        end,
    },
}

--- Parse SEARCH/REPLACE blocks out of a diff string.
--- @param diff string
--- @return { search: string, replace: string }[]
function M._parse_search_replace(diff)
    local blocks = {}
    -- Match: <<<<<<< SEARCH \n <search> \n ======= \n <replace> \n >>>>>>> REPLACE
    local pattern = "<<<<<<+ SEARCH%s*\n(.-)\n=======%s*\n(.-)\n>>>>>>+ REPLACE"
    for search, replace in diff:gmatch(pattern) do
        table.insert(blocks, { search = search, replace = replace })
    end
    return blocks
end

return M
