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
_emit_instructions() {
  local txt="${CLAUDE_PLUGIN_ROOT}/instructions.txt"
  if [[ -f "$txt" ]] && command -v jq >/dev/null 2>&1; then
    jq -Rs '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:.}}' <"$txt"
  elif [[ -f "$txt" ]]; then
    cat "$txt"
  fi
}
trap _emit_instructions EXIT

# --- Skip when the combiner was launched for us ----------------------------------
# When Claude Code is spawned by CodeCompanion / mcp-companion, the host editor
# has already started (and refcounts, via sharedserver) the combiner process, and
# injects its tokened endpoint as MCP_COMPANION_COMBINER_URL — the same var our
# .mcp.json consumes (`${MCP_COMPANION_COMBINER_URL:-http://127.0.0.1:9741/mcp}`).
# In that context the combiner is not ours to launch: just connect to it. The host
# Neovim instance outlives this session and owns the combiner's lifecycle.
if [[ -n "${MCP_COMPANION_COMBINER_URL:-}" ]]; then
  exit 0
fi

ss_bin="${CLAUDE_PLUGIN_ROOT}/bin/sharedserver"

# --- Resolve mcp-combiner command -------------------------------------------------
# Priority: env override → `mcp-combiner` on PATH → `uv run -m mcp_combiner` from the
# checkout if present. If none work, log and bail.
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

  # Resolve the combiner command (PyPI package & command: `mcp-combiner`).
  if command -v mcp-combiner >/dev/null 2>&1; then
    combiner_cmd=("mcp-combiner")
    return 0
  fi

  # Migration aid: this project was renamed `mcp-bridge` → `mcp-combiner`. If only the old
  # command is on PATH, keep working but loudly tell the user to reinstall under the new name.
  if command -v mcp-bridge >/dev/null 2>&1; then
    echo "mcp-combiner: ⚠ using the OLD 'mcp-bridge' command — it was renamed to 'mcp-combiner'." >&2
    echo "  Reinstall: uv tool uninstall mcp-bridge && uv tool install mcp-combiner" >&2
    echo "  (and rename any CLAUDE_MCP_BRIDGE_* env vars to CLAUDE_MCP_COMBINER_*)." >&2
    combiner_cmd=("mcp-bridge")
    return 0
  fi

  # Optional uv-run fallback — only when CLAUDE_MCP_COMBINER_CHECKOUT is explicitly
  # set to a combiner checkout. No hardcoded path default: prefer a real install
  # (`uv tool install <…>/combiner`, or a shared venv with its bin/ on PATH) so the
  # `mcp-combiner` lookup above resolves.
  local checkout="${CLAUDE_MCP_COMBINER_CHECKOUT:-}"
  if [[ -n "$checkout" && -d "$checkout" ]] && command -v uv >/dev/null 2>&1; then
    combiner_cmd=(uv run --project "$checkout" python -m mcp_combiner)
    return 0
  fi

  return 1
}

combiner_cmd=()
if ! resolve_combiner_command; then
  echo "mcp-combiner: cannot find the 'mcp-combiner' command." >&2
  echo "  Set CLAUDE_MCP_COMBINER_COMMAND, install with 'uv tool install mcp-combiner'," >&2
  echo "  or check out https://github.com/georgeharker/mcp-companion.nvim." >&2
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
log_file="${CLAUDE_MCP_COMBINER_LOG:-}"

# --- Resolve client PID --------------------------------------------------------
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

serve_flag=()
combiner_version="$("${combiner_cmd[@]}" --version 2>/dev/null | awk '{print $2}')"
if [[ -n "$combiner_version" ]] && version_ge "$combiner_version" "0.8.0"; then
  serve_flag=(--mcp)
fi

# --- Build and run sharedserver use --------------------------------------------
ss_args=(use "$name" --pid "$client_pid" --metadata "claude-$client_pid" --grace-period "$grace")
[[ -n "$log_file" ]] && ss_args+=(--log-file "$log_file")
ss_args+=(-- "${combiner_cmd[@]}" "${serve_flag[@]}" --config "$config" --port "$port")

if ! out="$("$ss_bin" "${ss_args[@]}" 2>&1)"; then
  echo "mcp-combiner: sharedserver use failed (exit $?):" >&2
  [[ -n "$out" ]] && echo "$out" | sed 's/^/  /' >&2
elif [[ -n "$out" ]]; then
  echo "$out" | sed 's/^/  /' >&2
fi

exit 0
