-- Test the ops facade against a stubbed combiner client.
-- Run: nvim --headless --noplugin -u NONE -c "set rtp+=$PWD" -c "luafile tests/test_ops.lua" -c "qa!"

local pass, fail = 0, 0
local function ok(cond, name)
    if cond then
        pass = pass + 1
        print("  PASS: " .. name)
    else
        fail = fail + 1
        print("  FAIL: " .. name)
    end
end

-- Silence notify; capture messages.
local notified = {}
vim.notify = function(msg, level)
    table.insert(notified, { msg = msg, level = level })
end

-- Stub state with a known server list.
package.loaded["mcp_companion.state"] = {
    field = function(key)
        if key == "servers" then
            return {
                { name = "_combiner" },
                { name = "alpha" },
                { name = "beta" },
            }
        end
        return nil
    end,
}

-- Stub combiner module with a fake connected client.
local calls = {}
local fake_client = {
    connected = true,
    restart_server = function(_, name, cb)
        table.insert(calls, { op = "restart", name = name })
        cb(nil, "restarted " .. name)
    end,
    toggle_server = function(_, name, cb)
        table.insert(calls, { op = "toggle", name = name })
        cb("toggle boom", nil)
    end,
    reload_config = function(_, cb)
        table.insert(calls, { op = "reload" })
        cb(nil, "reloaded")
    end,
}
package.loaded["mcp_companion.combiner"] = { client = fake_client }

local ops = require("mcp_companion.ops")

-- server_names filters the pseudo-server
local names = ops.server_names()
ok(#names == 2 and names[1] == "alpha" and names[2] == "beta", "server_names excludes _combiner")

-- restart_server routes through the client and reports success
local got_err, got_result
ops.restart_server("alpha", function(err, result)
    got_err, got_result = err, result
end, { quiet = true })
ok(calls[#calls].op == "restart" and calls[#calls].name == "alpha", "restart_server calls client")
ok(got_err == nil and got_result == "restarted alpha", "restart_server forwards result")

-- toggle_server surfaces errors
ops.toggle_server("beta", function(err)
    got_err = err
end, { quiet = true })
ok(calls[#calls].op == "toggle" and got_err == "toggle boom", "toggle_server forwards error")

-- reload_config
ops.reload_config(function(err, result)
    got_err, got_result = err, result
end, { quiet = true })
ok(calls[#calls].op == "reload" and got_result == "reloaded", "reload_config calls client")

-- default (non-quiet) path notifies
notified = {}
ops.reload_config()
ok(#notified >= 1, "default path emits vim.notify feedback")

-- disconnected client short-circuits with an error
package.loaded["mcp_companion.combiner"] = { client = { connected = false } }
local disc_err
ops.restart_server("alpha", function(err)
    disc_err = err
end, { quiet = true })
ok(disc_err == "combiner not connected", "disconnected client reports error")

print(string.format("=== Results: %d passed, %d failed ===", pass, fail))
if fail > 0 then
    vim.cmd("cq")
end
