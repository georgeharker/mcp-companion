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
--- @type number|nil Bufnr of the CC chat window that was focused when the status
--- window was opened.  Used to show per-session server state.
local _source_bufnr = nil

-- ─────────────────────────────────────────────────────────────────
-- Symbols and formatting helpers
-- ─────────────────────────────────────────────────────────────────

local icons = {
  connected = "●",
  disconnected = "○",
  disabled = "⊘",
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
  elseif status == "disabled" then
    return icons.disabled, "Comment"
  elseif status == "error" then
    return icons.error, "DiagnosticError"
  elseif status == "connecting" or status == "healthy" then
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
--- @field server_name? string Server name this line belongs to (for toggle)

--- @type UILine[]
local _lines = {}

--- Add a plain line
--- @param text string
--- @param hl? string Highlight group for full line
--- @param action? function
--- @param server_name? string
local function add_line(text, hl, action, server_name)
  text = text:gsub("\n", " ") -- nvim_buf_set_lines forbids embedded newlines
  local highlights = {}
  if hl then
    table.insert(highlights, { hl, 0, #text })
  end
  table.insert(_lines, { text = text, highlights = highlights, action = action, server_name = server_name })
end

--- Add a line with mixed highlights
--- @param segments table[] {text, hl?}
--- @param action? function
--- @param server_name? string
local function add_segments(segments, action, server_name)
  local text = ""
  local highlights = {}
  for _, seg in ipairs(segments) do
    local start = #text
    text = text .. seg[1]:gsub("\n", " ")
    if seg[2] then
      table.insert(highlights, { seg[2], start, #text })
    end
  end
  table.insert(_lines, { text = text, highlights = highlights, action = action, server_name = server_name })
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
--- @param session_disabled? table<string,boolean> Per-session disabled set for the source chat
local function render_server(srv, session_disabled, project_disabled)
  local icon, hl = status_icon(srv.status or "connected")
  local tools_n = srv.tools and #srv.tools or 0
  local res_n = srv.resources and #srv.resources or 0
  local prompts_n = srv.prompts and #srv.prompts or 0
  local is_expanded = _expanded[srv.name]
  local arrow = is_expanded and icons.collapse or icons.expand
  local disabled_label = srv.disabled and " [disabled]" or ""
  local session_off = session_disabled and session_disabled[srv.name]
  local project_off = project_disabled and project_disabled[srv.name]

  -- Server header line (clickable)
  local header_segments = {
    { "  " .. arrow .. " ", "Comment" },
    { icon .. " ", hl },
    { pad(srv.name, 20) },
  }
  if srv.disabled then
    table.insert(header_segments, { disabled_label, "Comment" })
  else
    table.insert(header_segments, {
      string.format("  %d %s  %d %s  %d %s", tools_n, icons.tool, res_n, icons.resource, prompts_n, icons.prompt),
      "Comment",
    })
  end
  if project_off then
    table.insert(header_segments, { " [project off]", "DiagnosticHint" })
  end
  if session_off then
    table.insert(header_segments, { " [session off]", "DiagnosticWarn" })
  end
  add_segments(header_segments, function()
    _expanded[srv.name] = not _expanded[srv.name]
    M.render()
  end, srv.name)

  if not is_expanded then
    return
  end

  -- Disabled servers: show hint instead of empty tool list
  if srv.disabled then
    add_line("    (press e to enable)", "Comment", nil, srv.name)
    add_line("")
    return
  end

  -- Tools
  if tools_n > 0 then
    add_line("    " .. icons.tool .. " Tools:", "Title", nil, srv.name)
    for _, tool in ipairs(srv.tools) do
      local display = tool._display or tool.name or "?"
      local desc = tool.description or ""
      if #desc > 60 then
        desc = desc:sub(1, 57) .. "..."
      end
      add_line(string.format("      %s  %s", pad(display, 28), desc), "Comment", nil, srv.name)
    end
  end

  -- Resources
  if res_n > 0 then
    add_line("    " .. icons.resource .. " Resources:", "Title", nil, srv.name)
    for _, res in ipairs(srv.resources) do
      local name = res.name or res.uri or "?"
      add_line("      " .. name, "Comment", nil, srv.name)
    end
  end

  -- Prompts
  if prompts_n > 0 then
    add_line("    " .. icons.prompt .. " Prompts:", "Title", nil, srv.name)
    for _, pr in ipairs(srv.prompts) do
      local name = pr.name or "?"
      add_line("      " .. name, "Comment", nil, srv.name)
    end
  end
  add_line("")
end

--- Build the status view lines
--- @param state table
local function build_status_view(state)
  _lines = {}

  -- Resolve per-session disabled state for the source chat buffer (if any)
  local session_disabled = nil
  if _source_bufnr then
    local ok, sc = pcall(require, "mcp_companion.cc.session_commands")
    if ok then
      session_disabled = sc.get_session_state(_source_bufnr)
    end
  end

  -- Resolve per-project disabled state from .mcp-companion.json walked up
  -- from cwd.  Indicator only appears when a project file is in scope, so the
  -- usual "no project file = all visible" case stays uncluttered.
  local project_disabled = nil
  do
    local ok, project = pcall(require, "mcp_companion.project")
    if ok then
      local cfg, _root = project.resolve()
      if cfg then
        local known = {}
        for _, srv in ipairs(state.servers or {}) do
          if srv.name and srv.name ~= "_bridge" then
            table.insert(known, srv.name)
          end
        end
        project_disabled = project.project_disabled_set(cfg, known)
      end
    end
  end

  -- Header
  add_line("")
  render_bridge(state)
  add_line("")
  add_separator()

  -- Session context hint
  if session_disabled and next(session_disabled) then
    add_line("")
    add_line(" Session (current chat):", "Title")
    add_line("   Some servers are hidden from this chat session.", "Comment")
    add_line("")
    add_separator()
  elseif _source_bufnr then
    add_line("")
    add_line(" Session (current chat): all servers active", "Comment")
    add_line("")
    add_separator()
  end

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
        render_server(srv, session_disabled, project_disabled)
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
    { "e", "Special" },
    { " global  ", "Comment" },
    { "p", "Special" },
    { " project  ", "Comment" },
    { "S", "Special" },
    { " session  ", "Comment" },
    { "r", "Special" },
    { " refresh  ", "Comment" },
    { "R", "Special" },
    { " restart  ", "Comment" },
    { "l", "Special" },
    { " logs  ", "Comment" },
    { "<CR>", "Special" },
    { " expand", "Comment" },
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

  -- Capture the current window's buffer before opening the float (the float
  -- takes focus and nvim_get_current_win would return the float afterwards).
  local cur_buf = vim.api.nvim_win_get_buf(vim.api.nvim_get_current_win())
  if vim.bo[cur_buf].filetype == "codecompanion" then
    _source_bufnr = cur_buf
  else
    _source_bufnr = nil
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
    M._fetch_and_render()
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

  map("e", function()
    local cursor = vim.api.nvim_win_get_cursor(_win)
    local line_idx = cursor[1]
    local line_data = _lines[line_idx]
    if not line_data or not line_data.server_name then
      return
    end
    local srv_name = line_data.server_name
    local bridge_mod = require("mcp_companion.bridge")
    local client = bridge_mod.client
    if not client or not client.connected then
      vim.notify("[mcp-companion] Bridge not connected", vim.log.levels.WARN)
      return
    end
    -- Show feedback
    vim.notify(string.format("[mcp-companion] Toggling %s...", srv_name), vim.log.levels.INFO)
    client:toggle_server(srv_name or "", function(err, result)
      if err then
        vim.notify(string.format("[mcp-companion] Toggle failed: %s", tostring(err)), vim.log.levels.ERROR)
      else
        vim.notify(string.format("[mcp-companion] %s", result or "done"), vim.log.levels.INFO)
      end
    end)
  end, "Toggle server enable/disable")

  map("p", function()
    local cursor = vim.api.nvim_win_get_cursor(_win)
    local line_idx = cursor[1]
    local line_data = _lines[line_idx]
    if not line_data or not line_data.server_name then
      return
    end
    local srv_name = line_data.server_name
    if srv_name == "_bridge" then return end

    local state = require("mcp_companion.state")
    local servers = state.field("servers") or {}
    local known = {}
    for _, srv in ipairs(servers) do
      if srv.name and srv.name ~= "_bridge" then
        table.insert(known, srv.name)
      end
    end
    if #known == 0 then
      vim.notify("[mcp-companion] No connected servers — bridge state not loaded yet",
        vim.log.levels.WARN)
      return
    end

    local project = require("mcp_companion.project")
    local ok, result = pcall(project.toggle_in_project_file, srv_name, known)
    if not ok then
      vim.notify("[mcp-companion] Project toggle failed: " .. tostring(result),
        vim.log.levels.ERROR)
      return
    end

    local new_state_label = result.now_visible and "visible" or "hidden"
    vim.notify(string.format(
      "[mcp-companion] %s %s in project (%s)",
      srv_name, new_state_label, result.path
    ), vim.log.levels.INFO)

    -- Re-render so the [project off] indicator updates immediately.
    M.render()
  end, "Toggle server in .mcp-companion.json")

  map("S", function()
    local cursor = vim.api.nvim_win_get_cursor(_win)
    local line_idx = cursor[1]
    local line_data = _lines[line_idx]
    if not line_data or not line_data.server_name then
      return
    end
    local srv_name = line_data.server_name
    if srv_name == "_bridge" then return end

    if not _source_bufnr then
      vim.notify(
        "[mcp-companion] No chat associated with this status window — open :MCPStatus from a CodeCompanion chat",
        vim.log.levels.WARN
      )
      return
    end

    local cc_ok, codecompanion = pcall(require, "codecompanion")
    if not cc_ok then
      vim.notify("[mcp-companion] CodeCompanion not available", vim.log.levels.WARN)
      return
    end
    local chat = codecompanion.buf_get_chat(_source_bufnr)
    if not chat then
      vim.notify("[mcp-companion] Source buffer is no longer a chat",
        vim.log.levels.WARN)
      return
    end

    local sc_ok, sc = pcall(require, "mcp_companion.cc.session_commands")
    if not sc_ok then
      vim.notify("[mcp-companion] Session commands not available",
        vim.log.levels.WARN)
      return
    end

    sc.toggle_server_for_session(chat, srv_name, function(err, info)
      if err then
        vim.notify("[mcp-companion] Session toggle failed: " .. err,
          vim.log.levels.ERROR)
        return
      end
      vim.notify(string.format("[mcp-companion] %s %s for this chat session",
        info.action, info.server), vim.log.levels.INFO)
    end)
  end, "Toggle server for this chat session")

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

  -- Fetch fresh session status from bridge before initial render
  M._fetch_and_render()
end

--- Fetch session status from bridge and re-render.
--- Used on open and can be called to refresh.
function M._fetch_and_render()
  -- Initial render with cached/empty state
  M.render()

  -- If we have a source chat buffer, fetch live session status
  if _source_bufnr then
    local cc_ok, codecompanion = pcall(require, "codecompanion")
    local chat = cc_ok and codecompanion.buf_get_chat(_source_bufnr)
    if chat then
      local sc_ok, sc = pcall(require, "mcp_companion.cc.session_commands")
      if sc_ok and sc.fetch_session_status then
        sc.fetch_session_status(chat, function(err, _)
          if not err then
            -- State is cached in _session_state by fetch_session_status
            -- Re-render to show updated state
            vim.schedule(function()
              if _buf and vim.api.nvim_buf_is_valid(_buf) then
                M.render()
              end
            end)
          end
        end)
      end
    end
  end
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
  _source_bufnr = nil
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
