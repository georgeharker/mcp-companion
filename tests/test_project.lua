--- Tests for mcp_companion.project — project-directory config discovery + resolution.
---
--- Run from the repo root:
---   nvim --headless -u NONE -c "luafile tests/test_project.lua" -c "q"

-- Make the plugin module discoverable: cwd-relative when run from repo root,
-- otherwise resolve relative to this file.
local function script_dir()
    local info = debug.getinfo(1, "S")
    local src = info.source:sub(1, 1) == "@" and info.source:sub(2) or info.source
    return vim.fn.fnamemodify(src, ":p:h")
end
local repo_root = vim.fn.fnamemodify(script_dir(), ":h")
package.path = repo_root .. "/lua/?.lua;" .. repo_root .. "/lua/?/init.lua;" .. package.path

-- Stub the log module so warnings emitted by project.lua don't pollute test
-- output and tests can assert on them when needed.
local _captured_warns = {}
package.loaded["mcp_companion.log"] = {
    debug = function() end,
    info = function() end,
    warn = function(fmt, ...) table.insert(_captured_warns, string.format(fmt, ...)) end,
    error = function() end,
}

local project = require("mcp_companion.project")

local passed, failed = 0, 0

local function test(name, fn)
    _captured_warns = {}
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
    a = a or {}
    b = b or {}
    table.sort(a); table.sort(b)
    assert_eq(table.concat(a, ","), table.concat(b, ","), msg)
end

local function with_tempdir(fn)
    local tmp = vim.fn.tempname()
    vim.fn.mkdir(tmp, "p")
    local ok, err = pcall(fn, tmp)
    vim.fn.delete(tmp, "rf")
    if not ok then error(err) end
end

local function write_file(path, content)
    vim.fn.mkdir(vim.fn.fnamemodify(path, ":h"), "p")
    local fd = assert(io.open(path, "w"))
    fd:write(content)
    fd:close()
end

print("=== mcp_companion.project ===")

test("find_root: returns nil when no project file is in scope", function()
    with_tempdir(function(tmp)
        local root, file = project.find_root(tmp)
        assert_eq(root, nil, "root should be nil")
        assert_eq(file, nil, "file should be nil")
    end)
end)

test("find_root: finds project file in start dir", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json", '{"allowed_servers": []}')
        local root, file = project.find_root(tmp)
        assert_eq(vim.fn.resolve(root), vim.fn.resolve(tmp), "root mismatch")
        assert_eq(file ~= nil and file:sub(-#".mcp-companion.json") == ".mcp-companion.json",
            true, "file path should end with .mcp-companion.json")
    end)
end)

test("find_root: walks up to parent directory", function()
    with_tempdir(function(tmp)
        local sub = tmp .. "/a/b/c"
        vim.fn.mkdir(sub, "p")
        write_file(tmp .. "/.mcp-companion.json", "{}")
        local root = project.find_root(sub)
        assert_eq(vim.fn.resolve(root), vim.fn.resolve(tmp),
            "should walk up to ancestor with the file")
    end)
end)

test("resolve: parses allowed_servers list", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["github", "gws"]}')
        local cfg = project.resolve(tmp)
        assert_eq(cfg ~= nil, true, "config should not be nil")
        assert_list_eq(cfg.allowed_servers, {"github", "gws"})
        assert_eq(cfg.disabled_servers, nil)
    end)
end)

test("resolve: parses disabled_servers list", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"disabled_servers": ["clickup"]}')
        local cfg = project.resolve(tmp)
        assert_eq(cfg ~= nil, true)
        assert_list_eq(cfg.disabled_servers, {"clickup"})
        assert_eq(cfg.allowed_servers, nil)
    end)
end)

test("resolve: rejects mutually-exclusive fields, returns nil + warns", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["a"], "disabled_servers": ["b"]}')
        local cfg = project.resolve(tmp)
        assert_eq(cfg, nil, "should reject conflicting fields")
        assert_eq(#_captured_warns >= 1, true, "should warn")
    end)
end)

test("resolve: rejects non-string entries, returns nil + warns", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["github", 42]}')
        local cfg = project.resolve(tmp)
        assert_eq(cfg, nil, "should reject non-string entries")
        assert_eq(#_captured_warns >= 1, true)
    end)
end)

test("resolve: returns nil + warns on invalid JSON", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json", "not json {")
        local cfg = project.resolve(tmp)
        assert_eq(cfg, nil)
        assert_eq(#_captured_warns >= 1, true)
    end)
end)

test("resolve: returns nil with no project file in scope", function()
    with_tempdir(function(tmp)
        local cfg, root = project.resolve(tmp)
        assert_eq(cfg, nil)
        assert_eq(root, nil)
    end)
end)

test("resolve_allowed: project allowed_servers wins over auto=true", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["github"]}')
        local out = project.resolve_allowed(true, {"github", "gws", "clickup"}, tmp)
        assert_list_eq(out, {"github"})
    end)
end)

test("resolve_allowed: project disabled_servers inverted against known", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"disabled_servers": ["clickup"]}')
        local out = project.resolve_allowed(true, {"github", "gws", "clickup"}, tmp)
        assert_list_eq(out, {"github", "gws"})
    end)
end)

test("resolve_allowed: drops unknown server names with a warning", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["github", "ghost"]}')
        local out = project.resolve_allowed(true, {"github", "gws"}, tmp)
        assert_list_eq(out, {"github"})
        assert_eq(#_captured_warns >= 1, true, "should warn about ghost")
    end)
end)

test("resolve_allowed: no project file → falls back to auto=false (empty list)", function()
    with_tempdir(function(tmp)
        local out = project.resolve_allowed(false, {"github", "gws"}, tmp)
        assert_list_eq(out, {})
    end)
end)

test("resolve_allowed: no project file → falls back to auto=string[]", function()
    with_tempdir(function(tmp)
        local out = project.resolve_allowed({"gws"}, {"github", "gws"}, tmp)
        assert_list_eq(out, {"gws"})
    end)
end)

test("resolve_allowed: no project file, auto=true → nil (no filter)", function()
    with_tempdir(function(tmp)
        local out = project.resolve_allowed(true, {"github", "gws"}, tmp)
        assert_eq(out, nil)
    end)
end)

test("resolve_allowed: project disabled_servers + no known list → nil + warns", function()
    -- We can't invert disabled→allowed without the bridge's known-server list.
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"disabled_servers": ["clickup"]}')
        local out = project.resolve_allowed(true, nil, tmp)
        assert_eq(out, nil)
        assert_eq(#_captured_warns >= 1, true)
    end)
end)

test("resolve_allowed: malformed project file → falls back to auto_value", function()
    -- A garbage .mcp-companion.json must not "lock in" an empty allow-list;
    -- it should warn and behave as if no project file were present so the
    -- global cc.auto_*_tools default still applies.
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json", "garbage{")
        local out = project.resolve_allowed({"gws"}, {"github", "gws"}, tmp)
        assert_list_eq(out, {"gws"})
        assert_eq(#_captured_warns >= 1, true, "should warn about parse failure")
    end)
end)

test("resolve_allowed: malformed project file + auto=false → empty list", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json", "garbage{")
        local out = project.resolve_allowed(false, {"github", "gws"}, tmp)
        assert_list_eq(out, {})
    end)
end)

test("resolve_allowed: malformed project file + auto=true → nil (no filter)", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json", "garbage{")
        local out = project.resolve_allowed(true, {"github", "gws"}, tmp)
        assert_eq(out, nil)
    end)
end)

test("resolve_allowed: empty allowed_servers in project file → empty list", function()
    -- An explicit empty allow-list means "expose nothing" for this project,
    -- not "fall back to global default".
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json", '{"allowed_servers": []}')
        local out = project.resolve_allowed(true, {"github", "gws"}, tmp)
        assert_list_eq(out, {})
    end)
end)

print("\n=== format_payload ===")

test("format_payload shortest: more disabled → allowed wins (shorter)", function()
    local p = project.format_payload(
        {"a", "b", "c"},
        {"a", "b", "c", "d"},
        "shortest"
    )
    assert_list_eq(p.allowed_servers, {"d"})
    assert_eq(p.disabled_servers, nil)
end)

test("format_payload shortest: more allowed → disabled wins (shorter)", function()
    local p = project.format_payload(
        {"a"},
        {"a", "b", "c", "d"},
        "shortest"
    )
    assert_list_eq(p.disabled_servers, {"a"})
    assert_eq(p.allowed_servers, nil)
end)

test("format_payload shortest: tie → allowed wins (matches doc workflow)", function()
    local p = project.format_payload({"a", "b"}, {"a", "b", "c", "d"}, "shortest")
    assert_list_eq(p.allowed_servers, {"c", "d"})
end)

test("format_payload allowed: forces allow-list even when longer", function()
    local p = project.format_payload(
        {"a"},
        {"a", "b", "c", "d"},
        "allowed"
    )
    assert_list_eq(p.allowed_servers, {"b", "c", "d"})
end)

test("format_payload disabled: forces disable-list even when longer", function()
    local p = project.format_payload(
        {"a", "b", "c"},
        {"a", "b", "c", "d"},
        "disabled"
    )
    assert_list_eq(p.disabled_servers, {"a", "b", "c"})
end)

test("format_payload accepts a set instead of a list", function()
    local p = project.format_payload(
        { a = true, b = true, c = false },
        {"a", "b", "c"},
        "disabled"
    )
    assert_list_eq(p.disabled_servers, {"a", "b"})
end)

test("format_payload all enabled → empty disabled list (shortest)", function()
    local p = project.format_payload({}, {"a", "b", "c"}, "shortest")
    -- 0 disabled vs 3 allowed → disabled wins, empty list
    assert_eq(p.disabled_servers ~= nil, true)
    assert_eq(#p.disabled_servers, 0)
end)

test("format_payload all disabled → empty allowed list (shortest)", function()
    local p = project.format_payload({"a", "b", "c"}, {"a", "b", "c"}, "shortest")
    assert_eq(p.allowed_servers ~= nil, true)
    assert_eq(#p.allowed_servers, 0)
end)

print("\n=== encode ===")

test("encode renders pretty JSON with $schema header", function()
    local json = project.encode({ allowed_servers = {"github", "gws"} })
    -- Field order matters for human readability — schema first
    local schema_idx = json:find('"$schema"', 1, true)
    local field_idx = json:find('"allowed_servers"', 1, true)
    assert_eq(schema_idx ~= nil and field_idx ~= nil and schema_idx < field_idx, true,
        "schema should appear before allowed_servers")
    -- Round-trip parses back to the same payload
    local parsed = vim.json.decode(json)
    assert_list_eq(parsed.allowed_servers, {"github", "gws"})
end)

test("encode handles empty arrays", function()
    local json = project.encode({ disabled_servers = {} })
    local parsed = vim.json.decode(json)
    assert_eq(parsed.disabled_servers ~= nil, true)
    assert_eq(#parsed.disabled_servers, 0)
end)

print("\n=== save / save_target_path ===")

test("save_target_path returns existing file when one is in scope", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json", "{}")
        local path = project.save_target_path(tmp)
        assert_eq(vim.fn.resolve(path), vim.fn.resolve(tmp .. "/.mcp-companion.json"))
    end)
end)

test("save_target_path falls back to start_dir/.mcp-companion.json", function()
    with_tempdir(function(tmp)
        local path = project.save_target_path(tmp)
        assert_eq(path, tmp .. "/.mcp-companion.json")
    end)
end)

test("save: writes new file when none exists", function()
    with_tempdir(function(tmp)
        local target = tmp .. "/.mcp-companion.json"
        local result = project.save({
            disabled = {"clickup"},
            known_servers = {"github", "gws", "clickup"},
            format = "shortest",
            path = target,
        })
        assert_eq(result.action, "wrote")
        assert_eq(result.path, target)
        local f = io.open(target, "r")
        assert_eq(f ~= nil, true, "file should exist")
        local body = f:read("*a"); f:close()
        local parsed = vim.json.decode(body)
        assert_list_eq(parsed.disabled_servers, {"clickup"})
    end)
end)

test("save: returns 'unchanged' when contents already match byte-for-byte", function()
    with_tempdir(function(tmp)
        local target = tmp .. "/.mcp-companion.json"
        local opts = {
            disabled = {"clickup"},
            known_servers = {"github", "gws", "clickup"},
            format = "shortest",
            path = target,
        }
        project.save(opts)        -- first write
        local r2 = project.save(opts)  -- second is no-op
        assert_eq(r2.action, "unchanged")
    end)
end)

test("save: returns 'would_overwrite' on differing existing file (no force)", function()
    with_tempdir(function(tmp)
        local target = tmp .. "/.mcp-companion.json"
        write_file(target, "{}\n")  -- existing, different
        local result = project.save({
            disabled = {"clickup"},
            known_servers = {"github", "gws", "clickup"},
            path = target,
        })
        assert_eq(result.action, "would_overwrite")
        assert_eq(result.existing, "{}\n")
        -- File should NOT have been modified
        local f = io.open(target, "r")
        local body = f:read("*a"); f:close()
        assert_eq(body, "{}\n")
    end)
end)

test("save: force=true overwrites differing existing file", function()
    with_tempdir(function(tmp)
        local target = tmp .. "/.mcp-companion.json"
        write_file(target, "{}\n")
        local result = project.save({
            disabled = {"clickup"},
            known_servers = {"github", "gws", "clickup"},
            path = target,
            force = true,
        })
        assert_eq(result.action, "wrote")
        local f = io.open(target, "r")
        local body = f:read("*a"); f:close()
        local parsed = vim.json.decode(body)
        assert_list_eq(parsed.disabled_servers, {"clickup"})
    end)
end)

test("save: discovers existing path when opts.path omitted", function()
    -- save() with no explicit path should land on the file that resolve()
    -- would read, completing the read↔write round-trip.
    with_tempdir(function(tmp)
        local original_cwd = vim.fn.getcwd()
        vim.cmd("cd " .. vim.fn.fnameescape(tmp))
        local ok, err = pcall(function()
            local sub = tmp .. "/a/b"
            vim.fn.mkdir(sub, "p")
            write_file(tmp .. "/.mcp-companion.json",
                '{"allowed_servers": ["old"]}')
            vim.cmd("cd " .. vim.fn.fnameescape(sub))
            local result = project.save({
                disabled = {},
                known_servers = {"github"},
                format = "allowed",
                force = true,
            })
            assert_eq(vim.fn.resolve(result.path),
                vim.fn.resolve(tmp .. "/.mcp-companion.json"),
                "should write to ancestor's project file, not the cwd")
        end)
        vim.cmd("cd " .. vim.fn.fnameescape(original_cwd))
        if not ok then error(err) end
    end)
end)

test("save → resolve round-trip yields the same allowed list", function()
    with_tempdir(function(tmp)
        local target = tmp .. "/.mcp-companion.json"
        project.save({
            disabled = {"clickup", "todoist"},
            known_servers = {"github", "gws", "clickup", "todoist"},
            format = "allowed",
            path = target,
        })
        local cfg = project.resolve(tmp)
        assert_eq(cfg ~= nil, true)
        assert_list_eq(cfg.allowed_servers, {"github", "gws"})
    end)
end)

print("\n=== project_disabled_set ===")

test("project_disabled_set: nil cfg → empty (default = all visible)", function()
    local d = project.project_disabled_set(nil, {"a", "b"})
    assert_eq(next(d), nil)
end)

test("project_disabled_set: disabled_servers form is direct", function()
    local d = project.project_disabled_set(
        { disabled_servers = {"x"} }, {"a", "b", "x"})
    assert_eq(d.x, true)
    assert_eq(d.a, nil)
end)

test("project_disabled_set: allowed_servers inverted against known", function()
    local d = project.project_disabled_set(
        { allowed_servers = {"a"} }, {"a", "b", "c"})
    assert_eq(d.a, nil)
    assert_eq(d.b, true)
    assert_eq(d.c, true)
end)

print("\n=== toggle_in_project_file ===")

test("toggle: no project file → creates one with the server hidden", function()
    with_tempdir(function(tmp)
        local original_cwd = vim.fn.getcwd()
        vim.cmd("cd " .. vim.fn.fnameescape(tmp))
        local ok, err = pcall(function()
            local result = project.toggle_in_project_file(
                "clickup", {"github", "gws", "clickup"})
            assert_eq(result.was_visible, true)
            assert_eq(result.now_visible, false)
            assert_eq(result.action, "wrote")
            local cfg = project.resolve(tmp)
            -- shortest: 1 disabled vs 2 allowed → disabled wins
            assert_list_eq(cfg.disabled_servers, {"clickup"})
        end)
        vim.cmd("cd " .. vim.fn.fnameescape(original_cwd))
        if not ok then error(err) end
    end)
end)

test("toggle: existing allowed_servers shape is preserved", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["github"]}')
        local original_cwd = vim.fn.getcwd()
        vim.cmd("cd " .. vim.fn.fnameescape(tmp))
        local ok, err = pcall(function()
            -- Toggle gws ON: it was hidden (not in allowed), should become visible.
            local result = project.toggle_in_project_file(
                "gws", {"github", "gws", "clickup"})
            assert_eq(result.was_visible, false)
            assert_eq(result.now_visible, true)
            local cfg = project.resolve(tmp)
            -- Shape preserved as allowed
            assert_list_eq(cfg.allowed_servers, {"github", "gws"})
            assert_eq(cfg.disabled_servers, nil)
        end)
        vim.cmd("cd " .. vim.fn.fnameescape(original_cwd))
        if not ok then error(err) end
    end)
end)

test("toggle: existing disabled_servers shape is preserved", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"disabled_servers": ["clickup"]}')
        local original_cwd = vim.fn.getcwd()
        vim.cmd("cd " .. vim.fn.fnameescape(tmp))
        local ok, err = pcall(function()
            -- Toggle github OFF: was visible, becomes hidden.
            local result = project.toggle_in_project_file(
                "github", {"github", "gws", "clickup"})
            assert_eq(result.was_visible, true)
            assert_eq(result.now_visible, false)
            local cfg = project.resolve(tmp)
            assert_list_eq(cfg.disabled_servers, {"clickup", "github"})
            assert_eq(cfg.allowed_servers, nil)
        end)
        vim.cmd("cd " .. vim.fn.fnameescape(original_cwd))
        if not ok then error(err) end
    end)
end)

test("validate: tool_system_prompts must be boolean", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"tool_system_prompts": "yes"}')
        local cfg = project.resolve(tmp)
        assert_eq(cfg, nil, "non-boolean tool_system_prompts should be rejected")
        assert_eq(#_captured_warns >= 1, true)
    end)
end)

test("resolve: parses tool_system_prompts=false", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"tool_system_prompts": false}')
        local cfg = project.resolve(tmp)
        assert_eq(cfg ~= nil, true)
        assert_eq(cfg.tool_system_prompts, false)
    end)
end)

test("resolve: tool_system_prompts coexists with allowed_servers", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["github"], "tool_system_prompts": true}')
        local cfg = project.resolve(tmp)
        assert_eq(cfg ~= nil, true)
        assert_list_eq(cfg.allowed_servers, {"github"})
        assert_eq(cfg.tool_system_prompts, true)
    end)
end)

print("\n=== resolve_tool_system_prompts ===")

test("resolve_tool_system_prompts: project file wins over global", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"tool_system_prompts": false}')
        assert_eq(project.resolve_tool_system_prompts(true, tmp), false)
        assert_eq(project.resolve_tool_system_prompts(nil, tmp), false)
    end)
end)

test("resolve_tool_system_prompts: project sets true overrides global=false", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"tool_system_prompts": true}')
        assert_eq(project.resolve_tool_system_prompts(false, tmp), true)
    end)
end)

test("resolve_tool_system_prompts: no project field → uses global value", function()
    with_tempdir(function(tmp)
        write_file(tmp .. "/.mcp-companion.json",
            '{"allowed_servers": ["github"]}')
        assert_eq(project.resolve_tool_system_prompts(false, tmp), false)
        assert_eq(project.resolve_tool_system_prompts(true, tmp), true)
    end)
end)

test("resolve_tool_system_prompts: no project file → uses global value", function()
    with_tempdir(function(tmp)
        assert_eq(project.resolve_tool_system_prompts(false, tmp), false)
        assert_eq(project.resolve_tool_system_prompts(true, tmp), true)
    end)
end)

test("resolve_tool_system_prompts: nil global, no project → defaults to true", function()
    with_tempdir(function(tmp)
        assert_eq(project.resolve_tool_system_prompts(nil, tmp), true)
    end)
end)

test("toggle: re-toggling restores the original visibility", function()
    with_tempdir(function(tmp)
        local original_cwd = vim.fn.getcwd()
        vim.cmd("cd " .. vim.fn.fnameescape(tmp))
        local ok, err = pcall(function()
            local known = {"github", "gws", "clickup"}
            project.toggle_in_project_file("clickup", known)  -- hide
            local r2 = project.toggle_in_project_file("clickup", known)  -- restore
            assert_eq(r2.was_visible, false)
            assert_eq(r2.now_visible, true)
            local cfg = project.resolve(tmp)
            local d = project.project_disabled_set(cfg, known)
            assert_eq(d.clickup, nil, "clickup should be visible again")
        end)
        vim.cmd("cd " .. vim.fn.fnameescape(original_cwd))
        if not ok then error(err) end
    end)
end)

print(string.format("\n=== %d passed, %d failed ===", passed, failed))
if failed > 0 then
    os.exit(1)
end
