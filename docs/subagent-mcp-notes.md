# MCP in pi subagents — investigation (RESOLVED)

**Status:** resolved. The original hypothesis below (MCP "stripped" for subagents) is
**disproven**. Subagents receive MCP fully; the real failure was a config-precedence + auth
problem that only *surfaced* through a subagent's fresh connect. The one-line answer:

> A pi-acp–generated `~/.pi/mcp.json` silently overrides the user's global combiner entry and,
> via pi-mcp-adapter's URL-bound credential stripping, disables the bearer — so the combiner
> returns **401**. It is not subagent-specific and it affects non-ACP shell sessions too.

Full root cause + fix: **`ext/pi/pi-acp/docs/bug-generated-mcp-config-strips-combiner-auth.md`**.

---

## What was actually happening

1. **Subagents DO get MCP.** A pi subagent is an in-memory child `AgentSession` (same process,
   own session id). It re-runs each extension factory in its own `ResourceLoader`, fires its own
   `session_start` / `before_agent_start`, and builds its own tool set fresh — MCP tools
   included (they're in none of the subagent deny lists). It also stands up its **own**
   `McpServerManager` and connects independently of the parent. So the combiner directive is
   injected into subagent turns and combiner tools are visible.

2. **The subagent just connected fresh and hit a 401.** The parent had a live keep-alive
   connection from earlier and rode past the problem; the subagent's cold connect exposed it.
   This is why it *looked* like "subagents don't get MCP."

3. **The 401 is a config-override + auth-stripping chain** (all confirmed, incl. a runtime trace
   showing the combiner `initialize` failing in ~3.6 ms — an auth reject, not a timeout):
   - pi-acp translates the ACP client's HTTP MCP servers into `~/.pi/mcp.json` as **`url` +
     `headers` only** (the ACP shape has no `auth`/`bearerTokenEnv`), repointing the combiner URL
     to `/mcp/<session-token>` and omitting the bearer wiring.
   - `~/.pi/mcp.json` is pi-mcp-adapter's **highest-precedence** source (`<cwd>/.pi/mcp.json`)
     whenever the session cwd is `$HOME`, so it wins over the user's global
     `~/.config/mcp/mcp.json` entry. This is a plain project-pi override — **not** ACP-scoped —
     so any `$HOME`-launched pi session (shell included) reads it.
   - Because the override changes the URL, `mergeServerMaps` strips the URL-bound auth fields
     (`bearerTokenEnv`) from the inherited entry while keeping `auth:"bearer"` and `trace:true`.
     The connect then takes the bearer branch with **no token source** → no `Authorization`
     header → combiner 401. (The stripping is a *correct* anti-credential-exfiltration measure;
     pi-acp is what trips it.)

## The original three questions, answered

- **Do subagents have their own id?** Yes — a pi-generated child session id; the parent is
  lineage metadata only. That separate id is the mechanism that gives each subagent its own MCP
  connections; it was never the problem.
- **ACP / externally-arriving session token?** This build has no ACP inside pi itself; the ACP
  layer is external (`@geohar/pi-acp`). The ACP session token arrives externally and pi-acp
  parks it as the combiner grouping token — **and drops the auth wiring on the way**. That was
  the actual bug (see the pi-acp report).
- **Same toolset?** Yes — freshly rebuilt, not filtered from the parent. The only "no" paths are
  by configuration (`isolated` / `extensions: false` / `ext:` selectors excluding pi-mcp), not
  by any session-id mechanism.

## Fix

The upstream root was in **this repo**: the ACP combiner spec builder
`lua/mcp_companion/cc/init.lua` `build_combiner_entry` emitted only the
`X-MCP-Combiner-Session` header and no `Authorization`, so the combiner entry that flowed
through codecompanion `transform_to_acp` → pi-acp → `~/.pi/mcp.json` had no bearer. **Fixed**
(HTTP branch) by adding a literal `Authorization: Bearer <token>` when
`MCP_COMBINER_AUTH_TOKEN` is set (else no header — the combiner is open), mirroring the nvim
direct client `lua/mcp_companion/combiner/client.lua:311-313`. The literal (not `$env:`) is
deliberate so any compliant ACP agent forwards it verbatim; the stdio/`mcp-remote` branch
still can't present a header, so a locked-down combiner is only reachable over the HTTP
transport.

Remaining hazards are **pi-acp's**: it leaves a persistent, highest-precedence
`~/.pi/mcp.json` that can override a user's global auth config and poison later shell sessions,
and the token now rides into that file on disk. Tracked in the pi-acp report below (fixes:
don't persist a high-precedence file at `$HOME`; guarantee cleanup).

## Immediate workaround

`rm ~/.pi/mcp.json` (it's `_generatedBy: pi-acp`, so it regenerates on the next `$HOME`-cwd ACP
session — the durable fix is in pi-acp).

## Cross-reference

- Root cause & fix: `ext/pi/pi-acp/docs/bug-generated-mcp-config-strips-combiner-auth.md`.
- Related but separate: plan/task ordering (subagents surfaced as ACP tasks) —
  `acp-companion/docs/codecompanion-plan-support.md`. That's the *display* of subagents; this
  doc was about their *access to MCP tools*.
