--- mcp-companion.nvim — Bridge lifecycle management
--- @module mcp_companion.bridge

local M = {}

local log = require("mcp_companion.log")
local http = require("mcp_companion.http")

--- @type MCPCompanion.Config
local _config ---@diagnostic disable-line: missing-fields

--- @type MCPCompanion.Client|nil Bridge MCP client instance
M.client = nil

--- @type boolean Whether setup() has been called
local _configured = false

--- @type any Direct subprocess handle (fallback mode)
M._job = nil

--- Setup bridge (stores config, does not start yet)
--- @param config MCPCompanion.Config
function M.setup(config)
  _config = config
  _configured = true
end

--- Start the bridge process and connect
function M.start()
  if not _configured then
    log.error("Bridge not configured — call setup() first")
    return
  end

  local state = require("mcp_companion.state")

  if not _config.bridge.config then
    log.error("No servers.json config path found")
    state.update("bridge", { status = "error", error = "No config file" })
    state.emit("bridge_error", "No config file")
    if _config.on_error then
      _config.on_error("No servers.json config path found")
    end
    return
  end

  state.update("bridge", { status = "connecting", port = _config.bridge.port, error = nil })

  -- Check if bridge is already running (another Neovim instance started it)
  M._check_existing(function(running)
    if running then
      log.info("Bridge already running on port %d, connecting...", _config.bridge.port)
      -- Register with sharedserver so this Neovim instance holds a refcount.
      -- Without this, only the instance that originally started the bridge
      -- keeps it alive — when that instance exits the bridge dies even though
      -- other instances are still connected.
      local ss_ok, ss = pcall(require, "sharedserver")
      if ss_ok and ss.start then
        M._register_with_sharedserver(ss)
        pcall(ss.start, "mcp-bridge")
      end
      state.update("bridge", { status = "healthy" })
      M._create_client()
    elseif pcall(require, "sharedserver") then
      M._start_with_sharedserver()
    else
      log.info("sharedserver not found, starting bridge directly")
      M._start_direct()
    end
  end)
end

--- Check if bridge is already running on the configured port
--- @param callback fun(running: boolean)
function M._check_existing(callback)
  local url = string.format("http://%s:%d/health", _config.bridge.host, _config.bridge.port)
  http.request({
    url = url,
    method = "get",
    timeout = 1000,
    callback = function(response)
      callback(response.status == 200)
    end,
  })
end

--- Build the bridge command + args
--- @return string[] cmd
local function _bridge_cmd()
  local cmd = {
    _config.bridge.python_cmd,
    "-m", "mcp_bridge",
    "--config", _config.bridge.config,
    "--port", tostring(_config.bridge.port),
    "--host", _config.bridge.host or "127.0.0.1",
  }
  if _config.bridge.log_file then
    table.insert(cmd, "--log-file")
    table.insert(cmd, _config.bridge.log_file)
  end
  return cmd
end

--- Build environment for bridge process
--- @return table<string,string>
local function _bridge_env()
  local env = vim.tbl_extend("force", _config.global_env or {}, {
    MCP_BRIDGE_PORT = tostring(_config.bridge.port),
  })
  -- Pass encryption key if configured
  if _config.bridge.token_key then
    env.MCP_BRIDGE_TOKEN_KEY = _config.bridge.token_key
  end
  return env
end

--- Register mcp-bridge with sharedserver if not already registered.
--- Safe to call multiple times; no-ops when already registered.
--- @param ss table sharedserver module
function M._register_with_sharedserver(ss)
  if ss.is_registered and ss.is_registered("mcp-bridge") then
    log.debug("mcp-bridge already registered with sharedserver, skipping re-registration")
    return
  end

  local cmd_parts = _bridge_cmd()
  local env = _bridge_env()
  local log_file = vim.fn.stdpath("log") .. "/mcp-bridge.log"

  -- Register with lazy=true to prevent auto-start on VimEnter;
  -- ss.start() is called explicitly by each call-site.
  ss.register("mcp-bridge", {
    command = cmd_parts[1],
    args = vim.list_slice(cmd_parts, 2),
    env = env,
    idle_timeout = _config.bridge.idle_timeout or "30m",
    log_file = log_file,
    lazy = true,
    on_start = function(pid)
      log.info("sharedserver started mcp-bridge (pid %d)", pid)
    end,
    on_exit = function(code)
      vim.schedule(function()
        local _state = require("mcp_companion.state")
        if code ~= 0 then
          log.error("mcp-bridge exited with code %d", code)
          _state.update("bridge", { status = "error", error = "Process exited: code " .. code })
          _state.emit("bridge_error", "Process exited with code " .. code)
        else
          log.info("mcp-bridge exited normally")
          _state.update("bridge", { status = "disconnected" })
        end
      end)
    end,
  })

  log.debug("mcp-bridge registered with sharedserver")
end

--- Start via sharedserver Lua plugin (shared process across Neovim instances)
function M._start_with_sharedserver()
  local ss = require("sharedserver")
  local log_file = vim.fn.stdpath("log") .. "/mcp-bridge.log"

  M._register_with_sharedserver(ss)

  log.info("Starting bridge via sharedserver Lua plugin (log: %s)", log_file)

  local ok, result = pcall(ss.start, "mcp-bridge")
  if not ok or result == false then
    log.error("sharedserver.start() failed (%s) — falling back to direct start",
      not ok and tostring(result) or "returned false")
    M._start_direct()
    return
  end

  M._wait_and_connect()
end

--- Start bridge as a direct subprocess (fallback without sharedserver)
function M._start_direct()
  local state = require("mcp_companion.state")
  local cmd = _bridge_cmd()

  log.info("Starting bridge: %s", table.concat(cmd, " "))

  M._job = vim.system(cmd, {
    text = true,
    env = _bridge_env(),
    stderr = function(_, data)
      if data then
        log.debug("bridge stderr: %s", data:gsub("\n$", ""))
      end
    end,
  }, function(result)
    vim.schedule(function()
      if result.code ~= 0 then
        log.error("Bridge exited with code %d", result.code)
        state.update("bridge", { status = "error", error = "Process exited: code " .. result.code })
        state.emit("bridge_error", result.stderr or "unknown error")
      else
        log.info("Bridge process exited normally")
        state.update("bridge", { status = "disconnected" })
      end
      M._job = nil
    end)
  end)

  M._wait_and_connect()
end

--- Poll health endpoint then create MCP client
function M._wait_and_connect()
  local state = require("mcp_companion.state")
  local url = string.format("http://%s:%d/health", _config.bridge.host, _config.bridge.port)
  local attempts = 0
  local max_attempts = _config.bridge.startup_timeout or 30

  local timer = vim.uv.new_timer()
  if not timer then
    log.error("Failed to create health-check timer")
    state.update("bridge", { status = "error", error = "Timer creation failed" })
    return
  end
  timer:start(
    500, -- initial delay
    1000, -- retry every 1s
    vim.schedule_wrap(function()
      attempts = attempts + 1
      if attempts > max_attempts then
        timer:stop()
        timer:close()
        log.error("Bridge health check timed out after %ds", max_attempts)
        state.update("bridge", { status = "error", error = "Health check timeout" })
        state.emit("bridge_error", "Health check timeout")
        if _config.on_error then
          _config.on_error("Bridge startup timed out")
        end
        return
      end

      http.request({
        url = url,
        method = "get",
        timeout = 1000,
        callback = function(response)
          if response.status == 200 then
            timer:stop()
            timer:close()
            log.info("Bridge healthy on port %d (after %ds)", _config.bridge.port, attempts)
            state.update("bridge", { status = "healthy" })
            M._create_client()
          else
            -- Connection failed or non-200 - just wait for next attempt
            log.debug("Health check attempt %d failed (status=%s)", attempts, response.status)
          end
        end,
      })
    end)
  )
end

--- Create MCP client and connect
function M._create_client()
  local state = require("mcp_companion.state")
  local Client = require("mcp_companion.bridge.client")

  local client = Client.new({
    host = _config.bridge.host or "127.0.0.1",
    port = _config.bridge.port,
    request_timeout = _config.bridge.request_timeout,
  })
  M.client = client

  client:connect(function(ok, err)
    if ok then
      state.update("bridge", { status = "connected" })
      state.emit("bridge_ready")
      log.info("MCP client connected (%d tools, %d resources, %d prompts)",
        #client.tools, #client.resources, #client.prompts)
    else
      state.update("bridge", { status = "error", error = tostring(err) })
      state.emit("bridge_error", err)
      state.add_error("MCP connection failed: " .. tostring(err))
      log.error("MCP client connection failed: %s", tostring(err))
      if _config.on_error then
        _config.on_error("MCP connection failed: " .. tostring(err))
      end
    end
  end)
end

--- Stop the bridge
function M.stop()
  local state = require("mcp_companion.state")

  local client = M.client
  if client then
    client:disconnect()
    M.client = nil
  end

  -- Stop via sharedserver if available
  local ss_ok, ss = pcall(require, "sharedserver")
  if ss_ok then
    pcall(ss.stop, "mcp-bridge")
  end

  -- Kill direct job if we have one
  if M._job then
    pcall(function()
      M._job:kill(15) -- SIGTERM
    end)
    M._job = nil
  end

  state.update("bridge", { status = "disconnected", error = nil })
  log.info("Bridge stopped")
end

--- Restart the bridge (stop then start)
--- @param opts? {force?: boolean} If force=true, kill the bridge even if other clients are attached
function M.restart(opts)
  opts = opts or {}
  local ss_ok, ss = pcall(require, "sharedserver")

  if ss_ok then
    local info = ss.status("mcp-bridge")
    local refcount = info and info.refcount or 0

    if not opts.force and refcount > 1 then
      -- We're not the sole owner — a normal stop won't actually restart the bridge
      vim.notify(
        string.format(
          "[mcp-companion] Bridge has %d clients attached. "
            .. "Use :MCPRestart! to force restart (affects all clients).",
          refcount
        ),
        vim.log.levels.WARN
      )
      return
    end

    if opts.force and refcount > 1 then
      vim.notify(
        string.format(
          "[mcp-companion] Force-restarting bridge (%d other clients will reconnect).",
          refcount - 1
        ),
        vim.log.levels.WARN
      )
    end
  end

  if opts.force and ss_ok then
    -- Force kill via sharedserver admin kill — ignores refcount
    local client = M.client
    if client then
      client:disconnect()
      M.client = nil
    end
    pcall(ss.stop, "mcp-bridge")
    -- Also kill the underlying process to ensure a fresh start
    local sharedserver_mod = require("sharedserver")
    pcall(sharedserver_mod._call_sharedserver, { "admin", "kill", "mcp-bridge" })
  else
    M.stop()
  end

  -- Small delay to allow port release
  vim.defer_fn(function()
    M.start()
  end, 1000)
end

--- Get bridge status
--- @return {running: boolean, shared: boolean, port?: number, clients?: number, pid?: number}
function M.status()
  local client = M.client
  local result = {
    running = client ~= nil and client.connected,
    shared = false,
    port = _configured and _config.bridge.port or nil,
  }

  local ss_ok, ss = pcall(require, "sharedserver")
  if ss_ok then
    local info = ss.status("mcp-bridge")
    result.shared = true
    result.running = info.running or false
    result.clients = info.clients
    result.pid = info.pid
  end

  return result
end

return M
