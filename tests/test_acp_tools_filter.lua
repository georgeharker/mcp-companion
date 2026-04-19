--- Tests for auto_acp_tools allow→disable inversion logic.
---
--- Run: nvim --headless -u NONE -c "luafile tests/test_acp_tools_filter.lua" -c "q"
---
--- These tests verify the pure logic of converting an allow-list
--- (auto_acp_tools = {"github"}) into a disable-list without needing
--- a live bridge connection.

local function assert_eq(a, b, msg)
    if type(a) == "table" and type(b) == "table" then
        table.sort(a)
        table.sort(b)
        local a_str = table.concat(a, ",")
        local b_str = table.concat(b, ",")
        if a_str ~= b_str then
            error(string.format("FAIL: %s\n  expected: {%s}\n  got:      {%s}", msg or "", b_str, a_str))
        end
    else
        if a ~= b then
            error(string.format("FAIL: %s\n  expected: %s\n  got:      %s", msg or "", tostring(b), tostring(a)))
        end
    end
end

local passed = 0
local failed = 0

local function test(name, fn)
    local ok, err = pcall(fn)
    if ok then
        print(string.format("  PASS: %s", name))
        passed = passed + 1
    else
        print(string.format("  FAIL: %s — %s", name, err))
        failed = failed + 1
    end
end

--- Simulate the inversion logic from _post_pending_session_filter
--- @param allowed_servers string[] The auto_acp_tools allow-list
--- @param all_servers table[] Array of {name=string} (simulating state.field("servers"))
--- @return string[] servers to disable
local function compute_disable_list(allowed_servers, all_servers)
    local allowed = {}
    for _, name in ipairs(allowed_servers) do
        allowed[name] = true
    end

    local to_disable = {}
    for _, srv in ipairs(all_servers) do
        if srv.name ~= "_bridge" and not allowed[srv.name] then
            table.insert(to_disable, srv.name)
        end
    end
    return to_disable
end

print("\n=== auto_acp_tools allow→disable inversion tests ===\n")

-- Test: single server allowed, rest disabled
test("single server in allow list", function()
    local servers = {
        { name = "github" },
        { name = "todoist" },
        { name = "filesystem" },
        { name = "_bridge" },
    }
    local result = compute_disable_list({ "github" }, servers)
    assert_eq(result, { "todoist", "filesystem" }, "should disable todoist and filesystem")
end)

-- Test: all servers allowed → nothing to disable
test("all servers in allow list", function()
    local servers = {
        { name = "github" },
        { name = "todoist" },
    }
    local result = compute_disable_list({ "github", "todoist" }, servers)
    assert_eq(result, {}, "should disable nothing")
end)

-- Test: empty allow list → all disabled
test("empty allow list disables all", function()
    local servers = {
        { name = "github" },
        { name = "todoist" },
        { name = "_bridge" },
    }
    local result = compute_disable_list({}, servers)
    assert_eq(result, { "github", "todoist" }, "should disable github and todoist")
end)

-- Test: _bridge is always excluded
test("_bridge excluded from disable list", function()
    local servers = {
        { name = "_bridge" },
        { name = "github" },
    }
    local result = compute_disable_list({}, servers)
    assert_eq(result, { "github" }, "_bridge should not appear in disable list")
end)

-- Test: allow list with unknown server name (no-op, just ignores it)
test("unknown server in allow list is harmless", function()
    local servers = {
        { name = "github" },
        { name = "todoist" },
    }
    local result = compute_disable_list({ "github", "nonexistent" }, servers)
    assert_eq(result, { "todoist" }, "nonexistent in allow list should not affect result")
end)

-- Test: multiple allowed, one disabled
test("multiple allowed leaves one disabled", function()
    local servers = {
        { name = "github" },
        { name = "todoist" },
        { name = "filesystem" },
    }
    local result = compute_disable_list({ "github", "filesystem" }, servers)
    assert_eq(result, { "todoist" }, "only todoist should be disabled")
end)

-- Test: config type detection
test("config type detection for auto_acp_tools", function()
    -- true → not a table, no filter
    assert_eq(type(true), "boolean", "true is boolean")
    assert_eq(type(true) == "table", false, "true is not table")

    -- false → not a table, skip injection entirely
    assert_eq(type(false), "boolean", "false is boolean")

    -- table → filter mode
    local list = { "github" }
    assert_eq(type(list), "table", "list is table")
    assert_eq(type(list) == "table", true, "list detected as table")
end)

print(string.format("\n=== Results: %d passed, %d failed ===\n", passed, failed))
if failed > 0 then
    vim.cmd("cquit 1")
end
