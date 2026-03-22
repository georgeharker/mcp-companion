-- Test: add User-Agent and Connection headers like curl
local uv = vim.uv

local tcp1 = assert(uv.new_tcp())
local session_id = nil

tcp1:connect('127.0.0.1', 9741, function(err)
  if err then print('err: ' .. err); vim.cmd('qa!'); return end

  local buf1 = ''
  tcp1:read_start(function(_, data)
    if not data then
      tcp1:read_stop()
      if not tcp1:is_closing() then tcp1:close() end
      vim.schedule(function()
        print('\n--- tools/list with extra headers ---')
        local tcp2 = assert(uv.new_tcp())
        tcp2:connect('127.0.0.1', 9741, function(err2)
          if err2 then print('err2: ' .. err2); vim.cmd('qa!'); return end

          local buf2 = ''
          tcp2:read_start(function(_, data2)
            if not data2 then
              print('EOF, buf2=' .. #buf2)
              print(buf2:sub(1, 600))
              vim.schedule(function() vim.cmd('qa!') end)
              return
            end
            buf2 = buf2 .. data2
            print('chunk: ' .. #data2 .. ' bytes, total=' .. #buf2)
          end)

          local payload = vim.json.encode({
            jsonrpc = '2.0', id = 2, method = 'tools/list',
            params = vim.empty_dict(),
          })

          -- Match curl's exact headers
          local req = 'POST /mcp HTTP/1.1\r\n'
            .. 'Host: 127.0.0.1:9741\r\n'
            .. 'User-Agent: curl/8.7.1\r\n'
            .. 'Content-Type: application/json\r\n'
            .. 'Accept: application/json, text/event-stream\r\n'
            .. 'Mcp-Session-Id: ' .. session_id .. '\r\n'
            .. 'Content-Length: ' .. #payload .. '\r\n'
            .. '\r\n' .. payload

          tcp2:write(req)
        end)
      end)
      return
    end
    buf1 = buf1 .. data
    local sid = buf1:match('mcp%-session%-id:%s*(%S+)')
    if sid then session_id = sid end
  end)

  local init_payload = vim.json.encode({
    jsonrpc = '2.0', id = 1, method = 'initialize',
    params = {
      protocolVersion = '2025-03-26',
      capabilities = { roots = { listChanged = false } },
      clientInfo = { name = 'test', version = '0.1.0' },
    }
  })

  tcp1:write('POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:9741\r\nUser-Agent: curl/8.7.1\r\nContent-Type: application/json\r\nAccept: application/json, text/event-stream\r\nContent-Length: ' .. #init_payload .. '\r\n\r\n' .. init_payload)
end)

vim.defer_fn(function() print('TIMEOUT'); vim.cmd('qa!') end, 20000)
