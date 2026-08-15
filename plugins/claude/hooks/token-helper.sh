#!/usr/bin/env bash
# headersHelper for the mcp-combiner MCP entry: present this chat's grouping
# token (X-MCP-Combiner-Session) on every connection.
#
# Claude Code runs this fresh on each MCP connection — session start AND every
# reconnect (including after a combiner restart, which is exactly when the
# token must be re-presented so the successor re-associates the chat's parked
# isolated sessions). Output contract: a JSON object of header name → value on
# stdout; {} means "no extra headers".
#
# Identity comes from the SessionStart hook (start.sh), which persists the
# Claude session id keyed by the claude process PID; we run as a child of the
# same claude process, so walking our parent chain finds the same PID. The
# session id is the stable, externally maintained chat identity — it survives
# /resume, /clear, /compact — which is what makes the token durable where the
# combiner-minted wire session id is not (the custody principle: see the
# design graph).

set -u

# --- Abstain under a supervisor -----------------------------------------------
# When the endpoint URL already carries a token in its path (/mcp/<token>),
# a host editor (nvim / CodeCompanion) minted this session's identity and the
# combiner gives the URL token precedence anyway. Emit nothing rather than a
# dead header on every request.
url="${CLAUDE_CODE_MCP_SERVER_URL:-}"
case "$url" in
  */mcp/?*)
    printf '{}'
    exit 0
    ;;
esac

# --- Explicit override ---------------------------------------------------------
# A token exported by the launching shell (inherited through the claude
# process) wins: explicit, user-maintained custody. Per-LAUNCH granularity —
# every session started from that environment shares it.
override="${MCP_COMBINER_CC_TOKEN:-}"
override="${override//[^a-zA-Z0-9._-]/}"
if [[ -n "$override" ]]; then
  printf '{"X-MCP-Combiner-Session": "%s"}' "$override"
  exit 0
fi

# --- Find the claude process (same walk as start.sh) --------------------------
find_claude_pid() {
  local pid=$PPID
  local comm
  while [[ "$pid" -gt 1 ]]; do
    comm=$(ps -o comm= -p "$pid" 2>/dev/null | tr -d ' ')
    [[ -z "$comm" ]] && break
    if [[ "${comm##*/}" == "claude" ]]; then
      echo "$pid"
      return 0
    fi
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    [[ -z "$pid" ]] && break
  done
  return 1
}

if ! claude_pid="$(find_claude_pid)"; then
  # No claude in the parent chain (unexpected): no identity to present.
  printf '{}'
  exit 0
fi

# --- Read the session id written by the SessionStart hook ---------------------
# The claude PID is a PRIVATE rendezvous key only — it names the state file
# shared with the hook (both processes are children of the same claude, so it
# is the one fact that distinguishes this session's invocation from another
# concurrent session's). It never appears in the token itself.
#
# MCP connections race the SessionStart hook at startup; poll briefly (well
# inside the helper's 10s budget) before falling back.
token_file="${XDG_STATE_HOME:-$HOME/.local/state}/mcp-companion/cc-token-${claude_pid}"
for _ in 1 2 3 4 5 6; do
  [[ -s "$token_file" ]] && break
  sleep 0.5
done

session_id=""
[[ -s "$token_file" ]] && session_id="$(cat "$token_file" 2>/dev/null)"
# Header-safe by construction (start.sh sanitizes), but never trust a file.
session_id="${session_id//[^a-zA-Z0-9._-]/}"

if [[ -z "$session_id" ]]; then
  # Hook never wrote (disabled, or the race lost for the whole poll): mint a
  # UUID once and PERSIST it in the same file, so every later reconnect of
  # this session presents the same identity. If the hook writes the real
  # session id afterwards it wins from the next connection on.
  session_id="$( (uuidgen 2>/dev/null || od -An -N16 -tx1 /dev/urandom | tr -d ' \n') |
    tr 'A-F' 'a-f')"
  session_id="${session_id//[^a-zA-Z0-9._-]/}"
  mkdir -p "$(dirname "$token_file")" 2>/dev/null
  (umask 077 && printf '%s' "$session_id" >"$token_file") 2>/dev/null
fi

printf '{"X-MCP-Combiner-Session": "cc-%s"}' "$session_id"
