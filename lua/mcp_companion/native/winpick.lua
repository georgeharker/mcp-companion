--- mcp-companion.nvim — Native server window/buffer placement
---
--- Native navigate/display tools (open_file, goto_diagnostic, set_cursor, …)
--- must NOT act on the focused window — when an agent runs in a CodeCompanion
--- chat, that window IS the chat. Instead they target a "code window": the
--- first normal window in the current tabpage that is not floating and not one
--- of the ignored filetypes/buftypes (chat, file trees, terminals, …).
---
--- Resolution is on demand (no autocmds, no tracked state).
--- @module mcp_companion.native.winpick

local M = {}

--- Placement options (config.native_servers.neovim.window), with fallback
--- defaults so this works even if config is partially specified.
local DEFAULTS = {
  ignore_filetypes = {
    "codecompanion", "neo-tree", "NvimTree", "aerial", "Outline",
    "trouble", "qf", "help", "TelescopePrompt", "neotest-summary",
    "dap-repl", "dapui_watches", "dapui_stacks", "dapui_breakpoints",
    "dapui_scopes", "dapui_console", "mcp-companion",
  },
  ignore_buftypes = { "nofile", "prompt", "terminal", "quickfix", "help" },
  no_code_window = "tab",
  focus = "file",
  reuse_visible = true,
}

--- @return table window-placement options merged over defaults
local function opts()
  local ok, config = pcall(require, "mcp_companion.config")
  local user = {}
  if ok then
    local ns = config.get().native_servers or {}
    user = (ns.neovim and ns.neovim.window) or {}
  end
  return user
end

--- Read one option, falling back to DEFAULTS.
--- @param key string
local function opt(key)
  local v = opts()[key]
  if v ~= nil then return v end
  return DEFAULTS[key]
end

--- @param list string[]|nil
--- @param v string
--- @return boolean
local function list_has(list, v)
  for _, x in ipairs(list or {}) do
    if x == v then return true end
  end
  return false
end

--- Is `win` a usable code window: valid, not floating, and not displaying an
--- ignored filetype/buftype (chat, tree, terminal, …)?
--- @param win integer|nil
--- @return boolean
function M.is_code_win(win)
  if not win or win == 0 or not vim.api.nvim_win_is_valid(win) then
    return false
  end
  local wcfg = vim.api.nvim_win_get_config(win)
  if wcfg.relative and wcfg.relative ~= "" then
    return false -- floating window
  end
  local buf = vim.api.nvim_win_get_buf(win)
  if not vim.api.nvim_buf_is_valid(buf) then return false end
  if list_has(opt("ignore_buftypes"), vim.bo[buf].buftype) then return false end
  if list_has(opt("ignore_filetypes"), vim.bo[buf].filetype) then return false end
  return true
end

--- The window to treat as the user's editing area: the first code window in the
--- current tabpage (skipping chat/tree/terminal/floating), or nil if none.
--- @return integer|nil winid
function M.code_win()
  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if M.is_code_win(win) then
      return win
    end
  end
  return nil
end

--- Apply the focus policy to `win`: focus it iff `focus == "file"`. Used by the
--- navigate tools that move within an existing window (so they don't otherwise
--- steal focus from the chat).
--- @param win integer|nil
function M.maybe_focus(win)
  if opt("focus") == "file" and win and vim.api.nvim_win_is_valid(win) then
    pcall(vim.api.nvim_set_current_win, win)
  end
end

--- The buffer shown in the code window, or nil if there is no code window.
--- Used as the default target for buffer-content tools when no `buffer=` arg
--- is given (more accurate than scanning for any visible normal-file buffer).
--- @return integer|nil bufnr
function M.code_buf()
  local win = M.code_win()
  return win and vim.api.nvim_win_get_buf(win) or nil
end

--- Display `path` in a code window per the placement policy, optionally jumping
--- to `line` (1-based), and return the window it landed in.
---
--- Precedence: a window already showing the file (if reuse_visible) → the code
--- window (edited in place, no focus change) → the no_code_window strategy when
--- no code window exists. Focus then follows the `focus` policy.
--- @param path string
--- @param line integer|nil
--- @return integer|nil winid, string|nil err
function M.open(path, line)
  if not path or path == "" then
    return nil, "open requires a path"
  end
  local esc = vim.fn.fnameescape(path)
  local from_win = vim.api.nvim_get_current_win()
  local from_tab = vim.api.nvim_get_current_tabpage()
  local target

  -- 1. Reuse a window already showing this file.
  if opt("reuse_visible") then
    local bufnr = vim.fn.bufnr(path)
    if bufnr ~= -1 then
      for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
        if vim.api.nvim_win_get_buf(win) == bufnr then
          target = win
          break
        end
      end
    end
  end

  -- 2. Reuse the code window (edit in place — does not steal focus).
  if not target then
    local cw = M.code_win()
    if cw then
      local ok, err = pcall(function()
        vim.api.nvim_win_call(cw, function() vim.cmd.edit(esc) end)
      end)
      if not ok then return nil, "edit failed: " .. tostring(err) end
      target = cw
    end
  end

  -- 3. No code window — apply the strategy (these move focus to the new window).
  if not target then
    local strat = opt("no_code_window")
    local cmd = ({
      tab = "tabedit ",
      split = "split ",
      vsplit = "vsplit ",
      replace = "edit ",
    })[strat] or "tabedit "
    local ok, err = pcall(vim.cmd, cmd .. esc)
    if not ok then return nil, "open failed: " .. tostring(err) end
    target = vim.api.nvim_get_current_win()
  end

  -- Jump to the requested line in the target window.
  if line and vim.api.nvim_win_is_valid(target) then
    pcall(vim.api.nvim_win_set_cursor, target, { line, 0 })
  end

  -- Focus policy.
  if opt("focus") == "file" then
    if vim.api.nvim_win_is_valid(target) then
      pcall(vim.api.nvim_set_current_win, target)
    end
  else -- "chat" — restore the original focus (tab then window).
    pcall(vim.api.nvim_set_current_tabpage, from_tab)
    pcall(vim.api.nvim_set_current_win, from_win)
  end

  return target
end

return M
