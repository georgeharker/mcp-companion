--- mcp-companion.nvim — Combiner install/ensure
---
--- Installs the Python combiner (`mcp_combiner`, exposing the `mcp-combiner` console
--- script) into a venv via `uv`, so the combiner command is available without a
--- separate build step. The target venv is `config.combiner.venv` if set, otherwise the
--- self-contained plugin-local `combiner/.venv`. The two are populated differently: the
--- plugin-local venv via `uv sync` (lock-pinned, uv manages the environment), a
--- user-specified one via an additive `uv pip install -e`. SAFETY: the plugin only ever
--- creates/manages its own plugin-local venv; a user-specified `combiner.venv` must
--- already exist and is only ever installed into (additive), never synced — `uv sync`
--- would treat it as ours to prune.
--- @module mcp_companion.install

local M = {}

local log = require("mcp_companion.log")

--- Resolve the combiner source directory (<plugin>/combiner).
--- @return string|nil
local function combiner_src()
    local config = require("mcp_companion.config")
    local root = config.plugin_dir()
    if not root then
        return nil
    end
    local src = root .. "/combiner"
    return vim.fn.isdirectory(src) == 1 and src or nil
end

--- Hash of the combiner's dependency declarations (pyproject.toml + uv.lock).
--- Used as an install stamp so `ensure()` reinstalls when deps change — an
--- editable install reflects code changes but NOT new/changed dependencies.
--- @return string|nil
local function source_hash()
    local src = combiner_src()
    if not src then
        return nil
    end
    local parts = {}
    for _, name in ipairs({ "pyproject.toml", "uv.lock" }) do
        local f = io.open(src .. "/" .. name, "r")
        if f then
            parts[#parts + 1] = f:read("*a")
            f:close()
        end
    end
    if #parts == 0 then
        return nil
    end
    return vim.fn.sha256(table.concat(parts, "\n"))
end

--- Path to the per-venv install stamp.
--- @param venv string already-expanded venv path
--- @return string
local function stamp_path(venv)
    return venv .. "/.mcp-companion-install"
end

--- The venv to install/run the combiner from: the configured `combiner.venv` if
--- set, otherwise the plugin-local `combiner/.venv` (self-contained default).
--- @return string|nil expanded absolute path
function M.target_venv()
    local cfg = require("mcp_companion.config").get().combiner
    if cfg.venv and cfg.venv ~= "" then
        return vim.fn.expand(cfg.venv)
    end
    local src = combiner_src()
    return src and (src .. "/.venv") or nil
end

--- Whether the *current* combiner is installed in `venv`: the `mcp-combiner` console
--- script exists AND the install stamp matches the current dependency hash (so a
--- stale install with old deps — e.g. missing pynvim — counts as NOT installed).
--- @param venv string
--- @return boolean
function M.is_installed(venv)
    local vexp = vim.fn.expand(venv)
    if vim.fn.executable(vexp .. "/bin/mcp-combiner") ~= 1 then
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

--- Ensure the combiner is installed into `venv` (async). No-op if already present
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
        return callback(false, "could not resolve a target venv (combiner source not found)", false)
    end

    if not force and M.is_installed(venv) then
        return callback(true, nil, false)
    end

    local src = combiner_src()
    if not src then
        return callback(false, "combiner source directory not found", false)
    end
    if vim.fn.executable("uv") ~= 1 then
        return callback(false, "uv not found on PATH — install uv or set combiner.python_cmd", false)
    end

    -- `venv` is already expanded; precompute paths here (main context) so nothing
    -- inside the vim.system callbacks (fast context) calls vim.fn.expand.
    local py = venv .. "/bin/python"
    local venv_exists = vim.fn.executable(py) == 1

    -- SAFETY: only ever CREATE/MANAGE the plugin's own self-managed venv (via `uv sync`,
    -- which makes <src>/.venv itself). A user-specified `combiner.venv` (e.g. ~/.venv) is
    -- theirs — never point an environment-managing command at it. We only `uv pip install`
    -- into it (additive), and require it to already exist.
    local plugin_local = src .. "/.venv"
    local self_managed = (venv == plugin_local)

    if not venv_exists and not self_managed then
        return callback(
            false,
            (
                "venv %s does not exist — create it yourself (`uv venv %s`) or unset combiner.venv "
                .. "to use the plugin-local venv. mcp-companion will not create a venv it doesn't own."
            ):format(venv, venv),
            false
        )
    end

    -- Stamp the venv and report — shared by both install strategies below.
    local function finish(obj)
        vim.schedule(function()
            if obj.code == 0 then
                local sf = io.open(stamp_path(venv), "w")
                if sf then
                    sf:write(source_hash() or "")
                    sf:close()
                end
                log.info("mcp-combiner installed into %s", venv)
                callback(true, nil, true)
            else
                log.warn("mcp-combiner install failed: %s", (obj.stderr or ""):sub(1, 400))
                callback(false, obj.stderr, false)
            end
        end)
    end

    if self_managed then
        -- Plugin-owned venv (the default, user-facing mode) → `uv sync`, which honours
        -- uv.lock. The stamp hashes that lock, so a lock change now both triggers a
        -- reinstall AND actually yields the locked versions (`uv pip install` re-resolves
        -- against the index and ignores the lock entirely). uv creates and manages
        -- <src>/.venv itself — which is exactly this venv — so there is no `uv venv` step.
        --   --frozen   install from the committed lock as-is; never re-resolve
        --   --no-dev   runtime deps only; the dev group is for `cd combiner && uv sync`
        --   --inexact  never prune — we own the combiner's deps here, not the whole venv
        -- UV_PROJECT_ENVIRONMENT is set explicitly rather than left to uv's default of
        -- <project>/.venv: a user with that variable exported (containerised workflows do
        -- this) would otherwise have the sync populate THEIR directory while we stamp and
        -- look for <src>/.venv — an install that reports success, leaves no binary where
        -- we expect one, and so reinstalls on every setup(). Naming the target makes the
        -- destination ours regardless of the ambient environment.
        log.info("Syncing mcp-combiner into %s …", venv)
        vim.system(
            { "uv", "sync", "--frozen", "--no-dev", "--inexact", "--project", src },
            { text = true, env = { UV_PROJECT_ENVIRONMENT = venv } },
            finish
        )
    else
        -- User-specified venv (the dev mode) → additive editable install, nothing else.
        -- Never `uv sync` here: it is project-scoped and would treat a venv we don't own
        -- as ours to manage.
        log.info("Installing mcp-combiner into %s …", venv)
        vim.system({ "uv", "pip", "install", "--python", py, "-e", src }, { text = true }, finish)
    end
end

return M
