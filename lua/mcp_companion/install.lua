--- mcp-companion.nvim — Bridge install/ensure
---
--- Installs the Python bridge (`mcp_bridge`, exposing the `mcp-bridge` console
--- script) into a venv via `uv`, so the bridge command is available without a
--- separate build step. The target venv is `config.bridge.venv` if set,
--- otherwise the self-contained plugin-local `bridge/.venv`. SAFETY: the plugin
--- only ever creates (`uv venv`) its own plugin-local venv; a user-specified
--- `bridge.venv` must already exist and is only ever installed into (additive).
--- @module mcp_companion.install

local M = {}

local log = require("mcp_companion.log")

--- Resolve the bridge source directory (<plugin>/bridge).
--- @return string|nil
local function bridge_src()
  local config = require("mcp_companion.config")
  local root = config.plugin_dir()
  if not root then return nil end
  local src = root .. "/bridge"
  return vim.fn.isdirectory(src) == 1 and src or nil
end

--- Hash of the bridge's dependency declarations (pyproject.toml + uv.lock).
--- Used as an install stamp so `ensure()` reinstalls when deps change — an
--- editable install reflects code changes but NOT new/changed dependencies.
--- @return string|nil
local function source_hash()
  local src = bridge_src()
  if not src then return nil end
  local parts = {}
  for _, name in ipairs({ "pyproject.toml", "uv.lock" }) do
    local f = io.open(src .. "/" .. name, "r")
    if f then
      parts[#parts + 1] = f:read("*a")
      f:close()
    end
  end
  if #parts == 0 then return nil end
  return vim.fn.sha256(table.concat(parts, "\n"))
end

--- Path to the per-venv install stamp.
--- @param venv string already-expanded venv path
--- @return string
local function stamp_path(venv)
  return venv .. "/.mcp-companion-install"
end

--- The venv to install/run the bridge from: the configured `bridge.venv` if
--- set, otherwise the plugin-local `bridge/.venv` (self-contained default).
--- @return string|nil expanded absolute path
function M.target_venv()
  local cfg = require("mcp_companion.config").get().bridge
  if cfg.venv and cfg.venv ~= "" then
    return vim.fn.expand(cfg.venv)
  end
  local src = bridge_src()
  return src and (src .. "/.venv") or nil
end

--- Whether the *current* bridge is installed in `venv`: the `mcp-bridge` console
--- script exists AND the install stamp matches the current dependency hash (so a
--- stale install with old deps — e.g. missing pynvim — counts as NOT installed).
--- @param venv string
--- @return boolean
function M.is_installed(venv)
  local vexp = vim.fn.expand(venv)
  if vim.fn.executable(vexp .. "/bin/mcp-bridge") ~= 1 then
    return false
  end
  local f = io.open(stamp_path(vexp), "r")
  if not f then
    return false -- script present but unstamped → treat as stale, reinstall
  end
  local stamp = f:read("*a")
  f:close()
  local want = source_hash()
  return want ~= nil and stamp == want
end

--- Ensure the bridge is installed into `venv` (async). No-op if already present
--- (unless `force`).
--- @param venv? string Defaults to M.target_venv() (configured venv or plugin-local).
--- @param callback? fun(ok: boolean, err?: string, installed?: boolean)
--- @param force? boolean Reinstall even if already present.
--- The callback's third arg is true only when an install actually ran (false on
--- a no-op), so callers can stay quiet when nothing changed.
function M.ensure(venv, callback, force)
  callback = callback or function() end
  venv = vim.fn.expand(venv or M.target_venv() or "")
  if venv == "" then
    return callback(false, "could not resolve a target venv (bridge source not found)", false)
  end

  if not force and M.is_installed(venv) then
    return callback(true, nil, false)
  end

  local src = bridge_src()
  if not src then
    return callback(false, "bridge source directory not found", false)
  end
  if vim.fn.executable("uv") ~= 1 then
    return callback(false, "uv not found on PATH — install uv or set bridge.python_cmd", false)
  end

  -- `venv` is already expanded; precompute paths here (main context) so nothing
  -- inside the vim.system callbacks (fast context) calls vim.fn.expand.
  local py = venv .. "/bin/python"
  local venv_exists = vim.fn.executable(py) == 1

  -- SAFETY: only ever CREATE (`uv venv`) the plugin's own self-managed venv.
  -- A user-specified `bridge.venv` (e.g. ~/.venv) is theirs — never `uv venv`
  -- it (that can wipe an existing venv). We only `uv pip install` into it
  -- (additive), and require it to already exist.
  local plugin_local = src .. "/.venv"
  local self_managed = (venv == plugin_local)

  if not venv_exists and not self_managed then
    return callback(
      false,
      ("venv %s does not exist — create it yourself (`uv venv %s`) or unset bridge.venv "
        .. "to use the plugin-local venv. mcp-companion will not create a venv it doesn't own.")
        :format(venv, venv),
      false
    )
  end

  -- Install the bridge (editable, additive — never clears the venv) + stamp it.
  local function pip_install()
    vim.system(
      { "uv", "pip", "install", "--python", py, "-e", src },
      { text = true },
      function(obj)
        vim.schedule(function()
          if obj.code == 0 then
            local sf = io.open(stamp_path(venv), "w")
            if sf then
              sf:write(source_hash() or "")
              sf:close()
            end
            log.info("mcp-bridge installed into %s", venv)
            callback(true, nil, true)
          else
            log.warn("mcp-bridge install failed: %s", (obj.stderr or ""):sub(1, 400))
            callback(false, obj.stderr, false)
          end
        end)
      end
    )
  end

  if self_managed and not venv_exists then
    -- Create the plugin-local venv (only when absent), then install.
    log.info("Creating plugin-local venv %s …", venv)
    vim.system({ "uv", "venv", venv }, { text = true }, function()
      pip_install()
    end)
  else
    -- venv already exists (plugin-local or user's) → install only, never `uv venv`.
    log.info("Installing mcp-bridge into %s …", venv)
    pip_install()
  end
end

return M
