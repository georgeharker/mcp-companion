--- mcp-companion.nvim — Configuration
--- @module mcp_companion.config

local M = {}

--- @class MCPCompanion.CombinerConfig
--- @field config? string Path to servers.json (auto-detected if nil)
--- @field port number Default 9741
--- @field host string Default "127.0.0.1"
--- @field idle_timeout string Default "30m" (sharedserver grace period)
--- @field python_cmd string Default "python3"
--- @field startup_timeout number Seconds to wait for combiner health. Default 30.
--- @field request_timeout number Default timeout for MCP requests in seconds. Default 60.
--- @field token_key? string Encryption key for OAuth token storage (or set MCP_COMBINER_TOKEN_KEY env var)
--- @field input_validation? boolean Tri-state JSON-schema validation of tool *input* arguments at the
---   combiner proxy. nil (default): leave the combiner default (off — inputs are coerced, not strictly
---   validated). true: force strict input validation on. false: force it off. Passed as
---   ``--input-validation`` / ``--no-input-validation``; omitted when nil.
--- @field output_validation? boolean Tri-state JSON-schema validation of tool *output* at the combiner proxy.
---   nil (default): leave the combiner default (on for tools that declare an outputSchema). false: force it
---   off — the upstream server already validated its structured output, so re-validating here is redundant
---   per-call work (measurably slow for large responses). true: force it on. Passed as
---   ``--output-validation`` / ``--no-output-validation``; omitted when nil.
--- @field log? MCPCompanion.CombinerLogConfig Combiner logging — same shape as the
---   top-level ``log`` table.  ``{ level = "info", file = true }`` by default.
---   ``file = true`` resolves to ``stdpath("log")/mcp-combiner-py.log``;
---   ``file = "<path>"`` writes to that path; ``file = false`` disables file
---   logging.  ``level = "debug"`` also turns on the upstream httpx /
---   mcp.client.auth / fastmcp.client.auth loggers.

--- @class MCPCompanion.CombinerLogConfig
--- @field level? "trace"|"debug"|"info"|"warn"|"error" Default "info".
--- @field file? boolean|string Default true (= default path).
--- @field token_in_url? boolean For HTTP ACP agents, also embed the session token in the URL path (/mcp/<token>) in addition to the X-MCP-Combiner-Session header. Default false (header-only; cleaner URLs, per ACP spec, relies on the agent forwarding the header). Set true for belt-and-braces — robust for any client, including ACP agents that don't forward custom HTTP headers. The stdio/mcp-remote fallback always uses the URL regardless of this flag.
---   If tools fail in a specific agent, try enabling this and please report at
---   https://github.com/georgeharker/mcp-companion/issues with the agent name.

--- @class MCPCompanion.CCAdapterConfig
--- @field auto_http_tools? boolean|string[] Per-adapter override for auto_http_tools (same semantics).
--- @field auto_acp_tools? boolean|string[] Per-adapter override for auto_acp_tools (same semantics).
--- @field auto_cli_tools? boolean|string[] Per-adapter override for auto_cli_tools (same semantics).

--- @class MCPCompanion.CCConfig
--- @field auto_http_tools boolean|string[] Controls which MCP tool groups are added to new chats.
---   true (default): add the aggregate @mcp-combiner group (all servers, one context block entry).
---   false: register tools but do not auto-add; user manually @-mentions groups in each chat.
---   string[]: add only the named server groups (e.g. {"github","filesystem"}).
---   Overridden per-project by .mcp-companion.json (see mcp_companion.project).
--- @field auto_acp_tools boolean|string[] Whether to inject the combiner as an MCP server into ACP sessions.
---   true (default): combiner is offered to ACP agents; all MCP servers visible.
---   false: combiner is not injected; ACP agents have no MCP tools from this plugin.
---   string[]: combiner is injected but only the named servers are visible (e.g. {"github","filesystem"}).
---   Overridden per-project by .mcp-companion.json (see mcp_companion.project).
--- @field auto_cli_tools boolean|string[] Per-session server filter applied to CodeCompanion CLI agents
---   (codecompanion.interactions.cli).  The CLI agent connects back to the combiner via its own MCP
---   client config; this setting controls which servers the combiner exposes on the per-session token.
---   true (default): all MCP servers visible to the CLI session.
---   false: no servers visible (per-token filter is set to empty).
---   string[]: only the named servers visible.
---   Overridden per-project by .mcp-companion.json (see mcp_companion.project).
--- @field tool_system_prompts boolean Whether to add per-tool natural-language system messages alongside
---   the tools array. Default true: helps models that ignore JSON-Schema descriptions.  Set false to save
---   tokens (descriptions duplicate the schema's `description` fields, ~one extra system message per tool).
---   Overridden per-project by .mcp-companion.json (see mcp_companion.project).
--- @field normalize_schema boolean Whether the combiner normalizes tool JSON schemas globally.
---   Fixes schemas where ``type`` and ``anyOf`` coexist at the same level, which strict
---   providers (e.g. moonshot-ai/kimi) reject with a 400 error.  The transformation is
---   semantically equivalent and accepted by lenient validators too.  Passed to the
---   combiner as ``--normalize-schema`` so every ``tools/list`` response is normalized at
---   cache-fill time.  Default false.
--- @field adapters? table<string, MCPCompanion.CCAdapterConfig> Per-adapter overrides for auto_http_tools
---   and auto_acp_tools.  Keys are adapter names as returned by chat.adapter.name (e.g. "moonshot-ai",
---   "claude", "openai").  Values override the corresponding top-level setting for sessions using that
---   adapter.  Further overridden per-project by .mcp-companion.json#/adapters/<name>.
---   Example: { ["moonshot-ai"] = { auto_http_tools = {"github"} } }

--- @class MCPCompanion.Config
--- @field combiner MCPCompanion.CombinerConfig
--- @field cc MCPCompanion.CCConfig
--- @field native_servers table<string, {enabled: boolean, expose_exec?: boolean, auto_approve?: boolean|string[]|fun(tool_name: string, server_name: string, tool_ctx: table): boolean}>
--- @field auto_approve boolean|fun(tool_name: string, server_name: string, tool_ctx: table): boolean
--- @field system_prompt_resources? boolean|string[] Resource name patterns to inject into system prompt
--- @field ui {enabled: boolean, width: number, height: number, border: string}
--- @field log {level: string, file: boolean}
--- @field global_env table<string, string>
--- @field on_ready? fun(combiner: table)
--- @field on_error? fun(err: string)

--- @type MCPCompanion.Config
M.defaults = {
  combiner = {
    config = nil,
    port = 9741,
    host = "127.0.0.1",
    idle_timeout = "30m",
    python_cmd = "python3",
    -- Optional venv to install/run the combiner from. Unset (default): a
    -- plugin-local venv (<plugin>/combiner/.venv) is created and used —
    -- self-contained. Set a shared venv to let other clients reuse the install
    -- (put its bin/ on PATH so the claude-mcp-combiner plugin finds `mcp-combiner`).
    -- A user-set venv must already exist — the plugin only `uv pip install`s
    -- into it (additive) and will never `uv venv` (wipe) a venv it doesn't own:
    --   venv = "~/.venv"
    -- Either way the combiner is ensured-installed on start via uv, unless
    -- `python_cmd` is a custom path.
    venv = nil,
    startup_timeout = 30,
    request_timeout = 60,
    token_key = nil,
    log = {
      level = "info",
      file = true,    -- true = default path, string = path, false = disabled
    },
    -- HTTP ACP agents: also put the token in the URL path, not just the header.
    -- false = header-only (cleaner; the default); true = belt-and-braces (robust).
    -- The stdio/mcp-remote fallback always uses the URL regardless of this flag.
    token_in_url = false,
    -- Tri-state control of the combiner's JSON-schema (re)validation of proxied
    -- tool calls. nil (default) leaves the combiner default; false forces off;
    -- true forces on. The upstream server already validates, so the meaningful
    -- win is output_validation = false, which removes per-call output validation
    -- (measurably slow for large structured responses). Listed here as nil for
    -- documentation — a nil entry is absent from the table, so no flag is passed.
    input_validation = nil,
    output_validation = nil,
  },

  native_servers = {
    neovim = {
      enabled = true,
      -- expose_exec = false,  -- include the exec tier (run_command/exec_lua)
      -- Auto-approve spec — same style as a proxied server's `autoApprove`:
      -- a list of tool-name globs, plus `tier:<read|navigate|write|exec>` alias
      -- tokens. `true` = approve all, `false`/`{}` = prompt for all, or a
      -- function(tool_name, server_name, ctx) -> boolean. Applies to in-process
      -- CodeCompanion chats (external ACP/CLI agents use their host's approval).
      auto_approve = { "tier:read", "tier:navigate" },
      -- Window/buffer placement for navigate/display tools (open_file,
      -- goto_diagnostic, set_cursor, …). These act on a *code* window — the
      -- first normal (non-chat/tree/terminal/float) window in the current
      -- tabpage — never the focused window, which is often a CodeCompanion chat.
      window = {
        -- Filetypes/buftypes that are NOT a code window (chat, file trees, etc).
        ignore_filetypes = {
          "codecompanion", "neo-tree", "NvimTree", "aerial", "Outline",
          "trouble", "qf", "help", "TelescopePrompt", "neotest-summary",
          "dap-repl", "dapui_watches", "dapui_stacks", "dapui_breakpoints",
          "dapui_scopes", "dapui_console", "mcp-companion",
        },
        ignore_buftypes = { "nofile", "prompt", "terminal", "quickfix", "help" },
        -- Where open_file puts a file when there's no code window to reuse:
        -- "tab" | "split" | "vsplit" | "replace".
        no_code_window = "tab",
        -- Where focus lands after open/navigate: "file" | "chat" (stay put).
        focus = "file",
        -- Reuse a window already showing the target file instead of re-opening.
        reuse_visible = true,
      },
    },
  },

  cc = {
    auto_http_tools = true,
    auto_acp_tools = true,
    auto_cli_tools = true,
    tool_system_prompts = true,
    normalize_schema = false,
    adapters = {},
  },

  auto_approve = false,

  system_prompt_resources = nil,

  ui = {
    enabled = true,
    width = 0.8,
    height = 0.7,
    border = "rounded",
  },

  log = {
    level = "warn",
    file = true,
  },

  global_env = {},

  on_ready = nil,
  on_error = nil,
}

--- @type MCPCompanion.Config|nil
local _config = nil

--- Resolve the plugin's own directory (for locating combiner/ subdir)
--- @return string|nil plugin_dir
local function _plugin_dir()
  -- Works in lazy.nvim, packer, or manual runtimepath
  local info = debug.getinfo(1, "S")
  if info and info.source and info.source:sub(1, 1) == "@" then
    -- source is @/path/to/lua/mcp_companion/config.lua
    local src = info.source:sub(2)
    return vim.fn.fnamemodify(src, ":h:h:h") -- up to plugin root
  end
  return nil
end

--- Resolve combiner python command.
--- Priority: explicit user path → configured `venv` (if the combiner is installed
--- there) → plugin-local `combiner/.venv` (legacy build step) → `python3`.
--- @param user_cmd? string User-specified python command
--- @param venv? string Configured venv (config.combiner.venv)
--- @return string python_cmd
local function _resolve_python_cmd(user_cmd, venv)
  if user_cmd and user_cmd ~= "python3" then
    return user_cmd -- user explicitly set a custom path
  end

  -- Configured venv, only if the combiner is actually installed there (the
  -- `mcp-combiner` console script is the install marker). install.ensure() puts
  -- it there on start; until then we fall through to the plugin-local venv.
  if venv and venv ~= "" then
    local vpath = vim.fn.expand(venv)
    if vim.fn.executable(vpath .. "/bin/mcp-combiner") == 1 then
      return vpath .. "/bin/python"
    end
  end

  -- Look for plugin-local venv (created by a build step)
  local root = _plugin_dir()
  if root then
    local venv_python = root .. "/combiner/.venv/bin/python"
    if vim.fn.executable(venv_python) == 1 then
      return venv_python
    end
  end

  return user_cmd or "python3"
end

--- Config search paths for servers.json
--- @return string[] candidates
local function _config_candidates()
  local cwd = vim.fn.getcwd()
  return {
    cwd .. "/.mcphub/servers.json",
    cwd .. "/servers.json",
    cwd .. "/.mcp/servers.json",
    cwd .. "/.mcp.json",
    vim.fn.stdpath("config") .. "/mcphub/servers.json",
    vim.fn.stdpath("data") .. "/mcp-companion/servers.json",
  }
end

--- Validate config and return list of issues
--- @param cfg MCPCompanion.Config
--- @return string[] issues
local function _validate(cfg)
  local issues = {}

  -- Port range
  if cfg.combiner.port < 1024 or cfg.combiner.port > 65535 then
    table.insert(issues, string.format("combiner.port %d out of range (1024-65535)", cfg.combiner.port))
  end

  -- Config file exists (if specified)
  if cfg.combiner.config and vim.fn.filereadable(cfg.combiner.config) ~= 1 then
    table.insert(issues, string.format("combiner.config file not found: %s", cfg.combiner.config))
  end

  -- auto_approve type
  if cfg.auto_approve ~= nil and type(cfg.auto_approve) ~= "boolean" and type(cfg.auto_approve) ~= "function" then
    table.insert(issues, "auto_approve must be boolean or function")
  end

  -- startup_timeout
  if cfg.combiner.startup_timeout < 1 then
    table.insert(issues, "combiner.startup_timeout must be >= 1")
  end

  if cfg.combiner.log and cfg.combiner.log.level ~= nil then
    local valid = { trace = true, debug = true, info = true, warn = true, error = true }
    if not valid[cfg.combiner.log.level] then
      table.insert(issues, string.format(
        "combiner.log.level %q must be one of: trace, debug, info, warn, error",
        tostring(cfg.combiner.log.level)
      ))
    end
  end
  if cfg.combiner.log and cfg.combiner.log.file ~= nil then
    local t = type(cfg.combiner.log.file)
    if t ~= "boolean" and t ~= "string" then
      table.insert(issues, "combiner.log.file must be a boolean or a string path")
    end
  end

  return issues
end

--- Merge user opts with defaults, auto-detect config path, validate
--- @param opts table
--- @return string[] issues Validation issues (empty if valid)
function M.setup(opts)
  opts = opts or {}

  -- Deprecation: the `combiner` config key was renamed to `combiner`.
  if opts.combiner ~= nil and opts.combiner == nil then
    vim.notify(
      "[mcp-companion] the `combiner` config key was renamed to `combiner` — "
        .. "rename it in your setup() call (e.g. `combiner = { … }`).",
      vim.log.levels.WARN
    )
    opts.combiner = opts.combiner
    opts.combiner = nil
  end

  _config = vim.tbl_deep_extend("force", {}, M.defaults, opts)

  -- Did the user pin a custom python? (before we auto-resolve it to a venv path)
  local user_py = opts.combiner and opts.combiner.python_cmd
  _config.combiner._custom_python = user_py ~= nil and user_py ~= "python3"

  -- Resolve python command (prefer configured venv, then plugin-local venv)
  _config.combiner.python_cmd = _resolve_python_cmd(_config.combiner.python_cmd, _config.combiner.venv)

  -- Auto-detect config path if not set
  if not _config.combiner.config then
    for _, path in ipairs(_config_candidates()) do
      if vim.fn.filereadable(path) == 1 then
        _config.combiner.config = path
        break
      end
    end
  end

  -- Resolve combiner.log.file: true → default path; string → kept; false → disabled.
  _config.combiner.log = _config.combiner.log or {}
  if _config.combiner.log.file == nil or _config.combiner.log.file == true then
    _config.combiner.log.file = vim.fn.stdpath("log") .. "/mcp-combiner-py.log"
  end
  if _config.combiner.log.level == nil then
    _config.combiner.log.level = "info"
  end

  return _validate(_config)
end

--- Get current config (returns defaults if setup not called)
--- @return MCPCompanion.Config
function M.get()
  return _config or M.defaults
end

--- Get the resolved combiner URL
--- @return string
function M.combiner_url()
  local cfg = M.get()
  return string.format("http://%s:%d", cfg.combiner.host, cfg.combiner.port)
end

--- Plugin root directory (contains lua/ and combiner/).
--- @return string|nil
function M.plugin_dir()
  return _plugin_dir()
end

--- Re-resolve combiner.python_cmd (e.g. after install.ensure() populates the venv).
--- @return string python_cmd
function M.refresh_python_cmd()
  if not _config then return "python3" end
  -- Re-run from the *original* intent: keep a user-pinned python; otherwise
  -- re-resolve from scratch ("python3") so a freshly-installed venv is picked.
  local user = _config.combiner._custom_python and _config.combiner.python_cmd or "python3"
  _config.combiner.python_cmd = _resolve_python_cmd(user, _config.combiner.venv)
  return _config.combiner.python_cmd
end

return M
