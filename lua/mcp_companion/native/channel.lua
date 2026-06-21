--- mcp-companion.nvim — Neovim back-channel registration
---
--- The bridge is a separate process; to let it call `neovim_*` tools back into
--- this editor it needs a connection to a private msgpack-RPC socket. This
--- module opens that socket (`serverstart`), registers it with the bridge, and
--- binds per-chat tokens to this instance so the bridge can route a chat's tool
--- calls to the right editor. See docs/designs/native-neovim-server.md (unit 2).
--- @module mcp_companion.native.channel

local log = require("mcp_companion.log")

local M = {}

--- @type string|nil  Stable unique id for this Neovim process.
local _instance_id = nil
--- @type string|nil  Private socket path once started.
local _socket = nil
--- @type "idle"|"pending"|"done"  Registration state with the bridge.
local _reg_state = "idle"
--- @type boolean  Whether serverstart() has opened the socket this session.
local _socket_started = false
--- @type integer  Registration attempts so far (for bounded retry/backoff).
local _reg_attempts = 0
--- @type string[]  Tokens awaiting registration before they can be bound.
local _pending_binds = {}
--- @type table<string, boolean>  Tokens currently bound to this instance,
--- re-asserted after a bridge restart.
local _bound_tokens = {}
--- @type string|nil  Last-seen bridge boot id; a change means the bridge
--- restarted and we must re-register + re-bind.
local _bridge_boot_id = nil

-- Registration may race the bridge becoming healthy (or hit a transient
-- failure). Retry with backoff before giving up.
local _MAX_REG_ATTEMPTS = 10
local _REG_BACKOFF_MS = 1000

--- @return string base URL of the bridge, e.g. http://127.0.0.1:9741
local function bridge_base()
  local cfg = require("mcp_companion.config").get()
  return string.format("http://%s:%d", cfg.bridge.host or "127.0.0.1", cfg.bridge.port or 9741)
end

--- @return boolean whether the native neovim server is enabled
function M.enabled()
  local ns = require("mcp_companion.config").get().native_servers or {}
  local n = ns.neovim
  return n == nil or n.enabled ~= false
end

--- A stable, unique id for this instance (NOT v:servername, which can collide).
--- @return string
function M.instance_id()
  if not _instance_id then
    local host = (vim.uv or vim.loop).os_gethostname() or "host"
    _instance_id = string.format("%s-%d-%04x", host, vim.fn.getpid(), math.random(0, 0xffff))
  end
  return _instance_id
end

--- Resolve the private socket path (in a user-only runtime dir).
--- @return string
function M.socket_path()
  if not _socket then
    local runtime = vim.env.XDG_RUNTIME_DIR
    local base = runtime and (runtime .. "/mcp-companion")
      or (vim.fn.stdpath("cache") .. "/mcp-companion-sock")
    vim.fn.mkdir(base, "p")
    _socket = base .. "/" .. M.instance_id() .. ".sock"
  end
  return _socket
end

--- POST /neovim/bind for a token (assumes the instance is registered).
--- @param token string
local function post_bind(token)
  require("mcp_companion.http").request({
    url = bridge_base() .. "/neovim/bind",
    method = "post",
    headers = { ["Content-Type"] = "application/json" },
    body = vim.json.encode({ token = token, instance_id = M.instance_id() }),
    timeout = 5000,
    callback = function(r)
      if r.status == 200 then
        log.debug("channel: bound token %s -> %s", token, M.instance_id())
      else
        log.warn("channel: bind failed (status %s): %s", r.status, r.body or "")
      end
    end,
  })
end

--- Open the socket and register this instance with the bridge (idempotent).
--- Retries with backoff if the bridge isn't reachable yet, and flushes any
--- tokens queued by bind() once registration completes.
function M.start()
  if not M.enabled() then return end
  if _reg_state == "done" or _reg_state == "pending" then return end

  local path = M.socket_path()
  -- serverstart errors if the same path is opened twice, so do it once.
  if not _socket_started then
    if not pcall(vim.fn.serverstart, path) then
      log.warn("channel: serverstart failed for %s", path)
      return
    end
    _socket_started = true
  end

  _reg_state = "pending"
  _reg_attempts = _reg_attempts + 1
  require("mcp_companion.http").request({
    url = bridge_base() .. "/neovim/instances",
    method = "post",
    headers = { ["Content-Type"] = "application/json" },
    body = vim.json.encode({
      instance_id = M.instance_id(),
      socket = path,
      pid = vim.fn.getpid(),
      -- Human-meaningful metadata so an agent can pick a target via
      -- neovim_list_instances when multiple editors are connected.
      cwd = vim.fn.getcwd(),
      name = vim.fn.fnamemodify(vim.fn.getcwd(), ":t"),
      servername = vim.v.servername,
    }),
    timeout = 5000,
    callback = function(r)
      if r.status == 200 then
        _reg_state = "done"
        _reg_attempts = 0
        log.info("channel: registered instance %s at %s", M.instance_id(), path)
        local queued = _pending_binds
        _pending_binds = {}
        for _, token in ipairs(queued) do
          post_bind(token)
        end
        -- Re-assert every known binding (recovers them after a bridge restart).
        for token in pairs(_bound_tokens) do
          post_bind(token)
        end
      else
        _reg_state = "idle"
        if _reg_attempts < _MAX_REG_ATTEMPTS then
          log.debug("channel: registration attempt %d failed (status %s); retrying",
            _reg_attempts, r.status)
          vim.defer_fn(function() M.start() end, _REG_BACKOFF_MS)
        else
          log.warn("channel: registration gave up after %d attempts (status %s): %s",
            _reg_attempts, r.status, r.body or "")
        end
      end
    end,
  })
end

--- Force a fresh registration (the bridge process may be new and have lost our
--- instance + token bindings). Re-binds all known tokens on success.
function M.reassert()
  if not M.enabled() then return end
  _reg_state = "idle"
  _reg_attempts = 0
  M.start()
end

--- Reconcile with the bridge: read its boot id from /health and, if it changed
--- (a restart) or we've never registered, re-register + re-bind. Cheap to call
--- often (e.g. on every SSE reconnect) — it only acts on an actual restart.
function M.sync()
  if not M.enabled() then return end
  require("mcp_companion.http").request({
    url = bridge_base() .. "/health",
    method = "get",
    timeout = 5000,
    callback = function(r)
      if r.status ~= 200 then return end
      local ok, data = pcall(vim.json.decode, r.body)
      if not ok or type(data) ~= "table" then return end
      local boot = data.boot_id
      if boot and boot ~= _bridge_boot_id then
        _bridge_boot_id = boot
        log.info("channel: bridge boot id changed -> re-registering")
        M.reassert()
      elseif _reg_state ~= "done" then
        -- Same bridge but we're not registered (e.g. first sync) — register.
        M.start()
      end
    end,
  })
end

--- Bind a chat token to this instance so the bridge routes its `neovim_*`
--- calls here. Safe to call before registration — the token is queued and
--- bound once registration completes.
--- @param token string|nil
function M.bind(token)
  if not token or not M.enabled() then return end
  _bound_tokens[token] = true
  if _reg_state == "done" then
    post_bind(token)
  else
    table.insert(_pending_binds, token)
    M.start()
  end
end

--- Unbind a chat token (on chat close).
--- @param token string|nil
function M.unbind(token)
  if not token or not M.enabled() then return end
  _bound_tokens[token] = nil
  for i = #_pending_binds, 1, -1 do
    if _pending_binds[i] == token then
      table.remove(_pending_binds, i)
    end
  end
  if _reg_state ~= "done" then return end
  require("mcp_companion.http").request({
    url = bridge_base() .. "/neovim/bind",
    method = "delete",
    headers = { ["Content-Type"] = "application/json" },
    body = vim.json.encode({ token = token }),
    timeout = 3000,
    callback = function(r)
      log.debug("channel: unbound token %s (status %s)", token, r.status)
    end,
  })
end

--- Deregister this instance and stop the socket (on VimLeave).
function M.deregister()
  if _reg_state == "idle" and not _socket then return end
  if _socket then
    pcall(vim.fn.serverstop, _socket)
  end
  -- Best-effort synchronous-ish notify; on VimLeave the event loop is winding
  -- down, so we fire the request and don't depend on its callback.
  require("mcp_companion.http").request({
    url = bridge_base() .. "/neovim/instances",
    method = "delete",
    headers = { ["Content-Type"] = "application/json" },
    body = vim.json.encode({ instance_id = M.instance_id() }),
    timeout = 2000,
    callback = function() end,
  })
  _reg_state = "idle"
  _socket = nil
end

return M
