--- mcp-companion.nvim — Logging
--- @module mcp_companion.log

local M = {}

local _level = "warn"
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
--- @param opts? {level?: string, file?: boolean}
function M.setup(opts)
  opts = opts or {}
  _level = opts.level or "warn"
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

  -- Always write to file if file logging enabled and level >= debug
  if _file_enabled and num >= _levels.debug then
    local ok, formatted = pcall(string.format, msg, ...)
    _write_file(level, ok and formatted or msg)
  end

  -- Only notify user if at threshold
  if num >= threshold then
    local ok, formatted = pcall(string.format, msg, ...)
    local text = ok and formatted or msg
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
