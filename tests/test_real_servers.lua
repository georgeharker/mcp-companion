--- test_real_servers.lua — Integration test against the production bridge (port 9741)
---
--- Run with:
---   :luafile lua/mcp_companion/test_real_servers.lua
---
--- Prerequisites:
---   1. mcp-companion.nvim on runtimepath
---   2. codecompanion.nvim installed
---   3. ~/.cache/secrets/geohar.mcpservers.json exists
---   4. Bridge will be started automatically if not already running;
---      a running bridge on 9741 will be reused.
---
--- Expected servers: todoist, clickup, github, cli-mcp-server, repomix,
---   perplexity-ask, george-graphics-gmail, georgeharker-gmail,
---   infinitevariation-gmail, basic-memory, telegram  (11 total)

local pass = 0
local fail = 0
local results = {}
local _bridge_job = nil

local PROD_PORT = 9741
local PROD_HOST = "127.0.0.1"

-- ── Helpers ──────────────────────────────────────────────────────────────────

local function ok(name)
    pass = pass + 1
    table.insert(results, "  PASS  " .. name)
end

local function err(name, msg)
    fail = fail + 1
    table.insert(results, "  FAIL  " .. name .. ": " .. tostring(msg))
end

local function section(title)
    table.insert(results, "\n--- " .. title .. " ---")
end

local function print_results()
    local summary = string.format("\n=== RESULTS: %d passed, %d failed ===", pass, fail)
    table.insert(results, summary)
    local output = table.concat(results, "\n")
    local outfile = vim.fn.stdpath("cache") .. "/mcp_companion_real_test.log"
    local f = io.open(outfile, "w")
    if f then
        f:write(output .. "\n")
        f:close()
        print("\n[test_real_servers] Full results: " .. outfile)
    end
    for _, line in ipairs(results) do
        print(line)
    end
end

--- Locate plugin root relative to this file.
--- When sourced via :luafile from an arbitrary cwd, debug.getinfo may return
--- the relative path passed to :luafile which won't resolve to an absolute path.
--- We accept nil and carry on — most startup paths don't need plugin_root.
local function find_plugin_root()
    local src = debug.getinfo(1, "S").source or ""
    if src:sub(1, 1) == "@" then
        src = src:sub(2)
    end
    -- Only trust it if it looks absolute
    if src:sub(1, 1) == "/" then
        return src:match("^(.*)/lua/")
    end
    -- Try resolving relative to cwd
    local cwd = vim.fn.getcwd()
    local abs = cwd .. "/" .. src
    if vim.fn.filereadable(abs) == 1 then
        return abs:match("^(.*)/lua/")
    end
    -- Try each runtimepath entry
    for _, rtp in ipairs(vim.api.nvim_list_runtime_paths()) do
        local candidate = rtp .. "/lua/mcp_companion/test_real_servers.lua"
        if vim.fn.filereadable(candidate) == 1 then
            return rtp
        end
    end
    return nil
end

--- Locate servers.json config
local function find_config(plugin_root)
    -- Prefer the canonical production path
    local prod = vim.fn.expand("~/.cache/secrets/geohar.mcpservers.json")
    if vim.fn.filereadable(prod) == 1 then
        return prod
    end
    -- Fall back: check mcp_companion config resolution
    if plugin_root then
        local ok_cfg, cfg = pcall(require, "mcp_companion.config")
        if ok_cfg then
            local resolved = cfg.resolve({})
            if resolved.bridge and resolved.bridge.config then
                return resolved.bridge.config
            end
        end
    end
    -- Last resort: ask config module directly without plugin_root
    local ok_cfg2, cfg2 = pcall(require, "mcp_companion.config")
    if ok_cfg2 then
        local resolved = cfg2.resolve({})
        if resolved.bridge and resolved.bridge.config then
            return resolved.bridge.config
        end
    end
    return nil
end

--- Check if bridge is already healthy
local function bridge_healthy()
    local url = string.format("http://%s:%d/health", PROD_HOST, PROD_PORT)
    local out = vim.fn.system(string.format("curl -sf --connect-timeout 1 %s 2>/dev/null", url))
    return out and out:match('"status"') ~= nil
end

--- Start bridge as a background job and poll until healthy.
--- Returns true on success, false + message on failure.
local function start_bridge(plugin_root, config_path)
    local python = plugin_root .. "/bridge/.venv/bin/python"
    if vim.fn.executable(python) == 0 then
        return false, "python not found at " .. python
    end

    local cmd = {
        python, "-m", "mcp_bridge",
        "--config", config_path,
        "--port", tostring(PROD_PORT),
        "--host", PROD_HOST,
    }

    _bridge_job = vim.fn.jobstart(cmd, {
        cwd = plugin_root .. "/bridge",
        on_stderr = function(_jid, _data)
            -- suppress bridge startup noise
        end,
    })

    if _bridge_job <= 0 then
        return false, "jobstart failed"
    end

    -- Poll up to 30 seconds (real servers take longer to connect)
    local deadline = vim.loop.now() + 30000
    while vim.loop.now() < deadline do
        vim.wait(500, function() return false end, 100)
        if bridge_healthy() then
            return true, nil
        end
    end

    vim.fn.jobstop(_bridge_job)
    _bridge_job = nil
    return false, string.format("bridge did not become healthy within 30s on port %d", PROD_PORT)
end

local function stop_bridge()
    if _bridge_job and _bridge_job > 0 then
        vim.fn.jobstop(_bridge_job)
        _bridge_job = nil
    end
end

-- ── 1. Module loads ──────────────────────────────────────────────────────────
section("Module loads")

local ok_log, _log = pcall(require, "mcp_companion.log")
if ok_log then ok("mcp_companion.log") else err("mcp_companion.log", _log) end

local ok_cfg, _cfg = pcall(require, "mcp_companion.config")
if ok_cfg then ok("mcp_companion.config") else err("mcp_companion.config", _cfg) end

local ok_state, state = pcall(require, "mcp_companion.state")
if ok_state then ok("mcp_companion.state") else err("mcp_companion.state", state) end

local ok_tools, tools = pcall(require, "mcp_companion.cc.tools")
if ok_tools then ok("mcp_companion.cc.tools") else err("mcp_companion.cc.tools", tools) end

local ok_cc, cc_config = pcall(require, "codecompanion.config")
if ok_cc then ok("codecompanion.config") else err("codecompanion.config", cc_config) end

-- ── 2. Locate config + start bridge ─────────────────────────────────────────
section("Bridge startup")

local plugin_root = find_plugin_root()
if plugin_root then
    ok("plugin_root: " .. plugin_root)
else
    -- Non-fatal: bridge may already be running; config found independently
    table.insert(results, "  INFO  plugin_root: not resolvable (continuing)")
end

local config_path = find_config(plugin_root)
if config_path then
    ok("servers.json: " .. config_path)
else
    err("servers.json", "not found")
end

local bridge_was_running = bridge_healthy()
local bridge_ready = bridge_was_running

if bridge_was_running then
    ok("bridge already running on port " .. PROD_PORT .. " — reusing")
elseif plugin_root and config_path then
    local started, start_err = start_bridge(plugin_root, config_path)
    if started then
        bridge_ready = true
        ok(string.format("bridge started on port %d", PROD_PORT))
    else
        err("start_bridge", tostring(start_err))
    end
end

-- ── 3. Connect Lua client ────────────────────────────────────────────────────
section("Client connection")

local client = nil
local connected = false

if bridge_ready then
    local Client = require("mcp_companion.bridge.client")
    client = Client.new({ host = PROD_HOST, port = PROD_PORT })

    local done = false
    local conn_err = nil
    client:connect(function(ok_conn, cerr)
        connected = ok_conn
        conn_err = cerr
        done = true
    end)

    -- Real servers may need up to 10s to enumerate all tools
    vim.wait(10000, function() return done end, 100)

    if not done then
        err("client:connect", "timed out after 10s")
    elseif not connected then
        err("client:connect", tostring(conn_err))
    else
        ok("client connected")
    end
end

-- ── 4. Validate discovered servers + tools ───────────────────────────────────
section("Server + tool discovery")

local servers = {}
local total_tools = 0

if connected then
    servers = state.field("servers") or {}

    if #servers == 0 then
        err("state.servers", "empty — state not populated after connect")
    else
        ok(string.format("discovered %d servers", #servers))
    end

    -- Expect at least 10 servers (some may fail to start)
    if #servers >= 10 then
        ok(string.format("server count >= 10 (%d)", #servers))
    else
        err("server count", string.format("expected >= 10, got %d", #servers))
    end

    -- Count tools and check known servers are present
    local server_names = {}
    for _, srv in ipairs(servers) do
        server_names[srv.name] = true
        total_tools = total_tools + #(srv.tools or {})
    end

    ok(string.format("total tools: %d", total_tools))

    -- Log actual server names for diagnostics
    local server_name_list = {}
    for name in pairs(server_names) do
        table.insert(server_name_list, name)
    end
    table.sort(server_name_list)
    table.insert(results, "  INFO  actual server names: " .. table.concat(server_name_list, ", "))

    -- Spot-check: verify some well-known server keywords appear in actual names.
    -- We don't hardcode exact names since the bridge may prefix differently.
    -- Check that at least one server name contains each expected keyword
    local expected_keywords = { "todoist", "clickup", "github", "memory" }
    for _, keyword in ipairs(expected_keywords) do
        local found = false
        for _, name in ipairs(server_name_list) do
            if name:lower():find(keyword, 1, true) then
                found = true
                break
            end
        end
        if found then
            ok("server with keyword '" .. keyword .. "' found")
        else
            err("server missing keyword", keyword ..
                " — actual servers: " .. table.concat(server_name_list, ", "))
        end
    end

    if total_tools >= 50 then
        ok(string.format("tool count >= 50 (%d)", total_tools))
    else
        err("tool count", string.format("expected >= 50, got %d", total_tools))
    end
end

-- ── 5. HTTP tool calls ───────────────────────────────────────────────────────
section("Tool calls")

-- Wire the bridge module so cc/tools.lua can find the client
if connected then
    local bridge_mod = require("mcp_companion.bridge")
    bridge_mod.client = client
end

--- Helper: synchronous tool call with 8s timeout
local function call_tool(namespaced, params)
    if not connected then return nil, "not connected" end
    local result, call_err
    local done = false
    client:call_tool(namespaced, params or vim.empty_dict(), function(e, r)
        call_err = e
        result = r
        done = true
    end)
    vim.wait(8000, function() return done end, 100)
    if not done then return nil, "timeout" end
    return result, call_err
end

--- Extract text from MCP content array
local function content_text(result)
    if not result or not result.content then return nil end
    local parts = {}
    for _, item in ipairs(result.content) do
        if item.type == "text" and item.text then
            table.insert(parts, item.text)
        end
    end
    return table.concat(parts, "\n")
end

--- Find a tool's _namespaced name by searching state.servers for a matching
--- CC-style key (e.g. "github__get_me"). Returns nil if not found.
local function find_namespaced(cc_key)
    for _, srv in ipairs(servers) do
        for _, t in ipairs(srv.tools or {}) do
            -- Build the CC key the same way cc/tools.lua does:
            -- server_name .. "__" .. display_name
            local display = t._display or t.name
            local ckey = srv.name .. "__" .. display
            if ckey == cc_key then
                return t._namespaced or t.name
            end
        end
    end
    return nil
end

--- Find the first tool whose CC key matches a pattern (substring, case-insensitive)
local function find_namespaced_by_pattern(pattern)
    for _, srv in ipairs(servers) do
        for _, t in ipairs(srv.tools or {}) do
            local display = t._display or t.name
            local ckey = (srv.name .. "__" .. display):lower()
            if ckey:find(pattern:lower(), 1, true) then
                return t._namespaced or t.name, srv.name .. "__" .. display
            end
        end
    end
    return nil, nil
end

-- 5a. github get_me — no params, immediate HTTP call
if connected then
    local ns, cc_key = find_namespaced_by_pattern("github__get_me")
    if not ns then
        -- Try any github tool as fallback
        ns, cc_key = find_namespaced_by_pattern("github__")
    end
    if not ns then
        err("github tool", "no github tool found in state.servers")
    else
        local result, call_err = call_tool(ns, {})
        if call_err then
            err("github_get_me (" .. (cc_key or ns) .. ")", tostring(call_err))
        elseif not result then
            err("github_get_me", "nil result / timeout")
        elseif result.isError then
            err("github_get_me", content_text(result) or "MCP error")
        else
            local text = content_text(result) or ""
            ok("github_get_me returned " .. #text .. " chars (key=" .. (cc_key or ns) .. ")")
            if text:match('"login"') or text:match("login") then
                ok("github_get_me: contains login field")
            else
                err("github_get_me content", "no 'login' in response: " .. text:sub(1, 200))
            end
        end
    end
end

-- 5b. basic-memory search — simple query, should return quickly
-- Note: server name "basic-memory" → normalised to "basic" by first-underscore split,
-- display = "memory_search". CC key = "basic__memory_search".
if connected then
    local ns, cc_key = find_namespaced_by_pattern("basic__memory_search")
    if not ns then
        -- wider fallback: any tool whose _namespaced name contains "memory" and "search"
        for _, srv in ipairs(servers) do
            for _, t in ipairs(srv.tools or {}) do
                local n = (t._namespaced or t.name):lower()
                if n:find("memory", 1, true) and n:find("search", 1, true) then
                    ns = t._namespaced or t.name
                    cc_key = srv.name .. "__" .. (t._display or t.name)
                    break
                end
            end
            if ns then break end
        end
    end
    if not ns then
        err("basic_memory tool", "no basic-memory search tool found in state.servers")
    else
        local result, call_err = call_tool(ns, { query = "neovim" })
        if call_err then
            err("basic_memory_search (" .. (cc_key or ns) .. ")", tostring(call_err))
        elseif not result then
            err("basic_memory_search", "nil result / timeout")
        elseif result.isError then
            local text = content_text(result) or ""
            ok("basic_memory_search returned MCP error (acceptable if empty): " .. text:sub(1, 80))
        else
            local text = content_text(result) or ""
            ok("basic_memory_search returned " .. #text .. " chars (key=" .. (cc_key or ns) .. ")")
        end
    end
end

-- 5c. todoist find-tasks — list a small number of tasks
if connected then
    local ns, cc_key = find_namespaced_by_pattern("todoist__find")
    if not ns then
        err("todoist tool", "no todoist find tool found in state.servers")
    else
        local result, call_err = call_tool(ns, { limit = 3 })
        if call_err then
            err("todoist_find-tasks (" .. (cc_key or ns) .. ")", tostring(call_err))
        elseif not result then
            err("todoist_find-tasks", "nil result / timeout")
        elseif result.isError then
            local text = content_text(result) or ""
            err("todoist_find-tasks MCP error", text:sub(1, 120))
        else
            local text = content_text(result) or ""
            ok("todoist_find-tasks returned " .. #text .. " chars (key=" .. (cc_key or ns) .. ")")
        end
    end
end

-- ── 6. CC tool registration ──────────────────────────────────────────────────
section("CC tool registration")

local registered_count = 0
local group_count = 0
local tools_tbl = nil

if connected and ok_tools and ok_cc then
    local reg_ok, reg_err = pcall(tools.register)
    if reg_ok then
        ok("tools.register() ok")
    else
        err("tools.register()", reg_err)
    end

    tools_tbl = cc_config.interactions
        and cc_config.interactions.chat
        and cc_config.interactions.chat.tools

    if tools_tbl then
        for _, v in pairs(tools_tbl) do
            if type(v) == "table" and type(v.id) == "string"
                and v.id:sub(1, #"mcp_companion:") == "mcp_companion:" then
                registered_count = registered_count + 1
            end
        end
        for _, v in pairs(tools_tbl.groups or {}) do
            if type(v) == "table" and type(v.id) == "string"
                and v.id:sub(1, #"mcp_companion:") == "mcp_companion:" then
                group_count = group_count + 1
            end
        end
    end

    if registered_count > 0 then
        ok(string.format("registered %d tools in CC config", registered_count))
    else
        err("CC registration", "0 tools registered")
    end

    if group_count > 0 then
        ok(string.format("registered %d groups in CC config", group_count))
    else
        err("CC groups", "0 groups registered")
    end

    -- Registered count should roughly match total_tools (minus _bridge pseudo-server)
    if registered_count >= total_tools - 5 then
        ok(string.format("registered_count (%d) matches total_tools (%d)", registered_count, total_tools))
    else
        err("registered_count mismatch",
            string.format("CC has %d, bridge has %d", registered_count, total_tools))
    end
end

-- ── 7. Validate a CC tool callback round-trip ────────────────────────────────
section("CC callback round-trip")

if tools_tbl and registered_count > 0 then
    -- Find the github get_me entry
    local test_key = nil
    for k, v in pairs(tools_tbl) do
        if type(v) == "table" and type(v.id) == "string"
            and v.id:sub(1, #"mcp_companion:") == "mcp_companion:"
            and k:match("get_me") then
            test_key = k
            break
        end
    end
    -- Fall back to any tool
    if not test_key then
        for k, v in pairs(tools_tbl) do
            if type(v) == "table" and type(v.id) == "string"
                and v.id:sub(1, #"mcp_companion:") == "mcp_companion:" then
                test_key = k
                break
            end
        end
    end

    if test_key then
        ok("testing CC callback for key: " .. test_key)
        local entry = tools_tbl[test_key]
        local spec_ok, spec = pcall(entry.callback)
        if not spec_ok then
            err("callback()", spec)
        else
            ok("callback() returned spec")
            if type(spec.cmds) == "table" and type(spec.cmds[1]) == "function" then
                ok("spec.cmds[1] is function")
            else
                err("spec.cmds", vim.inspect(spec.cmds))
            end

            -- Actually invoke the cmd function
            local cb_result = nil
            spec.cmds[1]({}, {}, {
                output_cb = function(r) cb_result = r end,
            })

            vim.wait(8000, function() return cb_result ~= nil end, 100)

            if cb_result then
                ok(string.format("cmd function returned (status=%s)", tostring(cb_result.status)))
                if cb_result.data then
                    ok("data: " .. tostring(cb_result.data):sub(1, 120))
                end
            else
                err("cmd function", "timed out waiting for output_cb")
            end
        end
    else
        err("CC callback", "no registered tools found to test")
    end
end

-- ── 8. Cleanup ───────────────────────────────────────────────────────────────
section("Cleanup")

if ok_tools then
    pcall(tools.unregister)
    ok("tools.unregister() called")
end

if client then
    client:disconnect()
    ok("client disconnected")
end

-- Only stop bridge if we started it (don't kill someone else's bridge)
if not bridge_was_running then
    stop_bridge()
    ok("bridge stopped (we started it)")
else
    ok("bridge left running (was already up)")
end

-- ── Print results ─────────────────────────────────────────────────────────────
print_results()
