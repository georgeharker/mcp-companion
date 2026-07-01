--- mcp-companion.nvim — Auto-approval logic
--- Determines whether a tool call should proceed immediately or require
--- user confirmation before execution.
---
--- Approval chain (in-process CodeCompanion chats only — external ACP/CLI
--- agents use their host harness's own approval):
---   1. Global `auto_approve` (boolean or function) — applies to every tool.
---   2. Per-server `auto_approve` spec — the SAME spec style for native and
---      proxied servers: a list of tool-name globs plus `tier:<tier>` alias
---      tokens (or `true` / a function). For native servers the spec comes from
---      `native_servers.<name>.auto_approve`; for proxied servers from the
---      server's `autoApprove` (surfaced into state.servers).
---   3. Prompt the user via `vim.ui.select`.
--- @module mcp_companion.cc.approval

local M = {}

--- Convert a tool-name glob (e.g. "read_*") to an anchored Lua pattern.
--- @param glob string
--- @return string
local function _glob_to_pattern(glob)
    -- Escape Lua pattern magic chars (NOT '*'), then turn '*' into '.*'.
    local out = glob:gsub("[%^%$%(%)%.%[%]%+%-%?%%]", "%%%1")
    out = out:gsub("%*", ".*")
    return "^" .. out .. "$"
end

--- Does `tool_name` satisfy an auto_approve spec for `server_name`?
--- spec: `true` (all) | string[] (globs + `tier:<tier>` aliases) | function.
--- @param spec boolean|string[]|function
--- @param server_name string
--- @param tool_name string
--- @return boolean
local function _spec_approves(spec, server_name, tool_name)
    if spec == true then
        return true
    end
    if type(spec) == "function" then
        local ok, r = pcall(spec, tool_name, server_name)
        return ok and r == true
    end
    if type(spec) ~= "table" then
        return false
    end

    local native_ok, native = pcall(require, "mcp_companion.native")
    for _, entry in ipairs(spec) do
        if type(entry) == "string" then
            local tier_alias = entry:match("^tier:(.+)$")
            if tier_alias then
                -- `tier:x` matches any tool whose internal tier is x (native only).
                if native_ok and native.tool_tier and native.tool_tier(tool_name) == tier_alias then
                    return true
                end
            elseif tool_name:match(_glob_to_pattern(entry)) then
                return true
            end
        end
    end
    return false
end

--- Resolve the per-server auto_approve spec.
--- Priority: per-project `.mcp-companion.json` override > plugin-level config
--- (native_servers.<name>.auto_approve for native, state.servers for proxied).
--- @param server_name string
--- @return boolean|string[]|function|nil
local function _server_auto_approve(server_name)
    -- 1. Per-project override.
    local proj_ok, project = pcall(require, "mcp_companion.project")
    if proj_ok then
        local override = project.resolve_auto_approve(server_name)
        if override ~= nil then
            return override
        end
    end

    -- 2. Plugin-level default.
    local native_ok, native = pcall(require, "mcp_companion.native")
    if native_ok and native.is_native_server(server_name) then
        local ns = (require("mcp_companion.config").get().native_servers or {})[server_name]
        return ns and ns.auto_approve
    end
    -- Proxied server: spec is surfaced onto the state.servers entry.
    local state = require("mcp_companion.state")
    for _, srv in ipairs(state.field("servers") or {}) do
        if srv.name == server_name then
            return srv.auto_approve
        end
    end
    return nil
end

--- Check if a tool call should be auto-approved.
--- If not auto-approved, prompts the user via vim.ui.select.
--- @param server_name string Server that owns the tool
--- @param tool_name string Tool being called
--- @param tool_ctx table CC tool context (self from handler)
--- @param callback fun(approved: boolean) Called with result
function M.check(server_name, tool_name, tool_ctx, callback)
    local config = require("mcp_companion.config").get()

    -- 1. Global auto_approve — authoritative when set.
    if config.auto_approve == true then
        return callback(true)
    end
    if type(config.auto_approve) == "function" then
        local ok, result = pcall(config.auto_approve, tool_name, server_name, tool_ctx)
        if ok then
            return callback(result and true or false)
        end
        -- If the function errors, fall through to the per-server spec.
    end

    -- 2. Per-server auto_approve spec — unified for native and proxied servers
    --    (globs + `tier:<tier>` aliases). Native default is read/navigate.
    local spec = _server_auto_approve(server_name)
    if spec ~= nil and _spec_approves(spec, server_name, tool_name) then
        return callback(true)
    end

    -- 3. Prompt user
    vim.schedule(function()
        vim.ui.select({ "Allow", "Deny" }, {
            prompt = string.format("MCP tool call: %s/%s", server_name, tool_name),
            kind = "mcp_approval",
        }, function(choice)
            callback(choice == "Allow")
        end)
    end)
end

return M
