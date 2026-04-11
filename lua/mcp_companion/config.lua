--- mcp-companion.nvim — Configuration
--- @module mcp_companion.config

local M = {}

--- @class MCPCompanion.BridgeConfig
--- @field config? string Path to servers.json (auto-detected if nil)
--- @field port number Default 9741
--- @field host string Default "127.0.0.1"
--- @field idle_timeout string Default "30m" (sharedserver grace period)
--- @field python_cmd string Default "python3"
--- @field startup_timeout number Seconds to wait for bridge health. Default 30.
--- @field request_timeout number Default timeout for MCP requests in seconds. Default 60.
--- @field token_key? string Encryption key for OAuth token storage (or set MCP_BRIDGE_TOKEN_KEY env var)
--- @field log_file? string Path for bridge Python-side log file (default: nil — relies on sharedserver stderr capture)

--- @class MCPCompanion.Config
--- @field bridge MCPCompanion.BridgeConfig
--- @field native_servers table<string, {enabled: boolean}>
--- @field auto_approve boolean|fun(tool_name: string, server_name: string, tool_ctx: table): boolean
--- @field system_prompt_resources? boolean|string[] Resource name patterns to inject into system prompt
--- @field ui {enabled: boolean, width: number, height: number, border: string}
--- @field log {level: string, file: boolean}
--- @field global_env table<string, string>
--- @field on_ready? fun(bridge: table)
--- @field on_error? fun(err: string)

--- @type MCPCompanion.Config
M.defaults = {
  bridge = {
    config = nil,
    port = 9741,
    host = "127.0.0.1",
    idle_timeout = "30m",
    python_cmd = "python3",
    startup_timeout = 30,
    request_timeout = 60,
    token_key = nil,
    log_file = nil,
  },

  native_servers = {
    neovim = { enabled = true },
  },

  auto_approve = false,

  system_prompt_resources = nil,

  ui = {
    enabled = true,
    width = 0.8,
    height = 0.7,
    border = "rounded",
  },

  log = {
    level = "warn",
    file = true,
  },

  global_env = {},

  on_ready = nil,
  on_error = nil,
}

--- @type MCPCompanion.Config|nil
local _config = nil

--- Resolve the plugin's own directory (for locating bridge/ subdir)
--- @return string|nil plugin_dir
local function _plugin_dir()
  -- Works in lazy.nvim, packer, or manual runtimepath
  local info = debug.getinfo(1, "S")
  if info and info.source and info.source:sub(1, 1) == "@" then
    -- source is @/path/to/lua/mcp_companion/config.lua
    local src = info.source:sub(2)
    return vim.fn.fnamemodify(src, ":h:h:h") -- up to plugin root
  end
  return nil
end

--- Resolve bridge python command (prefer plugin-local venv)
--- @param user_cmd? string User-specified python command
--- @return string python_cmd
local function _resolve_python_cmd(user_cmd)
  if user_cmd and user_cmd ~= "python3" then
    return user_cmd -- user explicitly set a custom path
  end

  -- Look for plugin-local venv (created by build step)
  local root = _plugin_dir()
  if root then
    local venv_python = root .. "/bridge/.venv/bin/python"
    if vim.fn.executable(venv_python) == 1 then
      return venv_python
    end
  end

  return user_cmd or "python3"
end

--- Config search paths for servers.json
--- @return string[] candidates
local function _config_candidates()
  local cwd = vim.fn.getcwd()
  return {
    cwd .. "/.mcphub/servers.json",
    cwd .. "/servers.json",
    cwd .. "/.mcp/servers.json",
    cwd .. "/.mcp.json",
    vim.fn.stdpath("config") .. "/mcphub/servers.json",
    vim.fn.stdpath("data") .. "/mcp-companion/servers.json",
  }
end

--- Validate config and return list of issues
--- @param cfg MCPCompanion.Config
--- @return string[] issues
local function _validate(cfg)
  local issues = {}

  -- Port range
  if cfg.bridge.port < 1024 or cfg.bridge.port > 65535 then
    table.insert(issues, string.format("bridge.port %d out of range (1024-65535)", cfg.bridge.port))
  end

  -- Config file exists (if specified)
  if cfg.bridge.config and vim.fn.filereadable(cfg.bridge.config) ~= 1 then
    table.insert(issues, string.format("bridge.config file not found: %s", cfg.bridge.config))
  end

  -- auto_approve type
  if cfg.auto_approve ~= nil and type(cfg.auto_approve) ~= "boolean" and type(cfg.auto_approve) ~= "function" then
    table.insert(issues, "auto_approve must be boolean or function")
  end

  -- startup_timeout
  if cfg.bridge.startup_timeout < 1 then
    table.insert(issues, "bridge.startup_timeout must be >= 1")
  end

  return issues
end

--- Merge user opts with defaults, auto-detect config path, validate
--- @param opts table
--- @return string[] issues Validation issues (empty if valid)
function M.setup(opts)
  _config = vim.tbl_deep_extend("force", {}, M.defaults, opts or {})

  -- Resolve python command (prefer plugin-local venv)
  _config.bridge.python_cmd = _resolve_python_cmd(_config.bridge.python_cmd)

  -- Auto-detect config path if not set
  if not _config.bridge.config then
    for _, path in ipairs(_config_candidates()) do
      if vim.fn.filereadable(path) == 1 then
        _config.bridge.config = path
        break
      end
    end
  end

  return _validate(_config)
end

--- Get current config (returns defaults if setup not called)
--- @return MCPCompanion.Config
function M.get()
  return _config or M.defaults
end

--- Get the resolved bridge URL
--- @return string
function M.bridge_url()
  local cfg = M.get()
  return string.format("http://%s:%d", cfg.bridge.host, cfg.bridge.port)
end

return M
