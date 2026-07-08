--- test_schema.lua — Validate mcp_companion.schema.normalize.
---
--- Run headless from the plugin root:
---   nvim --headless --noplugin -u NONE \
---     -c "set rtp+=$PWD" -c "luafile tests/test_schema.lua"
---
--- Guards issue #7's source fix: an empty Lua table encodes to JSON `[]`, which
--- strict adapters reject where an object is required. normalize() must pin empty
--- object positions to `{}` (via vim.empty_dict), recurse, and repair `required`.

local pass, fail, results = 0, 0, {}

local function ok(name)
    pass = pass + 1
    table.insert(results, "  PASS  " .. name)
end

local function err(name, msg)
    fail = fail + 1
    table.insert(results, "  FAIL  " .. name .. ": " .. tostring(msg))
end

local function section(t)
    table.insert(results, "\n--- " .. t .. " ---")
end

--- Assert a normalized schema, once encoded to JSON, contains no `[]` (which is
--- what an empty Lua table would wrongly become).
local function has_no_array(schema)
    return not vim.json.encode(schema):find("%[%]")
end

section("Module load")
local ok_mod, schema = pcall(require, "mcp_companion.schema")
if ok_mod then
    ok("schema module loads")
else
    err("schema module", schema)
end

if ok_mod then
    section("object-shape + empty encoding")

    -- nil / empty → object with empty (object-encoded) properties
    for _, case in ipairs({ { name = "nil", v = nil }, { name = "empty table", v = {} } }) do
        local out = schema.normalize(case.v)
        if out.type == "object" and has_no_array(out) then
            ok("normalize(" .. case.name .. ") → object, no []")
        else
            err("normalize(" .. case.name .. ")", vim.json.encode(out))
        end
    end

    -- empty properties must encode as {} not []
    do
        local out = schema.normalize({ type = "object", properties = {} })
        if vim.json.encode(out):find('"properties":%{%}') then
            ok("empty properties encodes as {}")
        else
            err("empty properties", vim.json.encode(out))
        end
    end

    -- nested empty object property is preserved recursively
    do
        local out = schema.normalize({
            type = "object",
            properties = { opts = { type = "object", properties = {} } },
        })
        if has_no_array(out) and out.properties.opts.type == "object" then
            ok("nested empty object preserved (recursive)")
        else
            err("nested empty object", vim.json.encode(out))
        end
    end

    section("required repair")

    -- bare string required → single-element array
    do
        local out = schema.normalize({ type = "object", properties = {}, required = "buffer" })
        if type(out.required) == "table" and out.required[1] == "buffer" and #out.required == 1 then
            ok("string required → array")
        else
            err("string required", vim.json.encode(out))
        end
    end

    -- non-string, non-list required → dropped
    do
        local out = schema.normalize({ type = "object", properties = {}, required = 5 })
        if out.required == nil then
            ok("numeric required → dropped")
        else
            err("numeric required", vim.json.encode(out))
        end
    end

    -- valid list required → preserved
    do
        local out = schema.normalize({
            type = "object",
            properties = { x = { type = "string" } },
            required = { "x" },
        })
        if out.required[1] == "x" and #out.required == 1 then
            ok("list required preserved")
        else
            err("list required", vim.json.encode(out))
        end
    end
end

table.insert(results, string.format("\n=== RESULTS: %d passed, %d failed ===", pass, fail))
for _, line in ipairs(results) do
    print(line)
end

if fail > 0 then
    vim.cmd("cq")
else
    vim.cmd("qa!")
end
