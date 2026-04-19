--- mcp-companion.nvim — CC Extension entry point
--- Bridges MCP capabilities into CodeCompanion:
---   - MCP tools → CC tools (function calling)
---   - MCP resources → CC #editor_context entries
---   - MCP prompts → CC / slash commands
---
--- Registered via CodeCompanion.register_extension("mcp_companion", M)
--- @module mcp_companion.cc

local M = {}

local log = require("mcp_companion.log")

-- Token generated in ACPSessionPre, consumed by the patched transform_to_acp
-- which runs (via _establish_session) immediately after Pre fires.
-- Keyed by adapter name so concurrent ACP sessions don't collide.
-- { [adapter_name] = { token=string, agent_capabilities=table|nil } }
M._pending_acp_tokens = {}

-- Adapter names known to be ACP-type (populated on first ACPSessionPre).
-- Used to skip per-chat HTTP client setup for ACP chats in _auto_http_tools.
M._acp_adapter_names = {}

--- Build bridge MCP server entry for ACP session/new.
--- Each ACP session gets a unique URL (/mcp/<token>) so the bridge can
--- associate the MCP connection with the correct ACP chat session.
--- @param agent_capabilities table|nil agentCapabilities from ACP INITIALIZE RPC
--- @param token string UUID token identifying this ACP session
--- @return table|nil bridge_entry MCP server entry or nil if no bridge config
local function build_bridge_entry(agent_capabilities, token)
  local config = require("mcp_companion.config").get()

  -- Need bridge config to know host/port
  if not config.bridge or not config.bridge.config then
    return nil
  end

  local host = config.bridge.host or "127.0.0.1"
  local port = config.bridge.port or 9741

  -- Check if agent supports HTTP MCP transport
  local caps = agent_capabilities and agent_capabilities.mcpCapabilities

  if caps and caps.http then
    -- Token is sent via X-MCP-Bridge-Session header (per ACP spec, HTTP MCPs
    -- must support custom headers). When bridge.token_in_url is true, the token
    -- is also embedded in the URL path as a fallback for agents whose MCP SDK
    -- does not forward headers. If your agent fails to route tools, enable
    -- token_in_url in config and report the agent at:
    -- https://github.com/geohar/mcp-companion/issues
    local token_in_url = config.bridge and config.bridge.token_in_url
    local bridge_url
    if token_in_url then
      bridge_url = string.format("http://%s:%d/mcp/%s", host, port, token)
    else
      bridge_url = string.format("http://%s:%d/mcp", host, port)
    end
    log.debug("CC ACP: using HTTP transport for bridge (token=%s url=%s token_in_url=%s)",
      token, bridge_url, tostring(token_in_url))
    return {
      type = "http",
      name = "mcp-bridge",
      url = bridge_url,
      headers = { { name = "X-MCP-Bridge-Session", value = token } },
    }
  else
    -- Fallback: stdio via mcp-remote
    local bridge_url = string.format("http://%s:%d/mcp", host, port)
    log.debug("CC ACP: using stdio mcp-remote transport for bridge (token=%s)", token)
    return {
      name = "mcp-bridge",
      command = "npx",
      args = { "-y", "mcp-remote", bridge_url },
      env = { { name = "MCP_ACP_TOKEN", value = token } },
    }
  end
end



--- Called by CodeCompanion when the extension is loaded.
--- Sets up event listeners that trigger (re)registration when the bridge
--- connects or capabilities change.
--- Also patches ACP to inject bridge as MCP server for ACP agents.
--- @param schema? table Extension schema from CC config
function M.setup(schema) -- luacheck: ignore 212/schema
  local state = require("mcp_companion.state")
  math.randomseed(vim.loop.hrtime())

  -- Start bridge when any chat adapter is created.
  -- Block briefly to ensure tools are registered before first submit.
  -- With parallel requests and "healthy" state, this blocks for
  -- at most the MCP client connect time (~300ms if bridge already up).
  -- Use a generous timeout (30s) to accommodate OAuth browser flows on first
  -- connection — the wait resolves immediately once the bridge is healthy.
  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionChatAdapter",
    callback = function()
      M._wait_for_bridge(30000)
    end,
  })

  -- Auto-enable MCP tool groups when chat is created
  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionChatCreated",
    callback = function(args)
      M._auto_http_tools(args.data)
    end,
  })

  -- Patch codecompanion.mcp.transform_to_acp (once) to:
  --   1. Also translate HTTP servers from config.mcp.servers (upstream only handles stdio)
  --   2. Append the bridge entry for the current ACP session token
  -- Guarded with _mcp_companion_patched so re-calling setup() never double-wraps.
  local ok, cc_mcp = pcall(require, "codecompanion.mcp")
  if ok and cc_mcp and cc_mcp.transform_to_acp and not cc_mcp._mcp_companion_patched then
    local _orig_transform_to_acp = cc_mcp.transform_to_acp
    cc_mcp.transform_to_acp = function(adapter_name)
      -- Call original (handles stdio servers in default_servers list)
      local result = _orig_transform_to_acp(adapter_name)

      -- Also translate HTTP servers from config.mcp.servers that upstream ignores.
      -- These are user-configured intent and should be passed through as-is.
      local cc_config = require("codecompanion.config")
      local mcp_servers = cc_config.mcp and cc_config.mcp.servers or {}
      local default_servers = cc_config.mcp and cc_config.mcp.opts and cc_config.mcp.opts.default_servers or {}
      for name, cfg in pairs(mcp_servers) do
        if vim.tbl_contains(default_servers, name) and cfg.url then
          local headers = {}
          if cfg.headers then
            for k, v in pairs(cfg.headers) do
              table.insert(headers, { name = k, value = v })
            end
          end
          -- Avoid duplicates (upstream may eventually handle these)
          local already = false
          for _, s in ipairs(result) do
            if s.name == name then already = true; break end
          end
          if not already then
            table.insert(result, { type = "http", name = name, url = cfg.url, headers = headers })
            log.debug("CC ACP: transform_to_acp added HTTP server %s → %s", name, cfg.url)
          end
        end
      end

      -- Append bridge entry for the pending ACP session token.
      -- CC calls transform_to_acp() with no args so adapter_name is nil;
      -- grab the first (only) pending entry — one ACP session establishes at a time.
      local pending = adapter_name and M._pending_acp_tokens[adapter_name]
      if not pending then
        local _, v = next(M._pending_acp_tokens)
        pending = v
      end
      if pending and pending.token then
        local bridge_entry = build_bridge_entry(pending.agent_capabilities, pending.token)
        if bridge_entry then
          local already = false
          for _, s in ipairs(result) do
            if s.name == "mcp-bridge" then already = true; break end
          end
          if not already then
            table.insert(result, bridge_entry)
            log.info("CC ACP: transform_to_acp injected bridge (token=%s)", pending.token)
          end
        end
      end

      return result
    end
    cc_mcp._mcp_companion_patched = true
    log.debug("CC ACP: patched codecompanion.mcp.transform_to_acp")
  elseif ok and cc_mcp and cc_mcp._mcp_companion_patched then
    log.debug("CC ACP: transform_to_acp already patched, skipping")
  else
    log.warn("CC ACP: could not patch transform_to_acp (codecompanion.mcp not available)")
  end

  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionACPSessionPre",
    callback = function(args)
      local adapter_modified = args.data and args.data.adapter_modified
      local agent_capabilities = args.data and args.data.agent_capabilities
      if not adapter_modified then
        log.warn("CC ACP: CodeCompanionACPSessionPre fired but adapter_modified is nil")
        return
      end
      log.debug("CC ACP: ACPSessionPre adapter=%s name=%s", tostring(adapter_modified), tostring(adapter_modified.name))

      -- Record this adapter name as ACP-type so _auto_http_tools skips it
      local adapter_name = adapter_modified.name
      M._acp_adapter_names[adapter_name] = true

      -- Kick off bridge warm-up (non-blocking).
      M._start_bridge_async()

      local cfg = require("mcp_companion.config").get()
      local auto_acp_tools = cfg.cc and cfg.cc.auto_acp_tools

      -- Generate per-session token. Store in _pending_acp_tokens so the
      -- patched transform_to_acp (called from _establish_session immediately
      -- after this event) can append the bridge entry.
      local token = M._generate_token()
      M._pending_acp_tokens[adapter_name] = {
        token = token,
        agent_capabilities = agent_capabilities,
      }
      log.info("CC ACP: Pre stored pending token for adapter=%s token=%s", adapter_name, token)

      -- If mcpServers is a concrete table (not "inherit_from_config"), inject
      -- directly — transform_to_acp is never called in that path.
      local defaults = adapter_modified.defaults
      if defaults and type(defaults.mcpServers) == "table" then
        local bridge_entry = build_bridge_entry(agent_capabilities, token)
        if bridge_entry then
          local already = false
          for _, s in ipairs(defaults.mcpServers) do
            if s.name == "mcp-bridge" then already = true; break end
          end
          if not already then
            table.insert(defaults.mcpServers, bridge_entry)
            log.info("CC ACP: Pre injected bridge into concrete mcpServers (token=%s)", token)
          end
        end
      end
      -- "inherit_from_config" case is handled by the patched transform_to_acp.

      -- Also store on chat.adapter so ACPSessionPost can retrieve it.
      local cc_ok, codecompanion = pcall(require, "codecompanion")
      if cc_ok then
        local all_chats = codecompanion.buf_get_chat()
        for _, entry in ipairs(all_chats or {}) do
          local c = entry.chat
          if c and c.adapter and c.adapter.name == adapter_name then
            -- Discard any per-chat HTTP client created before ACP session established
            if c._mcp_client then
              c._mcp_client:disconnect()
              c._mcp_client = nil
              log.debug("CC ACP: discarded pre-existing HTTP per-chat client (adapter=%s)", adapter_name)
            end
            c.adapter._mcp_token = token
            if auto_acp_tools == false then
              c.adapter._mcp_allowed_servers = {}
              log.debug("CC ACP: stored token on adapter (token=%s, allowed=none)", token)
            elseif type(auto_acp_tools) == "table" then
              c.adapter._mcp_allowed_servers = auto_acp_tools
              log.debug("CC ACP: stored token on adapter (token=%s, allowed=%s)", token, vim.inspect(auto_acp_tools))
            else
              log.debug("CC ACP: stored token on adapter (token=%s, allowed=all)", token)
            end
            break
          end
        end
      end
    end,
  })

  -- After the ACP session is established, find the chat and read the token
  -- from chat.adapter (stored there in ACPSessionPre, persists since
  -- acp_connection.adapter IS chat.adapter — same object reference).
  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionACPSessionPost",
    callback = function(args)
      log.debug("CC ACP: ACPSessionPost fired")
      local acp_session_id = args.data and args.data.session_id
      if not acp_session_id then
        log.debug("CC ACP: ACPSessionPost has no session_id in args.data")
        return
      end
      log.debug("CC ACP: ACPSessionPost session_id=%s", acp_session_id)

      -- Find the chat by iterating all chats and matching acp_connection.session_id.
      local chat
      local cc_ok, codecompanion = pcall(require, "codecompanion")
      if cc_ok then
        local all_chats = codecompanion.buf_get_chat()
        for _, entry in ipairs(all_chats or {}) do
          local c = entry.chat
          if c and c.acp_connection and c.acp_connection.session_id == acp_session_id then
            chat = c
            break
          end
        end
      end

      if not chat then
        log.debug("CC ACP: no chat found for session %s", acp_session_id)
        return
      end

      -- Read token from chat.adapter (stored there in ACPSessionPre)
      log.debug("CC ACP: Post found chat bufnr=%s adapter=%s adapter._mcp_token=%s",
        tostring(chat.bufnr), tostring(chat.adapter), tostring(chat.adapter and chat.adapter._mcp_token))
      local token = chat.adapter and chat.adapter._mcp_token
      local allowed_servers = chat.adapter and chat.adapter._mcp_allowed_servers

      if not token then
        log.warn("CC ACP: no token on chat.adapter for session %s (adapter=%s) — Pre event may have missed this chat",
          acp_session_id, tostring(chat.adapter))
        return
      end

      -- Clear the pending token now that it's been consumed by transform_to_acp
      local adapter_name = chat.adapter and chat.adapter.name
      if adapter_name then
        M._pending_acp_tokens[adapter_name] = nil
      end

      -- Copy to chat object for easy access in session_commands and cleanup
      chat._mcp_token = token
      chat._mcp_allowed_servers = allowed_servers
      log.info("CC ACP: token picked up in Post (session=%s token=%s bufnr=%s allowed=%s)",
        acp_session_id, token, tostring(chat.bufnr),
        allowed_servers and vim.inspect(allowed_servers) or "all")

      -- Apply filter immediately via token endpoint. Bridge stores it as pending
      -- if opencode hasn't connected yet, and applies it when the token is first seen.
      M._apply_token_filter(chat)
    end,
  })

  -- When bridge connects and capabilities are populated, register everything
  state.on("bridge_ready", function()
    log.debug("CC extension: bridge_ready — registering all")
    M._register_all()
  end)

  -- Re-register when servers change
  state.on("servers_updated", function()
    log.debug("CC extension: servers_updated — re-registering all")
    M._register_all()
  end)

  -- Register static /mcp-session slash command (once, not on bridge_ready)
  require("mcp_companion.cc.session_commands").register()

  -- Clean up per-chat session state when a chat buffer is closed
  vim.api.nvim_create_autocmd("User", {
    pattern = "CodeCompanionChatClosed",
    callback = function(args)
      if args.data and args.data.bufnr then
        local bufnr = args.data.bufnr
        require("mcp_companion.cc.session_commands").clear(bufnr)
        -- Retrieve the chat object to get the bridge session ID stored on it.
        local chat
        local cc_ok, codecompanion = pcall(require, "codecompanion")
        if cc_ok then
          chat = codecompanion.buf_get_chat(bufnr)
        end
        M._cleanup_session_filter(chat)
      end
    end,
  })

  log.info("CC extension initialized")
end

--- Auto-enable MCP tool groups in a newly created chat.
--- Behaviour is controlled by config.cc.auto_http_tools:
---   true (default) — add the aggregate @mcp-bridge group (all servers, one entry)
---   false          — do not auto-add anything; user @-mentions groups manually
---   string[]       — add only the named per-server groups (e.g. {"github","filesystem"})
--- @param event_data table Event data with bufnr and id
function M._auto_http_tools(event_data)
  if not event_data or not event_data.bufnr then
    return
  end

  local state = require("mcp_companion.state")
  if state.get().bridge.status ~= "connected" then
    log.debug("CC: bridge not connected, skipping auto-enable")
    return
  end

  -- Get the chat instance via bufnr
  local cc_ok, codecompanion = pcall(require, "codecompanion")
  if not cc_ok then return end

  local chat = codecompanion.buf_get_chat(event_data.bufnr)
  if not chat or not chat.tool_registry then
    log.debug("CC: chat or tool_registry not found for bufnr %s", event_data.bufnr)
    return
  end

  -- Always set up a per-chat bridge client for HTTP-adapter chats (non-ACP).
  -- This must happen even when auto_http_tools=false so the bridge has a
  -- per-chat session for filtering and MCPStatus can show session state.
  -- ACP chats are skipped — they get their token via ACPSessionPre/Post.
  local adapter_name = chat.adapter and chat.adapter.name
  if not M._acp_adapter_names[adapter_name] then
    M._setup_http_per_chat(chat)
  end

  local cfg = require("mcp_companion.config").get()
  local auto = cfg.cc and cfg.cc.auto_http_tools
  if auto == false then
    log.debug("CC: auto_http_tools=false, skipping tool group registration")
    return
  end

  local mcp_ok, cc_mcp = pcall(require, "codecompanion.mcp")
  if not mcp_ok then return end

  chat.tools:refresh({ adapter = chat.adapter })

  if auto == true then
    -- Add the aggregate bridge group — one context block entry covering all servers
    local bridge_group = cc_mcp.tool_prefix() .. "bridge"
    chat.tool_registry:add(bridge_group, { config = chat.tools.tools_config })
    log.info("CC: auto-enabled aggregate bridge tool group")
  elseif type(auto) == "table" then
    -- Add only the named per-server groups
    local enabled_count = 0
    for _, server_name in ipairs(auto) do
      local group_name = cc_mcp.tool_prefix() .. server_name
      chat.tool_registry:add(group_name, { config = chat.tools.tools_config })
      enabled_count = enabled_count + 1
    end
    log.info("CC: auto-enabled %d named MCP server tool groups", enabled_count)
  end
end

--- Create and connect a per-chat MCP client for HTTP-adapter chats.
--- Stores the client on chat._mcp_client and the token on chat._mcp_token.
--- The bridge-side filter is derived from config.cc.auto_http_tools so the
--- bridge is the source of truth for which servers are enabled per session.
--- @param chat table CC chat object
function M._setup_http_per_chat(chat)
  if chat._mcp_client or chat._mcp_token then
    return -- already set up
  end

  local bridge = require("mcp_companion.bridge")
  if not bridge.client or not bridge.client.connected then
    log.debug("CC HTTP: bridge not connected, skipping per-chat client setup")
    return
  end

  local token = M._generate_token()
  local cfg = require("mcp_companion.config").get()
  local auto_http = cfg.cc and cfg.cc.auto_http_tools

  -- Derive the allowed-servers list for the bridge-side filter.
  -- This makes the bridge the source of truth; the Neovim-side tool_registry
  -- mirrors this in _auto_http_tools().
  local allowed
  if auto_http == false then
    allowed = {}
  elseif type(auto_http) == "table" then
    allowed = auto_http
  end
  -- auto_http == true (default) → allowed stays nil → no filter → all servers

  chat._mcp_token = token
  chat._mcp_allowed_servers = allowed

  local per_chat_client = bridge.new_per_chat_client(token)
  chat._mcp_client = per_chat_client

  log.info("CC HTTP: connecting per-chat client (token=%s bufnr=%s)", token, tostring(chat.bufnr))

  per_chat_client:connect(function(ok, err)
    if ok then
      log.info("CC HTTP: per-chat client connected (token=%s)", token)
      M._apply_token_filter(chat)
    else
      log.warn("CC HTTP: per-chat client connect failed (token=%s): %s", token, tostring(err))
      -- Clear so we don't hold a broken client; tool calls fall back to singleton
      chat._mcp_client = nil
    end
  end)
end

--- Called on ChatAdapter event so bridge starts warming up while UI loads.
function M._start_bridge_async()
  local state = require("mcp_companion.state")
  local config = require("mcp_companion.config")

  -- Already connected, healthy, or connecting
  local bridge_status = state.get().bridge.status
  if bridge_status == "connected" or bridge_status == "connecting" or bridge_status == "healthy" then
    return
  end

  -- No bridge config
  if not config.get().bridge.config then
    log.debug("CC: no bridge config, skipping bridge start")
    return
  end

  log.info("CC: starting bridge async on ChatAdapter event")
  require("mcp_companion.bridge").start()
end

--- Wait for bridge to be fully connected (tools registered).
--- Used by ChatAdapter to ensure tools are available before first submit.
--- With parallel requests, the healthy→connected gap is ~200ms.
--- @param timeout_ms? number Maximum time to wait (default 5000)
--- @return boolean success Whether bridge is connected
function M._wait_for_bridge(timeout_ms)
  timeout_ms = timeout_ms or 5000
  local state = require("mcp_companion.state")

  local function is_connected()
    return state.get().bridge.status == "connected"
  end

  -- Already connected
  if is_connected() then
    return true
  end

  -- Not even started - start it now
  local s = state.get().bridge.status
  if s ~= "connecting" and s ~= "healthy" then
    M._start_bridge_async()
  end

  -- Wait for full connect (tools registered)
  local ok = vim.wait(timeout_ms, is_connected, 50)

  if ok then
    log.info("CC: bridge connected")
    -- Register tools synchronously so they're available on this tick.
    -- The bridge_ready event also triggers _register_all() via vim.schedule,
    -- but that runs on the next event loop tick — too late for the first
    -- chat submit.
    M._register_all()
  else
    log.warn("CC: bridge did not connect in %dms", timeout_ms)
  end

  return ok
end

--- Generate a random UUID v4 token for ACP session correlation.
--- @return string uuid
function M._generate_token()
  local t = {
    math.random(0, 0xffffffff),       -- 32 bits
    math.random(0, 0xffff),           -- 16 bits
    0x4000 + math.random(0, 0x0fff),  -- version 4: 0x4xxx
    0x8000 + math.random(0, 0x3fff),  -- variant: 10xx
    math.random(0, 0xffffffffffff),   -- 48 bits
  }
  return string.format("%08x-%04x-%04x-%04x-%012x", t[1], t[2], t[3], t[4], t[5])
end

--- Apply server filter for a chat session via the token endpoint.
--- Works for both ACP and HTTP adapter chats. The bridge stores the filter
--- as pending if the remote client hasn't connected yet (ACP case), and
--- applies it immediately when the token is first seen.
--- @param chat table CC chat object with _mcp_token and _mcp_allowed_servers set
function M._apply_token_filter(chat)
  if not chat or not chat._mcp_token then return end

  local token = chat._mcp_token
  local allowed = chat._mcp_allowed_servers
  if not allowed then
    log.debug("CC: no filter, all servers enabled (token=%s)", token)
    return
  end

  local cfg = require("mcp_companion.config").get()
  local host = cfg.bridge.host or "127.0.0.1"
  local port = cfg.bridge.port or 9741
  local http = require("mcp_companion.http")
  local body = vim.json.encode({ allowed_servers = allowed })

  http.request({
    url = string.format("http://%s:%d/sessions/token/%s/filter", host, port, token),
    method = "post",
    headers = { ["Content-Type"] = "application/json" },
    body = body,
    timeout = 5000,
    callback = function(r)
      if r.status == 200 then
        local r_ok, r_data = pcall(vim.json.decode, r.body)
        local disabled_list = r_ok and r_data and r_data.disabled_servers or {}
        local pending = r_ok and r_data and r_data.pending
        log.info("CC: session filter %s (token=%s allowed=%s disabled=%s)",
          pending and "stored as pending" or "applied",
          token, table.concat(allowed, ", "), table.concat(disabled_list, ", "))
        vim.schedule(function()
          local sc_ok, sc = pcall(require, "mcp_companion.cc.session_commands")
          if sc_ok and sc.set_session_state and chat.bufnr then
            local disabled_map = {}
            for _, name in ipairs(disabled_list) do disabled_map[name] = true end
            sc.set_session_state(chat.bufnr, disabled_map)
          end
        end)
      else
        log.warn("CC: session filter failed (status %s): %s", r.status, r.body or "")
      end
    end,
  })
end

--- Clean up session filter and per-chat client on chat close.
--- @param chat table|nil CC chat object (may be nil if chat already destroyed)
function M._cleanup_session_filter(chat)
  if not chat then return end

  -- Disconnect per-chat MCP client if present
  if chat._mcp_client then
    chat._mcp_client:disconnect()
    chat._mcp_client = nil
    log.debug("CC: per-chat client disconnected (bufnr=%s)", tostring(chat.bufnr))
  end

  if not chat._mcp_token then return end

  local token = chat._mcp_token
  chat._mcp_token = nil
  chat._mcp_allowed_servers = nil

  local cfg = require("mcp_companion.config").get()
  local host = cfg.bridge.host or "127.0.0.1"
  local port = cfg.bridge.port or 9741
  local http = require("mcp_companion.http")

  http.request({
    url = string.format("http://%s:%d/sessions/token/%s/filter", host, port, token),
    method = "delete",
    timeout = 3000,
    callback = function(r)
      log.debug("CC: session filter removed (token=%s status=%s)", token, r.status)
    end,
  })
end

function M._register_all()
  M._register_tools()
  M._register_editor_context()
  M._register_prompts()
end

function M._register_tools()
  local ok, tools = pcall(require, "mcp_companion.cc.tools")
  if ok then
    tools.register()
  else
    log.warn("Failed to load cc.tools: %s", tostring(tools))
  end
end

function M._register_editor_context()
  local ok, editor_ctx = pcall(require, "mcp_companion.cc.editor_context")
  if ok then
    editor_ctx.register()
  else
    log.warn("Failed to load cc.editor_context: %s", tostring(editor_ctx))
  end
end

function M._register_prompts()
  local ok, cmds = pcall(require, "mcp_companion.cc.slash_commands")
  if ok then
    cmds.register()
  else
    log.warn("Failed to load cc.slash_commands: %s", tostring(cmds))
  end
end

--- Extension exports (accessible via CodeCompanion.extensions.mcp_companion)
M.exports = {
  --- Get current plugin state
  status = function()
    return require("mcp_companion.state").get()
  end,

  --- Get bridge client (for direct MCP calls if needed)
  client = function()
    local bridge = require("mcp_companion.bridge")
    return bridge.client
  end,

  --- Force refresh all capabilities
  refresh = function()
    local bridge = require("mcp_companion.bridge")
    local client = bridge.client
    if client and client.connected then
      client:refresh_capabilities()
    end
  end,
}

return M
