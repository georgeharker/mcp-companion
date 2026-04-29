--- Tests for mcp_companion.cc._resolve_session_allowed
---
--- Verifies the cc/init.lua resolution helper is wired correctly to the
--- project-config layer and reads the cc.auto_*_tools fallback as expected.
--- Stubs out config / state / log / codecompanion deps so cc/init.lua can be
--- loaded without Neovim plugins on the runtimepath.
---
--- Run from the repo root:
---   nvim --headless -u NONE -c "luafile tests/test_cc_resolve_session.lua" -c "q"

local function script_dir()
    local info = debug.getinfo(1, "S")
    local src = info.source:sub(1, 1) == "@" and info.source:sub(2) or info.source
    return vim.fn.fnamemodify(src, ":p:h")
end
local repo_root = vim.fn.fnamemodify(script_dir(), ":h")
package.path = repo_root .. "/lua/?.lua;" .. repo_root .. "/lua/?/init.lua;" .. package.path

-- Stub modules that cc/init.lua pulls in eagerly.
package.loaded["mcp_companion.log"] = {
    debug = function() end, info = function() end,
    warn = function() end, error = function() end,
}

-- Mutable stub fixtures the tests poke at.
local _stub_cc_config = {}
local _stub_servers = {}

package.loaded["mcp_companion.config"] = {
    get = function() return { cc = _stub_cc_config } end,
}
package.loaded["mcp_companion.state"] = {
    field = function(key)
        if key == "servers" then return _stub_servers end
        return nil
    end,
}

-- The helper module reads codecompanion.* lazily inside callbacks; we don't
-- exercise those paths here, so no stub is needed.

-- Ensure project.lua's own log stub doesn't conflict.
package.loaded["mcp_companion.project"] = nil

local cc = require("mcp_companion.cc")

local passed, failed = 0, 0

local function test(name, fn)
    _stub_cc_config = {}
    _stub_servers = {}
    local ok, err = pcall(fn)
    if ok then
        passed = passed + 1
        print(string.format("  PASS: %s", name))
    else
        failed = failed + 1
        print(string.format("  FAIL: %s — %s", name, err))
    end
end

local function assert_eq(a, b, msg)
    if a ~= b then
        error(string.format("%s\n  expected: %s\n  got:      %s",
            msg or "values differ", tostring(b), tostring(a)))
    end
end

local function assert_list_eq(a, b, msg)
    a = a or {}; b = b or {}
    table.sort(a); table.sort(b)
    assert_eq(table.concat(a, ","), table.concat(b, ","), msg)
end

-- chdir to a temp dir for the duration of each test so project.find_root()
-- starts somewhere with no .mcp-companion.json visible.  Otherwise a project
-- file living in the repo root (or above) would taint the cc.auto_*_tools
-- fallback assertions.
local _saved_cwd = vim.fn.getcwd()
local function in_clean_cwd(fn)
    local tmp = vim.fn.tempname()
    vim.fn.mkdir(tmp, "p")
    vim.cmd("cd " .. vim.fn.fnameescape(tmp))
    local ok, err = pcall(fn, tmp)
    vim.cmd("cd " .. vim.fn.fnameescape(_saved_cwd))
    vim.fn.delete(tmp, "rf")
    if not ok then error(err) end
end

print("=== cc._resolve_session_allowed (no project file) ===")

test("auto_http_tools=true → nil (no filter)", function()
    in_clean_cwd(function()
        _stub_cc_config = { auto_http_tools = true }
        local out = cc._resolve_session_allowed("http")
        assert_eq(out, nil)
    end)
end)

test("auto_http_tools=false → empty list", function()
    in_clean_cwd(function()
        _stub_cc_config = { auto_http_tools = false }
        local out = cc._resolve_session_allowed("http")
        assert_list_eq(out, {})
    end)
end)

test("auto_http_tools={\"gws\"} → that list", function()
    in_clean_cwd(function()
        _stub_cc_config = { auto_http_tools = { "gws" } }
        local out = cc._resolve_session_allowed("http")
        assert_list_eq(out, { "gws" })
    end)
end)

test("auto_http_tools missing → nil (treated as default-true)", function()
    in_clean_cwd(function()
        _stub_cc_config = {}
        local out = cc._resolve_session_allowed("http")
        assert_eq(out, nil)
    end)
end)

test("kind='acp' reads auto_acp_tools, not auto_http_tools", function()
    in_clean_cwd(function()
        _stub_cc_config = {
            auto_http_tools = true,
            auto_acp_tools = false,
        }
        assert_eq(cc._resolve_session_allowed("http"), nil)
        assert_list_eq(cc._resolve_session_allowed("acp"), {})

        _stub_cc_config = {
            auto_http_tools = false,
            auto_acp_tools = { "github" },
        }
        assert_list_eq(cc._resolve_session_allowed("http"), {})
        assert_list_eq(cc._resolve_session_allowed("acp"), { "github" })
    end)
end)

print("\n=== cc._resolve_session_allowed (with project file) ===")

local function write_file(path, content)
    local fd = assert(io.open(path, "w"))
    fd:write(content)
    fd:close()
end

test("project allowed_servers wins over auto_http_tools=true", function()
    in_clean_cwd(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["gws"]}')
        _stub_cc_config = { auto_http_tools = true }
        _stub_servers = { { name = "gws" }, { name = "github" } }
        local out = cc._resolve_session_allowed("http")
        assert_list_eq(out, { "gws" })
    end)
end)

test("project file enables servers that auto_http_tools=false would suppress", function()
    -- The "default off, enable per project" workflow.
    in_clean_cwd(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["gws", "github"]}')
        _stub_cc_config = { auto_http_tools = false }
        _stub_servers = {
            { name = "gws" }, { name = "github" }, { name = "clickup" },
        }
        local out = cc._resolve_session_allowed("http")
        assert_list_eq(out, { "gws", "github" })
    end)
end)

test("project disabled_servers inverts against state.servers", function()
    in_clean_cwd(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"disabled_servers": ["clickup"]}')
        _stub_cc_config = { auto_http_tools = true }
        _stub_servers = {
            { name = "gws" }, { name = "github" }, { name = "clickup" },
        }
        local out = cc._resolve_session_allowed("http")
        assert_list_eq(out, { "gws", "github" })
    end)
end)

print(string.format("\n=== %d passed, %d failed ===", passed, failed))
if failed > 0 then os.exit(1) end
