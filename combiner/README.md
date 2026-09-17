# mcp-combiner

An **MCP aggregator** — fronts multiple MCP servers behind a single Streamable HTTP endpoint, so
one connection exposes every backend server's tools. Built on
[FastMCP](https://github.com/jlowin/fastmcp). Shareable across clients (via `sharedserver`), it
powers the [mcp-companion](https://github.com/georgeharker/mcp-companion) Neovim plugin and the
[`mcp-combiner`](https://github.com/georgeharker/mcp-companion/tree/main/plugins/claude) Claude Code plugin, and works standalone with any MCP client.

> PyPI package · command · import package: **`mcp-combiner`** / `mcp-combiner` / `mcp_combiner`.

> ⚠️ **Renamed from `mcp-bridge`.** If you ran an earlier build:
>
> - command/import are now `mcp-combiner` / `mcp_combiner`; reinstall:
>   `uv tool uninstall mcp-bridge` then `uv tool install …` (see Install below).
> - config env vars `MCP_BRIDGE_*` → `MCP_COMBINER_*` (and `MCP_COMPANION_COMBINER_URL` →
>   `MCP_COMPANION_COMBINER_URL`).
> - OAuth token storage moved to `~/.cache/mcp-combiner/` — you'll **re-authenticate each MCP
>   server once** (old tokens under `~/.cache/mcp-companion/` are no longer read).

## Install

Needs only [uv](https://docs.astral.sh/uv/) — `uvx` fetches and runs it, no venv to manage:

```bash
uvx mcp-combiner --help                                                # once published to PyPI
# before PyPI (or to track main) — the package lives in the combiner/ subdirectory:
uvx --from "git+https://github.com/georgeharker/mcp-companion#subdirectory=combiner" mcp-combiner
```

Or install it: `uv pip install mcp-combiner` (PyPI), or from the repo subdir
`uv pip install "git+https://github.com/georgeharker/mcp-companion#subdirectory=combiner"`.

## Usage

```bash
mcp-combiner --config /path/to/servers.json --port 9741
```

## Interactive resource host (mcp-app widgets)

The combiner serves interactive widget resources itself: open
`/ui/<token>/?resource=<uri>` and it renders a sandboxed host page (provider
HTML on a second loopback origin — port+1 — never same-origin with the
capability-holding page), streams tool data to the widget over SSE, and gates
widget-initiated tool calls through the same permission pipeline as agent
calls.

A tool result whose **tool definition** binds a widget resource
(`meta.ui.resourceUri`, e.g. todoist's `find-tasks-by-date`) holds the agent's
call in flight while you interact: the found data streams into the widget,
your actions run as tool calls through the combiner, and **Done** resolves the
call with a summary of the interaction. The hold budget is 50s by default
(`MCP_COMBINER_UI_HOLD_TIMEOUT`).

Retrieve what happened inside any widget with the meta-tools:

- `combiner__ui_sessions` — open/recent widget sessions per chat
- `combiner__ui_messages` — the prompts, contexts, and tool calls the widget made

Security: `/ui/*` is bearer-free (the path token is the capability) while
`/mcp` and the control routes keep the bearer gate; the sandbox denies
`connect-src` unless the widget's resource metadata declares domains. For
testing, the mockserver serves a spec-speaking widget at
`ui://mock/mock/widget` (run it with `--transport http` and point a
`mock` server entry at it). Design details:
[`docs/designs/interactive-resource-host.md`](../docs/designs/interactive-resource-host.md).

## Inbound authentication

By default the `/mcp` endpoint is **unauthenticated** — fine on loopback, but a
hole the moment you bind beyond `127.0.0.1` (`--host`) or share the box. Set a
bearer token and every request to `/mcp` must present `Authorization: Bearer <token>`:

```bash
MCP_COMBINER_AUTH_TOKEN=$(cat ~/secrets/combiner-token) \
  mcp-combiner --config … --port 9741
# or, daemon-side:
mcp-combiner --config … --auth-token-file ~/secrets/combiner-token
```

- Unset (both) → endpoint stays open (default; nothing changes).
- `--auth-token-file` wins over the env var; a blank/unreadable file → stays open.
- **Scope**: `/mcp` **and** the control routes that mutate the running server —
  `/sessions*` (session filters) and `/handover*` (restart handover). `/health`
  stays open for liveness probes. Because the control routes are gated, the
  combiner's own callers present the token too: the `mcp-combiner` ctl and the
  Neovim host's REST client both read `MCP_COMBINER_AUTH_TOKEN`.
- A missing/wrong token gets a plain `401` — no `WWW-Authenticate: Bearer`, so
  standards clients surface an honest auth error instead of falling into OAuth /
  Dynamic Client Registration. Clients present the token pre-emptively.

The companion clients pick the token up from the **same `MCP_COMBINER_AUTH_TOKEN`
env var** (Claude Code, OpenCode, Pi, and the Neovim host) — provision it once in
your environment (or via each client's documented hook) and it flows to all of them.

> **Claude Code caveat.** Claude Code strips secret-looking env-var names
> (`*TOKEN*`/`*KEY*`/`*SECRET*`/…) from the `headersHelper` subprocess it spawns, so
> that helper cannot read `MCP_COMBINER_AUTH_TOKEN` from its own environment. The
> Claude plugin works around it: the `SessionStart` hook (which runs with the full,
> unredacted environment) relays the token to the helper via a mode-`600`
> `cc-bearer-<pid>` file under `$XDG_STATE_HOME/mcp-companion/`, written before the
> session-id file and removed on `SessionEnd`. It's written once per SessionStart and
> persists across combiner reconnects, so the helper re-reads it without the hook
> re-running. The daemon is unaffected — it too is launched by that hook, not by a
> headersHelper. (mcp-companion ≥ 0.13.3.)

### Reusing the gate on other FastMCP servers

The middleware lives in `mcp_combiner/inbound_auth.py` and is **self-contained**
(stdlib + starlette only), so a sibling FastMCP server (e.g. `cribsheet`,
`svg-mcp`) can vendor the file and gate its own `/mcp`. Each server names its own
env var — set them to the same value for one shared secret, or distinct values for
per-service isolation:

| server       | env var                   |
| ------------ | ------------------------- |
| mcp-combiner | `MCP_COMBINER_AUTH_TOKEN` |
| cribsheet    | `CRIBSHEET_AUTH_TOKEN`    |
| svg-mcp      | `SVG_MCP_AUTH_TOKEN`      |

```python
from inbound_auth import BearerAuthMiddleware, resolve_auth_token

token = resolve_auth_token("CRIBSHEET_AUTH_TOKEN")   # or a --auth-token-file
if token:
    app.add_middleware(
        BearerAuthMiddleware, token=token,
        is_protected=lambda path: path != "/health",  # plain servers: gate all but health
    )
```

The combiner, in turn, presents the backend's token when it connects — add it to
that server's `servers.json` entry: `{"auth": {"bearer": "${CRIBSHEET_AUTH_TOKEN}"}}`
(or a `headers` map). See [Authentication → Bearer token](../README.md#authentication)
in the top-level README.

## Development

```bash
uv sync
pytest
```
