--- mcp-companion.nvim — MCP HTTP Client (JSON-RPC over Streamable HTTP)
--- @module mcp_companion.bridge.client
---
--- FastMCP Streamable HTTP sends ALL responses as SSE (text/event-stream)
--- with Transfer-Encoding: chunked, then CLOSES the TCP connection.
---
--- This client creates a new vim.uv TCP connection for each request,
--- reads the full chunked SSE response, extracts the JSON-RPC payload,
--- then lets the connection close naturally.

local log = require("mcp_companion.log")

--- @class MCPCompanion.Client
--- @field host string
--- @field port number
--- @field base_path string URL path prefix for MCP (default: "/mcp")
--- @field token? string Session token sent as X-MCP-Bridge-Session header
--- @field session_id? string MCP session ID
--- @field request_id number Monotonic counter
--- @field connected boolean
--- @field server_info? table Server info from initialize
--- @field tools table[] Cached tool definitions
--- @field resources table[] Cached resource definitions
--- @field resource_templates table[] Cached resource template definitions
--- @field prompts table[] Cached prompt definitions
--- @field _poll_timer? uv.uv_timer_t Polling timer handle
--- @field _sse_tcp? uv.uv_tcp_t Dedicated SSE stream TCP handle
--- @field _sse_buf string SSE stream accumulation buffer
--- @field _sse_reconnect_timer? uv.uv_timer_t SSE reconnect delay timer
--- @field _sse_connected boolean Whether SSE stream is active
--- @field _server_health table<string, table> Per-server health data from /health
--- @field _config MCPCompanion.ClientConfig Original config
local Client = {}
Client.__index = Client

--- @class MCPCompanion.ClientConfig
--- @field host? string
--- @field port? number
--- @field request_timeout? number
--- @field poll_interval? number
--- @field base_path? string  URL path prefix for MCP (default: "/mcp")
--- @field token? string      Session token sent as X-MCP-Bridge-Session header on every request
--- @field lite? boolean      If true, skip SSE/polling/capability fetching (lightweight session-only client)

--- Create a new MCP HTTP client
--- @param config MCPCompanion.ClientConfig
--- @return MCPCompanion.Client
function Client.new(config)
  return setmetatable({
    host = config.host or "127.0.0.1",
    port = config.port or 9741,
    base_path = config.base_path or "/mcp",
    token = config.token or nil,
    session_id = nil,
    request_id = 0,
    connected = false,
    server_info = nil,

    -- Cached capabilities
    tools = {},
    resources = {},
    resource_templates = {},
    prompts = {},

    -- Internals
    _poll_timer = nil,
    _sse_tcp = nil,
    _sse_buf = "",
    _sse_reconnect_timer = nil,
    _sse_connected = false,
    _server_health = {},
    _config = config,
  }, Client)
end

--- Ensure a table is encoded as a JSON object (not array).
--- Recursively converts empty tables {} to vim.empty_dict().
--- @param t? table
--- @return table
local function _ensure_dict(t)
  if t == nil then
    return vim.empty_dict()
  end
  if type(t) ~= "table" then
    return t
  end
  if next(t) == nil then
    return vim.empty_dict()
  end
  for k, v in pairs(t) do
    if type(v) == "table" and next(v) == nil then
      t[k] = vim.empty_dict()
    end
  end
  return t
end

-------------------------------------------------------------------------------
-- HTTP response parsing (chunked Transfer-Encoding + SSE)
-------------------------------------------------------------------------------

--- Parse a complete HTTP response from raw bytes.
--- Returns (response, leftover) or (nil, raw) if incomplete.
--- @param raw string Raw TCP data accumulated so far
--- @return table|nil response {status_code, headers, body}
--- @return string leftover Remaining unparsed data
local function _parse_http_response(raw)
  -- Find end of headers
  local header_end = raw:find("\r\n\r\n")
  if not header_end then
    return nil, raw
  end

  local header_block = raw:sub(1, header_end - 1)
  local rest = raw:sub(header_end + 4)

  -- Parse status line
  local status_code = tonumber(header_block:match("HTTP/%S+%s+(%d+)"))
  if not status_code then
    return nil, raw
  end

  -- Parse headers
  local headers = {}
  for line in header_block:gmatch("[^\r\n]+") do
    local key, val = line:match("^([^:]+):%s*(.*)$")
    if key then
      headers[key:lower()] = val
    end
  end

  -- Determine body reading strategy
  local te = headers["transfer-encoding"]
  local cl = headers["content-length"]

  if te and te:lower():find("chunked") then
    -- Chunked Transfer-Encoding: read chunks until 0-chunk
    local body = ""
    local buf = rest
    while true do
      -- Read chunk size line
      local crlf = buf:find("\r\n")
      if not crlf then
        return nil, raw -- incomplete, need more data
      end
      local size_str = buf:sub(1, crlf - 1):match("^%s*(%x+)")
      if not size_str then
        break -- malformed
      end
      buf = buf:sub(crlf + 2)
      local chunk_size = tonumber(size_str, 16) or 0

      if chunk_size == 0 then
        -- Final chunk — skip optional trailing CRLF
        if buf:sub(1, 2) == "\r\n" then
          buf = buf:sub(3)
        end
        return { status_code = status_code, headers = headers, body = body }, buf
      end

      -- Read chunk data
      if #buf < chunk_size + 2 then
        return nil, raw -- incomplete
      end
      body = body .. buf:sub(1, chunk_size)
      buf = buf:sub(chunk_size + 3) -- skip data + \r\n
    end
    return nil, raw

  elseif cl then
    -- Content-Length
    local content_length = tonumber(cl) or 0
    if #rest < content_length then
      return nil, raw -- incomplete
    end
    return {
      status_code = status_code,
      headers = headers,
      body = rest:sub(1, content_length),
    }, rest:sub(content_length + 1)

  else
    -- No body (202, etc.)
    return { status_code = status_code, headers = headers, body = "" }, rest
  end
end

--- Extract JSON-RPC response from an HTTP response body.
--- The body may be SSE format (data: {json}) or plain JSON.
--- @param body string
--- @return table|nil parsed JSON-RPC response object
local function _extract_jsonrpc(body)
  -- Try SSE format first: look for "data:" lines
  for line in body:gmatch("[^\r\n]+") do
    local data = line:match("^data:%s*(.+)$")
    if data then
      local ok, parsed = pcall(vim.json.decode, data)
      if ok and type(parsed) == "table" and parsed.jsonrpc then
        return parsed
      end
    end
  end

  -- Try plain JSON
  local trimmed = vim.trim(body)
  if trimmed ~= "" then
    local ok, parsed = pcall(vim.json.decode, trimmed)
    if ok and type(parsed) == "table" and parsed.jsonrpc then
      return parsed
    end
  end

  return nil
end

-------------------------------------------------------------------------------
-- Per-request TCP connection
-------------------------------------------------------------------------------

--- Send an HTTP request over a new TCP connection, read full response, close.
--- @param method string HTTP method (POST, GET)
--- @param path string URL path
--- @param body? string Request body
--- @param timeout_ms number Timeout in milliseconds
--- @param callback fun(err?: string, response?: table) Called with parsed HTTP response
function Client:_http_request(method, path, body, timeout_ms, callback)
  local tcp = vim.uv.new_tcp()
  if not tcp then
    callback("Failed to create TCP handle")
    return
  end

  local buf = ""
  local responded = false
  local timer = vim.uv.new_timer()

  -- Cleanup helper — close TCP immediately after response is parsed.
  local function cleanup()
    if responded then
      return
    end
    responded = true
    if timer and not timer:is_closing() then
      pcall(function()
        timer:stop()
        timer:close()
      end)
    end
    if tcp then
      pcall(function()
        tcp:read_stop()
      end)
      if not tcp:is_closing() then
        pcall(function()
          tcp:close()
        end)
      end
    end
  end

  -- Timeout
  if timer then
    timer:start(timeout_ms, 0, function()
      if not responded then
        cleanup()
        vim.schedule(function()
          callback("Request timed out")
        end)
      end
    end)
  end

  tcp:connect(self.host, self.port, function(connect_err)
    if connect_err then
      cleanup()
      vim.schedule(function()
        callback("TCP connect failed: " .. tostring(connect_err))
      end)
      return
    end

    -- Set TCP_NODELAY for low-latency
    tcp:nodelay(true)

    -- Build HTTP request — Always use Connection: close.
    -- This tells the server to close the connection after responding,
    -- which triggers EOF for proper response detection. Without it,
    -- keep-alive connections leak TCP handles, and when the process exits,
    -- the OS sends RST which corrupts the FastMCP proxy state.
    local headers = {
      string.format("%s %s HTTP/1.1", method, path),
      string.format("Host: %s:%d", self.host, self.port),
      "Content-Type: application/json",
      "Accept: application/json, text/event-stream",
      "Connection: close",
    }

    if self.session_id then
      table.insert(headers, "Mcp-Session-Id: " .. self.session_id)
    end

    if self.token then
      table.insert(headers, "X-MCP-Bridge-Session: " .. self.token)
    end

    if body then
      table.insert(headers, "Content-Length: " .. #body)
    else
      table.insert(headers, "Content-Length: 0")
    end

    local raw_request = table.concat(headers, "\r\n") .. "\r\n\r\n"
    if body then
      raw_request = raw_request .. body
    end

    -- Start reading before writing
    tcp:read_start(function(read_err, data)
      if read_err then
        cleanup()
        vim.schedule(function()
          callback("TCP read error: " .. tostring(read_err))
        end)
        return
      end

      if not data then
        -- EOF — server closed connection. Always close our side cleanly.
        if not responded then
          -- Try to parse whatever we have
          if #buf > 0 then
            local resp = _parse_http_response(buf)
            if resp then
              responded = true
              if timer and not timer:is_closing() then
                pcall(function() timer:stop(); timer:close() end)
              end
              vim.schedule(function()
                callback(nil, resp)
              end)
            else
              responded = true
              if timer and not timer:is_closing() then
                pcall(function() timer:stop(); timer:close() end)
              end
              vim.schedule(function()
                callback("Connection closed before response completed")
              end)
            end
          else
            responded = true
            if timer and not timer:is_closing() then
              pcall(function() timer:stop(); timer:close() end)
            end
            vim.schedule(function()
              callback("Connection closed with no data")
            end)
          end
        end
        -- Always close TCP on EOF
        if tcp and not tcp:is_closing() then
          pcall(function() tcp:close() end)
        end
        return
      end

      buf = buf .. data

      -- Try to parse a complete HTTP response
      if not responded then
        local resp = _parse_http_response(buf)
        if resp then
          -- Response is complete! Deliver callback but DON'T close TCP yet.
          -- Let the connection drain to EOF so we don't send TCP RST which
          -- can corrupt FastMCP proxy state. The timeout timer still guards
          -- against hanging connections.
          responded = true
          if timer and not timer:is_closing() then
            pcall(function() timer:stop(); timer:close() end)
          end
          -- Set a shorter drain timeout — close after 2s if EOF doesn't come
          local drain_timer = vim.uv.new_timer()
          if drain_timer then
            drain_timer:start(2000, 0, function()
              if tcp and not tcp:is_closing() then
                pcall(function() tcp:read_stop() end)
                pcall(function() tcp:close() end)
              end
              if not drain_timer:is_closing() then
                drain_timer:close()
              end
            end)
          end
          vim.schedule(function()
            callback(nil, resp)
          end)
        end
      end
    end)

    -- Write request
    tcp:write(raw_request, function(write_err)
      if write_err and not responded then
        cleanup()
        vim.schedule(function()
          callback("TCP write error: " .. tostring(write_err))
        end)
      end
    end)
  end)
end

-------------------------------------------------------------------------------
-- JSON-RPC over MCP Streamable HTTP
-------------------------------------------------------------------------------

--- Send a JSON-RPC request (new TCP connection per request).
--- @param method string MCP method (e.g. "tools/list")
--- @param params? table
--- @param callback? fun(err?: string, result?: table) If nil, synchronous
--- @return table|nil result (sync mode only)
function Client:request(method, params, callback)
  self.request_id = self.request_id + 1
  local id = self.request_id

  local payload = vim.json.encode({
    jsonrpc = "2.0",
    id = id,
    method = method,
    params = _ensure_dict(params),
  })

  local timeout_ms = (self._config.request_timeout or 60) * 1000

  if callback then
    -- Async mode
    self:_http_request("POST", self.base_path, payload, timeout_ms, function(err, resp)
      if err then
        callback(err)
        return
      end

      -- Capture session ID
      if resp.headers["mcp-session-id"] then
        self.session_id = resp.headers["mcp-session-id"]
      end

      -- Extract JSON-RPC
      local jsonrpc = _extract_jsonrpc(resp.body)
      if not jsonrpc then
        if resp.status_code == 202 then
          callback(nil, nil) -- notification accepted
        else
          callback(string.format("No JSON-RPC in response (HTTP %d, %d bytes)", resp.status_code, #resp.body))
        end
        return
      end

      if jsonrpc.error then
        callback(string.format("MCP error [%d]: %s", jsonrpc.error.code or -1, jsonrpc.error.message or "unknown"))
      else
        callback(nil, jsonrpc.result)
      end
    end)
  else
    -- Sync mode
    local result_val = nil
    local error_val = nil
    local done = false

    self:_http_request("POST", self.base_path, payload, timeout_ms, function(err, resp)
      if err then
        error_val = err
        done = true
        return
      end

      -- Capture session ID
      if resp.headers["mcp-session-id"] then
        self.session_id = resp.headers["mcp-session-id"]
      end

      local jsonrpc = _extract_jsonrpc(resp.body)
      if not jsonrpc then
        if resp.status_code == 202 then
          done = true
        else
          error_val = string.format("No JSON-RPC in response (HTTP %d)", resp.status_code)
          done = true
        end
        return
      end

      if jsonrpc.error then
        error_val = string.format("MCP error [%d]: %s", jsonrpc.error.code or -1, jsonrpc.error.message or "unknown")
      else
        result_val = jsonrpc.result
      end
      done = true
    end)

    vim.wait(timeout_ms, function()
      return done
    end, 10)

    if not done then
      error("Request timed out for " .. method)
    end

    if error_val then
      error(error_val)
    end
    return result_val
  end
end

--- Send a JSON-RPC notification (no response expected).
--- Opens a new TCP connection, sends the payload, closes.
--- @param method string
--- @param params? table
function Client:notify(method, params)
  self.request_id = self.request_id + 1
  local payload = vim.json.encode({
    jsonrpc = "2.0",
    method = method,
    params = _ensure_dict(params),
  })

  -- Fire-and-forget: short timeout, ignore response
  self:_http_request("POST", self.base_path, payload, 5000, function(err, resp)
    if err then
      log.debug("Notification %s delivery: %s", method, tostring(err))
    else
      -- Capture session ID from notify response too
      if resp and resp.headers["mcp-session-id"] then
        self.session_id = resp.headers["mcp-session-id"]
      end
    end
  end)
end

--- Initialize MCP session
--- @param callback fun(ok: boolean, err?: string)
function Client:connect(callback)
  log.debug("Connecting to bridge at %s:%d", self.host, self.port)

  self:request("initialize", {
    protocolVersion = "2025-03-26",
    capabilities = {
      roots = { listChanged = false },
    },
    clientInfo = {
      name = "mcp-companion.nvim",
      version = "0.1.0",
    },
  }, function(err, result)
    if err then
      log.error("Initialize failed: %s", tostring(err))
      return callback(false, err)
    end

    self.server_info = result and result.serverInfo or nil
    self.connected = true
    log.debug("Initialize OK, server: %s", vim.inspect(self.server_info))

    -- Send initialized notification
    self:notify("notifications/initialized")

    -- Lite mode: just establish the session (for per-chat token clients).
    -- Skip capability fetching, SSE, and polling — only tool calls are needed.
    if self._config.lite then
      callback(true)
      return
    end

    -- Fetch server names and capabilities in parallel.
    -- Server names are needed by _update_server_state() which runs inside
    -- refresh_capabilities, so we collect all data first, then update.
    -- Use a barrier: 4 tasks = 1 health + 3 capability requests.
    local pending = 4
    local function check_all_done()
      pending = pending - 1
      if pending == 0 then
        -- All data collected — run state update once
        self:_update_server_state()
        require("mcp_companion.state").emit("servers_updated")

        -- Start SSE notification stream for real-time tool change events
        self:_start_sse()

        -- Start capability polling as fallback for missed SSE events
        local interval = self._config.poll_interval or 30000
        self:_start_polling(interval)

        callback(true)
      end
    end

    -- Health endpoint (for server names / disabled status)
    self:_fetch_server_names(check_all_done)

    -- Capability requests (parallel) — skip internal _update_server_state
    self:request("tools/list", {}, function(req_err, req_result)
      if not req_err and req_result then
        self.tools = req_result.tools or {}
      else
        log.warn("tools/list failed: %s", tostring(req_err))
      end
      check_all_done()
    end)

    self:request("resources/list", {}, function(req_err2, req_result2)
      if not req_err2 and req_result2 then
        self.resources = req_result2.resources or {}
        self.resource_templates = req_result2.resourceTemplates or {}
      else
        log.warn("resources/list failed: %s", tostring(req_err2))
      end
      check_all_done()
    end)

    self:request("prompts/list", {}, function(req_err3, req_result3)
      if not req_err3 and req_result3 then
        self.prompts = req_result3.prompts or {}
      else
        log.warn("prompts/list failed: %s", tostring(req_err3))
      end
      check_all_done()
    end)
  end)
end

--- Disconnect and clean up
function Client:disconnect()
  self.connected = false
  self:_stop_sse()
  self:_stop_polling()
  self.session_id = nil
  self.tools = {}
  self.resources = {}
  self.resource_templates = {}
  self.prompts = {}
  self._known_server_names = {}
  self._server_health = {}
  log.debug("Client disconnected")
end

--- Fetch server names from bridge health endpoint
--- Used for correct tool name parsing (servers may have hyphens in names)
--- @param callback fun()
function Client:_fetch_server_names(callback)
  -- Use mcp_companion.http for health endpoint (it's not an MCP request)
  local http = require("mcp_companion.http")
  local url = string.format("http://%s:%d/health", self.host, self.port)

  http.request({
    url = url,
    method = "get",
    timeout = 5000,
    callback = function(response)
      if response.status == 0 then
        log.warn("Failed to fetch server names: %s", response.body)
        self._known_server_names = {}
        callback()
        return
      end
      if response.status ~= 200 then
        log.warn("Failed to fetch server names: status=%s", response.status)
        self._known_server_names = {}
        callback()
        return
      end

      local ok, data = pcall(vim.json.decode, response.body)
      if not ok or not data or not data.servers then
        log.warn("Invalid health response: %s", response.body and response.body:sub(1, 100) or "empty")
        self._known_server_names = {}
        callback()
        return
      end

      -- Extract server names, sorted by length descending so longer names match first
      local names = {}
      for name, _ in pairs(data.servers) do
        table.insert(names, name)
      end
      -- Add "bridge" for meta-tools (bridge__status, bridge__enable_server, etc.)
      -- These use double underscore but we still need to match the prefix
      table.insert(names, "bridge")
      table.sort(names, function(a, b) return #a > #b end)

      self._known_server_names = names
      -- Store per-server health data (transport, disabled, etc.) for UI state
      self._server_health = data.servers or {}
      log.debug("Known server names: %s", vim.inspect(names))
      callback()
    end,
  })
end

--- Refresh all capabilities from bridge
--- @param callback? fun() Called when all refreshes complete
function Client:refresh_capabilities(callback)
  local state = require("mcp_companion.state")

  -- Fire all three requests in parallel, collect results with a counter
  local pending = 3
  local function check_done()
    pending = pending - 1
    if pending == 0 then
      -- All responses received — update state once
      self:_update_server_state()
      state.emit("servers_updated")
      if callback then
        callback()
      end
    end
  end

  self:request("tools/list", {}, function(err, result)
    if not err and result then
      self.tools = result.tools or {}
    else
      log.warn("tools/list failed: %s", tostring(err))
    end
    check_done()
  end)

  self:request("resources/list", {}, function(err2, result2)
    if not err2 and result2 then
      self.resources = result2.resources or {}
      self.resource_templates = result2.resourceTemplates or {}
    else
      log.warn("resources/list failed: %s", tostring(err2))
    end
    check_done()
  end)

  self:request("prompts/list", {}, function(err3, result3)
    if not err3 and result3 then
      self.prompts = result3.prompts or {}
    else
      log.warn("prompts/list failed: %s", tostring(err3))
    end
    check_done()
  end)
end

--- Parse tools into per-server groups and update state.servers
function Client:_update_server_state()
  local state = require("mcp_companion.state")
  local server_map = {} --- @type table<string, MCPCompanion.ServerInfo>

  -- Build list of known server names from health endpoint (stored during connect)
  -- This allows us to correctly parse tool names like "basic-memory_write_note"
  -- where the server name contains hyphens.
  local known_servers = self._known_server_names or {}

  -- Group tools by server namespace (FastMCP uses "_" separator)
  for _, tool in ipairs(self.tools) do
    local server_name, tool_name = nil, nil

    -- Try to match against known server names first
    for _, srv_name in ipairs(known_servers) do
      local prefix = srv_name .. "_"
      if tool.name:sub(1, #prefix) == prefix then
        server_name = srv_name
        tool_name = tool.name:sub(#prefix + 1)
        -- Strip leading underscore if present (for bridge__ meta-tools)
        if tool_name:sub(1, 1) == "_" then
          tool_name = tool_name:sub(2)
        end
        break
      end
    end

    -- Fallback: split on first underscore (for unknown servers)
    if not server_name then
      server_name, tool_name = tool.name:match("^(.-)_(.+)$")
    end

    -- If still no match, it's a bridge-level tool
    if not server_name or server_name == "" then
      server_name = "_bridge"
      tool_name = tool.name
    end

    if not server_map[server_name] then
      server_map[server_name] = {
        name = server_name,
        status = "connected",
        tools = {},
        resources = {},
        resource_templates = {},
        prompts = {},
      }
    end
    table.insert(server_map[server_name].tools, vim.tbl_extend("force", tool, {
      _namespaced = tool.name,
      _display = tool_name,
    }))
  end

  -- Group resources by server namespace.
  -- Resource URIs use arbitrary schemes (e.g. "ui://", "memory://") that don't
  -- correspond to server names. Match by looking for a known server name in the
  -- URI path, falling back to _bridge.
  local known_server_set = {} -- luacheck: ignore 421
  for name in pairs(server_map) do
    known_server_set[name] = true
  end

  for _, res in ipairs(self.resources) do
    local assigned = "_bridge"
    if res.uri then
      -- Check if any known server name appears as a path component in the URI
      for name in pairs(known_server_set) do
        if name ~= "_bridge" and res.uri:find("/" .. name .. "/", 1, true) then
          assigned = name
          break
        end
      end
    end
    if not server_map[assigned] then
      server_map[assigned] = {
        name = assigned,
        status = "connected",
        tools = {},
        resources = {},
        resource_templates = {},
        prompts = {},
      }
    end
    table.insert(server_map[assigned].resources, res)
  end

  -- Convert map to sorted array
  local servers = {}
  for _, info in pairs(server_map) do
    table.insert(servers, info)
  end

  -- Merge disabled servers from health data (they have no tools mounted)
  local health = self._server_health or {}
  for name, info in pairs(health) do
    if not server_map[name] then
      -- Server exists in config but has no tools (disabled or disconnected)
      table.insert(servers, {
        name = name,
        status = info.disabled and "disabled" or "disconnected",
        disabled = info.disabled or false,
        tools = {},
        resources = {},
        resource_templates = {},
        prompts = {},
      })
    end
  end
  -- Mark enabled/disabled status on existing servers from health data
  for _, srv in ipairs(servers) do
    if health[srv.name] then
      srv.disabled = health[srv.name].disabled or false
      if srv.disabled then
        srv.status = "disabled"
      end
    else
      srv.disabled = false
    end
  end

  table.sort(servers, function(a, b)
    return a.name < b.name
  end)

  state.update("servers", servers)
end

--- Refresh server health/status from the bridge /health endpoint
--- Updates _server_health and re-runs _update_server_state to merge disabled info
--- @param callback? fun()
function Client:_refresh_server_health(callback)
  local http = require("mcp_companion.http")
  local url = string.format("http://%s:%d/health", self.host, self.port)

  http.request({
    url = url,
    method = "get",
    timeout = 5000,
    callback = function(response)
      if response.status == 200 then
        local ok, data = pcall(vim.json.decode, response.body)
        if ok and data and data.servers then
          self._server_health = data.servers
          -- Update known server names in case servers were added/removed
          local names = {}
          for name, _ in pairs(data.servers) do
            table.insert(names, name)
          end
          table.insert(names, "bridge")
          table.sort(names, function(a, b) return #a > #b end)
          self._known_server_names = names
        end
      elseif response.status == 0 then
        log.warn("Health refresh error: %s", response.body)
      else
        log.warn("Health refresh failed: status=%s", response.status)
      end
      if callback then
        callback()
      end
    end,
  })
end

--- Toggle a server's enabled/disabled state
--- Calls bridge__enable_server or bridge__disable_server, then refreshes
--- @param server_name string
--- @param callback? fun(err?: string, result?: string)
function Client:toggle_server(server_name, callback)
  -- Determine current state from health data
  local health = self._server_health or {}
  local info = health[server_name]
  if not info then
    local msg = "Unknown server: " .. server_name
    log.warn(msg)
    if callback then
      callback(msg)
    end
    return
  end

  local tool_name = info.disabled and "bridge__enable_server" or "bridge__disable_server"
  local action = info.disabled and "Enabling" or "Disabling"
  log.info("%s server: %s", action, server_name)

  self:call_tool(tool_name, { server_name = server_name }, function(err, result)
    if err then
      log.error("Failed to toggle server %s: %s", server_name, tostring(err))
      if callback then
        callback(err)
      end
      return
    end

    -- Extract result text from MCP response
    local result_text = ""
    if result and result.content then
      for _, item in ipairs(result.content) do
        if item.text then
          result_text = result_text .. item.text
        end
      end
    end
    log.info("Toggle result: %s", result_text)

    -- Refresh health data first (to get updated disabled status),
    -- then refresh capabilities (to get updated tool list)
    self:_refresh_server_health(function()
      self:refresh_capabilities(function()
        if callback then
          callback(nil, result_text)
        end
      end)
    end)
  end)
end

--- Call a tool on the bridge
--- @param name string Tool name (namespaced: "server_tool")
--- @param arguments table Tool arguments
--- @param callback? fun(err?: string, result?: table)
--- @return table|nil result (sync mode)
function Client:call_tool(name, arguments, callback)
  log.debug("call_tool: %s", name)
  return self:request("tools/call", { name = name, arguments = _ensure_dict(arguments) }, callback)
end

--- Read a resource
--- @param uri string Resource URI
--- @param callback? fun(err?: string, result?: table)
--- @return table|nil result (sync mode)
function Client:read_resource(uri, callback)
  log.debug("read_resource: %s", uri)
  return self:request("resources/read", { uri = uri }, callback)
end

--- Get a prompt
--- @param name string Prompt name
--- @param arguments? table Prompt arguments
--- @param callback? fun(err?: string, result?: table)
--- @return table|nil result (sync mode)
function Client:get_prompt(name, arguments, callback)
  log.debug("get_prompt: %s", name)
  return self:request("prompts/get", { name = name, arguments = arguments or vim.empty_dict() }, callback)
end

-------------------------------------------------------------------------------
-- SSE Notification Stream
--
-- A dedicated long-lived TCP connection that listens for server-push events.
-- Uses Connection: keep-alive (NOT close) so the SSE stream stays open.
-- Handles: notifications/tools/list_changed, notifications/resources/list_changed,
-- notifications/prompts/list_changed — auto-refreshes capabilities on each.
--
-- On disconnect: graceful tcp:shutdown() (FIN, not RST) to avoid corrupting
-- FastMCP's session state. Auto-reconnects with backoff.
-------------------------------------------------------------------------------

--- Process a single SSE event (one "data:" line's parsed JSON-RPC).
--- @param msg table Parsed JSON-RPC notification
function Client:_handle_sse_notification(msg)
  if not msg.method then
    return -- Not a notification (could be a response to our GET — ignore)
  end

  log.debug("SSE notification: %s", msg.method)

  local state = require("mcp_companion.state")

  if msg.method == "notifications/tools/list_changed" then
    self:request("tools/list", {}, function(err, result)
      if not err and result then
        self.tools = result.tools or {}
        self:_update_server_state()
        state.emit("tool_list_changed")
        state.emit("servers_updated")
        log.debug("SSE: tools refreshed (%d tools)", #self.tools)
      end
    end)
  elseif msg.method == "notifications/resources/list_changed" then
    self:request("resources/list", {}, function(err, result)
      if not err and result then
        self.resources = result.resources or {}
        self.resource_templates = result.resourceTemplates or {}
        self:_update_server_state()
        state.emit("resource_list_changed")
        state.emit("servers_updated")
        log.debug("SSE: resources refreshed (%d resources)", #self.resources)
      end
    end)
  elseif msg.method == "notifications/prompts/list_changed" then
    self:request("prompts/list", {}, function(err, result)
      if not err and result then
        self.prompts = result.prompts or {}
        state.emit("prompt_list_changed")
        state.emit("servers_updated")
        log.debug("SSE: prompts refreshed (%d prompts)", #self.prompts)
      end
    end)
  end
end

--- Process accumulated SSE buffer, extracting complete events.
--- SSE events are separated by blank lines (\r\n\r\n or \n\n).
--- Each event has "event:" and "data:" lines.
function Client:_process_sse_buffer()
  while true do
    -- Find the next complete SSE event (terminated by double newline)
    local event_end = self._sse_buf:find("\r\n\r\n")
    local end_len = 4
    if not event_end then
      event_end = self._sse_buf:find("\n\n")
      end_len = 2
    end
    if not event_end then
      break -- No complete event yet
    end

    local event_text = self._sse_buf:sub(1, event_end - 1)
    self._sse_buf = self._sse_buf:sub(event_end + end_len)

    -- Extract "data:" lines and parse JSON-RPC
    for line in event_text:gmatch("[^\r\n]+") do
      local data = line:match("^data:%s*(.+)$")
      if data then
        local ok, parsed = pcall(vim.json.decode, data)
        if ok and type(parsed) == "table" and parsed.jsonrpc then
          self:_handle_sse_notification(parsed)
        end
      end
    end
  end
end

--- Start the SSE notification stream.
--- Opens a dedicated TCP connection with Connection: keep-alive,
--- sends GET /mcp with Accept: text/event-stream, reads events indefinitely.
function Client:_start_sse()
  if self._sse_connected then
    return -- Already running
  end
  if not self.session_id then
    log.debug("SSE: No session ID, skipping SSE stream")
    return
  end

  log.debug("SSE: Starting notification stream")

  local tcp = vim.uv.new_tcp()
  if not tcp then
    log.warn("SSE: Failed to create TCP handle")
    return
  end

  self._sse_tcp = tcp
  self._sse_buf = ""

  tcp:connect(self.host, self.port, function(connect_err)
    if connect_err then
      log.warn("SSE: TCP connect failed: %s", tostring(connect_err))
      self:_cleanup_sse()
      self:_schedule_sse_reconnect()
      return
    end

    tcp:nodelay(true)

    -- Build GET request for SSE stream — keep-alive, NOT close
    local headers = {
      string.format("GET %s HTTP/1.1", self.base_path),
      string.format("Host: %s:%d", self.host, self.port),
      "Accept: text/event-stream",
      "Connection: keep-alive",
      "Cache-Control: no-cache",
    }
    if self.session_id then
      table.insert(headers, "Mcp-Session-Id: " .. self.session_id)
    end
    table.insert(headers, "Content-Length: 0")

    local raw_request = table.concat(headers, "\r\n") .. "\r\n\r\n"

    -- Start reading SSE events
    tcp:read_start(function(read_err, data)
      if read_err then
        log.warn("SSE: Read error: %s", tostring(read_err))
        vim.schedule(function()
          self:_cleanup_sse()
          self:_schedule_sse_reconnect()
        end)
        return
      end

      if not data then
        -- EOF — server closed SSE stream
        log.debug("SSE: Stream closed by server")
        vim.schedule(function()
          self:_cleanup_sse()
          self:_schedule_sse_reconnect()
        end)
        return
      end

      -- Accumulate and process
      self._sse_buf = self._sse_buf .. data

      -- Skip HTTP response headers on first data
      -- The SSE stream starts with HTTP/1.1 200 OK + headers
      if self._sse_buf:find("^HTTP/") then
        local header_end = self._sse_buf:find("\r\n\r\n")
        if header_end then
          self._sse_buf = self._sse_buf:sub(header_end + 4)
          self._sse_connected = true
          log.debug("SSE: Stream connected, processing events")
        else
          return -- Headers not complete yet
        end
      end

      -- Process any complete SSE events (schedule to main loop)
      vim.schedule(function()
        self:_process_sse_buffer()
      end)
    end)

    -- Write the GET request
    tcp:write(raw_request, function(write_err)
      if write_err then
        log.warn("SSE: Write error: %s", tostring(write_err))
        vim.schedule(function()
          self:_cleanup_sse()
          self:_schedule_sse_reconnect()
        end)
      end
    end)
  end)
end

--- Clean up SSE TCP connection gracefully.
--- Uses tcp:shutdown() for graceful FIN (not RST) to avoid corrupting
--- FastMCP's session state.
function Client:_cleanup_sse()
  self._sse_connected = false
  self._sse_buf = ""

  if self._sse_tcp then
    local tcp = self._sse_tcp --[[@as uv.uv_tcp_t]]
    self._sse_tcp = nil
    pcall(function()
      tcp:read_stop()
    end)
    -- Graceful shutdown: send FIN, wait for server to close
    pcall(function()
      tcp:shutdown(function()
        if not tcp:is_closing() then
          tcp:close()
        end
      end)
    end)
    -- Fallback: if shutdown fails, force close
    vim.defer_fn(function()
      if tcp and not tcp:is_closing() then
        pcall(function()
          tcp:close()
        end)
      end
    end, 1000)
  end
end

--- Stop the SSE notification stream completely (no reconnect).
function Client:_stop_sse()
  -- Cancel any pending reconnect
  if self._sse_reconnect_timer then
    pcall(function()
      self._sse_reconnect_timer:stop()
      if not self._sse_reconnect_timer:is_closing() then
        self._sse_reconnect_timer:close()
      end
    end)
    self._sse_reconnect_timer = nil
  end

  self:_cleanup_sse()
  log.debug("SSE: Notification stream stopped")
end

--- Schedule SSE reconnection after a delay.
--- Uses exponential-ish backoff: 2 seconds.
function Client:_schedule_sse_reconnect()
  if not self.connected then
    return -- Don't reconnect if client is disconnected
  end

  log.debug("SSE: Scheduling reconnect in 2s")

  local timer = vim.uv.new_timer()
  if not timer then
    return
  end

  -- Clean up any existing reconnect timer
  if self._sse_reconnect_timer then
    pcall(function()
      self._sse_reconnect_timer:stop()
      if not self._sse_reconnect_timer:is_closing() then
        self._sse_reconnect_timer:close()
      end
    end)
  end

  self._sse_reconnect_timer = timer
  timer:start(2000, 0, function()
    self._sse_reconnect_timer = nil
    if not timer:is_closing() then
      timer:close()
    end
    if self.connected and not self._sse_connected then
      vim.schedule(function()
        self:_start_sse()
      end)
    end
  end)
end

--- Start polling for capability changes
--- @param interval_ms number Polling interval in milliseconds (default: 30000)
function Client:_start_polling(interval_ms)
  self:_stop_polling()
  interval_ms = interval_ms or 30000

  local timer = vim.uv.new_timer()
  if not timer then
    log.warn("Failed to create polling timer")
    return
  end

  self._poll_timer = timer
  timer:start(interval_ms, interval_ms, function()
    if not self.connected then
      return
    end
    vim.schedule(function()
      self:refresh_capabilities()
    end)
  end)

  log.debug("Capability polling started (interval: %dms)", interval_ms)
end

--- Stop capability polling
function Client:_stop_polling()
  if self._poll_timer then
    pcall(function()
      self._poll_timer:stop()
      if not self._poll_timer:is_closing() then
        self._poll_timer:close()
      end
    end)
    self._poll_timer = nil
  end
end

return Client
