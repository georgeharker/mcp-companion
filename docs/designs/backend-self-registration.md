# Backend self-registration — the `MCP_COMBINER` switch

A convention for backends that the combiner proxies **and** that also ship their own
Claude Code plugin. Without it, installing such a plugin mounts that backend's tool
surface twice: once through the combiner, once through the plugin.

Status: design. Adopters (in order): `cribsheet`, `svg-mcp`.

## The problem

The combiner aggregates many backends behind one endpoint; their tools arrive
namespaced (`cribsheet_note_apropos`, `svg-mcp_add_path`, …). Several of those
backends also publish a Claude Code plugin that registers the *same* server
directly, because that plugin must also work for people who don't run a combiner.

Install both and you get duplicated tools, duplicated tokens, and ambiguity about
which copy to call. Two observed cases:

- **cribsheet** — plugin registers `http://127.0.0.1:7732/mcp`; combiner proxies the
  same warm process.
- **svg-mcp** — plugin declares a *stdio* server (`uv run … svg-mcp`); the combiner
  serves svg-mcp warm on `:7731` via sharedserver. Installing the plugin both
  double-registers **and** cold-starts a second process per session.

Today the only fix is shipping two near-identical plugins (a registering one and an
instructions-only one) and making the installer choose. That does not scale — one
split per backend, and picking wrong fails silently.

## Why the switch has to be shell, not config

These were measured on Claude Code **v2.1.210**; they rule out every tidier design,
so record them rather than re-discover them.

1. **No `disable` exists.** Neither `plugin.json`'s `mcpServers` nor a
   plugin-shipped `.mcp.json` has an `enabled`/`disabled`/`when` field. An entry
   that exists is an entry that registers.
2. **Env expansion can pick a URL, never remove a server.** Only `${VAR}` and
   `${VAR:-default}` are supported (no `${VAR:+alt}`, no nesting). Claude Code's
   `:-` is *not* bash's — set-but-empty keeps the empty value — but an env-emptied
   URL yields **✘ Failed to connect**, a red error row, not the "not configured"
   placeholder the docs describe. There is no way to switch a server off from env
   via config.
3. **Hooks cannot change the current session's MCP set.** It is fixed at startup; a
   hook's config write lands for the *next* session.
4. **No plugin can configure another.** Expansion reads the environment Claude Code
   was *launched with*, so "the combiner plugin disables the backend plugins" is not
   a load-order bug — it is impossible. The switch must be set before Claude starts.
5. **Writing MCP config mid-session forces a reload.** Observed: a user-scope
   add/remove during a live session triggered a reload that dropped a connected
   combiner's **173 tools**. Config writes are neither free nor silent.

Conclusion: the conditional must live in **shell** (which has `if`) inside each
backend plugin's SessionStart hook, driven by env set outside the session.

## The contract

A backend plugin that the combiner may proxy:

1. **Declares no `mcpServers`** in `plugin.json`, and ships no `.mcp.json`.
2. In its **SessionStart hook**, resolves `combiner_serves <name>` and either
   removes its entry (combiner serves it) or adds it and warms its backend
   (we serve it).
3. **Guards every write** — steady state must perform *zero* `claude mcp` writes
   (see constraint 5). Only a first run or a real mode flip may write.

```sh
if combiner_serves cribsheet; then
  claude mcp get cribsheet >/dev/null 2>&1 && claude mcp remove cribsheet --scope user
else
  claude mcp get cribsheet >/dev/null 2>&1 || \
    claude mcp add --transport http cribsheet http://127.0.0.1:7732/mcp --scope user
  sharedserver use cribsheet --pid "$PPID" --grace-period 1h -- \
    crib --mcp --http --host 127.0.0.1 --port 7732 >/dev/null 2>&1 || true
fi
```

### The flip is bidirectional — both branches must mutate

This is the load-bearing property, and the reason **neither branch may be a no-op**:
the combiner branch actively *removes*, the standalone branch actively *adds*. The
hook is a **convergence step**, not a one-way disable — it drives the registry to
match the env on every start, in whichever direction it currently disagrees.

| current registry | env says | hook does | result |
| --- | --- | --- | --- |
| absent | standalone (unset) | `add` + warm | registered |
| present | standalone (unset) | nothing (guard hits) | registered — **no write** |
| present | combiner (set) | `remove` | unregistered |
| absent | combiner (set) | nothing (guard hits) | unregistered — **no write** |

So setting the env flips a machine to combiner, and *unsetting it flips back* to
standalone, with no manual `claude mcp` step in either direction. Each flip costs
one session (constraint 3) and exactly one write; the two steady-state rows write
nothing, which is what keeps constraint 5 from biting every session.

A consequence worth stating: because the standalone branch **re-adds**, a user who
manually `claude mcp remove`s the server while the env is unset will find it back
next session. The env is the source of truth, not the registry.

## The switch

| variable | scope | meaning |
| --- | --- | --- |
| `MCP_COMBINER` | all backends | is a combiner serving my MCPs? |
| `MCP_COMBINER_SERVES_<NAME>` | one backend | override for that backend only |

Per-backend wins over the global; `<NAME>` is the server name upper-cased with `-`
→ `_` (`svg-mcp` → `MCP_COMBINER_SERVES_SVG_MCP`). Default (nothing set) is
**standalone**, so a fresh plugin install works with no configuration.

```sh
export MCP_COMBINER=1                    # combiner serves my MCPs…
export MCP_COMBINER_SERVES_CRIBSHEET=0   # …except crib, which I run standalone
```

**Values, not presence.** Unset, empty, `0`, `false`, `no`, `off` are false;
anything else is true. Presence-based tests would make `MCP_COMBINER=0` mean
"combiner on", and the per-backend override is useless unless `0` can say
"no — force standalone here".

### Why `SERVES_`, not `MCP_COMBINER_<NAME>`

`MCP_COMBINER_*` is **already this project's own settings namespace**
(`MCP_COMBINER_CONFIG`, `MCP_COMBINER_PORT`, `MCP_COMBINER_TOKEN_KEY`,
`MCP_COMBINER_HEALTH_INTERVAL`, `MCP_COMBINER_SCHEMA_FIXES`, …). A bare
`MCP_COMBINER_CRIBSHEET` is indistinguishable from a setting — a backend named
`config` or `port` collides outright — and a reader cannot tell which system owns
the variable. The infix keeps them apart and states the question being asked.

### Reference implementation

Copy this into each backend plugin's hook. It is deliberately **duplicated, not
shared**: plugins cannot depend on each other (constraint 4), so a shared helper is
not expressible. Keep the semantics identical.

```sh
_truthy() { case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
              ''|0|false|no|off) return 1 ;; *) return 0 ;; esac; }

combiner_serves() {                     # $1 = backend name, e.g. cribsheet
  local name per per_set
  name=$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')
  eval "per=\${MCP_COMBINER_SERVES_$name-}"
  eval "per_set=\${MCP_COMBINER_SERVES_$name+set}"
  [ -n "$per_set" ] && { _truthy "$per"; return; }   # per-backend wins
  _truthy "${MCP_COMBINER-}"                          # else the global
}
```

## The switch is a global toggle, not a per-session one

**This is a design constraint, not an incidental property.** The switch describes
*how this machine gets its MCPs*, and it must be set once, machine-wide:

```sh
# ~/.zshenv — set once; every shell, and everything they spawn, inherits it
export MCP_COMBINER=1
```

It must be set **before** Claude Code starts (constraint 4), and `zshenv` satisfies
that for every path — interactive shells, neovim, and the ACP/codecompanion agents
neovim spawns all inherit it. **Nothing needs to inject it per session.**

**Do not vary it per session.** The registry it drives (`claude mcp --scope user`)
is global, so two concurrent sessions disagreeing about the mode would thrash —
each one's hook converging the *shared* registry against its own env, undoing the
other at every start. The switch and the state it controls are both global; keeping
them at the same scope is what makes the convergence in the contract sound.

A machine is therefore in exactly one mode. A backend that disagrees with the
global is expressed with `MCP_COMBINER_SERVES_<NAME>` — still a global statement
about that backend, not a per-session one.

Note `MCP_COMPANION_COMBINER_URL` is **not** a usable signal for any of this — it is
a URL *override* (the ACP wrapper sets it per session to a scoped token URL), and it
is unset in the common case where the combiner is very much active, because the
plugin's `.mcp.json` default supplies `:9741`. It answers "which combiner endpoint",
never "is a combiner serving me".

## `mcp-combiner env-disable`

The env above can drift from reality: add a backend to `mcpservers.json`, forget the
export, and it silently double-registers — the same silent failure the two-plugin
split had, merely relocated. The combiner already knows exactly what it proxies, so
it should emit the switch itself.

```
$ mcp-combiner env-disable
export MCP_COMBINER_SERVES_CRIBSHEET=1
export MCP_COMBINER_SERVES_SVG_MCP=1
export MCP_COMBINER_SERVES_GITHUB=1
export MCP_COMBINER_SERVES_JUPYTER=1
```

- Emits one `export` per **enabled** server in the loaded config — servers the
  combiner will actually serve. Disabled entries are omitted, so a backend marked
  `disabled: true` correctly falls back to standalone.
- Emits **per-backend vars only**, never the blunt `MCP_COMBINER` global: precision
  is the entire point, and it lets a non-proxied backend stay standalone.
- Name mapping is the same as the contract (upper-case, `-` → `_`).

### Implementation notes

It slots into the existing control-verb machinery (`ctl.py`, added in v0.8.3):

- Register in `ctl.add_ctl_parsers()` alongside `status` / `health` / `reload` /
  `tools`; dispatch is `ctl.run()` → `asyncio.run(args.func(args))`, so the handler
  is an `async def … -> int` like its siblings, even though it awaits nothing.
- Reuse **`_resolve_config(args.config)`** for discovery — it already honours
  `--config`, then `MCP_COMBINER_CONFIG` / `CLAUDE_MCP_COMBINER_CONFIG`, then the
  standard files, mirroring the plugin's `start.sh` search order.
- Use **`CombinerConfig.get_enabled_servers()`** for the list; it already filters
  `ServerConfig.disabled`, which is exactly the semantic above — no new filtering.
- Emit via `_emit()` so `--json` comes for free (it prints `str` data verbatim).

**It must be offline.** Unlike `status` / `health` / `tools`, `env-disable` reads
config and **never connects** — `ctl.run()`'s `httpx.ConnectError` path must not be
reachable from it. This is a hard requirement, not an optimisation: the verb is
evaluated from `zshenv` at *shell startup*, routinely before any combiner exists,
and on machines where one may never start. A version that needs the combiner
running would deadlock the very bootstrap it feeds.

Suggested flags: `--config PATH` (passed to `_resolve_config`), `--shell sh|fish`
for syntax.

**Startup cost.** `eval "$(mcp-combiner env-disable)"` in `zshenv` runs a Python CLI
on **every shell spawn** — a real per-shell latency tax. Prefer generating a static
snippet and sourcing that, refreshed when the config changes:

```sh
# regenerate on config change; source the cheap result
mcp-combiner env-disable > ~/.cache/mcp-combiner/env.sh
. ~/.cache/mcp-combiner/env.sh
```

## Risks

- **Global toggle only** — the switch is machine-wide by design; varying it per
  session is unsupported and will thrash the shared user-scope registry. Project
  scope would allow per-repo modes but needs approval and pollutes repos, so it is
  explicitly not offered.
- **Plugins writing global MCP config** is unusual and surprising; each adopter must
  say so prominently in its README.
- **Uninstall leaves a stray entry** — removing a plugin does not unregister its
  server. Adopters need a cleanup path, or a documented manual `claude mcp remove`.
- **One-session lag** on first install and on every flip (constraint 3).
