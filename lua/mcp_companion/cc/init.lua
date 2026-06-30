--- mcp-companion.nvim — CC Extension entry point
--- Combiners MCP capabilities into CodeCompanion:
---   - MCP tools → CC tools (function calling)
---   - MCP resources → CC #editor_context entries
---   - MCP prompts → CC / slash commands
---
--- Registered via CodeCompanion.register_extension("mcp_companion", M)
--- @module mcp_companion.cc

local M = {}

local log = require("mcp_companion.log")

-- Token generated in ACPSessionPre, consumed by the patched transform_to_acp
-- which runs (via _establish_session) immediately after Pre fires.
-- Keyed by adapter name so concurrent ACP sessions don't collide.
-- { [adapter_name] = { token=string, agent_capabilities=table|nil } }
M._pending_acp_tokens = {}

-- CodeCompanionCLI instances tracked by bufnr.  Populated by the patched
-- ``codecompanion.interactions.cli.create`` (one entry per CLI window) and
-- drained by the ``CodeCompanionCLIClosed`` subscriber.  Each instance carries
-- _mcp_token / _mcp_client / _mcp_allowed_servers so existing helpers
-- (_apply_token_filter, _cleanup_session_filter, session_commands.*) accept it
-- as a chat-shaped handle without modification.
--- @type table<integer, table>
M._cli_instances = {}

--- Resolve the per-session allowed-servers filter.
--- A ``.mcp-companion.json`` walked up from cwd takes precedence over the
--- global ``cc.auto_http_tools`` / ``cc.auto_acp_tools`` / ``cc.auto_cli_tools``
--- setting, so a single global default (e.g. ``auto_http_tools = false``) can
--- be selectively enabled per-project.
---
--- Resolution order (first match wins):
---   1. Project file ``adapters.<adapter_name>`` entry.
---   2. Project file top-level ``allowed_servers`` / ``disabled_servers``.
---   3. Global ``cc.adapters.<adapter_name>.auto_{http,acp,cli}_tools``.
---   4. Global ``cc.auto_{http,acp,cli}_tools``.
---
--- @param kind "http"|"acp"|"cli"
--- @param adapter_name? string Adapter name for per-adapter config lookup.
--- @return string[]|nil allowed nil = no filter (all servers visible)
function M._resolve_session_allowed(kind, adapter_name)
    local cfg = require("mcp_companion.config").get()
    local cc = cfg.cc or {}

    local kind_key = {
        acp = "auto_acp_tools",
        cli = "auto_cli_tools",
        http = "auto_http_tools",
    }
    local key = kind_key[kind] or "auto_http_tools"

    -- Resolve global auto_value, preferring adapter-specific override.
    local auto_value
    local adapter_cfg = adapter_name and cc.adapters and cc.adapters[adapter_name]
    if adapter_cfg and adapter_cfg[key] ~= nil then
        auto_value = adapter_cfg[key]
    end
    if auto_value == nil then
        auto_value = cc[key]
    end

    local known_servers
    local state_ok, state = pcall(require, "mcp_companion.state")
    if state_ok then
        local servers = state.field("servers") or {}
        known_servers = {}
        for _, srv in ipairs(servers) do
            if srv.name then
                table.insert(known_servers, srv.name)
            end
        end
    end

    local project = require("mcp_companion.project")
    return project.resolve_allowed(auto_value, known_servers, nil, adapter_name)
end

--- Build combiner MCP server entry for ACP session/new.
--- Each ACP session gets a unique URL (/mcp/<token>) so the combiner can
--- associate the MCP connection with the correct ACP chat session.
--- @param agent_capabilities table|nil agentCapabilities from ACP INITIALIZE RPC
--- @param token string UUID token identifying this ACP session
--- @return table|nil combiner_entry MCP server entry or nil if no combiner config
local function build_combiner_entry(agent_capabilities, token)
    local config = require("mcp_companion.config").get()

    -- Need combiner config to know host/port
    if not config.combiner or not config.combiner.config then
        return nil
    end

    local host = config.combiner.host or "127.0.0.1"
    local port = config.combiner.port or 9741

    -- Token correlation. The combiner's single correlation key is the
    -- X-MCP-Combiner-Session *header* (read in nvim_proxy.record_session_token /
    -- the per-chat filter). It can be populated two ways:
    --   * the agent sends the header directly (needs MCP-SDK header support), or
    --   * the token rides in the URL path /mcp/<token> and TokenRewriteMiddleware
    --     injects the header internally.
    --
    -- `combiner.token_in_url` (default false) controls the HTTP branch:
    --   false → /mcp + header only (cleaner; relies on the client forwarding headers)
    --   true  → /mcp/<token> + header (belt-and-braces; works for any HTTP client)
    -- The stdio (mcp-remote) branch ALWAYS uses /mcp/<token>: mcp-remote forwards
    -- neither headers nor env, so the URL is the only channel that can correlate.
    local caps = agent_capabilities and agent_capabilities.mcpCapabilities
    local token_in_url = config.combiner.token_in_url == true -- default false (header-only)
    local plain_url = string.format("http://%s:%d/mcp", host, port)
    local token_url = string.format("http://%s:%d/mcp/%s", host, port, token)

    if caps and caps.http then
        local combiner_url = token_in_url and token_url or plain_url
        log.debug(
            "CC ACP: HTTP combiner transport (token=%s url=%s token_in_url=%s)",
            token,
            combiner_url,
            tostring(token_in_url)
        )
        return {
            type = "http",
            name = "mcp-combiner",
            url = combiner_url,
            -- Header is always sent; it is the primary correlation channel.
            headers = { { name = "X-MCP-Combiner-Session", value = token } },
        }
    else
        -- stdio via mcp-remote: token must ride in the URL (env/header not forwarded).
        log.debug("CC ACP: stdio mcp-remote combiner transport (token=%s url=%s)", token, token_url)
        return {
            name = "mcp-combiner",
            command = "npx",
            args = { "-y", "mcp-remote", token_url },
            env = { { name = "MCP_ACP_TOKEN", value = token } },
        }
    end
end

--- Called by CodeCompanion when the extension is loaded.
--- Sets up event listeners that trigger (re)registration when the combiner
--- connects or capabilities change.
--- Also patches ACP to inject combiner as MCP server for ACP agents.
--- @param schema? table Extension schema from CC config
function M.setup(schema) -- luacheck: ignore 212/schema
    local state = require("mcp_companion.state")
    math.randomseed(vim.loop.hrtime())

    -- Start combiner when any chat adapter is created.
    -- Block briefly to ensure tools are registered before first submit.
    -- With parallel requests and "healthy" state, this blocks for
    -- at most the MCP client connect time (~300ms if combiner already up).
    -- Use a generous timeout (30s) to accommodate OAuth browser flows on first
    -- connection — the wait resolves immediately once the combiner is healthy.
    vim.api.nvim_create_autocmd("User", {
        pattern = "CodeCompanionChatAdapter",
        callback = function()
            M._wait_for_combiner(30000)
        end,
    })

    -- Auto-enable MCP tool groups when chat is created
    vim.api.nvim_create_autocmd("User", {
        pattern = "CodeCompanionChatCreated",
        callback = function(args)
            M._auto_native_tools(args.data)
            M._auto_http_tools(args.data)
        end,
    })

    -- Patch codecompanion.mcp.transform_to_acp (once) to:
    --   1. Also translate HTTP servers from config.mcp.servers (upstream only handles stdio)
    --   2. Append the combiner entry for the current ACP session token
    -- Guarded with _mcp_companion_patched so re-calling setup() never double-wraps.
    local ok, cc_mcp = pcall(require, "codecompanion.mcp")
    if ok and cc_mcp and cc_mcp.transform_to_acp and not cc_mcp._mcp_companion_patched then
        local _orig_transform_to_acp = cc_mcp.transform_to_acp
        cc_mcp.transform_to_acp = function(adapter_name)
            -- Call original (handles stdio servers in default_servers list)
            local result = _orig_transform_to_acp(adapter_name)

            -- Also translate HTTP servers from config.mcp.servers that upstream ignores.
            -- These are user-configured intent and should be passed through as-is.
            local cc_config = require("codecompanion.config")
            local mcp_servers = cc_config.mcp and cc_config.mcp.servers or {}
            local default_servers = cc_config.mcp and cc_config.mcp.opts and cc_config.mcp.opts.default_servers or {}
            for name, cfg in pairs(mcp_servers) do
                if vim.tbl_contains(default_servers, name) and cfg.url then
                    local headers = {}
                    if cfg.headers then
                        for k, v in pairs(cfg.headers) do
                            table.insert(headers, { name = k, value = v })
                        end
                    end
                    -- Avoid duplicates (upstream may eventually handle these)
                    local already = false
                    for _, s in ipairs(result) do
                        if s.name == name then
                            already = true
                            break
                        end
                    end
                    if not already then
                        table.insert(result, { type = "http", name = name, url = cfg.url, headers = headers })
                        log.debug("CC ACP: transform_to_acp added HTTP server %s → %s", name, cfg.url)
                    end
                end
            end

            -- Append combiner entry for the pending ACP session token.
            -- CC calls transform_to_acp() with no args so adapter_name is nil;
            -- grab the first (only) pending entry — one ACP session establishes at a time.
            local pending = adapter_name and M._pending_acp_tokens[adapter_name]
            if not pending then
                local _, v = next(M._pending_acp_tokens)
                pending = v
            end
            if pending and pending.token then
                local combiner_entry = build_combiner_entry(pending.agent_capabilities, pending.token)
                if combiner_entry then
                    local already = false
                    for _, s in ipairs(result) do
                        if s.name == "mcp-combiner" then
                            already = true
                            break
                        end
                    end
                    if not already then
                        table.insert(result, combiner_entry)
                        log.info("CC ACP: transform_to_acp injected combiner (token=%s)", pending.token)
                    end
                end
            end

            return result
        end
        cc_mcp._mcp_companion_patched = true
        log.debug("CC ACP: patched codecompanion.mcp.transform_to_acp")
    elseif ok and cc_mcp and cc_mcp._mcp_companion_patched then
        log.debug("CC ACP: transform_to_acp already patched, skipping")
    else
        log.warn("CC ACP: could not patch transform_to_acp (codecompanion.mcp not available)")
    end

    vim.api.nvim_create_autocmd("User", {
        pattern = "CodeCompanionACPSessionPre",
        callback = function(args)
            local adapter_modified = args.data and args.data.adapter_modified
            local agent_capabilities = args.data and args.data.agent_capabilities
            if not adapter_modified then
                log.warn("CC ACP: CodeCompanionACPSessionPre fired but adapter_modified is nil")
                return
            end
            log.debug(
                "CC ACP: ACPSessionPre adapter=%s name=%s",
                tostring(adapter_modified),
                tostring(adapter_modified.name)
            )

            local adapter_name = adapter_modified.name

            -- Kick off combiner warm-up (non-blocking).
            M._start_combiner_async()

            -- Resolve allowed-servers for this session.  Project file
            -- (.mcp-companion.json walked up from cwd) overrides cc.auto_acp_tools.
            local allowed = M._resolve_session_allowed("acp", adapter_name)

            -- Generate per-session token. Store in _pending_acp_tokens so the
            -- patched transform_to_acp (called from _establish_session immediately
            -- after this event) can append the combiner entry.
            local token = M._generate_token()
            M._pending_acp_tokens[adapter_name] = {
                token = token,
                agent_capabilities = agent_capabilities,
            }
            log.info("CC ACP: Pre stored pending token for adapter=%s token=%s", adapter_name, token)

            -- Register our Neovim instance and bind this token to it BEFORE the agent
            -- connects and lists tools, so the combiner advertises neovim_* tools for
            -- this ACP session. bind() queues until registration completes, and the
            -- combiner fires tools/list_changed on bind so a late bind still refreshes.
            pcall(function()
                local channel = require("mcp_companion.native.channel")
                channel.sync() -- reconcile registration (recover from a combiner restart)
                channel.bind(token) -- track + bind this chat's token to our instance
            end)

            -- If mcpServers is a concrete table (not "inherit_from_config"), inject
            -- directly — transform_to_acp is never called in that path.
            local defaults = adapter_modified.defaults
            if defaults and type(defaults.mcpServers) == "table" then
                local combiner_entry = build_combiner_entry(agent_capabilities, token)
                if combiner_entry then
                    local already = false
                    for _, s in ipairs(defaults.mcpServers) do
                        if s.name == "mcp-combiner" then
                            already = true
                            break
                        end
                    end
                    if not already then
                        table.insert(defaults.mcpServers, combiner_entry)
                        log.info("CC ACP: Pre injected combiner into concrete mcpServers (token=%s)", token)
                    end
                end
            end
            -- "inherit_from_config" case is handled by the patched transform_to_acp.

            -- Also store on chat.adapter so ACPSessionPost can retrieve it.
            local cc_ok, codecompanion = pcall(require, "codecompanion")
            if cc_ok then
                local all_chats = codecompanion.buf_get_chat()
                for _, entry in ipairs(all_chats or {}) do
                    local c = entry.chat
                    if c and c.adapter and c.adapter.name == adapter_name then
                        c.adapter._mcp_token = token
                        c.adapter._mcp_allowed_servers = allowed
                        if allowed == nil then
                            log.debug("CC ACP: stored token on adapter (token=%s, allowed=all)", token)
                        elseif #allowed == 0 then
                            log.debug("CC ACP: stored token on adapter (token=%s, allowed=none)", token)
                        else
                            log.debug(
                                "CC ACP: stored token on adapter (token=%s, allowed=%s)",
                                token,
                                vim.inspect(allowed)
                            )
                        end
                        break
                    end
                end
            end
        end,
    })

    -- After the ACP session is established, find the chat and read the token
    -- from chat.adapter (stored there in ACPSessionPre, persists since
    -- acp_connection.adapter IS chat.adapter — same object reference).
    vim.api.nvim_create_autocmd("User", {
        pattern = "CodeCompanionACPSessionPost",
        callback = function(args)
            log.debug("CC ACP: ACPSessionPost fired")
            local acp_session_id = args.data and args.data.session_id
            if not acp_session_id then
                log.debug("CC ACP: ACPSessionPost has no session_id in args.data")
                return
            end
            log.debug("CC ACP: ACPSessionPost session_id=%s", acp_session_id)

            -- Find the chat by iterating all chats and matching acp_connection.session_id.
            local chat
            local cc_ok, codecompanion = pcall(require, "codecompanion")
            if cc_ok then
                local all_chats = codecompanion.buf_get_chat()
                for _, entry in ipairs(all_chats or {}) do
                    local c = entry.chat
                    if c and c.acp_connection and c.acp_connection.session_id == acp_session_id then
                        chat = c
                        break
                    end
                end
            end

            if not chat then
                log.debug("CC ACP: no chat found for session %s", acp_session_id)
                return
            end

            -- Read token from chat.adapter (stored there in ACPSessionPre)
            log.debug(
                "CC ACP: Post found chat bufnr=%s adapter=%s adapter._mcp_token=%s",
                tostring(chat.bufnr),
                tostring(chat.adapter),
                tostring(chat.adapter and chat.adapter._mcp_token)
            )
            local token = chat.adapter and chat.adapter._mcp_token
            local allowed_servers = chat.adapter and chat.adapter._mcp_allowed_servers

            if not token then
                log.warn(
                    "CC ACP: no token on chat.adapter for session %s (adapter=%s) — Pre event may have missed this chat",
                    acp_session_id,
                    tostring(chat.adapter)
                )
                return
            end

            -- Clear the pending token now that it's been consumed by transform_to_acp
            local adapter_name = chat.adapter and chat.adapter.name
            if adapter_name then
                M._pending_acp_tokens[adapter_name] = nil
            end

            -- Copy to chat object for easy access in session_commands and cleanup
            chat._mcp_token = token
            chat._mcp_allowed_servers = allowed_servers
            log.info(
                "CC ACP: token picked up in Post (session=%s token=%s bufnr=%s allowed=%s)",
                acp_session_id,
                token,
                tostring(chat.bufnr),
                allowed_servers and vim.inspect(allowed_servers) or "all"
            )

            -- Apply filter immediately via token endpoint. Combiner stores it as pending
            -- if opencode hasn't connected yet, and applies it when the token is first seen.
            M._apply_token_filter(chat)
        end,
    })

    -- When combiner connects and capabilities are populated, register everything
    state.on("combiner_ready", function()
        log.debug("CC extension: combiner_ready — registering all")
        M._register_all()
    end)

    -- Re-register when servers change
    state.on("servers_updated", function()
        log.debug("CC extension: servers_updated — re-registering all")
        M._register_all()
    end)

    -- Register static /mcp-session slash command (once, not on combiner_ready)
    require("mcp_companion.cc.session_commands").register()

    -- Patch codecompanion.interactions.cli.create (once) to attach per-CLI
    -- combiner session state to the returned instance. Mirrors the
    -- transform_to_acp pattern: same _mcp_companion_patched guard, same
    -- call-original-then-mutate shape. The instance becomes chat-shaped
    -- (_mcp_token / _mcp_client / _mcp_allowed_servers on it) so the existing
    -- session_commands and UI helpers consume it without modification.
    local cli_ok, cc_cli_mod = pcall(require, "codecompanion.interactions.cli")
    if cli_ok and cc_cli_mod and cc_cli_mod.create and not cc_cli_mod._mcp_companion_patched then
        local _orig_create = cc_cli_mod.create
        cc_cli_mod.create = function(create_args)
            -- Warm the combiner synchronously, mirroring the CodeCompanionChatAdapter
            -- → _wait_for_combiner(30000) hop the chat path uses.  Without this, a
            -- CLI opened before any chat would find combiner.client.connected = false
            -- and _setup_http_per_chat would early-return, leaving the instance
            -- with no _mcp_token and :MCPStatus showing the combiner as disconnected.
            M._wait_for_combiner(30000)
            -- Per-chat back-channel for CLI agents. The CLI agent connects to the
            -- combiner via its OWN static MCP config — but that config's URL is
            -- `${MCP_COMPANION_COMBINER_URL:-http://127.0.0.1:9741/mcp}`. We mint a
            -- per-chat token, bind it to this editor, and inject the tokened URL into
            -- the agent's launch environment (in the exec args, scoped to this spawn).
            -- The agent then dials /mcp/<token> and the combiner correlates its session
            -- to this editor (neovim_* routing) + applies the per-chat filter. If the
            -- env is unset (standalone claude) the `:-` default keeps it tokenless.
            local prep = M._cli_inject_combiner_env(create_args)
            local instance = _orig_create(create_args)
            if prep then
                prep.restore()
            end
            if instance and instance.bufnr then
                -- Synthesise an adapter shape so _resolve_session_allowed reads
                -- instance.adapter.name; type="cli" selects auto_cli_tools.
                instance.adapter = instance.adapter or { name = instance.agent_name, type = "cli" }
                M._cli_instances[instance.bufnr] = instance
                -- The CLI agent carries the per-chat token via its env-driven combiner URL,
                -- so its OWN combiner session does tool routing + filtering — exactly like
                -- ACP. No lite client. Set the token + allowed servers and POST the
                -- (pending) filter; the combiner applies it when the agent connects.
                if prep then
                    instance._mcp_token = prep.token
                    instance._mcp_allowed_servers = M._resolve_session_allowed("cli", instance.adapter.name)
                    M._apply_token_filter(instance)
                end
            end
            return instance
        end
        cc_cli_mod._mcp_companion_patched = true
        log.debug("CC CLI: patched codecompanion.interactions.cli.create")
    elseif cli_ok and cc_cli_mod and cc_cli_mod._mcp_companion_patched then
        log.debug("CC CLI: create already patched, skipping")
    else
        log.debug("CC CLI: codecompanion.interactions.cli not available, skipping patch")
    end

    -- Clean up per-CLI session state when a CLI window closes.
    -- Mirrors the CodeCompanionChatClosed handler above.
    vim.api.nvim_create_autocmd("User", {
        pattern = "CodeCompanionCLIClosed",
        callback = function(args)
            if not (args.data and args.data.bufnr) then
                return
            end
            local bufnr = args.data.bufnr
            require("mcp_companion.cc.session_commands").clear(bufnr)
            local instance = M._cli_instances[bufnr]
            if instance then
                M._cleanup_session_filter(instance)
                M._cli_instances[bufnr] = nil
            end
        end,
    })

    -- Clean up per-chat session state when a chat buffer is closed
    vim.api.nvim_create_autocmd("User", {
        pattern = "CodeCompanionChatClosed",
        callback = function(args)
            if args.data and args.data.bufnr then
                local bufnr = args.data.bufnr
                require("mcp_companion.cc.session_commands").clear(bufnr)
                -- Retrieve the chat object to get the combiner session ID stored on it.
                local chat
                local cc_ok, codecompanion = pcall(require, "codecompanion")
                if cc_ok then
                    chat = codecompanion.buf_get_chat(bufnr)
                end
                M._cleanup_session_filter(chat)
            end
        end,
    })

    log.info("CC extension initialized")
end

--- Auto-enable the in-process native `neovim` tool group in a new chat.
--- Independent of the combiner: native tools dispatch directly in Lua. ACP chats
--- are skipped — they receive `neovim_*` tools via the combiner injection instead.
--- @param event_data table Event data with bufnr and id
function M._auto_native_tools(event_data)
    if not event_data or not event_data.bufnr then
        return
    end

    local channel_ok, channel = pcall(require, "mcp_companion.native.channel")
    if not channel_ok or not channel.enabled() then
        return
    end

    local cc_ok, codecompanion = pcall(require, "codecompanion")
    if not cc_ok then
        return
    end

    local chat = codecompanion.buf_get_chat(event_data.bufnr)
    if not chat or not chat.tool_registry then
        return
    end

    -- ACP chats get neovim tools through the combiner (ToolProcessingMiddleware),
    -- not the in-process CC registry.
    if chat.adapter and chat.adapter.type == "acp" then
        return
    end

    local mcp_ok, cc_mcp = pcall(require, "codecompanion.mcp")
    if not mcp_ok then
        return
    end

    -- Ensure the native group exists in CC's MCP registry (idempotent; does not
    -- depend on the combiner being connected).
    pcall(function()
        require("mcp_companion.cc.tools").register_native()
    end)

    chat.tools:refresh({ adapter = chat.adapter })
    local group = cc_mcp.tool_prefix() .. "neovim"
    local ok_add = pcall(function()
        chat.tool_registry:add(group, { config = chat.tools.tools_config })
    end)
    if ok_add then
        log.info("CC: auto-enabled native neovim tool group")
    else
        log.debug("CC: failed to add native neovim group (%s)", group)
    end
end

--- Auto-enable MCP tool groups in a newly created chat.
--- Behaviour is controlled by config.cc.auto_http_tools:
---   true (default) — add the aggregate @mcp-combiner group (all servers, one entry)
---   false          — do not auto-add anything; user @-mentions groups manually
---   string[]       — add only the named per-server groups (e.g. {"github","filesystem"})
--- @param event_data table Event data with bufnr and id
function M._auto_http_tools(event_data)
    if not event_data or not event_data.bufnr then
        return
    end

    local state = require("mcp_companion.state")
    if state.get().combiner.status ~= "connected" then
        log.debug("CC: combiner not connected, skipping auto-enable")
        return
    end

    -- Get the chat instance via bufnr
    local cc_ok, codecompanion = pcall(require, "codecompanion")
    if not cc_ok then
        return
    end

    local chat = codecompanion.buf_get_chat(event_data.bufnr)
    if not chat or not chat.tool_registry then
        log.debug("CC: chat or tool_registry not found for bufnr %s", event_data.bufnr)
        return
    end

    -- Skip ACP chats entirely. Their combiner MCP server is injected into the
    -- ACP session via ACPSessionPre / transform_to_acp (using auto_acp_tools),
    -- so the CC tool_registry path here doesn't apply and must not consult
    -- auto_http_tools.
    local adapter_name = chat.adapter and chat.adapter.name
    if chat.adapter and chat.adapter.type == "acp" then
        log.debug(
            "CC: ACP chat (adapter=%s) — tool exposure handled via ACPSessionPre, skipping HTTP path",
            tostring(adapter_name)
        )
        return
    end

    -- Always set up a per-chat combiner client for HTTP-adapter chats.
    -- This must happen even when auto_http_tools=false so the combiner has a
    -- per-chat session for filtering and MCPStatus can show session state.
    M._setup_http_per_chat(chat)

    -- Resolve allowed servers (project file > cc.auto_http_tools).
    -- "http" is the correct kind here: ACP chats already returned above, so
    -- everything reaching this point is an HTTP-adapter chat.
    -- nil  → no filter (aggregate group)
    -- []   → no servers (skip registration)
    -- list → register named per-server groups
    local allowed = M._resolve_session_allowed("http", adapter_name)
    if type(allowed) == "table" and #allowed == 0 then
        log.debug("CC HTTP: auto_http_tools resolved to empty allow-list, skipping tool group registration")
        return
    end

    local mcp_ok, cc_mcp = pcall(require, "codecompanion.mcp")
    if not mcp_ok then
        return
    end

    chat.tools:refresh({ adapter = chat.adapter })

    if allowed == nil then
        -- No filter — aggregate combiner group covers all servers
        local combiner_group = cc_mcp.tool_prefix() .. "combiner"
        chat.tool_registry:add(combiner_group, { config = chat.tools.tools_config })
        log.info("CC: auto-enabled aggregate combiner tool group")
    else
        local enabled_count = 0
        for _, server_name in ipairs(allowed) do
            local group_name = cc_mcp.tool_prefix() .. server_name
            chat.tool_registry:add(group_name, { config = chat.tools.tools_config })
            enabled_count = enabled_count + 1
        end
        log.info("CC: auto-enabled %d named MCP server tool groups", enabled_count)
    end
end

--- Create and connect a per-chat MCP client for HTTP-adapter chats or CLI sessions.
--- Stores the client on chat._mcp_client and the token on chat._mcp_token.
--- The combiner-side filter is derived from auto_http_tools or auto_cli_tools
--- depending on adapter.type, so the combiner is the source of truth for which
--- servers are visible on this session's token.
--- @param chat table CC chat object or CLI instance (chat-shaped via the cli.create patch)
--- Inject the per-chat combiner URL into a CLI agent's launch.
---
--- Mints a token, binds it to this editor's Neovim instance, and temporarily
--- wraps the agent's exec as `env MCP_COMPANION_COMBINER_URL=<url> <cmd> <args…>`
--- so the spawned agent (and only it) inherits the tokened combiner URL. The agent
--- config's `${MCP_COMPANION_COMBINER_URL:-…}` URL then dials /mcp/<token>.
---
--- The agent table is mutated in place and MUST be restored via the returned
--- `restore()` immediately after the (synchronous) spawn.
--- @param create_args? { agent?: string }
--- @return { token: string, restore: fun() }|nil
function M._cli_inject_combiner_env(create_args)
    -- The token enables BOTH per-chat server filtering (the agent's own session
    -- carries it) and — when the native server is enabled — the neovim
    -- back-channel. So it's gated on the combiner being connected, not on native.
    local combiner = require("mcp_companion.combiner")
    if not combiner.client or not combiner.client.connected then
        return nil
    end

    local cc_ok, cc_config = pcall(require, "codecompanion.config")
    if not cc_ok then
        return nil
    end
    local agent_name = (create_args and create_args.agent) or cc_config.interactions.cli.agent
    local agents = cc_config.interactions.cli.agents or {}
    local agent = agents[agent_name]
    if not agent or not agent.cmd then
        return nil
    end

    -- Mint the token before the agent spawns/lists. Bind it to this editor's
    -- Neovim instance for the back-channel — only if the native server is enabled.
    local token = M._generate_token()
    pcall(function()
        local channel = require("mcp_companion.native.channel")
        if channel.enabled() then
            channel.sync()
            channel.bind(token)
        end
    end)

    local cfg = require("mcp_companion.config").get()
    local host = cfg.combiner.host or "127.0.0.1"
    local port = cfg.combiner.port or 9741
    local token_url = string.format("http://%s:%d/mcp/%s", host, port, token)

    -- Wrap as `env VAR=<url> <cmd> <args…>` — scoped to this spawn only.
    local orig_cmd, orig_args = agent.cmd, agent.args
    local wrapped = { "MCP_COMPANION_COMBINER_URL=" .. token_url, orig_cmd }
    for _, a in ipairs(orig_args or {}) do
        table.insert(wrapped, a)
    end
    agent.cmd = "env"
    agent.args = wrapped
    log.info("CC CLI: injected combiner env for agent=%s (token=%s)", tostring(agent_name), token)

    return {
        token = token,
        restore = function()
            agent.cmd = orig_cmd
            agent.args = orig_args
        end,
    }
end

--- Create a lightweight per-chat MCP client for an HTTP-adapter chat.
--- The chat's LLM runs in-process (CC is the host), so CC routes the chat's
--- tool calls through this client — giving the combiner a tokened session to
--- filter per chat. CLI and ACP agents are *separate processes* that carry
--- their own token (via env-URL / mcpServers injection), so they do NOT use
--- this — see the cli.create patch and ACPSessionPost.
--- @param chat table CC chat object (HTTP adapter)
function M._setup_http_per_chat(chat)
    if chat._mcp_client or chat._mcp_token then
        return -- already set up
    end

    local combiner = require("mcp_companion.combiner")
    if not combiner.client or not combiner.client.connected then
        log.debug("CC HTTP: combiner not connected, skipping per-chat client setup")
        return
    end

    local token = M._generate_token()

    -- Allowed-servers for the combiner-side filter. A .mcp-companion.json walked up
    -- from cwd overrides the global auto_http_tools setting.
    local adapter_name = chat.adapter and chat.adapter.name
    local allowed = M._resolve_session_allowed("http", adapter_name)

    chat._mcp_token = token
    chat._mcp_allowed_servers = allowed

    local per_chat_client = combiner.new_per_chat_client(token)
    chat._mcp_client = per_chat_client

    log.info("CC HTTP: connecting per-chat client (token=%s bufnr=%s)", token, tostring(chat.bufnr))

    per_chat_client:connect(function(ok, err)
        if ok then
            log.info("CC HTTP: per-chat client connected (token=%s)", token)
            M._apply_token_filter(chat)
        else
            log.warn("CC HTTP: per-chat client connect failed (token=%s): %s", token, tostring(err))
            -- Clear so we don't hold a broken client; tool calls fall back to singleton
            chat._mcp_client = nil
        end
    end)
end

--- Called on ChatAdapter event so combiner starts warming up while UI loads.
function M._start_combiner_async()
    local state = require("mcp_companion.state")
    local config = require("mcp_companion.config")

    -- Already connected, healthy, or connecting
    local combiner_status = state.get().combiner.status
    if combiner_status == "connected" or combiner_status == "connecting" or combiner_status == "healthy" then
        return
    end

    -- No combiner config
    if not config.get().combiner.config then
        log.debug("CC: no combiner config, skipping combiner start")
        return
    end

    log.info("CC: starting combiner async on ChatAdapter event")
    require("mcp_companion.combiner").start()
end

--- Wait for combiner to be fully connected (tools registered).
--- Used by ChatAdapter to ensure tools are available before first submit.
--- With parallel requests, the healthy→connected gap is ~200ms.
--- @param timeout_ms? number Maximum time to wait (default 5000)
--- @return boolean success Whether combiner is connected
function M._wait_for_combiner(timeout_ms)
    timeout_ms = timeout_ms or 5000
    local state = require("mcp_companion.state")

    local function is_connected()
        return state.get().combiner.status == "connected"
    end

    -- Already connected
    if is_connected() then
        return true
    end

    -- Not even started - start it now
    local s = state.get().combiner.status
    if s ~= "connecting" and s ~= "healthy" then
        M._start_combiner_async()
    end

    -- Wait for full connect (tools registered)
    local ok = vim.wait(timeout_ms, is_connected, 50)

    if ok then
        log.info("CC: combiner connected")
        -- Register tools synchronously so they're available on this tick.
        -- The combiner_ready event also triggers _register_all() via vim.schedule,
        -- but that runs on the next event loop tick — too late for the first
        -- chat submit.
        M._register_all()
    else
        log.warn("CC: combiner did not connect in %dms", timeout_ms)
    end

    return ok
end

--- Generate a random UUID v4 token for ACP session correlation.
--- @return string uuid
function M._generate_token()
    local t = {
        math.random(0, 0xffffffff), -- 32 bits
        math.random(0, 0xffff), -- 16 bits
        0x4000 + math.random(0, 0x0fff), -- version 4: 0x4xxx
        0x8000 + math.random(0, 0x3fff), -- variant: 10xx
        math.random(0, 0xffffffffffff), -- 48 bits
    }
    return string.format("%08x-%04x-%04x-%04x-%012x", t[1], t[2], t[3], t[4], t[5])
end

--- Apply server filter for a chat session via the token endpoint.
--- Works for both ACP and HTTP adapter chats. The combiner stores the filter
--- as pending if the remote client hasn't connected yet (ACP case), and
--- applies it immediately when the token is first seen.
--- @param chat table CC chat object with _mcp_token and _mcp_allowed_servers set
function M._apply_token_filter(chat)
    if not chat or not chat._mcp_token then
        return
    end

    local token = chat._mcp_token

    -- Bind this chat's token to our Neovim instance so the combiner can route
    -- `neovim_*` tool calls back here. Safe before combiner registration completes.
    pcall(function()
        require("mcp_companion.native.channel").bind(token)
    end)

    local allowed = chat._mcp_allowed_servers

    -- Nothing to do if no server filter is needed
    if allowed == nil then
        log.debug("CC: no filter, all servers enabled (token=%s)", token)
        return
    end

    local cfg = require("mcp_companion.config").get()
    local host = cfg.combiner.host or "127.0.0.1"
    local port = cfg.combiner.port or 9741
    local http = require("mcp_companion.http")
    local body = vim.json.encode({ allowed_servers = allowed })

    http.request({
        url = string.format("http://%s:%d/sessions/token/%s/filter", host, port, token),
        method = "post",
        headers = { ["Content-Type"] = "application/json" },
        body = body,
        timeout = 5000,
        callback = function(r)
            if r.status == 200 then
                local r_ok, r_data = pcall(vim.json.decode, r.body)
                local disabled_list = r_ok and r_data and r_data.disabled_servers or {}
                local pending = r_ok and r_data and r_data.pending
                log.info(
                    "CC: session filter %s (token=%s allowed=%s disabled=%s)",
                    pending and "stored as pending" or "applied",
                    token,
                    allowed and table.concat(allowed, ", ") or "all",
                    table.concat(disabled_list, ", ")
                )
                vim.schedule(function()
                    local sc_ok, sc = pcall(require, "mcp_companion.cc.session_commands")
                    if sc_ok and sc.set_session_state and chat.bufnr then
                        local disabled_map = {}
                        for _, name in ipairs(disabled_list) do
                            disabled_map[name] = true
                        end
                        sc.set_session_state(chat.bufnr, disabled_map)
                    end
                end)
            else
                log.warn("CC: session filter failed (status %s): %s", r.status, r.body or "")
            end
        end,
    })
end

--- Resolve the chat session's MCP token (works for both HTTP and ACP chats).
--- @param chat table CC chat object
--- @return string|nil token
local function _chat_token(chat)
    if not chat then
        return nil
    end
    if chat._mcp_token then
        return chat._mcp_token
    end
    if chat.adapter and chat.adapter._mcp_token then
        return chat.adapter._mcp_token
    end
    return nil
end

--- Snapshot the current session's disabled-server list and write a
--- ``.mcp-companion.json`` with the result.  Asynchronous: queries the combiner
--- for the authoritative filter state, then writes (or prompts for overwrite).
---
--- @param chat table CC chat object with _mcp_token set
--- @param format "shortest"|"allowed"|"disabled" Defaults to "shortest".
--- @param force boolean Skip the overwrite confirmation. Defaults to false.
--- @param done? fun(err: string|nil, result: table|nil) Optional completion callback.
function M._save_project_config(chat, format, force, done)
    done = done or function() end
    format = format or "shortest"

    local token = _chat_token(chat)
    if not token then
        local err = "no MCP session token on this chat — open a CodeCompanion chat first"
        done(err, nil)
        return
    end

    local cfg = require("mcp_companion.config").get()
    local host = cfg.combiner.host or "127.0.0.1"
    local port = cfg.combiner.port or 9741
    local http = require("mcp_companion.http")

    http.request({
        url = string.format("http://%s:%d/sessions/token/%s/filter", host, port, token),
        method = "get",
        timeout = 5000,
        callback = function(r)
            if r.status ~= 200 then
                done(string.format("combiner filter lookup failed (status %s): %s", r.status, r.body or ""), nil)
                return
            end
            local ok_decode, data = pcall(vim.json.decode, r.body)
            if not ok_decode or type(data) ~= "table" then
                done("combiner returned malformed JSON", nil)
                return
            end
            local disabled = data.disabled_servers or {}

            -- Compute the canonical known-server list (excluding the internal
            -- combiner pseudo-server, which is never user-toggleable).
            local state = require("mcp_companion.state")
            local servers = state.field("servers") or {}
            local known = {}
            for _, srv in ipairs(servers) do
                if srv.name and srv.name ~= "_combiner" then
                    table.insert(known, srv.name)
                end
            end

            local project = require("mcp_companion.project")
            vim.schedule(function()
                local result = project.save({
                    disabled = disabled,
                    known_servers = known,
                    format = format,
                    force = force,
                })
                done(nil, result)
            end)
        end,
    })
end

--- High-level wrapper: invoke ``_save_project_config`` with vim.ui.select-based
--- overwrite confirmation and vim.notify for the result.  Used by the user
--- command and slash command surfaces.
--- @param chat table CC chat object
--- @param format "shortest"|"allowed"|"disabled"
function M._save_project_config_interactive(chat, format)
    M._save_project_config(chat, format, false, function(err, result)
        if err then
            vim.notify("mcp-companion: save failed — " .. err, vim.log.levels.ERROR)
            return
        end
        if result.action == "unchanged" then
            vim.notify("mcp-companion: " .. result.path .. " already up to date", vim.log.levels.INFO)
            return
        end
        if result.action == "wrote" then
            vim.notify("mcp-companion: wrote " .. result.path, vim.log.levels.INFO)
            return
        end
        if result.action == "would_overwrite" then
            vim.ui.select({ "overwrite", "cancel" }, {
                prompt = string.format("Overwrite %s?", result.path),
            }, function(choice)
                if choice ~= "overwrite" then
                    vim.notify("mcp-companion: save cancelled", vim.log.levels.INFO)
                    return
                end
                M._save_project_config(chat, format, true, function(err2, r2)
                    if err2 then
                        vim.notify("mcp-companion: save failed — " .. err2, vim.log.levels.ERROR)
                    else
                        vim.notify("mcp-companion: wrote " .. r2.path, vim.log.levels.INFO)
                    end
                end)
            end)
        end
    end)
end

--- Resolve a session-bearing handle for ``bufnr``.  Returns a CodeCompanion
--- chat object if the buffer is a chat with a token; otherwise the tracked
--- CodeCompanionCLI instance for that bufnr; otherwise nil.  Both shapes
--- expose ``bufnr`` and ``_mcp_token``, which is all that session_commands
--- and the :MCPStatus UI need.
--- @param bufnr integer
--- @return table|nil handle Chat object or CLI instance
function M._handle_for_bufnr(bufnr)
    local cc_ok, codecompanion = pcall(require, "codecompanion")
    if cc_ok then
        local chat = codecompanion.buf_get_chat(bufnr)
        if chat and chat._mcp_token then
            return chat
        end
    end
    return M._cli_instances[bufnr]
end

--- Find the chat the save command should snapshot.
--- Preference order: chat in the current buffer, then any chat with an
--- MCP token (most-recently-created wins, since CC orders chats that way).
--- @return table|nil chat
function M._current_chat_for_save()
    local cc_ok, codecompanion = pcall(require, "codecompanion")
    if not cc_ok then
        return nil
    end

    local chat = codecompanion.buf_get_chat(vim.api.nvim_get_current_buf())
    if chat and _chat_token(chat) then
        return chat
    end

    local all = codecompanion.buf_get_chat()
    local fallback
    for _, entry in ipairs(all or {}) do
        local c = entry.chat
        if c and _chat_token(c) then
            fallback = c
        end
    end
    return fallback
end

--- Clean up session filter and per-chat client on chat close.
--- @param chat table|nil CC chat object (may be nil if chat already destroyed)
function M._cleanup_session_filter(chat)
    if not chat then
        return
    end

    -- Disconnect per-chat MCP client if present
    if chat._mcp_client then
        chat._mcp_client:disconnect()
        chat._mcp_client = nil
        log.debug("CC: per-chat client disconnected (bufnr=%s)", tostring(chat.bufnr))
    end

    if not chat._mcp_token then
        return
    end

    local token = chat._mcp_token
    chat._mcp_token = nil
    chat._mcp_allowed_servers = nil

    -- Unbind the token from our Neovim instance on the combiner.
    pcall(function()
        require("mcp_companion.native.channel").unbind(token)
    end)

    local cfg = require("mcp_companion.config").get()
    local host = cfg.combiner.host or "127.0.0.1"
    local port = cfg.combiner.port or 9741
    local http = require("mcp_companion.http")

    http.request({
        url = string.format("http://%s:%d/sessions/token/%s/filter", host, port, token),
        method = "delete",
        timeout = 3000,
        callback = function(r)
            log.debug("CC: session filter removed (token=%s status=%s)", token, r.status)
        end,
    })
end

function M._register_all()
    M._register_tools()
    M._register_editor_context()
    M._register_prompts()
end

function M._register_tools()
    local ok, tools = pcall(require, "mcp_companion.cc.tools")
    if ok then
        -- Native (in-process) servers register independently of the combiner.
        pcall(tools.register_native)
        tools.register()
    else
        log.warn("Failed to load cc.tools: %s", tostring(tools))
    end
end

function M._register_editor_context()
    local ok, editor_ctx = pcall(require, "mcp_companion.cc.editor_context")
    if ok then
        editor_ctx.register()
    else
        log.warn("Failed to load cc.editor_context: %s", tostring(editor_ctx))
    end
end

function M._register_prompts()
    local ok, cmds = pcall(require, "mcp_companion.cc.slash_commands")
    if ok then
        cmds.register()
    else
        log.warn("Failed to load cc.slash_commands: %s", tostring(cmds))
    end
end

--- Extension exports (accessible via CodeCompanion.extensions.mcp_companion)
M.exports = {
    --- Get current plugin state
    status = function()
        return require("mcp_companion.state").get()
    end,

    --- Get combiner client (for direct MCP calls if needed)
    client = function()
        local combiner = require("mcp_companion.combiner")
        return combiner.client
    end,

    --- Force refresh all capabilities
    refresh = function()
        local combiner = require("mcp_companion.combiner")
        local client = combiner.client
        if client and client.connected then
            client:refresh_capabilities()
        end
    end,
}

return M
