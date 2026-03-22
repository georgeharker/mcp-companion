--- mcp-companion.nvim — CC Editor Context (MCP resources → #variables)
---
--- Two features:
---
--- 1. MCP resources → CC #editor_context entries
---    Each resource is registered so users can type #mcp:resource_name
---    to insert its content into chat.
---
--- 2. System prompt injection
---    Resources tagged for system prompt injection (via config.system_prompt_resources)
---    are fetched and injected into every new CC chat via the ChatCreated autocmd.
---    Useful for guidance/context resources like basic-memory's "ai assistant guide".
---
--- CC API (v19+):
---   editor_context lives at config.interactions.shared.editor_context
---   entries can use { callback = fn, description = "..." } for dynamic values
---   or { path = "module.path", description = "..." } for module-based providers
---   We use the callback form since our resources are dynamic.
---
--- @module mcp_companion.cc.editor_context

local M = {}

local log = require("mcp_companion.log")

--- @type number|nil Autocmd group for ChatCreated listener
local _augroup = nil

--- Fetch a resource synchronously using vim.wait.
--- Returns content string or nil on error.
--- @param client table MCPCompanion.Client
--- @param uri string Resource URI
--- @return string|nil
local function fetch_resource_sync(client, uri)
    local result = nil
    local done = false

    client:read_resource(uri, function(err, res)
        if not err and res and res.contents then
            local parts = {}
            for _, content in ipairs(res.contents) do
                if content.text then
                    table.insert(parts, content.text)
                end
            end
            result = table.concat(parts, "\n")
        end
        done = true
    end)

    -- Block until callback fires (max 10s)
    vim.wait(10000, function() return done end, 50)

    return result
end

--- Register MCP resources as CC #editor_context entries.
--- Called on bridge_ready and servers_updated events.
function M.register()
    local bridge = require("mcp_companion.bridge")

    if not bridge.client or not bridge.client.connected then
        return
    end

    local cc_config_ok, cc_config = pcall(require, "codecompanion.config")
    if not cc_config_ok then
        log.debug("codecompanion.config not available, skipping editor_context registration")
        return
    end

    -- CC v19+: editor_context lives at interactions.shared.editor_context
    local editor_ctx = cc_config.interactions
        and cc_config.interactions.shared
        and cc_config.interactions.shared.editor_context
    if not editor_ctx then
        log.debug("CC editor_context table not found, skipping")
        return
    end

    -- Remove previously registered mcp: entries before re-registering
    for key in pairs(editor_ctx) do
        if type(key) == "string" and key:match("^mcp:") then
            editor_ctx[key] = nil
        end
    end

    local state = require("mcp_companion.state")
    local servers = state.field("servers") or {}
    local count = 0

    for _, server in ipairs(servers) do
        if server.name ~= "_bridge" then
            for _, resource in ipairs(server.resources or {}) do
                local var_name = string.format("mcp:%s", resource.name or resource.uri or "unknown")
                local captured_uri = resource.uri
                local captured_name = resource.name or resource.uri

                editor_ctx[var_name] = {
                    description = resource.description
                        or string.format("MCP resource: %s", captured_name),
                    ---@param args {Chat?: table, is_slash_command?: boolean}
                    callback = function(args)
                        local content = fetch_resource_sync(bridge.client, captured_uri)
                        local text = content
                            or string.format("[Error reading resource: %s]", captured_name)

                        -- For chat interaction: add as a hidden message
                        if args and args.Chat then
                            args.Chat:add_message({
                                role = "user",
                                content = text,
                            }, {
                                _meta = { source = "editor_context", tag = var_name },
                                visible = false,
                            })
                        end

                        return text
                    end,
                }
                count = count + 1
            end
        end
    end

    if count > 0 then
        log.info("Registered %d MCP resources as CC editor_context entries", count)
    end

    -- (Re)register system prompt injection autocmd
    M._setup_system_prompt_injection()
end

--- Inject configured resources into the system prompt of every new CC chat.
--- Resources to inject are determined by config.system_prompt_resources:
---   - true: inject ALL resources
---   - table of resource name/uri patterns: inject matching resources
---   - false/nil: disabled
function M._setup_system_prompt_injection()
    local config = require("mcp_companion.config").get()
    local spr = config.system_prompt_resources

    if not spr then
        return
    end

    local bridge = require("mcp_companion.bridge")

    -- Collect resources to inject
    local to_inject = {} --- @type {uri: string, name: string}[]
    local state = require("mcp_companion.state")
    local servers = state.field("servers") or {}

    for _, server in ipairs(servers) do
        if server.name ~= "_bridge" then
            for _, resource in ipairs(server.resources or {}) do
                local include = false
                if spr == true then
                    include = true
                elseif type(spr) == "table" then
                    for _, pattern in ipairs(spr) do
                        if (resource.name and resource.name:match(pattern))
                            or (resource.uri and resource.uri:match(pattern)) then
                            include = true
                            break
                        end
                    end
                end
                if include then
                    table.insert(to_inject, {
                        uri = resource.uri,
                        name = resource.name or resource.uri,
                    })
                end
            end
        end
    end

    if #to_inject == 0 then
        return
    end

    -- Create/replace autocmd group
    if _augroup then
        pcall(vim.api.nvim_del_augroup_by_id, _augroup)
    end
    _augroup = vim.api.nvim_create_augroup("MCPCompanionSysPrompt", { clear = true })

    vim.api.nvim_create_autocmd("User", {
        group = _augroup,
        pattern = "CodeCompanionChatCreated",
        callback = function(ev)
            local chat_id = ev.data and ev.data.id
            if not chat_id then
                return
            end

            -- Find the chat object via the registry
            local registry_ok, registry = pcall(require, "codecompanion.interactions.shared.registry")
            local chat = nil
            if registry_ok and registry.get then
                -- Try to find by buffer number from event data
                local bufnr = ev.data and ev.data.bufnr
                if bufnr then
                    local entry = registry.get(bufnr)
                    if entry then
                        chat = entry
                    end
                end
            end

            if not chat then
                -- Fallback: try the buf_get_chat pattern
                local bufnr = ev.data and ev.data.bufnr
                if bufnr then
                    local chats_ok, chats_module = pcall(require, "codecompanion.interactions.chat")
                    if chats_ok and chats_module.buf_get_chat then
                        chat = chats_module.buf_get_chat(bufnr)
                    end
                end
            end

            if not chat then
                log.debug("MCPSysPrompt: could not find chat object for ChatCreated event")
                return
            end

            -- Fetch and inject each resource
            vim.defer_fn(function()
                for _, res in ipairs(to_inject) do
                    local content = fetch_resource_sync(bridge.client, res.uri)
                    if content and content ~= "" then
                        chat:add_message({
                            role = "system",
                            content = content,
                        }, {
                            visible = false,
                            _meta = {
                                source = "editor_context",
                                tag = string.format("mcp_resource_%s", res.name:gsub("[^%w]", "_")),
                            },
                        })
                        log.debug("Injected resource '%s' into system prompt", res.name)
                    end
                end
            end, 50) -- small defer to ensure chat is fully initialised
        end,
    })

    log.info("System prompt injection configured for %d resources", #to_inject)
end

--- Tear down the system prompt injection autocmd (e.g. on bridge disconnect).
function M.teardown()
    if _augroup then
        pcall(vim.api.nvim_del_augroup_by_id, _augroup)
        _augroup = nil
    end
end

return M
