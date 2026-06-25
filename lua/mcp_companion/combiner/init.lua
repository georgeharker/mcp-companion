--- mcp-companion.nvim — Combiner lifecycle management
--- @module mcp_companion.combiner

local M = {}

local log = require("mcp_companion.log")
local http = require("mcp_companion.http")

--- @type MCPCompanion.Config
local _config ---@diagnostic disable-line: missing-fields

--- @type MCPCompanion.Client|nil Combiner MCP client instance
M.client = nil

--- @type boolean Whether setup() has been called
local _configured = false

--- @type any Direct subprocess handle (fallback mode)
M._job = nil

--- Setup combiner (stores config, does not start yet)
--- @param config MCPCompanion.Config
function M.setup(config)
  _config = config
  _configured = true
end

--- Start the combiner process and connect
function M.start()
  if not _configured then
    log.error("Combiner not configured — call setup() first")
    return
  end

  local state = require("mcp_companion.state")

  if not _config.combiner.config then
    log.error("No servers.json config path found")
    state.update("combiner", { status = "error", error = "No config file" })
    state.emit("combiner_error", "No config file")
    if _config.on_error then
      _config.on_error("No servers.json config path found")
    end
    return
  end

  state.update("combiner", { status = "connecting", port = _config.combiner.port, error = nil })

  -- Check if combiner is already running (another Neovim instance started it)
  M._check_existing(function(running)
    if running then
      log.info("Combiner already running on port %d, connecting...", _config.combiner.port)
      -- Register with sharedserver so this Neovim instance holds a refcount.
      -- Without this, only the instance that originally started the combiner
      -- keeps it alive — when that instance exits the combiner dies even though
      -- other instances are still connected.
      local ss_ok, ss = pcall(require, "sharedserver")
      if ss_ok and ss.start then
        M._register_with_sharedserver(ss)
        pcall(ss.start, "mcp-combiner")
      end
      state.update("combiner", { status = "healthy" })
      M._create_client()
    elseif pcall(require, "sharedserver") then
      M._start_with_sharedserver()
    else
      log.info("sharedserver not found, starting combiner directly")
      M._start_direct()
    end
  end)
end

--- Check if combiner is already running on the configured port
--- @param callback fun(running: boolean)
function M._check_existing(callback)
  local url = string.format("http://%s:%d/health", _config.combiner.host, _config.combiner.port)
  http.request({
    url = url,
    method = "get",
    timeout = 1000,
    callback = function(response)
      callback(response.status == 200)
    end,
  })
end

--- Build the combiner command + args
--- @return string[] cmd
local function _combiner_cmd()
  local cmd = {
    _config.combiner.python_cmd,
    "-m", "mcp_combiner",
    "--config", _config.combiner.config,
    "--port", tostring(_config.combiner.port),
    "--host", _config.combiner.host or "127.0.0.1",
  }
  local blog = _config.combiner.log or {}
  if type(blog.file) == "string" then  -- false to opt out; string = path
    table.insert(cmd, "--log-file")
    table.insert(cmd, blog.file)
  end
  if blog.level then
    table.insert(cmd, "--log-level")
    table.insert(cmd, blog.level)
  end
  if _config.cc and _config.cc.normalize_schema then
    table.insert(cmd, "--normalize-schema")
  end
  -- Tri-state validation flags: nil → omit, true → --x-validation, false → --no-x-validation.
  local iv = _config.combiner.input_validation
  if iv ~= nil then
    table.insert(cmd, iv and "--input-validation" or "--no-input-validation")
  end
  local ov = _config.combiner.output_validation
  if ov ~= nil then
    table.insert(cmd, ov and "--output-validation" or "--no-output-validation")
  end
  return cmd
end

--- Build environment for combiner process
--- @return table<string,string>
local function _combiner_env()
  local env = vim.tbl_extend("force", _config.global_env or {}, {
    MCP_COMBINER_PORT = tostring(_config.combiner.port),
  })
  -- Pass encryption key if configured
  if _config.combiner.token_key then
    env.MCP_COMBINER_TOKEN_KEY = _config.combiner.token_key
  end
  return env
end

--- Register mcp-combiner with sharedserver if not already registered.
--- Safe to call multiple times; no-ops when already registered.
--- @param ss table sharedserver module
function M._register_with_sharedserver(ss)
  if ss.is_registered and ss.is_registered("mcp-combiner") then
    log.debug("mcp-combiner already registered with sharedserver, skipping re-registration")
    return
  end

  local cmd_parts = _combiner_cmd()
  local env = _combiner_env()
  local log_file = vim.fn.stdpath("log") .. "/mcp-combiner.log"

  -- Register with lazy=true to prevent auto-start on VimEnter;
  -- ss.start() is called explicitly by each call-site.
  ss.register("mcp-combiner", {
    command = cmd_parts[1],
    args = vim.list_slice(cmd_parts, 2),
    env = env,
    idle_timeout = _config.combiner.idle_timeout or "30m",
    log_file = log_file,
    lazy = true,
    on_start = function(pid)
      log.info("sharedserver started mcp-combiner (pid %d)", pid)
    end,
    on_exit = function(code)
      vim.schedule(function()
        local _state = require("mcp_companion.state")
        if code ~= 0 then
          log.error("mcp-combiner exited with code %d", code)
          _state.update("combiner", { status = "error", error = "Process exited: code " .. code })
          _state.emit("combiner_error", "Process exited with code " .. code)
        else
          log.info("mcp-combiner exited normally")
          _state.update("combiner", { status = "disconnected" })
        end
      end)
    end,
  })

  log.debug("mcp-combiner registered with sharedserver")
end

--- Start via sharedserver Lua plugin (shared process across Neovim instances)
function M._start_with_sharedserver()
  local ss = require("sharedserver")
  local log_file = vim.fn.stdpath("log") .. "/mcp-combiner.log"

  M._register_with_sharedserver(ss)

  log.info("Starting combiner via sharedserver Lua plugin (log: %s)", log_file)

  local ok, result = pcall(ss.start, "mcp-combiner")
  if not ok or result == false then
    log.error("sharedserver.start() failed (%s) — falling back to direct start",
      not ok and tostring(result) or "returned false")
    M._start_direct()
    return
  end

  M._wait_and_connect()
end

--- Start combiner as a direct subprocess (fallback without sharedserver)
function M._start_direct()
  local state = require("mcp_companion.state")
  local cmd = _combiner_cmd()

  log.info("Starting combiner: %s", table.concat(cmd, " "))

  M._job = vim.system(cmd, {
    text = true,
    env = _combiner_env(),
    stderr = function(_, data)
      if data then
        log.debug("combiner stderr: %s", data:gsub("\n$", ""))
      end
    end,
  }, function(result)
    vim.schedule(function()
      if result.code ~= 0 then
        log.error("Combiner exited with code %d", result.code)
        state.update("combiner", { status = "error", error = "Process exited: code " .. result.code })
        state.emit("combiner_error", result.stderr or "unknown error")
      else
        log.info("Combiner process exited normally")
        state.update("combiner", { status = "disconnected" })
      end
      M._job = nil
    end)
  end)

  M._wait_and_connect()
end

--- Poll health endpoint then create MCP client
function M._wait_and_connect()
  local state = require("mcp_companion.state")
  local url = string.format("http://%s:%d/health", _config.combiner.host, _config.combiner.port)
  local attempts = 0
  local max_attempts = _config.combiner.startup_timeout or 30

  local timer = vim.uv.new_timer()
  if not timer then
    log.error("Failed to create health-check timer")
    state.update("combiner", { status = "error", error = "Timer creation failed" })
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
        log.error("Combiner health check timed out after %ds", max_attempts)
        state.update("combiner", { status = "error", error = "Health check timeout" })
        state.emit("combiner_error", "Health check timeout")
        if _config.on_error then
          _config.on_error("Combiner startup timed out")
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
            log.info("Combiner healthy on port %d (after %ds)", _config.combiner.port, attempts)
            state.update("combiner", { status = "healthy" })
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
  local Client = require("mcp_companion.combiner.client")

  local client = Client.new({
    host = _config.combiner.host or "127.0.0.1",
    port = _config.combiner.port,
    request_timeout = _config.combiner.request_timeout,
  })
  M.client = client

  client:connect(function(ok, err)
    if ok then
      state.update("combiner", { status = "connected" })
      state.emit("combiner_ready")
      log.info("MCP client connected (%d tools, %d resources, %d prompts)",
        #client.tools, #client.resources, #client.prompts)
    else
      state.update("combiner", { status = "error", error = tostring(err) })
      state.emit("combiner_error", err)
      state.add_error("MCP connection failed: " .. tostring(err))
      log.error("MCP client connection failed: %s", tostring(err))
      if _config.on_error then
        _config.on_error("MCP connection failed: " .. tostring(err))
      end
    end
  end)
end

--- Create a lightweight per-chat MCP client for session mapping.
--- The client establishes an MCP session for the token but skips capability
--- fetching, SSE, and polling (lite mode). Used by HTTP-adapter chats to route
--- tool calls through their own session so the combiner applies per-chat filters.
--- Token is sent via X-MCP-Combiner-Session header. When combiner.token_in_url is
--- true, the token is also embedded in the URL path as a fallback.
--- @param token string UUID token for this chat session
--- @return MCPCompanion.Client
function M.new_per_chat_client(token)
  local Client = require("mcp_companion.combiner.client")
  local token_in_url = _config.combiner and _config.combiner.token_in_url
  local base_path = token_in_url and ("/mcp/" .. token) or "/mcp"
  return Client.new({
    host = _config.combiner.host or "127.0.0.1",
    port = _config.combiner.port,
    base_path = base_path,
    token = token,
    request_timeout = _config.combiner.request_timeout,
    lite = true,
  })
end

--- Stop the combiner
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
    pcall(ss.stop, "mcp-combiner")
  end

  -- Kill direct job if we have one
  if M._job then
    pcall(function()
      M._job:kill(15) -- SIGTERM
    end)
    M._job = nil
  end

  state.update("combiner", { status = "disconnected", error = nil })
  log.info("Combiner stopped")
end

--- Restart the combiner (stop then start)
--- @param opts? {force?: boolean} If force=true, kill the combiner even if other clients are attached
function M.restart(opts)
  opts = opts or {}
  local ss_ok, ss = pcall(require, "sharedserver")

  if ss_ok then
    local info = ss.status("mcp-combiner")
    local refcount = info and info.refcount or 0

    if not opts.force and refcount > 1 then
      -- We're not the sole owner — a normal stop won't actually restart the combiner
      vim.notify(
        string.format(
          "[mcp-companion] Combiner has %d clients attached. "
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
          "[mcp-companion] Force-restarting combiner (%d other clients will reconnect).",
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
    pcall(ss.stop, "mcp-combiner")
    -- Also kill the underlying process to ensure a fresh start
    local sharedserver_mod = require("sharedserver")
    pcall(sharedserver_mod._call_sharedserver, { "admin", "kill", "mcp-combiner" })
  else
    M.stop()
  end

  -- Small delay to allow port release
  vim.defer_fn(function()
    M.start()
  end, 1000)
end

--- Get combiner status
--- @return {running: boolean, shared: boolean, port?: number, clients?: number, pid?: number}
function M.status()
  local client = M.client
  local result = {
    running = client ~= nil and client.connected,
    shared = false,
    port = _configured and _config.combiner.port or nil,
  }

  local ss_ok, ss = pcall(require, "sharedserver")
  if ss_ok then
    local info = ss.status("mcp-combiner")
    result.shared = true
    result.running = info.running or false
    result.clients = info.clients
    result.pid = info.pid
  end

  return result
end

return M
