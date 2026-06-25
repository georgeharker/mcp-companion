--- test_native.lua — Validate the native `neovim` server dispatcher in-editor.
---
--- Run headless from the plugin root:
---   nvim --headless --noplugin -u NONE \
---     -c "set rtp+=$PWD" -c "luafile tests/test_native.lua"
---
--- No combiner and no CodeCompanion required — this exercises M.dispatch directly.

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

--- Extract the text payload of an MCP result.
local function rtext(r)
  return r and r.content and r.content[1] and r.content[1].text or ""
end

--- Decode a JSON tool result.
local function rjson(r)
  local ok_d, v = pcall(vim.json.decode, rtext(r))
  return ok_d and v or nil
end

-- ── Load + setup ─────────────────────────────────────────────────────────────
section("Module load + setup")

local ok_native, native = pcall(require, "mcp_companion.native")
if ok_native then ok("native module loads") else err("native module", native) end

if ok_native then
  local ok_s = pcall(native.setup, { native_servers = { neovim = { enabled = true } } })
  if ok_s then ok("native.setup() ran") else err("native.setup()", "errored") end

  if native.is_native_server("neovim") then
    ok("neovim is registered as a native server")
  else
    err("is_native_server", "neovim not registered")
  end
end

-- ── Prepare a scratch file buffer ────────────────────────────────────────────
section("Buffer fixture")

local tmp = vim.fn.tempname() .. ".txt"
local buf = vim.api.nvim_create_buf(true, false)
vim.api.nvim_buf_set_name(buf, tmp)
vim.api.nvim_buf_set_lines(buf, 0, -1, false, { "alpha", "beta", "gamma", "delta" })
vim.api.nvim_set_current_buf(buf)
ok("scratch buffer created: " .. tmp)

-- ── read tools ───────────────────────────────────────────────────────────────
section("read tier")

if ok_native then
  local r = native.dispatch("read_buffer", { buffer = buf })
  if rtext(r):match("1\talpha") and rtext(r):match("4\tdelta") then
    ok("read_buffer returns numbered lines")
  else
    err("read_buffer", rtext(r))
  end

  local lb = rjson(native.dispatch("list_buffers", {}))
  if lb and lb.buffers and #lb.buffers >= 1 then
    ok("list_buffers returns buffers")
  else
    err("list_buffers", vim.inspect(lb))
  end

  -- namespaced name should resolve too
  local rn = native.dispatch("neovim_read_buffer", { buffer = buf })
  if rtext(rn):match("alpha") then ok("namespaced name resolves") else err("namespaced", rtext(rn)) end

  local unknown = native.dispatch("does_not_exist", {})
  if unknown.isError then ok("unknown tool returns isError") else err("unknown tool", "no error") end
end

-- ── write tier ───────────────────────────────────────────────────────────────
section("write tier")

if ok_native then
  local sr = native.dispatch("set_buffer_lines", { buffer = buf, start = 2, ["end"] = 2, lines = { "BETA" } })
  local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
  if not sr.isError and lines[2] == "BETA" then
    ok("set_buffer_lines replaced line 2")
  else
    err("set_buffer_lines", vim.inspect(lines))
  end

  local diff = "<<<<<<< SEARCH\ngamma\n=======\nGAMMA\n>>>>>>> REPLACE"
  local er = rjson(native.dispatch("edit_buffer", { buffer = buf, diff = diff }))
  local l3 = vim.api.nvim_buf_get_lines(buf, 2, 3, false)[1]
  if er and er.applied == 1 and l3 == "GAMMA" then
    ok("edit_buffer applied SEARCH/REPLACE block")
  else
    err("edit_buffer", vim.inspect(er) .. " line3=" .. tostring(l3))
  end

  local miss = rjson(native.dispatch("edit_buffer", { buffer = buf, diff =
    "<<<<<<< SEARCH\nNOPE\n=======\nX\n>>>>>>> REPLACE" }))
  if miss and miss.applied == 0 and #miss.unmatched == 1 then
    ok("edit_buffer reports unmatched block")
  else
    err("edit_buffer unmatched", vim.inspect(miss))
  end
end

-- ── filesystem tools ─────────────────────────────────────────────────────────
section("filesystem")

if ok_native then
  local wf = native.dispatch("write_file", { path = tmp .. ".out", content = "hello\nworld" })
  if not wf.isError then ok("write_file wrote a new file") else err("write_file", rtext(wf)) end

  local rf = native.dispatch("read_file", { path = tmp .. ".out", start_line = 2, end_line = 2 })
  if rtext(rf) == "world" then ok("read_file honours line range") else err("read_file", rtext(rf)) end

  local del = rjson(native.dispatch("delete_items", { paths = { tmp .. ".out" } }))
  if del and #del.deleted == 1 then ok("delete_items removed the file") else err("delete_items", vim.inspect(del)) end
end

-- ── diagnostics ──────────────────────────────────────────────────────────────
section("diagnostics")

if ok_native then
  local gd = rjson(native.dispatch("get_diagnostics", { scope = "buffer", buffer = buf }))
  if gd and gd.scope == "buffer" and type(gd.diagnostics) == "table" then
    ok("get_diagnostics returns structured buffer diagnostics")
  else
    err("get_diagnostics", vim.inspect(gd))
  end
end

-- ── resources ────────────────────────────────────────────────────────────────
section("resources")

if ok_native then
  local res = rjson(native.read_resource("neovim://workspace"))
  if res and res.cwd then ok("neovim://workspace resource") else err("workspace resource", vim.inspect(res)) end
end

-- ── state publication ────────────────────────────────────────────────────────
section("state")

do
  local state = require("mcp_companion.state")
  local ns = state.field("native_servers") or {}
  if #ns >= 1 and ns[1].name == "neovim" and #ns[1].tools > 0 then
    ok(string.format("native_servers published (%d tools)", #ns[1].tools))
  else
    err("native_servers", vim.inspect(ns))
  end
end

-- ── window placement (never act on the chat window) ──────────────────────────
section("window placement")

if ok_native then
  local winpick = require("mcp_companion.native.winpick")

  -- Current window shows the scratch *code* buffer (buftype "", named).
  local code_win = vim.api.nvim_get_current_win()
  vim.api.nvim_win_set_buf(code_win, buf)

  -- Add a "chat" window: a split whose buffer has filetype=codecompanion.
  vim.cmd("vsplit")
  local chat_win = vim.api.nvim_get_current_win()
  local chat_buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_win_set_buf(chat_win, chat_buf)
  vim.bo[chat_buf].filetype = "codecompanion"
  vim.api.nvim_set_current_win(chat_win) -- simulate the agent running in the chat

  if winpick.code_win() == code_win then
    ok("code_win() skips the chat window")
  else
    err("code_win()", "got " .. tostring(winpick.code_win()) .. " want " .. tostring(code_win))
  end

  -- open_file from the chat: file lands in the code window, chat untouched.
  local target = tmp .. ".open"
  vim.fn.writefile({ "one", "two", "three" }, target)
  vim.api.nvim_set_current_win(chat_win)
  local of = rjson(native.dispatch("open_file", { path = target, line = 2 }))
  local landed = of and of.window == code_win
  local code_name = vim.api.nvim_buf_get_name(vim.api.nvim_win_get_buf(code_win))
  if landed and vim.fn.resolve(code_name) == vim.fn.resolve(vim.fn.fnamemodify(target, ":p")) then
    ok("open_file opens in the code window, not the chat")
  else
    err("open_file placement", vim.inspect(of) .. " code_buf=" .. code_name)
  end
  if vim.api.nvim_win_get_buf(chat_win) == chat_buf then
    ok("open_file left the chat window untouched")
  else
    err("open_file chat", "chat window buffer changed")
  end

  -- get_cursor reads the code window, not the focused chat.
  vim.api.nvim_set_current_win(chat_win)
  local gc = rjson(native.dispatch("get_cursor", {}))
  if gc and gc.buffer == vim.api.nvim_win_get_buf(code_win) then
    ok("get_cursor reads the code window")
  else
    err("get_cursor", vim.inspect(gc))
  end

  vim.fn.delete(target)
end

-- ── Results ──────────────────────────────────────────────────────────────────
table.insert(results, string.format("\n=== RESULTS: %d passed, %d failed ===", pass, fail))
for _, line in ipairs(results) do
  print(line)
end

if fail > 0 then
  vim.cmd("cq")
else
  vim.cmd("qa!")
end
