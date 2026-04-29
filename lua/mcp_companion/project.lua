--- mcp-companion.nvim — Project-directory configuration
---
--- Walks up from a starting directory looking for ``.mcp-companion.json`` and
--- returns its parsed contents.  The file controls per-project visibility of
--- MCP servers — independent of the global ``cc.auto_http_tools`` /
--- ``cc.auto_acp_tools`` settings.  Schema:
---
---     {
---       "$schema": "https://geohar.github.io/mcp-companion/project.schema.json",
---       "allowed_servers": ["github", "gws"]   // mutually exclusive with disabled_servers
---     }
---
--- The discovery is stateless: every chat-session-init re-reads the file so
--- changes take effect without reloading Neovim.
---
--- @module mcp_companion.project

local M = {}

local PROJECT_FILE = ".mcp-companion.json"

local log = require("mcp_companion.log")

--- Walk upward from *start_dir* looking for ``.mcp-companion.json``.
--- Returns the directory containing the file, or nil if none is found before
--- the filesystem root.
--- @param start_dir? string Defaults to vim.fn.getcwd().
--- @return string|nil root_dir
--- @return string|nil file_path
function M.find_root(start_dir)
    local dir = start_dir or vim.fn.getcwd()
    if not dir or dir == "" then
        return nil, nil
    end

    -- vim.fs.find walks upward when given upward=true; cap at first hit.
    local hits = vim.fs.find(PROJECT_FILE, {
        upward = true,
        type = "file",
        path = dir,
        limit = 1,
    })
    local file = hits[1]
    if not file then
        return nil, nil
    end
    return vim.fs.dirname(file), file
end

--- Read and parse the project file at *file_path*.
--- Returns the parsed table on success, or (nil, error_string) on any failure.
--- @param file_path string
--- @return table|nil
--- @return string|nil error
local function read_and_parse(file_path)
    local fd = io.open(file_path, "r")
    if not fd then
        return nil, "could not open " .. file_path
    end
    local content = fd:read("*a")
    fd:close()
    if not content or content == "" then
        return nil, file_path .. " is empty"
    end

    local ok, decoded = pcall(vim.json.decode, content)
    if not ok then
        return nil, "invalid JSON in " .. file_path .. ": " .. tostring(decoded)
    end
    if type(decoded) ~= "table" then
        return nil, file_path .. " must contain a JSON object at the top level"
    end
    return decoded, nil
end

--- Validate the parsed project config against the schema.
--- Returns the validated config or (nil, error).  Unknown server names are
--- *not* checked here — that's done at apply time against the live server list.
--- @param raw table
--- @return table|nil config
--- @return string|nil error
local function validate(raw)
    local allowed = raw.allowed_servers
    local disabled = raw.disabled_servers

    if allowed ~= nil and disabled ~= nil then
        return nil, "allowed_servers and disabled_servers are mutually exclusive"
    end

    local function check_string_list(value, field)
        if type(value) ~= "table" then
            return field .. " must be an array of strings"
        end
        for i, v in ipairs(value) do
            if type(v) ~= "string" or v == "" then
                return field .. "[" .. i .. "] must be a non-empty string"
            end
        end
        return nil
    end

    if allowed ~= nil then
        local err = check_string_list(allowed, "allowed_servers")
        if err then return nil, err end
    end
    if disabled ~= nil then
        local err = check_string_list(disabled, "disabled_servers")
        if err then return nil, err end
    end

    local tool_system_prompts = raw.tool_system_prompts
    if tool_system_prompts ~= nil and type(tool_system_prompts) ~= "boolean" then
        return nil, "tool_system_prompts must be a boolean"
    end

    return {
        allowed_servers = allowed,
        disabled_servers = disabled,
        tool_system_prompts = tool_system_prompts,
    }, nil
end

--- Discover and load the project config, if any.
--- Returns the validated config table or nil if no project file is in scope.
--- Errors (parse, validation) are logged at warn level and surfaced as nil so
--- callers fall through to their global defaults.
--- @param start_dir? string Defaults to vim.fn.getcwd().
--- @return table|nil config
--- @return string|nil root_dir
function M.resolve(start_dir)
    local root, file = M.find_root(start_dir)
    if not root or not file then
        return nil, nil
    end

    local raw, parse_err = read_and_parse(file)
    if not raw then
        log.warn("project config: %s", parse_err)
        return nil, root
    end

    local cfg, validate_err = validate(raw)
    if not cfg then
        log.warn("project config (%s): %s", file, validate_err)
        return nil, root
    end

    return cfg, root
end

--- Decide the per-session allowed-servers list.
--- Project file wins; otherwise fall back to ``auto_*_tools`` from cc config.
---
--- Returns one of:
---   * ``nil``       — no filter, expose all servers (the bridge's default).
---   * ``string[]``  — allow-list; only the named servers are visible.
---
--- The ``disabled_servers`` form in the project file is converted to an
--- allow-list against the supplied ``known_servers``.  Unknown names in either
--- form are dropped with a warning so a stale project file doesn't 400 the
--- bridge filter endpoint.
---
--- @param auto_value boolean|string[]|nil The cc.auto_*_tools config value.
--- @param known_servers? string[] All server names known to the bridge.
--- @param start_dir? string Defaults to vim.fn.getcwd().
--- @return string[]|nil allowed
function M.resolve_allowed(auto_value, known_servers, start_dir)
    local project_cfg = M.resolve(start_dir)
    if project_cfg then
        return M._apply_project_to_allowed(project_cfg, known_servers)
    end

    if auto_value == false then
        return {}
    elseif type(auto_value) == "table" then
        return auto_value
    end
    -- auto_value == true (or nil) → no filter
    return nil
end

--- @param project_cfg table
--- @param known_servers? string[]
--- @return string[]|nil
function M._apply_project_to_allowed(project_cfg, known_servers)
    local known = {}
    if known_servers then
        for _, name in ipairs(known_servers) do
            known[name] = true
        end
    end

    local function keep_known(list, field)
        local out = {}
        local dropped = {}
        for _, name in ipairs(list) do
            if next(known) == nil or known[name] then
                table.insert(out, name)
            else
                table.insert(dropped, name)
            end
        end
        if #dropped > 0 then
            log.warn(
                "project config: %s contains unknown server(s): %s — dropping",
                field,
                table.concat(dropped, ", ")
            )
        end
        return out
    end

    if project_cfg.allowed_servers then
        return keep_known(project_cfg.allowed_servers, "allowed_servers")
    end

    if project_cfg.disabled_servers then
        if next(known) == nil then
            -- Without a known-server list we can't invert disabled→allowed.
            -- Bail out and let the global default take effect.
            log.warn(
                "project config: disabled_servers requires the bridge to be ready; falling back to global default"
            )
            return nil
        end
        local disabled = {}
        for _, name in ipairs(keep_known(project_cfg.disabled_servers, "disabled_servers")) do
            disabled[name] = true
        end
        local out = {}
        for _, name in ipairs(known_servers) do
            if not disabled[name] then
                table.insert(out, name)
            end
        end
        return out
    end

    return nil
end

-- ---------------------------------------------------------------------------
-- Saving a project file from current session state.
-- ---------------------------------------------------------------------------

local SCHEMA_URL = "https://geohar.github.io/mcp-companion/project.schema.json"

--- Convert a disabled-servers value (list or set) to a sorted name list.
--- @param disabled string[]|table<string, boolean>
--- @return string[]
local function as_sorted_list(disabled)
    local out = {}
    if disabled[1] ~= nil then
        for _, v in ipairs(disabled) do table.insert(out, v) end
    else
        for k, v in pairs(disabled) do
            if v then table.insert(out, k) end
        end
    end
    table.sort(out)
    return out
end

--- Choose the payload shape to write.
--- @param disabled string[]|table<string,boolean> Currently-disabled servers.
--- @param known_servers string[] All servers known to the bridge (excluding _bridge).
--- @param format "shortest"|"allowed"|"disabled" Defaults to "shortest".
--- @return table payload Ready for JSON encoding.
function M.format_payload(disabled, known_servers, format)
    format = format or "shortest"
    local disabled_list = as_sorted_list(disabled)
    local disabled_set = {}
    for _, name in ipairs(disabled_list) do disabled_set[name] = true end

    local allowed_list = {}
    for _, name in ipairs(known_servers or {}) do
        if not disabled_set[name] then table.insert(allowed_list, name) end
    end
    table.sort(allowed_list)

    local pick
    if format == "allowed" then
        pick = "allowed"
    elseif format == "disabled" then
        pick = "disabled"
    else
        -- Shortest list wins.  Tie → allowed (matches the documented
        -- "default off, opt in per project" workflow).
        if #disabled_list < #allowed_list then
            pick = "disabled"
        else
            pick = "allowed"
        end
    end

    if pick == "allowed" then
        return { allowed_servers = allowed_list }
    end
    return { disabled_servers = disabled_list }
end

--- Render *payload* as a pretty-printed JSON document with the schema URL.
--- @param payload table { allowed_servers = ... } or { disabled_servers = ... }
--- @return string json
function M.encode(payload)
    local field, items
    if payload.allowed_servers ~= nil then
        field, items = "allowed_servers", payload.allowed_servers
    elseif payload.disabled_servers ~= nil then
        field, items = "disabled_servers", payload.disabled_servers
    else
        error("project.encode: payload missing allowed_servers/disabled_servers")
    end

    local lines = {
        "{",
        '    "$schema": ' .. vim.json.encode(SCHEMA_URL) .. ",",
    }
    if #items == 0 then
        table.insert(lines, "    " .. vim.json.encode(field) .. ": []")
    else
        table.insert(lines, "    " .. vim.json.encode(field) .. ": [")
        for i, name in ipairs(items) do
            local sep = (i < #items) and "," or ""
            table.insert(lines, "        " .. vim.json.encode(name) .. sep)
        end
        table.insert(lines, "    ]")
    end
    table.insert(lines, "}")
    return table.concat(lines, "\n") .. "\n"
end

--- Resolve the path the project file should be saved to.
--- An existing ``.mcp-companion.json`` walked up from *start_dir* wins so that
--- a save call updates the same file the chat is already reading.  Otherwise
--- the file is created at *start_dir*/.mcp-companion.json.
--- @param start_dir? string Defaults to vim.fn.getcwd().
--- @return string path
function M.save_target_path(start_dir)
    start_dir = start_dir or vim.fn.getcwd()
    local _, file = M.find_root(start_dir)
    if file then return file end
    return start_dir .. "/" .. PROJECT_FILE
end

--- Resolve the effective ``tool_system_prompts`` value.
---
--- Priority order (project file > plugin config > built-in default):
---   1. ``.mcp-companion.json`` walked up from cwd, if it sets the field.
---   2. The ``global_value`` argument (``cfg.cc.tool_system_prompts``).
---   3. ``true`` (the built-in default).
---
--- Use this from anywhere that previously read ``cfg.cc.tool_system_prompts``.
---
--- @param global_value boolean|nil The cc.tool_system_prompts plugin setting.
--- @param start_dir? string Defaults to vim.fn.getcwd().
--- @return boolean
function M.resolve_tool_system_prompts(global_value, start_dir)
    local cfg = M.resolve(start_dir)
    if cfg and cfg.tool_system_prompts ~= nil then
        return cfg.tool_system_prompts
    end
    if global_value ~= nil then
        return global_value
    end
    return true
end

--- Compute the set of servers currently *hidden* by a project file.
---
--- ``allowed_servers``  → hidden = known_servers \ allowed
--- ``disabled_servers`` → hidden = the listed names
--- nil project_cfg      → hidden = empty (no file = all visible)
---
--- @param project_cfg table|nil Result of M.resolve(); nil if no file in scope.
--- @param known_servers string[]
--- @return table<string, boolean>
function M.project_disabled_set(project_cfg, known_servers)
    local out = {}
    if not project_cfg then return out end
    if project_cfg.disabled_servers then
        for _, name in ipairs(project_cfg.disabled_servers) do
            out[name] = true
        end
        return out
    end
    if project_cfg.allowed_servers then
        local allowed = {}
        for _, name in ipairs(project_cfg.allowed_servers) do
            allowed[name] = true
        end
        for _, name in ipairs(known_servers or {}) do
            if not allowed[name] then out[name] = true end
        end
    end
    return out
end

--- Toggle *server_name*'s presence in the project file.
---
--- Reads the current project file (if any), flips the server's visibility,
--- and writes back.  The output keeps the same shape as the existing file
--- (``allowed_servers`` or ``disabled_servers``); when no file exists yet, the
--- new file is rendered in the shortest form via ``format = "shortest"``.
---
--- @param server_name string
--- @param known_servers string[] All servers known to the bridge (without _bridge).
--- @param start_dir? string Defaults to vim.fn.getcwd().
--- @return table result {
---   action = "wrote"|"unchanged",
---   path = string,
---   payload = table,
---   was_visible = boolean,    -- visibility BEFORE the toggle
---   now_visible = boolean,    -- visibility AFTER the toggle
--- }
function M.toggle_in_project_file(server_name, known_servers, start_dir)
    assert(type(server_name) == "string" and server_name ~= "",
        "project.toggle_in_project_file: server_name required")

    local project_cfg, _root = M.resolve(start_dir)
    local disabled = M.project_disabled_set(project_cfg, known_servers)

    local was_visible = not disabled[server_name]
    if disabled[server_name] then
        disabled[server_name] = nil
    else
        disabled[server_name] = true
    end
    local now_visible = not disabled[server_name]

    -- Preserve the existing file's shape; default to "shortest" when creating.
    local format = "shortest"
    if project_cfg then
        if project_cfg.allowed_servers then
            format = "allowed"
        elseif project_cfg.disabled_servers then
            format = "disabled"
        end
    end

    local result = M.save({
        disabled = disabled,
        known_servers = known_servers,
        format = format,
        path = M.save_target_path(start_dir),
        force = true,
    })
    result.was_visible = was_visible
    result.now_visible = now_visible
    return result
end

--- Persist *payload* to the project file.
---
--- Behaviour:
---   * File missing            → write, return action="wrote".
---   * File exists, same bytes → no-op,  return action="unchanged".
---   * File exists, different  → if opts.force → overwrite (action="wrote"),
---                               else action="would_overwrite" so the caller
---                               can prompt the user before retrying with
---                               force=true.
---
--- @param opts table {
---   disabled       = string[]|table<string,boolean>,
---   known_servers  = string[],
---   format         = "shortest"|"allowed"|"disabled" (default "shortest"),
---   path           = string (optional, override discovery),
---   force          = boolean (default false),
--- }
--- @return table result {
---   action = "wrote"|"unchanged"|"would_overwrite",
---   path = string,
---   payload = table,
---   existing = string|nil,
---   contents = string,
--- }
function M.save(opts)
    assert(type(opts) == "table", "project.save: opts table required")
    assert(opts.known_servers, "project.save: known_servers required")

    local payload = M.format_payload(opts.disabled or {}, opts.known_servers, opts.format)
    local contents = M.encode(payload)
    local path = opts.path or M.save_target_path()

    local fd = io.open(path, "r")
    local existing
    if fd then
        existing = fd:read("*a")
        fd:close()
    end

    if existing == contents then
        return { action = "unchanged", path = path, payload = payload, contents = contents }
    end

    if existing ~= nil and not opts.force then
        return {
            action = "would_overwrite",
            path = path,
            payload = payload,
            existing = existing,
            contents = contents,
        }
    end

    local parent = vim.fs.dirname(path)
    if parent and parent ~= "" then
        vim.fn.mkdir(parent, "p")
    end

    local out = assert(io.open(path, "w"),
        "project.save: could not open " .. path .. " for writing")
    out:write(contents)
    out:close()

    return { action = "wrote", path = path, payload = payload, contents = contents }
end

return M
