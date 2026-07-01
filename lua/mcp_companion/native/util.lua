--- mcp-companion.nvim — Native server shared helpers
--- Result builders (MCP content shape) + Neovim context resolution used by the
--- built-in `neovim` tool handlers.
--- @module mcp_companion.native.util

local M = {}

--- Wrap text in an MCP tool result.
--- @param text string
--- @return table
function M.text(text)
    return { content = { { type = "text", text = text or "" } } }
end

--- Wrap a Lua value as a JSON text result (structured output).
--- @param value any
--- @return table
function M.json(value)
    local ok, encoded = pcall(vim.json.encode, value)
    if not ok then
        return M.err("failed to encode result: " .. tostring(encoded))
    end
    return M.text(encoded)
end

--- Build an MCP error result (isError = true).
--- @param msg string
--- @return table
function M.err(msg)
    return { isError = true, content = { { type = "text", text = tostring(msg) } } }
end

--- Best-effort "current file buffer": the buffer shown in the user's code
--- window (first non-chat/tree/terminal/float window in the current tabpage).
--- This matters for in-process CodeCompanion, where the *focused* buffer is the
--- chat, not the code the user means. Falls back to a visible normal-file buffer
--- scan, then the focused buffer. Always prefer an explicit `buffer=` arg.
--- @return integer bufnr
function M.current_file_buf()
    -- Primary: the code window's buffer (shares the placement policy with the
    -- navigate tools, so reads/edits land where open_file/goto_diagnostic act).
    local ok, winpick = pcall(require, "mcp_companion.native.winpick")
    if ok then
        local buf = winpick.code_buf()
        if buf then
            return buf
        end
    end
    -- Fallback: first visible normal-file buffer in the current tabpage.
    for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
        local buf = vim.api.nvim_win_get_buf(win)
        if vim.api.nvim_buf_is_valid(buf) and vim.bo[buf].buftype == "" and vim.api.nvim_buf_get_name(buf) ~= "" then
            return buf
        end
    end
    return vim.api.nvim_get_current_buf()
end

--- Resolve the target buffer for a tool call.
--- Precedence: explicit args.buffer → normalized ctx.nvim.current_buf → best-effort.
--- NOTE: the fallback relies on current_file_buf(), whose heuristic is flagged as
--- possibly dubious — see above. Explicit args.buffer is always the safe path.
--- @param args table
--- @param ctx table
--- @return integer bufnr
function M.resolve_buf(args, ctx)
    if args and type(args.buffer) == "number" and vim.api.nvim_buf_is_valid(args.buffer) then
        return args.buffer
    end
    if ctx and ctx.nvim and ctx.nvim.current_buf and vim.api.nvim_buf_is_valid(ctx.nvim.current_buf) then
        return ctx.nvim.current_buf
    end
    return M.current_file_buf()
end

--- Find the window (in the current tabpage) currently displaying `buf`.
--- @param buf integer
--- @return integer|nil winid
function M.win_for_buf(buf)
    for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
        if vim.api.nvim_win_get_buf(win) == buf then
            return win
        end
    end
    return nil
end

return M
