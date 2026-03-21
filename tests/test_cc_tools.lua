--- test_cc_tools.lua — Validate CC tool registration in Neovim
---
--- Run with:
---   :luafile lua/mcp_companion/test_cc_tools.lua
---
--- Prerequisites:
---   1. codecompanion.nvim installed (lazy, plug, etc.)
---   2. mcp-companion.nvim on runtimepath
---   3. npx available (for @modelcontextprotocol/server-everything)
---
--- The test manages its own bridge instance on port 9742 to avoid
--- conflicting with any production bridge on 9741.

local pass = 0
local fail = 0
local results = {}
local _bridge_job = nil   -- track the test bridge process for cleanup

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
    -- Write to file so output isn't truncated by notify/cmdline limits
    local outfile = vim.fn.stdpath("cache") .. "/mcp_companion_test.log"
    local f = io.open(outfile, "w")
    if f then
        f:write(output .. "\n")
        f:close()
        print("\n[test_cc_tools] Full results written to: " .. outfile)
    end
    -- Also print each line individually to :messages (cmdline handles long output better than notify)
    for _, line in ipairs(results) do
        print(line)
    end
end

--- Locate the bridge directory relative to this file
local function find_bridge_dir()
    -- Try relative to this file's location
    local this_file = debug.getinfo(1, "S").source:sub(2)  -- strip leading '@'
    -- this_file is e.g. .../mcp-companion.nvim/lua/mcp_companion/test_cc_tools.lua
    local plugin_root = this_file:match("^(.*)/lua/")
    if plugin_root then
        local bridge = plugin_root .. "/bridge"
        if vim.fn.isdirectory(bridge) == 1 then
            return bridge
        end
    end
    -- Fallback: look relative to cwd
    local cwd = vim.fn.getcwd()
    for _, rel in ipairs({ "bridge", "../bridge" }) do
        local p = cwd .. "/" .. rel
        if vim.fn.isdirectory(p) == 1 then
            return p
        end
    end
    return nil
end

local TEST_PORT = 9742

--- Start a test bridge on TEST_PORT and wait until /health responds.
--- Returns true on success, false + message on failure.
local function start_test_bridge(bridge_dir, fixture_path)
    local python = bridge_dir .. "/.venv/bin/python"
    if vim.fn.executable(python) == 0 then
        return false, "python not found at " .. python
    end

    local cmd = {
        python, "-m", "mcp_bridge",
        "--config", fixture_path,
        "--port", tostring(TEST_PORT),
    }

    _bridge_job = vim.fn.jobstart(cmd, {
        cwd = bridge_dir,
        on_stderr = function(_jid, _data)
            -- suppress noisy bridge logs during test
        end,
    })

    if _bridge_job <= 0 then
        return false, "jobstart failed"
    end

    -- Poll /health until ready or timeout (12 seconds, 200ms intervals)
    local deadline = vim.loop.now() + 12000
    local ready = false
    while vim.loop.now() < deadline do
        vim.wait(200, function() return false end, 50)
        local out = vim.fn.system(
            string.format("curl -sf http://127.0.0.1:%d/health 2>/dev/null", TEST_PORT)
        )
        if out and out:match('"status":"ok"') then
            ready = true
            break
        end
    end

    if not ready then
        vim.fn.jobstop(_bridge_job)
        _bridge_job = nil
        return false, string.format("bridge did not become healthy within 12s on port %d", TEST_PORT)
    end

    return true, nil
end

--- Stop the test bridge if we started it
local function stop_test_bridge()
    if _bridge_job and _bridge_job > 0 then
        vim.fn.jobstop(_bridge_job)
        _bridge_job = nil
    end
end

-- ── 1. Module loads ─────────────────────────────────────────────────────────
section("Module loads")

local ok_log, log = pcall(require, "mcp_companion.log")
if ok_log then ok("mcp_companion.log loads") else err("mcp_companion.log", log) end

local ok_cfg, cfg = pcall(require, "mcp_companion.config")
if ok_cfg then ok("mcp_companion.config loads") else err("mcp_companion.config", cfg) end

local ok_state, state = pcall(require, "mcp_companion.state")
if ok_state then ok("mcp_companion.state loads") else err("mcp_companion.state", state) end

local ok_tools, tools = pcall(require, "mcp_companion.cc.tools")
if ok_tools then ok("mcp_companion.cc.tools loads") else err("mcp_companion.cc.tools", tools) end

local ok_cc, cc_config = pcall(require, "codecompanion.config")
if ok_cc then ok("codecompanion.config loads") else err("codecompanion.config", cc_config) end

-- ── 2. CC config structure ───────────────────────────────────────────────────
section("CC config structure")

if ok_cc then
    local tools_tbl = cc_config.interactions
        and cc_config.interactions.chat
        and cc_config.interactions.chat.tools
    if tools_tbl then
        ok("config.interactions.chat.tools exists")
    else
        err("config.interactions.chat.tools", "nil — path missing")
    end

    if tools_tbl and type(tools_tbl.groups) == "table" then
        ok("config.interactions.chat.tools.groups is a table")
    elseif tools_tbl then
        ok("config.interactions.chat.tools.groups absent (will be created)")
    end
end

-- ── 3. Start test bridge + connect ──────────────────────────────────────────
section("Bridge startup + connection")

local bridge_dir = find_bridge_dir()
local fixture = nil
if bridge_dir then
    fixture = bridge_dir .. "/tests/fixtures/servers.json"
    ok("bridge_dir: " .. bridge_dir)
else
    err("bridge_dir", "could not locate bridge/ directory")
end

local bridge_started = false
if bridge_dir and fixture and vim.fn.filereadable(fixture) == 1 then
    ok("fixture found: " .. fixture)
    local started, start_err = start_test_bridge(bridge_dir, fixture)
    if started then
        bridge_started = true
        ok(string.format("test bridge started on port %d", TEST_PORT))
    else
        err("start_test_bridge", tostring(start_err))
    end
else
    err("fixture servers.json", "not found at " .. tostring(fixture))
end

if bridge_started and ok_cfg then
    cfg.setup({ bridge = { config = fixture, port = TEST_PORT } })
end

-- All remaining state lives here so Lua scoping is flat
local client = nil
local connected = false
local servers = {}
local tools_tbl = nil
local registered_names = {}

if bridge_started then
    local Client = require("mcp_companion.bridge.client")
    client = Client.new({ host = "127.0.0.1", port = TEST_PORT })

    local connect_done = false
    local connect_err_msg = nil

    client:connect(function(ok_conn, cerr)
        connected = ok_conn
        connect_err_msg = cerr
        connect_done = true
    end)

    vim.wait(5100, function() return connect_done end, 50)

    if not connect_done then
        err("client:connect", "timed out after 5s")
    elseif not connected then
        err("client:connect", tostring(connect_err_msg))
    else
        ok("client connected")
    end
end

-- ── 4. State ─────────────────────────────────────────────────────────────────
section("State after connect")

if connected then
    servers = state.field("servers") or {}
    if #servers == 0 then
        err("state.servers", "empty after connect — client may not have populated state")
    else
        ok(string.format("state.servers populated: %d servers", #servers))
        local total_tools = 0
        for _, srv in ipairs(servers) do
            total_tools = total_tools + #(srv.tools or {})
        end
        ok(string.format("total tools across servers: %d", total_tools))
    end

    -- Wire bridge module so cc/tools.lua can find the client
    local bridge_mod = require("mcp_companion.bridge")
    bridge_mod.client = client
end

-- ── 5. Register tools ────────────────────────────────────────────────────────
section("Tool registration")

if connected and #servers > 0 and ok_tools then
    local reg_ok, reg_err = pcall(tools.register)
    if reg_ok then
        ok("tools.register() completed without error")
    else
        err("tools.register()", reg_err)
    end

    tools_tbl = ok_cc
        and cc_config.interactions
        and cc_config.interactions.chat
        and cc_config.interactions.chat.tools

    local registered_count = 0
    local group_count = 0

    if tools_tbl then
        for key, value in pairs(tools_tbl) do
            if key ~= "groups" and type(value) == "table" and type(value.id) == "string" then
                if value.id:sub(1, #"mcp_companion:") == "mcp_companion:" then
                    registered_count = registered_count + 1
                    table.insert(registered_names, key)
                end
            end
        end
        for _, value in pairs(tools_tbl.groups or {}) do
            if type(value) == "table" and type(value.id) == "string" then
                if value.id:sub(1, #"mcp_companion:") == "mcp_companion:" then
                    group_count = group_count + 1
                end
            end
        end
    end

    if registered_count > 0 then
        ok(string.format("registered %d tool entries in CC config", registered_count))
    else
        err("tool entries", "none registered in config.interactions.chat.tools")
    end

    if group_count > 0 then
        ok(string.format("registered %d tool groups in CC config", group_count))
    else
        err("tool groups", "none registered")
    end
else
    err("tool registration", "skipped (no connection or state)")
end

-- ── 6. Validate tool structure ───────────────────────────────────────────────
section("Tool structure validation")

local sample_key = registered_names[1]
if sample_key and tools_tbl then
    local entry = tools_tbl[sample_key]
    ok("sample tool key: " .. sample_key)

    if type(entry.id) == "string" then ok("entry.id is string") else err("entry.id", type(entry.id)) end
    if type(entry.description) == "string" then ok("entry.description is string") else err("entry.description", type(entry.description)) end
    if type(entry.callback) == "function" then ok("entry.callback is function") else err("entry.callback", type(entry.callback)) end

    local spec_ok, spec = pcall(entry.callback)
    if spec_ok then
        ok("callback() returns without error")
        if type(spec.name) == "string" then ok("spec.name: " .. spec.name) else err("spec.name", type(spec.name)) end
        if type(spec.cmds) == "table" and #spec.cmds > 0 and type(spec.cmds[1]) == "function" then
            ok("spec.cmds[1] is function")
        else
            err("spec.cmds", vim.inspect(spec.cmds))
        end
        if type(spec.output) == "table" and type(spec.output.success) == "function" then
            ok("spec.output.success is function")
        else
            err("spec.output.success", type(spec.output and spec.output.success))
        end
        if type(spec.schema) == "table" and spec.schema["function"] then
            ok("spec.schema is valid CC function schema")
        else
            err("spec.schema", vim.inspect(spec.schema))
        end
    else
        err("callback()", spec)
    end
else
    err("tool structure", "no registered tools to validate")
end

-- ── 7. Simulate a tool call ───────────────────────────────────────────────────
section("Simulated tool call")

if #registered_names > 0 and tools_tbl then
    -- Try echo first, otherwise use the first available tool
    local test_key = nil
    local test_input = {}
    for _, k in ipairs(registered_names) do
        if k:match("echo") then
            test_key = k
            test_input = { message = "hello from test" }
            break
        end
    end
    if not test_key then
        test_key = registered_names[1]
        -- Use empty params for unknown tools
        test_input = {}
    end

    ok("testing tool call with: " .. test_key)
    local entry = tools_tbl[test_key]
    local spec = entry.callback()
    local cmd_fn = spec.cmds[1]

    local call_result = nil
    cmd_fn(
        {},         -- self
        test_input, -- action (tool input from LLM)
        { output_cb = function(res) call_result = res end }
    )

    vim.wait(5000, function() return call_result ~= nil end, 50)

    if call_result then
        ok(string.format("tool call returned (status=%s)", tostring(call_result.status)))
        if call_result.data then
            ok("result.data: " .. tostring(call_result.data):sub(1, 120))
        end
    else
        err("tool call", "timed out waiting for result")
    end
else
    err("tool call", "skipped (no registered tools)")
end

-- ── 8. Cleanup ───────────────────────────────────────────────────────────────
section("Cleanup")

if ok_tools and tools_tbl then
    local unr_ok, unr_err = pcall(tools.unregister)
    if unr_ok then ok("tools.unregister() ok") else err("tools.unregister()", unr_err) end

    local remaining = 0
    for _, value in pairs(tools_tbl) do
        if type(value) == "table" and type(value.id) == "string" then
            if value.id:sub(1, #"mcp_companion:") == "mcp_companion:" then
                remaining = remaining + 1
            end
        end
    end
    if remaining == 0 then
        ok("all mcp_companion tools removed after unregister")
    else
        err("cleanup", remaining .. " tools still present")
    end
end

if client then
    client:disconnect()
    ok("client disconnected")
end

stop_test_bridge()
ok("test bridge stopped")

-- ── Print results ────────────────────────────────────────────────────────────
print_results()
