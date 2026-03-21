--- mcp-companion.nvim — Logging
--- @module mcp_companion.log

local M = {}

local _level = "warn"
local _notify_level = "error"   -- vim.notify only fires at this level and above
local _levels = { trace = 0, debug = 1, info = 2, warn = 3, error = 4 }
local _vim_levels = {
  trace = vim.log.levels.TRACE,
  debug = vim.log.levels.DEBUG,
  info = vim.log.levels.INFO,
  warn = vim.log.levels.WARN,
  error = vim.log.levels.ERROR,
}
local _log_file = nil
local _file_enabled = false

--- Setup logging
--- @param opts? {level?: string, notify?: string, file?: boolean}
function M.setup(opts)
  opts = opts or {}
  _level = opts.level or "warn"
  _notify_level = opts.notify or "error"
  _file_enabled = opts.file ~= false

  if _file_enabled then
    local log_dir = vim.fn.stdpath("log") or vim.fn.stdpath("cache")
    local log_path = log_dir .. "/mcp-companion.log"
    -- Ensure dir exists
    vim.fn.mkdir(log_dir, "p")
    _log_file = log_path
  end
end

--- Write to log file
--- @param level string
--- @param msg string
local function _write_file(level, msg)
  if not _log_file then
    return
  end
  local f = io.open(_log_file, "a")
  if f then
    f:write(string.format("[%s] [%s] %s\n", os.date("%Y-%m-%d %H:%M:%S"), level:upper(), msg))
    f:close()
  end
end

--- Log at given level
--- @param level string
--- @param msg string
--- @param ... any
local function _log(level, msg, ...)
  local num = _levels[level] or 0
  local threshold = _levels[_level] or 0
  local notify_threshold = _levels[_notify_level] or _levels.error

  -- Capture varargs into a table so closures can access them
  local args = { ... }
  local formatted
  local function get_formatted()
    if formatted == nil then
      local ok, s = pcall(string.format, msg, unpack(args))
      formatted = ok and s or msg
    end
    return formatted
  end

  -- Always write to file if file logging enabled and level >= debug
  if _file_enabled and num >= _levels.debug then
    _write_file(level, get_formatted())
  end

  -- Only notify user if at or above both the log threshold and notify threshold
  if num >= threshold and num >= notify_threshold then
    local text = get_formatted()
    vim.schedule(function()
      vim.notify("[mcp-companion] " .. text, _vim_levels[level] or vim.log.levels.INFO)
    end)
  end
end

function M.trace(msg, ...)
  _log("trace", msg, ...)
end

function M.debug(msg, ...)
  _log("debug", msg, ...)
end

function M.info(msg, ...)
  _log("info", msg, ...)
end

function M.warn(msg, ...)
  _log("warn", msg, ...)
end

function M.error(msg, ...)
  _log("error", msg, ...)
end

--- Get log file path
--- @return string|nil
function M.get_log_path()
  return _log_file
end

return M
