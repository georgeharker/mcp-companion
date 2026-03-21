-- Multi-session: 5s delay between sessions
local Client = require("mcp_companion.bridge.client")

local function test_session(label, on_done)
  print(("=== %s ==="):format(label))
  local client = Client.new({ host = "127.0.0.1", port = 9741, request_timeout = 15, poll_interval = 0 })
  client:connect(function(ok, err)
    if not ok then
      print(("%s CONNECT FAIL: %s"):format(label, tostring(err)))
      on_done(false)
      return
    end
    print(("%s: tools=%d"):format(label, #client.tools))
    client:call_tool("everything_echo", { message = "from " .. label }, function(terr, result)
      local text = (not terr and result) and result.content[1].text or tostring(terr)
      print(("%s ECHO: %s"):format(label, text))
      client:disconnect()
      on_done(not terr)
    end)
  end)
end

local done = false
test_session("S1", function(s1)
  print("S1: " .. (s1 and "PASS" or "FAIL"))
  print("Waiting 5 seconds...")
  vim.defer_fn(function()
    test_session("S2", function(s2)
      print("S2: " .. (s2 and "PASS" or "FAIL"))
      done = true
    end)
  end, 5000)
end)

vim.wait(60000, function() return done end, 50)
if not done then print("TIMEOUT") end
vim.cmd("qa!")
