--- mcp-companion.nvim — Native server registry + dispatcher
---
--- Native servers run pure-Lua, in-process, against the live Neovim instance.
--- The catalog is **fixed and curated** — there is intentionally no public
--- `add_tool`/`add_server` DSL (see docs/designs/native-neovim-server.md). The
--- single entry point is `M.dispatch(name, args, ctx)`, which is also the only
--- function the Python combiner ever invokes over msgpack-RPC.
--- @module mcp_companion.native

local util = require("mcp_companion.native.util")

local M = {}

--- @class MCPCompanion.NativeServer
--- @field name string
--- @field displayName? string
--- @field description? string
--- @field tools table[]
--- @field resources table[]

--- @type table<string, MCPCompanion.NativeServer>
local _servers = {}

--- Flat tool lookup: tool name → { server, tool }
--- @type table<string, { server: string, tool: table }>
local _tool_index = {}

--- Flat resource lookup: uri → { server, resource }
--- @type table<string, { server: string, resource: table }>
local _resource_index = {}

--- Install a server definition into the registry (internal).
--- @param def MCPCompanion.NativeServer
local function _install(def)
    _servers[def.name] = def
    for _, tool in ipairs(def.tools or {}) do
        _tool_index[tool.name] = { server = def.name, tool = tool }
    end
    for _, res in ipairs(def.resources or {}) do
        _resource_index[res.uri] = { server = def.name, resource = res }
    end
end

--- Get or create a server record, ensuring all capability arrays exist.
--- @param name string
--- @return table server
local function _ensure_server(name)
    local srv = _servers[name]
    if not srv then
        srv = { name = name, tools = {}, resources = {}, resource_templates = {}, prompts = {} }
        _servers[name] = srv
    end
    srv.tools = srv.tools or {}
    srv.resources = srv.resources or {}
    srv.resource_templates = srv.resource_templates or {}
    srv.prompts = srv.prompts or {}
    return srv
end

--- Publish the native servers into shared state so :MCPStatus and the CC
--- registration path can see them (shape mirrors MCPCompanion.ServerInfo).
local function _publish()
    local state = require("mcp_companion.state")
    local list = {}
    for _, def in pairs(_servers) do
        local tools = {}
        for _, t in ipairs(def.tools or {}) do
            table.insert(tools, {
                name = t.name,
                description = t.description,
                inputSchema = t.inputSchema,
                tier = t.tier,
                _display = t.name,
                _namespaced = def.name .. "_" .. t.name,
            })
        end
        table.insert(list, {
            name = def.name,
            status = "connected",
            tools = tools,
            resources = def.resources or {},
            resource_templates = def.resource_templates or {},
            prompts = def.prompts or {},
        })
    end
    state.update("native_servers", list)
end

--- Setup native servers from plugin config.
--- @param config table Plugin config (uses config.native_servers)
function M.setup(config)
    _servers = {}
    _tool_index = {}
    _resource_index = {}

    local ns = (config and config.native_servers) or {}
    local nvim_cfg = ns.neovim
    if nvim_cfg == nil or nvim_cfg.enabled ~= false then
        _install(require("mcp_companion.native.neovim").build(nvim_cfg or {}))
    end

    _publish()
end

--- Strip a leading "<server>_" namespace from a tool name, if present.
--- @param name string
--- @return string
local function _strip_prefix(name)
    for server_name in pairs(_servers) do
        local prefix = server_name .. "_"
        if name:sub(1, #prefix) == prefix then
            return name:sub(#prefix + 1)
        end
    end
    return name
end

--- Resolve the normalized Neovim context once per dispatch.
--- @return table { current_buf, cursor }
local function _resolve_context()
    local buf = util.current_file_buf()
    local cursor = { line = 0, col = 0 }
    local win = util.win_for_buf(buf)
    if win then
        local ok, pos = pcall(vim.api.nvim_win_get_cursor, win)
        if ok then
            cursor = { line = pos[1], col = pos[2] }
        end
    end
    return { current_buf = buf, cursor = cursor }
end

--- The single dispatch contract. Validates the tool name, resolves a normalized
--- context, runs the handler, and returns an MCP-shaped result. Performs NO
--- approval — that is the host/agent's responsibility.
--- @param name string Tool name (plain or namespaced, e.g. "read_buffer" or "neovim_read_buffer")
--- @param args? table Tool arguments
--- @param ctx? table Call context { token, caller, session_id, call_id, chat, nvim }
--- @return table result MCP content table, or { isError = true, ... }
function M.dispatch(name, args, ctx)
    ctx = ctx or {}
    args = args or {}

    local entry = _tool_index[name] or _tool_index[_strip_prefix(name)]
    if not entry then
        return util.err("unknown neovim tool: " .. tostring(name))
    end

    ctx.nvim = ctx.nvim or _resolve_context()

    local ok, res = pcall(entry.tool.handler, args, ctx)
    if not ok then
        return util.err(string.format("tool '%s' failed: %s", name, tostring(res)))
    end
    if type(res) ~= "table" then
        return util.err(string.format("tool '%s' returned a non-table result", name))
    end
    return res
end

--- Read a native resource by URI.
--- @param uri string
--- @param ctx? table
--- @return table result MCP content table, or { isError = true, ... }
function M.read_resource(uri, ctx)
    ctx = ctx or {}
    local entry = _resource_index[uri]
    if not entry then
        return util.err("unknown native resource: " .. tostring(uri))
    end
    ctx.nvim = ctx.nvim or _resolve_context()
    local ok, res = pcall(entry.resource.handler, ctx)
    if not ok then
        return util.err(string.format("resource '%s' failed: %s", uri, tostring(res)))
    end
    return res
end

--- Produce the tool/resource manifest for every native server.
--- Fetched by the combiner over the channel (a trivial exec_lua) at instance
--- registration and associated with that instance — so the combiner advertises
--- `neovim` tools to agents iff a live instance is attached.
--- @return table<string, { tools: table[], resources: table[] }>
function M.manifest()
    local out = {}
    for name, def in pairs(_servers) do
        local tools = {}
        for _, t in ipairs(def.tools or {}) do
            table.insert(tools, {
                name = t.name,
                description = t.description,
                inputSchema = t.inputSchema or { type = "object", properties = {} },
                tier = t.tier,
            })
        end
        local resources = {}
        for _, r in ipairs(def.resources or {}) do
            table.insert(resources, {
                name = r.name,
                uri = r.uri,
                mimeType = r.mimeType,
                description = r.description,
            })
        end
        out[name] = { tools = tools, resources = resources }
    end
    return out
end

--- Get all registered native servers (list form).
--- @return MCPCompanion.NativeServer[]
function M.get_servers()
    local result = {}
    for _, server in pairs(_servers) do
        table.insert(result, server)
    end
    return result
end

--- Check if a server name is a native server.
--- @param name string
--- @return boolean
function M.is_native_server(name)
    return _servers[name] ~= nil
end

-- ── Public registration API ────────────────────────────────────────────────
-- Register extra native servers/tools/resources. **Registration-only and
-- setup-time:** call these from your plugin's setup(), before any editor
-- connects to the combiner. The combiner captures the tool catalog once (frozen
-- per combiner process), so tools added after an instance has connected — or
-- tools that differ between instances — are NOT reflected in the combiner's
-- advertised manifest. Keep registrations identical across instances.

--- Register a native server, optionally with tools/resources/prompts inline.
--- @param name string
--- @param def? table { displayName?, description?, tools?, resources?, resource_templates?, prompts? }
--- @return table server
function M.add_server(name, def)
    def = def or {}
    local srv = _ensure_server(name)
    srv.displayName = def.displayName or srv.displayName
    srv.description = def.description or srv.description
    for _, t in ipairs(def.tools or {}) do
        M.add_tool(name, t)
    end
    for _, r in ipairs(def.resources or {}) do
        M.add_resource(name, r)
    end
    for _, t in ipairs(def.resource_templates or {}) do
        M.add_resource_template(name, t)
    end
    for _, p in ipairs(def.prompts or {}) do
        M.add_prompt(name, p)
    end
    _publish()
    return srv
end

--- Add a tool to a native server (creating the server if needed).
--- @param server_name string
--- @param tool table { name, description?, inputSchema?, tier?, handler }
function M.add_tool(server_name, tool)
    local srv = _ensure_server(server_name)
    table.insert(srv.tools, tool)
    _tool_index[tool.name] = { server = server_name, tool = tool }
    _publish()
end

--- Add a read-only resource to a native server.
--- @param server_name string
--- @param resource table { name?, uri, mimeType?, description?, handler }
function M.add_resource(server_name, resource)
    local srv = _ensure_server(server_name)
    table.insert(srv.resources, resource)
    _resource_index[resource.uri] = { server = server_name, resource = resource }
    _publish()
end

--- Add a resource template to a native server (stored; surfaced in state).
--- @param server_name string
--- @param template table { uriTemplate, mimeType?, description?, handler }
function M.add_resource_template(server_name, template)
    local srv = _ensure_server(server_name)
    table.insert(srv.resource_templates, template)
    _publish()
end

--- Add a prompt to a native server (stored; surfaced in state).
--- @param server_name string
--- @param prompt table { name, description?, handler }
function M.add_prompt(server_name, prompt)
    local srv = _ensure_server(server_name)
    table.insert(srv.prompts, prompt)
    _publish()
end

--- Return the risk tier of a tool ("read"|"navigate"|"write"|"exec"), or nil.
--- Accepts a plain or namespaced tool name. Drives in-process approval policy.
--- @param name string
--- @return string|nil
function M.tool_tier(name)
    local entry = _tool_index[name] or _tool_index[_strip_prefix(name)]
    return entry and entry.tool.tier or nil
end

return M
