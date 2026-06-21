--- mcp-companion.nvim — Native `neovim` server: filesystem tools
--- @module mcp_companion.native.neovim.files

local util = require("mcp_companion.native.util")

--- Read a file from disk into a list of lines.
--- @param path string
--- @return string[]|nil lines, string|nil err
local function read_lines(path)
  local fd = io.open(path, "r")
  if not fd then return nil, "cannot open: " .. path end
  local content = fd:read("*a")
  fd:close()
  return vim.split(content or "", "\n", { plain = true })
end

local M = {}

M.tools = {
  {
    name = "read_file",
    tier = "read",
    description = "Read a file from disk, optionally a 1-based inclusive line range.",
    inputSchema = {
      type = "object",
      properties = {
        path = { type = "string", description = "File path" },
        start_line = { type = "integer", description = "1-based first line (default 1)" },
        end_line = { type = "integer", description = "1-based last line, inclusive (default: last)" },
      },
      required = { "path" },
    },
    handler = function(args, _ctx)
      local lines, err = read_lines(args.path)
      if not lines then return util.err(err) end
      local first = math.max(1, args.start_line or 1)
      local last = args.end_line and math.min(#lines, args.end_line) or #lines
      return util.text(table.concat(vim.list_slice(lines, first, last), "\n"))
    end,
  },

  {
    name = "read_files",
    tier = "read",
    description = "Read several files from disk in one call.",
    inputSchema = {
      type = "object",
      properties = {
        paths = { type = "array", items = { type = "string" }, description = "File paths" },
      },
      required = { "paths" },
    },
    handler = function(args, _ctx)
      local out = {}
      for _, path in ipairs(args.paths or {}) do
        local lines, err = read_lines(path)
        out[path] = lines and table.concat(lines, "\n") or ("ERROR: " .. tostring(err))
      end
      return util.json({ files = out })
    end,
  },

  {
    name = "find_files",
    tier = "read",
    description = "Glob for files matching a pattern under a directory.",
    inputSchema = {
      type = "object",
      properties = {
        pattern = { type = "string", description = "Glob pattern, e.g. *.lua" },
        path = { type = "string", description = "Directory to search (default cwd)" },
        recursive = { type = "boolean", description = "Recurse into subdirectories (default true)" },
      },
      required = { "pattern" },
    },
    handler = function(args, _ctx)
      local dir = args.path or vim.fn.getcwd()
      local recursive = args.recursive ~= false
      local glob = recursive and ("**/" .. args.pattern) or args.pattern
      local matches = vim.fn.globpath(dir, glob, false, true)
      return util.json({ matches = matches })
    end,
  },

  {
    name = "list_directory",
    tier = "read",
    description = "List the entries of a directory with type and size.",
    inputSchema = {
      type = "object",
      properties = {
        path = { type = "string", description = "Directory (default cwd)" },
      },
    },
    handler = function(args, _ctx)
      local dir = args.path or vim.fn.getcwd()
      local entries = {}
      for name, kind in vim.fs.dir(dir) do
        local full = dir .. "/" .. name
        local stat = vim.uv.fs_stat(full)
        table.insert(entries, {
          name = name,
          type = kind,
          size = stat and stat.size or nil,
        })
      end
      return util.json({ path = dir, entries = entries })
    end,
  },

  {
    name = "write_file",
    tier = "write",
    description = "Write content to a file on disk. For files NOT open in a buffer; "
      .. "use edit_buffer/set_buffer_lines for open buffers.",
    inputSchema = {
      type = "object",
      properties = {
        path = { type = "string", description = "File path" },
        content = { type = "string", description = "Full file content" },
      },
      required = { "path", "content" },
    },
    handler = function(args, _ctx)
      if vim.fn.bufexists(args.path) == 1 and vim.fn.getbufvar(vim.fn.bufnr(args.path), "&modified") == 1 then
        return util.err("refusing to write_file: '" .. args.path
          .. "' is open with unsaved changes — use edit_buffer/save_buffer")
      end
      local fd, oerr = io.open(args.path, "w")
      if not fd then return util.err("cannot write: " .. tostring(oerr)) end
      fd:write(args.content)
      fd:close()
      -- Reload the buffer if it is open so editor state matches disk.
      local bufnr = vim.fn.bufnr(args.path)
      if bufnr ~= -1 then
        pcall(function() vim.api.nvim_buf_call(bufnr, function() vim.cmd("edit!") end) end)
      end
      return util.json({ path = args.path, bytes = #args.content })
    end,
  },

  {
    name = "move_item",
    tier = "write",
    description = "Move or rename a file or directory.",
    inputSchema = {
      type = "object",
      properties = {
        path = { type = "string", description = "Source path" },
        new_path = { type = "string", description = "Destination path" },
      },
      required = { "path", "new_path" },
    },
    handler = function(args, _ctx)
      local ok, err = vim.uv.fs_rename(args.path, args.new_path)
      if not ok then return util.err("move failed: " .. tostring(err)) end
      return util.json({ from = args.path, to = args.new_path })
    end,
  },

  {
    name = "delete_items",
    tier = "write",
    description = "Delete files or directories (recursive). Destructive — host approval advised.",
    inputSchema = {
      type = "object",
      properties = {
        paths = { type = "array", items = { type = "string" }, description = "Paths to delete" },
      },
      required = { "paths" },
    },
    handler = function(args, _ctx)
      local deleted, failed = {}, {}
      for _, path in ipairs(args.paths or {}) do
        local ok = vim.fn.delete(path, "rf") == 0
        table.insert(ok and deleted or failed, path)
      end
      return util.json({ deleted = deleted, failed = failed })
    end,
  },
}

return M
