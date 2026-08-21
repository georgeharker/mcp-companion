#!/usr/bin/env bash
# SessionEnd hook: detach from the shared mcp-combiner process. Best-effort —
# if it fails or doesn't run (hard crash), sharedserver's dead-client poller
# reaps the refcount within ~5s.

set -u

# Mirror start.sh: $PPID is the ephemeral hook-wrapper shell, so walk parents
# to find the claude PID we originally registered.
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

# Remove the relay files the SessionStart hook wrote for token-helper.sh — the
# chat-identity token and the inbound-auth bearer. Unconditional (start.sh writes
# them even under a host nvim); best-effort.
if _claude_pid="$(find_claude_pid)"; then
  _state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/mcp-companion"
  rm -f "$_state_dir/cc-token-${_claude_pid}" "$_state_dir/cc-bearer-${_claude_pid}" 2>/dev/null
fi

# Mirror start.sh: when launched under CodeCompanion / mcp-companion the combiner
# was started (and is refcounted) by the host editor, not by us — we never ran
# `sharedserver use`, so there is nothing to detach. The host owns teardown.
#
# This condition MUST stay identical to start.sh's. A URL with CLAUDE_MCP_COMBINER_PORT
# also set means the user picked a port and start.sh DID launch, so we do have a
# refcount to release; deferring here would leak it until the grace period expired.
if [[ -n "${MCP_COMPANION_COMBINER_URL:-}" && -z "${CLAUDE_MCP_COMBINER_PORT:-}" ]]; then
  exit 0
fi

ss_bin="${CLAUDE_PLUGIN_ROOT}/bin/sharedserver"
name="${CLAUDE_MCP_COMBINER_NAME:-mcp-combiner}"

# find_claude_pid is defined at the top (shared with the token-file cleanup).
client_pid=$(find_claude_pid) || client_pid="$PPID"

"$ss_bin" unuse "$name" --pid "$client_pid" >/dev/null 2>&1 || true

exit 0
