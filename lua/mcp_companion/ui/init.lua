--- mcp-companion.nvim — Status UI
--- Single floating window with bridge status, servers, tools/resources/prompts, logs
--- @module mcp_companion.ui

local M = {}

--- @type number|nil Buffer handle
local _buf = nil
--- @type number|nil Window handle
local _win = nil
--- @type function|nil State unsubscribe handle
local _unsub = nil
--- @type string Current view tab
local _view = "status" -- "status" | "logs"
--- @type table<string, boolean> Expanded server sections
local _expanded = {}
--- @type number|nil Autocmd group
local _augroup = nil

-- ─────────────────────────────────────────────────────────────────
-- Symbols and formatting helpers
-- ─────────────────────────────────────────────────────────────────

local icons = {
  connected = "●",
  disconnected = "○",
  error = "✗",
  connecting = "◌",
  tool = "⚡",
  resource = "📄",
  prompt = "💬",
  expand = "▸",
  collapse = "▾",
  separator = "─",
  bridge_on = "⬢",
  bridge_off = "⬡",
}

--- @param status string
--- @return string icon, string hl_group
local function status_icon(status)
  if status == "connected" then
    return icons.connected, "DiagnosticOk"
  elseif status == "error" then
    return icons.error, "DiagnosticError"
  elseif status == "connecting" then
    return icons.connecting, "DiagnosticWarn"
  else
    return icons.disconnected, "Comment"
  end
end

--- Pad a string to width
--- @param s string
--- @param w number
--- @return string
local function pad(s, w)
  if #s >= w then
    return s
  end
  return s .. string.rep(" ", w - #s)
end

-- ─────────────────────────────────────────────────────────────────
-- Rendering: Status view
-- ─────────────────────────────────────────────────────────────────

--- @class UILine
--- @field text string Plain text of the line
--- @field highlights table[] {group, col_start, col_end}
--- @field action? function Action when <CR> pressed on this line

--- @type UILine[]
local _lines = {}

--- Add a plain line
--- @param text string
--- @param hl? string Highlight group for full line
--- @param action? function
local function add_line(text, hl, action)
  local highlights = {}
  if hl then
    table.insert(highlights, { hl, 0, #text })
  end
  table.insert(_lines, { text = text, highlights = highlights, action = action })
end

--- Add a line with mixed highlights
--- @param segments table[] {text, hl?}
--- @param action? function
local function add_segments(segments, action)
  local text = ""
  local highlights = {}
  for _, seg in ipairs(segments) do
    local start = #text
    text = text .. seg[1]
    if seg[2] then
      table.insert(highlights, { seg[2], start, #text })
    end
  end
  table.insert(_lines, { text = text, highlights = highlights, action = action })
end

--- Add a separator line
--- @param width? number
local function add_separator(width)
  add_line(string.rep(icons.separator, width or 50), "Comment")
end

--- Render bridge status section
--- @param state table
local function render_bridge(state)
  local b = state.bridge or {}
  local icon, hl = status_icon(b.status or "disconnected")

  add_segments({
    { " " .. icon .. " ", hl },
    { "Bridge: ", "Title" },
    { b.status or "disconnected", hl },
  })

  if b.port then
    add_line("   Port: " .. tostring(b.port), "Comment")
  end
  if b.pid then
    add_line("   PID:  " .. tostring(b.pid), "Comment")
  end
  if b.clients and b.clients > 0 then
    add_line("   Clients: " .. tostring(b.clients), "Comment")
  end
  if b.error then
    add_line("   Error: " .. b.error, "DiagnosticError")
  end
end

--- Render a single server
--- @param srv MCPCompanion.ServerInfo
local function render_server(srv)
  local icon, hl = status_icon(srv.status or "connected")
  local tools_n = srv.tools and #srv.tools or 0
  local res_n = srv.resources and #srv.resources or 0
  local prompts_n = srv.prompts and #srv.prompts or 0
  local is_expanded = _expanded[srv.name]
  local arrow = is_expanded and icons.collapse or icons.expand

  -- Server header line (clickable)
  add_segments({
    { "  " .. arrow .. " ", "Comment" },
    { icon .. " ", hl },
    { pad(srv.name, 20) },
    { string.format("  %d %s  %d %s  %d %s", tools_n, icons.tool, res_n, icons.resource, prompts_n, icons.prompt), "Comment" },
  }, function()
    _expanded[srv.name] = not _expanded[srv.name]
    M.render()
  end)

  if not is_expanded then
    return
  end

  -- Tools
  if tools_n > 0 then
    add_line("    " .. icons.tool .. " Tools:", "Title")
    for _, tool in ipairs(srv.tools) do
      local display = tool._display or tool.name or "?"
      local desc = tool.description or ""
      if #desc > 60 then
        desc = desc:sub(1, 57) .. "..."
      end
      add_line(string.format("      %s  %s", pad(display, 28), desc), "Comment")
    end
  end

  -- Resources
  if res_n > 0 then
    add_line("    " .. icons.resource .. " Resources:", "Title")
    for _, res in ipairs(srv.resources) do
      local name = res.name or res.uri or "?"
      add_line("      " .. name, "Comment")
    end
  end

  -- Prompts
  if prompts_n > 0 then
    add_line("    " .. icons.prompt .. " Prompts:", "Title")
    for _, pr in ipairs(srv.prompts) do
      local name = pr.name or "?"
      add_line("      " .. name, "Comment")
    end
  end
  add_line("")
end

--- Build the status view lines
--- @param state table
local function build_status_view(state)
  _lines = {}

  -- Header
  add_line("")
  render_bridge(state)
  add_line("")
  add_separator()

  -- Bridge servers
  local servers = state.servers or {}
  if #servers == 0 then
    add_line("")
    add_line("  (no servers connected)", "Comment")
  else
    add_line("")
    add_line(" Bridge Servers", "Title")
    add_line("")
    for _, srv in ipairs(servers) do
      if srv.name ~= "_bridge" then
        render_server(srv)
      end
    end
  end

  -- Native servers
  add_separator()
  local native_ok, native = pcall(require, "mcp_companion.native")
  local native_servers = native_ok and native.get_servers() or {}
  if #native_servers > 0 then
    add_line("")
    add_line(" Native Servers", "Title")
    add_line("")
    for _, srv in ipairs(native_servers) do
      render_server(srv)
    end
  end

  -- Footer
  add_separator()
  add_line("")
  add_segments({
    { " q", "Special" },
    { " close  ", "Comment" },
    { "r", "Special" },
    { " refresh  ", "Comment" },
    { "R", "Special" },
    { " restart  ", "Comment" },
    { "l", "Special" },
    { " logs  ", "Comment" },
    { "<CR>", "Special" },
    { " expand/collapse", "Comment" },
  })
end

-- ─────────────────────────────────────────────────────────────────
-- Rendering: Logs view
-- ─────────────────────────────────────────────────────────────────

--- Build the logs view lines
--- @param state table
local function build_logs_view(state)
  _lines = {}

  add_line("")
  add_line(" Logs", "Title")
  add_line("")
  add_separator()

  -- Errors first
  local errors = state.errors or {}
  if #errors > 0 then
    add_line("")
    add_line(" Errors (" .. #errors .. ")", "DiagnosticError")
    add_line("")
    for i, err in ipairs(errors) do
      if i > 20 then
        add_line("  ... " .. (#errors - 20) .. " more", "Comment")
        break
      end
      local ts = err.timestamp and os.date("%H:%M:%S", err.timestamp) or "?"
      add_line(string.format("  [%s] %s", ts, err.message or "?"), "DiagnosticError")
    end
  end

  -- Recent logs
  local logs = state.logs or {}
  add_line("")
  add_line(" Recent Logs (" .. #logs .. ")", "Title")
  add_line("")
  if #logs == 0 then
    add_line("  (no logs)", "Comment")
  else
    local start = math.max(1, #logs - 50)
    for i = #logs, start, -1 do
      local entry = logs[i]
      local ts = entry.timestamp and os.date("%H:%M:%S", entry.timestamp) or "?"
      local level = entry.level or "info"
      local hl = level == "error" and "DiagnosticError" or level == "warn" and "DiagnosticWarn" or "Comment"
      add_line(string.format("  [%s] [%s] %s", ts, level, entry.message or ""), hl)
    end
  end

  -- Footer
  add_line("")
  add_separator()
  add_line("")
  add_segments({
    { " q", "Special" },
    { " close  ", "Comment" },
    { "s", "Special" },
    { " status  ", "Comment" },
    { "C", "Special" },
    { " clear logs", "Comment" },
  })
end

-- ─────────────────────────────────────────────────────────────────
-- Window management
-- ─────────────────────────────────────────────────────────────────

--- Toggle the status window
function M.toggle()
  if _win and vim.api.nvim_win_is_valid(_win) then
    M.close()
  else
    M.open()
  end
end

--- Open the status window
function M.open()
  if _win and vim.api.nvim_win_is_valid(_win) then
    vim.api.nvim_set_current_win(_win)
    return
  end

  local config = require("mcp_companion.config").get()

  -- Create buffer
  _buf = vim.api.nvim_create_buf(false, true)
  vim.bo[_buf].buftype = "nofile"
  vim.bo[_buf].bufhidden = "wipe"
  vim.bo[_buf].filetype = "mcp-companion"
  vim.bo[_buf].swapfile = false

  -- Calculate window size
  local ui_opts = config.ui or {}
  local width = math.floor(vim.o.columns * (ui_opts.width or 0.8))
  local height = math.floor(vim.o.lines * (ui_opts.height or 0.7))
  local row = math.floor((vim.o.lines - height) / 2)
  local col = math.floor((vim.o.columns - width) / 2)

  -- Open floating window
  _win = vim.api.nvim_open_win(_buf, true, {
    relative = "editor",
    width = width,
    height = height,
    row = row,
    col = col,
    style = "minimal",
    border = ui_opts.border or "rounded",
    title = " MCP Companion ",
    title_pos = "center",
  })

  -- Window options
  vim.wo[_win].wrap = false
  vim.wo[_win].cursorline = true

  -- Set up keymaps
  local function map(key, fn, desc)
    vim.keymap.set("n", key, fn, { buffer = _buf, nowait = true, desc = desc })
  end

  map("q", function()
    M.close()
  end, "Close")

  map("r", function()
    M.render()
  end, "Refresh")

  map("R", function()
    local bridge = require("mcp_companion.bridge")
    bridge.restart()
  end, "Restart bridge")

  map("l", function()
    _view = "logs"
    M.render()
  end, "Logs view")

  map("s", function()
    _view = "status"
    M.render()
  end, "Status view")

  map("C", function()
    local state = require("mcp_companion.state")
    state.update("errors", {})
    state.update("logs", {})
    M.render()
  end, "Clear logs")

  map("<CR>", function()
    local cursor = vim.api.nvim_win_get_cursor(_win)
    local line_idx = cursor[1]
    local line_data = _lines[line_idx]
    if line_data and line_data.action then
      line_data.action()
    end
  end, "Activate")

  -- Subscribe to state changes for live updates
  local state = require("mcp_companion.state")
  _unsub = state.subscribe("ui", function()
    if _buf and vim.api.nvim_buf_is_valid(_buf) then
      vim.schedule(function()
        M.render()
      end)
    end
  end)

  -- Clean up on window close
  _augroup = vim.api.nvim_create_augroup("MCPCompanionUI", { clear = true })
  vim.api.nvim_create_autocmd("WinClosed", {
    group = _augroup,
    pattern = tostring(_win),
    once = true,
    callback = function()
      M._cleanup()
    end,
  })

  vim.api.nvim_create_autocmd("VimResized", {
    group = _augroup,
    callback = function()
      if _win and vim.api.nvim_win_is_valid(_win) then
        M.render()
      end
    end,
  })

  M.render()
end

--- Clean up resources without closing window
function M._cleanup()
  if _unsub then
    _unsub()
    _unsub = nil
  end
  if _augroup then
    pcall(vim.api.nvim_del_augroup_by_id, _augroup)
    _augroup = nil
  end
  _win = nil
  _buf = nil
end

--- Close the status window
function M.close()
  if _win and vim.api.nvim_win_is_valid(_win) then
    vim.api.nvim_win_close(_win, true)
  end
  M._cleanup()
end

--- Render the current view
function M.render()
  if not _buf or not vim.api.nvim_buf_is_valid(_buf) then
    return
  end

  local state = require("mcp_companion.state").get()

  if _view == "logs" then
    build_logs_view(state)
  else
    build_status_view(state)
  end

  -- Write lines to buffer
  local text_lines = {}
  for _, line in ipairs(_lines) do
    table.insert(text_lines, line.text)
  end

  vim.bo[_buf].modifiable = true
  vim.api.nvim_buf_set_lines(_buf, 0, -1, false, text_lines)
  vim.bo[_buf].modifiable = false

  -- Apply highlights
  local ns = vim.api.nvim_create_namespace("mcp_companion_ui")
  vim.api.nvim_buf_clear_namespace(_buf, ns, 0, -1)

  for i, line in ipairs(_lines) do
    for _, hl in ipairs(line.highlights) do
      vim.api.nvim_buf_set_extmark(_buf, ns, i - 1, hl[2], {
        end_col = hl[3],
        hl_group = hl[1],
      })
    end
  end
end

--- Check if UI is open
--- @return boolean
function M.is_open()
  return _win ~= nil and vim.api.nvim_win_is_valid(_win)
end

return M
