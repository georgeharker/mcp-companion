#!/usr/bin/env bash
# SessionStart hook: attach to (or start) the mcp-combiner process
# via sharedserver. The combiner is registered under name "mcp-combiner" by default;
# multiple Claude Code sessions and other clients (nvim, OpenCode) that use the
# same name share one process.

set -u

# --- Instructions ----------------------------------------------------------------
# Tell the session that combined tools arrive name-prefixed, and to discover what is
# available rather than assume. Emitted as SessionStart additionalContext on EVERY exit
# path (several branches below exit early, so a trap rather than a tail fall-through),
# mirroring the sibling svg-mcp / cribsheet plugins' instructions.txt pattern.
# Canonical source: CLAUDE.md.example at the repo root, which instructions.txt symlinks.
# NOTE: stdout IS the hook's JSON payload — every other line in this script goes to
# stderr so it cannot corrupt it.
# Escape stdin as a JSON string body (no surrounding quotes), for the no-jq path.
# Pure bash parameter expansion — no awk/sed/python dependency. Backslash MUST be
# substituted first (it is the escape character for every rule after it) and the
# newline last, so the \n it introduces is not re-escaped by an earlier rule.
_json_escape() {
  local s
  s=$(cat)
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\t'/\\t}
  s=${s//$'\r'/\\r}
  s=${s//$'\n'/\\n}
  # JSON forbids RAW characters in U+0000–U+001F, not merely the three with names above.
  # One stray byte invalidates the whole envelope, and since that envelope carries the
  # warnings as well as the instructions, the session would silently lose both. Warning
  # text interpolates values we do not control (a $MCP_COMPANION_COMBINER_URL from the
  # environment, a version parsed from a shim that colourises its output and so emits a
  # raw ESC), so this is reachable in practice. Delete rather than \uXXXX-escape: these
  # carry no meaning in instructions or warnings, and escaping arbitrary bytes in bash
  # needs a per-character loop. Safe as a blanket range because it runs AFTER the three
  # named escapes, which have already turned those bytes into two-character sequences.
  s=${s//[$'\x01'-$'\x1f']/}
  printf '%s' "$s"
}

# User-visible warnings, collected here and emitted as `systemMessage` in the ONE
# envelope below. SessionStart stderr is invisible at exit 0 (only `claude --debug`
# shows it), so anything the user must act on has to travel this way instead.
# Deliberately NOT a second printf'd JSON object: stdout is the hook payload, and two
# concatenated objects mean at best one of them is silently dropped.
_warnings=""
warn() { # warn <message> — surfaces to the user AND to the debug log
  _warnings="${_warnings}${_warnings:+ }$1"
  echo "$1" >&2
}

_emit_instructions() {
  local txt="${CLAUDE_PLUGIN_ROOT}/instructions.txt"
  local ctx=""
  [[ -f "$txt" ]] && ctx="$(cat "$txt")"
  [[ -z "$ctx" && -z "$_warnings" ]] && return 0
  # Both branches emit the SAME JSON envelope. The previous no-jq fallback `cat`ed the
  # raw markdown, which SessionStart does accept as bare-text context — but it meant the
  # payload shape silently depended on whether jq was installed, so nothing could be
  # added alongside additionalContext (this systemMessage, say) without breaking it.
  if command -v jq >/dev/null 2>&1; then
    jq -n --arg ctx "$ctx" --arg sys "$_warnings" \
      '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}
       + (if $sys == "" then {} else {systemMessage:$sys} end)'
  else
    local sys_field=""
    [[ -n "$_warnings" ]] &&
      sys_field=",\"systemMessage\":\"$(printf '%s' "$_warnings" | _json_escape)\""
    printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}%s}\n' \
      "$(printf '%s' "$ctx" | _json_escape)" "$sys_field"
  fi
}
trap _emit_instructions EXIT

# --- Resolve client PID (used below AND by the chat-identity bridge) --------------
# $PPID is the wrapper shell that claude execs to run this hook — ephemeral, so
# sharedserver's dead-client poller would reap our registration ~5s after the
# hook returns. Walk the parent chain to find the actual claude process.
find_claude_pid() {
  local pid=$PPID
  local comm
  while [[ "$pid" -gt 1 ]]; do
    comm=$(ps -o comm= -p "$pid" 2>/dev/null | tr -d ' ')
    [[ -z "$comm" ]] && break
    # `comm` is the full executable path on macOS, basename elsewhere.
    if [[ "${comm##*/}" == "claude" ]]; then
      echo "$pid"
      return 0
    fi
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    [[ -z "$pid" ]] && break
  done
  return 1
}

# --- Chat identity bridge (grouping token) ----------------------------------------
# The hook's stdin JSON carries the Claude session id — the stable, externally
# maintained chat identity (survives /resume, /clear, /compact). MCP config has no
# per-session channel, so persist it keyed by the claude PID; token-helper.sh (the
# combiner entry's headersHelper, a child of the same claude process) walks its own
# parent chain to the same PID, reads this file, and presents the id as the chat's
# X-MCP-Combiner-Session grouping token. Written BEFORE the supervised-session
# early-exit below, unconditionally: under a host nvim the helper abstains (the URL
# already carries the supervisor's token, which wins in the combiner regardless),
# so a stale file is harmless — and stop.sh removes it.
_hook_input="$(cat 2>/dev/null || true)"
_session_id=""
if [[ -n "$_hook_input" ]]; then
  if command -v jq >/dev/null 2>&1; then
    _session_id="$(printf '%s' "$_hook_input" | jq -r '.session_id // empty' 2>/dev/null)"
  else
    _session_id="$(printf '%s' "$_hook_input" |
      sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
  fi
fi
# Defense in depth: the value lands in an HTTP header — keep it to token-safe chars.
_session_id="${_session_id//[^a-zA-Z0-9._-]/}"
if [[ -n "$_session_id" ]] && _claude_pid="$(find_claude_pid)"; then
  _token_dir="${XDG_STATE_HOME:-$HOME/.local/state}/mcp-companion"
  if mkdir -p "$_token_dir" 2>/dev/null; then
    (umask 077 && printf '%s' "$_session_id" >"$_token_dir/cc-token-${_claude_pid}") 2>/dev/null
  fi
fi

# --- Skip when the combiner was launched for us ----------------------------------
# When Claude Code is spawned by CodeCompanion / mcp-companion, the host editor
# has already started (and refcounts, via sharedserver) the combiner process, and
# injects its tokened endpoint as MCP_COMPANION_COMBINER_URL — the same var our
# .mcp.json consumes (`${MCP_COMPANION_COMBINER_URL:-http://127.0.0.1:9741/mcp}`).
# In that context the combiner is not ours to launch: just connect to it. The host
# Neovim instance outlives this session and owns the combiner's lifecycle.
#
# …unless CLAUDE_MCP_COMBINER_PORT is ALSO set, which is how a user says "I picked
# this port, launch here". That distinction is what makes a custom port reachable at
# all: .mcp.json can only be redirected via MCP_COMPANION_COMBINER_URL (its env
# expansion has no nesting — a `${A:-…${B:-9741}…}` default silently mangles to a
# literal), so choosing a port REQUIRES setting the URL, and treating a set URL as
# "someone else owns this" would make that self-defeating. The host never sets the
# port var, so this cannot change its behaviour. stop.sh MUST mirror this condition
# — if we launch and it defers, the detach never happens and the refcount leaks.
if [[ -n "${MCP_COMPANION_COMBINER_URL:-}" && -z "${CLAUDE_MCP_COMBINER_PORT:-}" ]]; then
  exit 0
fi

ss_bin="${CLAUDE_PLUGIN_ROOT}/bin/sharedserver"

# --- Version helpers --------------------------------------------------------------
# mcp-combiner >= 0.8.0 is a control CLI by default and serves with --mcp; older releases
# serve on a bare --config and have no --version flag at all. That same boundary is the
# floor for trusting a PATH install: below it we would rather fetch a known-good release
# via uvx than limp along on a stale one. This is deliberately NOT the lockstep release
# version — it moves only when this plugin starts depending on newer combiner behaviour.
MIN_COMBINER_VERSION="0.8.0"

version_ge() { # version_ge A B -> success when dotted-numeric prefix of A >= B
  local IFS=.
  local -a a b
  read -r -a a <<<"${1%%[!0-9.]*}"
  read -r -a b <<<"${2%%[!0-9.]*}"
  local i x y
  for i in 0 1 2; do
    x="${a[i]:-0}"
    y="${b[i]:-0}"
    ((10#${x:-0} > 10#${y:-0})) && return 0
    ((10#${x:-0} < 10#${y:-0})) && return 1
  done
  return 0
}

# Dotted version of a combiner command, or empty when it cannot be determined (pre-0.8.0
# has no --version, and anything unexpected on stdout parses to nothing).
combiner_version_of() {
  "$@" --version 2>/dev/null | awk '{print $2}'
}

# This plugin's own version. Lockstep releases keep it equal to the published PyPI
# mcp-combiner (scripts/bump-version.sh writes both; release.yml refuses to publish a
# mismatched tag), so it doubles as the uvx pin — derived, never duplicated here.
plugin_version() {
  local pj="${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json"
  [[ -f "$pj" ]] || return 1
  if command -v jq >/dev/null 2>&1; then
    jq -r '.version // empty' <"$pj"
  else
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$pj" | head -1
  fi
}

# --- Resolve mcp-combiner command -------------------------------------------------
# Priority: env override → `mcp-combiner` on PATH (only when new enough) → legacy
# `mcp-bridge` → `uv run -m mcp_combiner` from an explicit checkout → a pinned release
# fetched with uvx. That last tail is what makes a bare plugin install work out of the
# box — nothing to install by hand, and a pinned version rather than whatever happens
# to be lying around. If none work, log and bail.
resolve_combiner_command() {
  if [[ -n "${CLAUDE_MCP_COMBINER_COMMAND:-}" ]]; then
    combiner_cmd=("${CLAUDE_MCP_COMBINER_COMMAND}")
    if [[ -n "${CLAUDE_MCP_COMBINER_ARGS:-}" ]]; then
      # Split on whitespace; users wanting embedded spaces should set combiner_cmd directly.
      read -r -a extra <<<"${CLAUDE_MCP_COMBINER_ARGS}"
      combiner_cmd+=("${extra[@]}")
    fi
    return 0
  fi

  # A PATH install wins outright when it is new enough: it is the user's explicit choice
  # and costs no fetch. Too old and we fall through rather than limp — the uvx tail below
  # will get a known-good release, so staleness self-heals instead of lingering forever.
  if command -v mcp-combiner >/dev/null 2>&1; then
    local path_version
    path_version="$(combiner_version_of mcp-combiner)"
    if [[ -n "$path_version" ]] && version_ge "$path_version" "$MIN_COMBINER_VERSION"; then
      combiner_cmd=("mcp-combiner")
      resolved_version="$path_version"
      return 0
    fi
    warn "mcp-combiner: the 'mcp-combiner' on PATH reports ${path_version:-a pre-0.8.0 version}, older than the ${MIN_COMBINER_VERSION} this plugin needs — ignoring it and fetching a pinned release instead. Upgrade it with: uv tool install --upgrade mcp-combiner"
  fi

  # The legacy `mcp-bridge` name (renamed to `mcp-combiner`) is no longer resolved. It
  # predates the rename and is therefore older than everything the floor above rejects,
  # so honouring it would have meant preferring the stalest possible binary over the
  # pinned release below. Anyone still on it lands in the uvx path and gets a current
  # combiner; the leftover command can be removed with `uv tool uninstall mcp-bridge`.

  # Optional uv-run fallback — only when CLAUDE_MCP_COMBINER_CHECKOUT is explicitly
  # set to a combiner checkout. No hardcoded path default: prefer a real install
  # (`uv tool install <…>/combiner`, or a shared venv with its bin/ on PATH) so the
  # `mcp-combiner` lookup above resolves.
  local checkout="${CLAUDE_MCP_COMBINER_CHECKOUT:-}"
  if [[ -n "$checkout" && -d "$checkout" ]] && command -v uv >/dev/null 2>&1; then
    combiner_cmd=(uv run --project "$checkout" python -m mcp_combiner)
    return 0
  fi

  # Out-of-the-box path: no install required, fetch from PyPI. Pinned to this plugin's
  # own version so the pair moves in lockstep; if that exact release is missing (a failed
  # publish, say) fall back to latest rather than failing outright. The --version probe
  # both validates the pin and warms uv's cache, so the real spawn below is a cache hit.
  # The floor is enforced here too: this branch exists to BE the known-good option, so
  # accepting anything that merely answers --version would leave that claim untested (a
  # rolled-back plugin version, or a yanked `latest`, could sit below the floor).
  if command -v uvx >/dev/null 2>&1; then
    local want uvx_version
    want="$(plugin_version || true)"
    if [[ -n "$want" ]]; then
      uvx_version="$(combiner_version_of uvx "mcp-combiner@${want}")"
      if [[ -n "$uvx_version" ]] && version_ge "$uvx_version" "$MIN_COMBINER_VERSION"; then
        combiner_cmd=(uvx "mcp-combiner@${want}")
        resolved_version="$uvx_version"
        return 0
      fi
    fi
    uvx_version="$(combiner_version_of uvx mcp-combiner)"
    if [[ -n "$uvx_version" ]] && version_ge "$uvx_version" "$MIN_COMBINER_VERSION"; then
      warn "mcp-combiner: the pinned release ${want:-<unknown>} could not be fetched or is too old, so this session is using mcp-combiner $uvx_version (latest from PyPI) instead. The plugin and combiner versions may not match."
      combiner_cmd=(uvx mcp-combiner)
      resolved_version="$uvx_version"
      return 0
    fi
  fi

  return 1
}

combiner_cmd=()
# Version of the resolved combiner, when resolution already had to determine it (the PATH
# and uvx branches both probe). Reused by the serve-flag gate below so the same command is
# not asked its version twice — which for the uvx branch means a second uv resolution.
resolved_version=""
if ! resolve_combiner_command; then
  echo "mcp-combiner: cannot find or fetch the 'mcp-combiner' command." >&2
  echo "  Install uv (https://docs.astral.sh/uv/) and this plugin needs nothing else —" >&2
  echo "  it will fetch a pinned mcp-combiner from PyPI on demand." >&2
  echo "  Otherwise: install with 'uv tool install mcp-combiner', or set" >&2
  echo "  CLAUDE_MCP_COMBINER_COMMAND / CLAUDE_MCP_COMBINER_CHECKOUT." >&2
  exit 0
fi

# --- Resolve config path --------------------------------------------------------
config="${CLAUDE_MCP_COMBINER_CONFIG:-}"
if [[ -z "$config" ]]; then
  for candidate in \
    "$HOME/.cache/secrets/$USER.mcpservers.json" \
    "$HOME/.config/mcp-combiner/servers.json" \
    "$HOME/.config/mcp/servers.json"; do
    if [[ -f "$candidate" ]]; then
      config="$candidate"
      break
    fi
  done
fi
if [[ -z "$config" || ! -f "$config" ]]; then
  echo "mcp-combiner: no mcp-servers config found." >&2
  echo "  Set CLAUDE_MCP_COMBINER_CONFIG or create ~/.config/mcp-combiner/servers.json." >&2
  exit 0
fi

# --- Other knobs ---------------------------------------------------------------
port="${CLAUDE_MCP_COMBINER_PORT:-9741}"
grace="${CLAUDE_MCP_COMBINER_GRACE:-30m}"
name="${CLAUDE_MCP_COMBINER_NAME:-mcp-combiner}"

# --- Logging ---------------------------------------------------------------------
# Parity with the Neovim plugin, which gives the combiner two log files under
# stdpath("log"): mcp-combiner.log (raw stdout/stderr, captured by sharedserver's
# --log-file) and mcp-combiner-py.log (the combiner's own --log-file — fastmcp,
# OAuth, httpx detail — at level "info"). There is no stdpath("log") here, so both
# default under $XDG_STATE_HOME/mcp-combiner. Set a variable to "none" to disable
# that file. Whichever client STARTS the shared process fixes its argv — when
# Neovim launched the combiner these have no effect (its stdpath paths are already
# in place); they matter exactly when this hook is the launcher, which was
# previously the only launch path with no logs at all (stdout/stderr to /dev/null,
# no --log-file: an outage left no server-side record).
log_dir="${XDG_STATE_HOME:-$HOME/.local/state}/mcp-combiner"
log_file="${CLAUDE_MCP_COMBINER_LOG:-$log_dir/mcp-combiner.log}"
py_log_file="${CLAUDE_MCP_COMBINER_PYLOG:-$log_dir/mcp-combiner-py.log}"
log_level="${CLAUDE_MCP_COMBINER_LOG_LEVEL:-info}"
[[ "$log_file" == "none" ]] && log_file=""
[[ "$py_log_file" == "none" ]] && py_log_file=""
# sharedserver won't create the capture file's directory; the combiner does create
# its own --log-file parent, so only the capture path needs help.
[[ -n "$log_file" ]] && mkdir -p "$(dirname "$log_file")" 2>/dev/null

# Choosing a port takes BOTH variables, because they answer different questions and
# neither can be derived from the other: CLAUDE_MCP_COMBINER_PORT is where we tell
# sharedserver to serve, while .mcp.json can only be redirected via
# MCP_COMPANION_COMBINER_URL (its `${VAR:-default}` expansion does not nest, so the
# 9741 in that file is necessarily a literal). Setting one without the other is always
# a broken session, so say so rather than letting it look like a failed start.
url="${MCP_COMPANION_COMBINER_URL:-}"
if [[ -n "${CLAUDE_MCP_COMBINER_PORT:-}" && -z "$url" ]]; then
  warn "mcp-combiner: CLAUDE_MCP_COMBINER_PORT=$port is set but MCP_COMPANION_COMBINER_URL is not, so the combiner will serve on $port while Claude still dials 127.0.0.1:9741. Set MCP_COMPANION_COMBINER_URL=http://127.0.0.1:$port/mcp too (both must be exported before Claude starts)."
elif [[ -n "$url" ]]; then
  # Both set: the URL wins. It is the only one Claude can act on — .mcp.json was
  # expanded from it before this hook ran, and nothing we do now can change it — so
  # serving anywhere else guarantees an unreachable combiner. Take the port from the
  # URL and report the disagreement rather than starting something nothing can reach.
  url_hostport="${url#*://}"      # strip scheme
  url_hostport="${url_hostport%%/*}" # strip path
  url_port="${url_hostport##*:}"     # after the last colon
  if [[ "$url_port" == "$url_hostport" || -z "$url_port" || -n "${url_port//[0-9]/}" ]]; then
    warn "mcp-combiner: MCP_COMPANION_COMBINER_URL=$url has no explicit port, so the port to serve on cannot be determined from it. Serving on $port; use an explicit port in the URL (e.g. http://127.0.0.1:$port/mcp)."
  elif [[ "$url_port" != "$port" ]]; then
    warn "mcp-combiner: CLAUDE_MCP_COMBINER_PORT=$port disagrees with MCP_COMPANION_COMBINER_URL=$url. Claude dials the URL, so serving on $port would be unreachable — using $url_port instead. Set them to the same port."
    port="$url_port"
  fi
fi

# --- Resolve client PID --------------------------------------------------------
# find_claude_pid is defined at the top (shared with the chat-identity bridge).
if client_pid=$(find_claude_pid); then
  :
else
  echo "mcp-combiner: no 'claude' process in parent chain; falling back to PPID=$PPID" >&2
  client_pid="$PPID"
fi

# --- Serve-mode flag -------------------------------------------------------------
# mcp-combiner >= 0.8.0 is a control CLI by default and serves with --mcp; older
# releases serve on bare --config. Gate on the reported version (behavior changed
# at 0.8.0): releases before 0.8.0 have no --version flag, so the query comes back
# empty and we take the legacy invocation. Bare --config still serves on newer
# versions too (deprecation warning), so a wrong guess degrades gracefully.
# Only the explicit-override and legacy-mcp-bridge branches can still land here below
# the floor — the PATH branch now rejects those, and uvx pins a modern release.
serve_flag=()
# Only the explicit-override and checkout branches arrive unprobed; everything else
# recorded its version during resolution, so this is a cache read rather than a respawn.
combiner_version="${resolved_version:-$(combiner_version_of "${combiner_cmd[@]}")}"
if [[ -n "$combiner_version" ]] && version_ge "$combiner_version" "$MIN_COMBINER_VERSION"; then
  serve_flag=(--mcp)
fi

# Combiner-side log flags ride the same version gate as --mcp: releases below the
# floor predate --log-file/--log-level, and an unknown-version override shouldn't
# be handed flags it may not parse.
combiner_log_args=()
if [[ ${#serve_flag[@]} -gt 0 ]]; then
  [[ -n "$py_log_file" ]] && combiner_log_args+=(--log-file "$py_log_file")
  combiner_log_args+=(--log-level "$log_level")
fi

# --- Build and run sharedserver use --------------------------------------------
ss_args=(use "$name" --pid "$client_pid" --metadata "claude-$client_pid" --grace-period "$grace")
[[ -n "$log_file" ]] && ss_args+=(--log-file "$log_file")
ss_args+=(-- "${combiner_cmd[@]}" "${serve_flag[@]}" --config "$config" --port "$port" "${combiner_log_args[@]}")

if ! out="$("$ss_bin" "${ss_args[@]}" 2>&1)"; then
  echo "mcp-combiner: sharedserver use failed (exit $?):" >&2
  [[ -n "$out" ]] && echo "$out" | sed 's/^/  /' >&2
elif [[ -n "$out" ]]; then
  echo "$out" | sed 's/^/  /' >&2
fi

exit 0
