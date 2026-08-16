"""Control-plane CLI: drive a running combiner from the command line.

Mirrors the operations surface the Neovim plugin exposes (see the Lua ops in
lua/mcp_companion): status/health over REST, server lifecycle + tool calls
over MCP (the ``combiner__*`` meta-tools via ``tools/call``), and per-chat
session filters over the token REST routes.

Like the plugin's control channel, this connects as a TOKENLESS control
session and names target chats by token where needed.

Session-control verbs are WIP: every CLI invocation is a transient MCP
session (there is no CLI "this session" to self-scope filters to), and
token-addressed filters are recorded but not yet applied by the combiner
pending the session-addressing rework — see QUESTIONS.md Q1/Q4.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from typing import Any

import httpx


def _base_url(args: argparse.Namespace) -> str:
    if args.url:
        return str(args.url).rstrip("/")
    return f"http://{args.host}:{args.port}"


async def _rest(
    args: argparse.Namespace, method: str, path: str, body: Any | None = None
) -> tuple[int, Any]:
    async with httpx.AsyncClient() as http:
        r = await http.request(method, f"{_base_url(args)}{path}", json=body, timeout=10.0)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text


async def _call_meta(args: argparse.Namespace, tool: str, tool_args: dict[str, Any]) -> str:
    """Invoke a combiner__* meta-tool over a short-lived MCP control session."""
    from fastmcp import Client

    async with Client(f"{_base_url(args)}/mcp") as client:
        result = await client.call_tool(tool, tool_args)
        parts = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
        return "\n".join(parts)


def _emit(args: argparse.Namespace, data: Any) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, indent=2, default=str))


# ── start / stop: combiner process lifecycle via sharedserver ──────────
#
# These two verbs are process-lifecycle (they shell out to the ``sharedserver``
# binary to refcount the *combiner's own* process), unlike the other verbs which
# drive an already-running combiner over REST/MCP. They are the native-CLI
# equivalent of the Claude plugin's ``hooks/start.sh``.
#
# The reference is tied to the *calling shell* (this CLI process's parent), NOT
# to the short-lived ``mcp-combiner`` process — so the combiner outlives
# ``mcp-combiner start`` and stops when the shell exits (or on ``mcp-combiner
# stop``). sharedserver's ``--pid`` default is the immediate caller, which here
# would be this CLI and would drop the reference the instant we return, so we
# always pass ``--pid`` explicitly.


def _resolve_config(explicit: str | None) -> str | None:
    """Resolve the servers.json path: explicit flag, then env, then standard files.

    Mirrors the plugin ``start.sh`` search order so an existing plugin setup's
    env/config is picked up by the CLI verb too.
    """
    if explicit:
        return explicit
    for env in ("MCP_COMBINER_CONFIG", "CLAUDE_MCP_COMBINER_CONFIG"):
        val = os.environ.get(env)
        if val:
            return val
    user = os.environ.get("USER", "")
    candidates = [
        os.path.expanduser(f"~/.cache/secrets/{user}.mcpservers.json"),
        os.path.expanduser("~/.config/mcp-combiner/servers.json"),
        os.path.expanduser("~/.config/mcp/servers.json"),
    ]
    return next((c for c in candidates if os.path.isfile(c)), None)


def _default_log_dir() -> str:
    """Default directory for combiner log files (XDG state convention).

    Parity with the Neovim plugin, which gives the combiner two files under
    stdpath("log"): ``mcp-combiner.log`` (raw stdout/stderr, captured by
    sharedserver's ``--log-file``) and ``mcp-combiner-py.log`` (the combiner's
    own ``--log-file`` — fastmcp, OAuth, httpx detail). Outside Neovim there is
    no stdpath("log"), so both default here instead.
    """
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "mcp-combiner")


def _resolve_capture_log(explicit: str | None) -> str | None:
    """Resolve the sharedserver stdout/stderr capture path (``use --log-file``).

    ``--log-file none`` disables capture; unset defaults to
    ``<state>/mcp-combiner/mcp-combiner.log``. Previously the default was NO
    capture, which meant a combiner started by this CLI discarded stdout/stderr
    entirely — an outage left no server-side record.
    """
    if explicit == "none":
        return None
    if explicit:
        return explicit
    log_dir = _default_log_dir()
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "mcp-combiner.log")


def _combiner_serve_argv(config: str, host: str, port: int, extra: list[str]) -> list[str]:
    """Build the ``mcp-combiner --mcp …`` serve command sharedserver will run.

    Prefers the installed ``mcp-combiner`` entry point (absolute path, so it works
    detached from the current venv activation); falls back to ``python -m
    mcp_combiner`` using the running interpreter.

    The served combiner also gets a default ``--log-file`` at
    ``<state>/mcp-combiner/mcp-combiner-py.log`` — the second half of the
    Neovim plugin's two-file scheme (see ``_default_log_dir``). An explicit
    ``--log-file`` in *extra* wins; the combiner creates the parent directory
    itself.
    """
    exe = shutil.which("mcp-combiner")
    base = [exe] if exe else [sys.executable, "-m", "mcp_combiner"]
    argv = [*base, "--mcp", "--config", config, "--port", str(port), "--host", host, *extra]
    if "--log-file" not in extra:
        argv += ["--log-file", os.path.join(_default_log_dir(), "mcp-combiner-py.log")]
    return argv


def _build_start_cmd(
    binary: str,
    *,
    name: str,
    pid: int,
    grace_period: str,
    serve_argv: list[str],
    log_file: str | None = None,
) -> list[str]:
    """Build the ``sharedserver use`` argv that launches the combiner attached to *pid*."""
    cmd = [
        binary,
        "use",
        name,
        "--pid",
        str(pid),
        "--grace-period",
        grace_period,
        "--metadata",
        f"cli-{pid}",
    ]
    if log_file:
        cmd += ["--log-file", log_file]
    cmd.append("--")
    cmd += serve_argv
    return cmd


def _build_stop_cmd(binary: str, *, name: str, pid: int) -> list[str]:
    """Build the ``sharedserver unuse`` argv that drops *pid*'s reference."""
    return [binary, "unuse", name, "--pid", str(pid)]


# How long start/restart --wait polls the combiner's /health. The combiner binds
# its port only AFTER its ASGI lifespan startup completes, and that startup
# `sharedserver use`s + health-polls every backing server first (config
# health_timeout, default 30s, plus up to 15s for the `use` itself — concurrent
# across servers, so ONE dead upstream delays the bind by ~45s). The previous
# 30s poll therefore expired mid-startup and reported "not yet healthy" for
# restarts that came up fine moments later. 60s covers that worst default case
# with margin while keeping true-failure reporting reasonably snappy.
_STARTUP_HEALTH_TIMEOUT = 60.0


def _parse_registered_command(info_text: str) -> list[str] | None:
    """Extract the registered child command from ``sharedserver info`` output.

    The ``Command:`` line holds the argv sharedserver spawns, space-joined;
    shlex-split it back. ``None`` when the line is missing or empty. An argv
    element with embedded spaces would mis-split — acceptable for a best-effort
    reuse that falls back to a rebuilt command.
    """
    import shlex

    for line in info_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Command:"):
            rest = stripped[len("Command:") :].strip()
            return shlex.split(rest) if rest else None
    return None


async def _registered_command(binary: str, name: str) -> list[str] | None:
    """Best-effort harvest of the running server's registered command, else None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "info",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except OSError:
        return None
    if proc.returncode != 0 or not out:
        return None
    return _parse_registered_command(out.decode(errors="replace"))


def _argv_flag_value(argv: list[str], flag: str) -> str | None:
    """The value following *flag* in *argv*, or None."""
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _strip_flag_with_value(argv: list[str], flag: str) -> list[str]:
    """Remove every ``flag <value>`` pair from *argv*.

    Harvest hygiene: ``--restore`` is a one-shot flag consumed by the boot it
    was passed to — a harvested argv must never echo it into the next restart.
    """
    out: list[str] = []
    skip = False
    for item in argv:
        if skip:
            skip = False
            continue
        if item == flag:
            skip = True
            continue
        out.append(item)
    return out


def _handover_path(name: str) -> str:
    """Where a sanctioned restart stages its one-shot handover file."""
    cache_dir = os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "mcp-combiner"
    )
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"handover-{name}.json")


async def _arm_handover(host: str, port: int, path: str) -> bool:
    """Ask the running combiner to write a handover file at shutdown.

    Best-effort: a combiner too wedged to answer gets a restore-less restart
    (today's fresh boot — correct, its state is suspect).
    """
    url = f"http://{host}:{port}/handover/prepare"
    try:
        async with httpx.AsyncClient() as http:
            r = await http.post(url, json={"path": path}, timeout=3.0)
            return r.status_code == 200
    except httpx.HTTPError:
        return False


async def _poll_health(host: str, port: int, timeout: float) -> bool:
    """Poll the combiner's /health until it returns 200 or *timeout* expires."""
    import time

    deadline = time.monotonic() + timeout
    url = f"http://{host}:{port}/health"
    async with httpx.AsyncClient() as http:
        while time.monotonic() < deadline:
            try:
                r = await http.get(url, timeout=2.0)
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    return False


async def cmd_start(args: argparse.Namespace) -> int:
    from mcp_combiner.sharedserver import _require_binary

    config = _resolve_config(args.config)
    if not config:
        print(
            "start: no config file found. Pass --config PATH, set MCP_COMBINER_CONFIG, "
            "or create ~/.config/mcp-combiner/servers.json.",
            file=sys.stderr,
        )
        return 2
    if not os.path.isfile(config):
        print(f"start: config file not found: {config}", file=sys.stderr)
        return 2

    # Attach the reference to the calling shell (this CLI's parent), not the CLI.
    pid = args.pid if args.pid is not None else os.getppid()

    extra = list(args.serve_args or [])
    if extra and extra[0] == "--":  # argparse REMAINDER keeps the separator
        extra = extra[1:]
    serve_argv = _combiner_serve_argv(config, args.host, args.port, extra)

    try:
        binary = _require_binary()
    except FileNotFoundError as exc:
        print(f"start: {exc}", file=sys.stderr)
        return 1

    cmd = _build_start_cmd(
        binary,
        name=args.name,
        pid=pid,
        grace_period=args.grace_period,
        serve_argv=serve_argv,
        log_file=_resolve_capture_log(args.log_file),
    )
    if args.dry_run:
        print(" ".join(cmd))
        return 0

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        print(f"start: sharedserver use failed (exit {proc.returncode})", file=sys.stderr)
        if err and err.strip():
            print("  " + err.decode().strip(), file=sys.stderr)
        return 1
    if out and out.strip():
        print(out.decode().strip())

    url = f"http://{args.host}:{args.port}/mcp"
    if args.wait:
        ready = await _poll_health(args.host, args.port, _STARTUP_HEALTH_TIMEOUT)
        state = "ready" if ready else _not_serving_state()
    else:
        state = "attached"
    print(f"combiner '{args.name}' {state} — {url} (ref pid {pid}, grace {args.grace_period})")
    return 0


def _not_serving_state() -> str:
    """Verdict for a --wait poll that expired without /health answering.

    Deliberately NOT "unhealthy": the port binds only after lifespan startup
    finishes bringing up every backing sharedserver, so an expired poll usually
    means "still starting", not "failed". Say so, and point at the evidence.
    """
    return (
        f"not serving after {int(_STARTUP_HEALTH_TIMEOUT)}s — likely still starting "
        "(the port binds only once every backing sharedserver is up); check "
        f"`mcp-combiner status` shortly, or the logs in {_default_log_dir()}"
    )


async def cmd_stop(args: argparse.Namespace) -> int:
    from mcp_combiner.sharedserver import _require_binary

    pid = args.pid if args.pid is not None else os.getppid()
    try:
        binary = _require_binary()
    except FileNotFoundError as exc:
        print(f"stop: {exc}", file=sys.stderr)
        return 1

    cmd = _build_stop_cmd(binary, name=args.name, pid=pid)
    if args.dry_run:
        print(" ".join(cmd))
        return 0

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        print(f"stop: sharedserver unuse failed (exit {proc.returncode})", file=sys.stderr)
        if err and err.strip():
            print("  " + err.decode().strip(), file=sys.stderr)
        return 1
    if out and out.strip():
        print(out.decode().strip())
    print(
        f"combiner '{args.name}' reference dropped (pid {pid}); it stops when the last "
        f"client detaches and the grace period lapses."
    )
    return 0


async def _sharedserver_refcount(binary: str, name: str) -> int | None:
    """Current refcount for *name* via ``sharedserver info --json``.

    Returns ``None`` if the server isn't running or the output can't be parsed.
    """
    proc = await asyncio.create_subprocess_exec(
        binary,
        "info",
        name,
        "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return None
    if isinstance(data, list):  # tolerate an array-shaped payload
        data = next((d for d in data if isinstance(d, dict) and d.get("name") == name), None)
    rc = data.get("refcount") if isinstance(data, dict) else None
    return rc if isinstance(rc, int) else None


async def cmd_restart_combiner(args: argparse.Namespace) -> int:
    """Bounce the whole combiner process (mirrors ``:MCPRestart`` / ``:MCPRestart!``).

    A plain decref would NOT restart a sole-client combiner — it just drops the
    refcount to 0 and the watcher enters its grace window with the process still
    running, so a re-``use`` re-attaches the SAME process. Instead we
    ``admin stop --force`` (SIGTERM, then SIGKILL) so the combiner runs its
    lifespan shutdown and decrefs its OWN downstreams first, then ``use`` a fresh
    process attached to the calling shell. Other clients reconnect on their next
    stream/health cycle.
    """
    from mcp_combiner.sharedserver import _require_binary

    try:
        binary = _require_binary()
    except FileNotFoundError as exc:
        print(f"restart: {exc}", file=sys.stderr)
        return 1

    name = args.name
    refcount = await _sharedserver_refcount(binary, name)
    if refcount is not None and refcount > 1 and not args.force:
        print(
            f"restart: combiner '{name}' has {refcount} clients attached; restarting "
            f"bounces the process for all of them. Re-run with --force.",
            file=sys.stderr,
        )
        return 1
    if args.force and refcount is not None and refcount > 1:
        print(
            f"restart: force-restarting '{name}' ({refcount - 1} other client(s) will reconnect)",
            file=sys.stderr,
        )

    pid = args.pid if args.pid is not None else os.getppid()

    # Serve argv: reuse the RUNNING daemon's registered command verbatim unless
    # the caller explicitly re-specifies serve parameters (--config/--host/--port).
    # Restart means "the same thing, fresh process" — the registered argv carries
    # flags this CLI cannot reconstruct (Neovim's --schema-fix / validation /
    # --log-file choices), and rebuilding from scratch silently dropped them and
    # relocated the py-log. Fall back to a rebuild when nothing is registered or
    # the info output is unparsable.
    serve_argv: list[str] | None = None
    overridden = args.config is not None or args.host is not None or args.port is not None
    if not overridden:
        serve_argv = await _registered_command(binary, name)
        if serve_argv:
            print("restart: reusing the running combiner's registered command", file=sys.stderr)
    if serve_argv is None:
        config = _resolve_config(args.config)
        if not config:
            print(
                "restart: no config file found. Pass --config PATH, set MCP_COMBINER_CONFIG, "
                "or create ~/.config/mcp-combiner/servers.json.",
                file=sys.stderr,
            )
            return 2
        if not os.path.isfile(config):
            print(f"restart: config file not found: {config}", file=sys.stderr)
            return 2
        serve_argv = _combiner_serve_argv(config, args.host or "127.0.0.1", args.port or 9741, [])

    # Health-poll / URL target: explicit flags win; otherwise read host/port from
    # the argv we are about to run (a reused command may serve off-default).
    host = args.host or _argv_flag_value(serve_argv, "--host") or "127.0.0.1"
    try:
        port = args.port or int(_argv_flag_value(serve_argv, "--port") or 9741)
    except ValueError:
        port = 9741

    # A harvested argv must never echo a consumed --restore into this restart.
    final_argv: list[str] = _strip_flag_with_value(serve_argv, "--restore")

    stop_cmd = [binary, "admin", "stop", "--force", name]

    def _start_cmd() -> list[str]:
        return _build_start_cmd(
            binary,
            name=name,
            pid=pid,
            grace_period=args.grace_period,
            serve_argv=final_argv,
            log_file=_resolve_capture_log(args.log_file),
        )

    if args.dry_run:
        # Dry run must not touch the running combiner, so the handover is not
        # armed and --restore is not shown — the real run adds it when armed.
        print(" ".join(stop_cmd))
        print(" ".join(_start_cmd()))
        return 0

    # Sanctioned handover: arm the running combiner to write its one-shot
    # state file at shutdown (token filters, nvim binds, parked isolated
    # sessions); the successor consumes it via --restore. A combiner too
    # wedged to answer restarts restore-less — fresh boot, its state is
    # suspect anyway.
    handover_file = _handover_path(name)
    if await _arm_handover(host, port, handover_file):
        final_argv = [*final_argv, "--restore", handover_file]
        print("restart: handover armed — successor will restore session state", file=sys.stderr)
    else:
        print(
            "restart: combiner did not answer handover prepare — restarting fresh",
            file=sys.stderr,
        )
    start_cmd = _start_cmd()

    # 1) Graceful stop (escalates to SIGKILL). A non-zero exit (e.g. "not
    #    running") is fine — we're about to start a fresh one regardless.
    proc = await asyncio.create_subprocess_exec(
        *stop_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    if out and out.strip():
        print(out.decode().strip())

    # 2) Start a fresh process attached to the calling shell. `admin stop` waits
    #    for teardown to converge before returning, so the port is already free.
    proc = await asyncio.create_subprocess_exec(
        *start_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        print(f"restart: sharedserver use failed (exit {proc.returncode})", file=sys.stderr)
        if err and err.strip():
            print("  " + err.decode().strip(), file=sys.stderr)
        return 1
    if out and out.strip():
        print(out.decode().strip())

    url = f"http://{host}:{port}/mcp"
    if args.wait:
        ready = await _poll_health(host, port, _STARTUP_HEALTH_TIMEOUT)
        state = "ready" if ready else _not_serving_state()
    else:
        state = "attached"
    print(
        f"combiner '{name}' restarted, {state} — {url} (ref pid {pid}, grace {args.grace_period})"
    )
    return 0


_STATE_GLYPH = {
    "ready": "●",
    "connected": "◐",
    "starting": "◌",
    "disabled": "○",
    "auth_failed": "✗",
    "disconnected": "✗",
    "unreachable": "✗",
}


async def cmd_status(args: argparse.Namespace) -> int:
    code, health = await _rest(args, "GET", "/health")
    if code != 200:
        print(f"combiner unreachable at {_base_url(args)} (HTTP {code})", file=sys.stderr)
        return 1
    if args.json:
        _emit(args, health)
        return 0
    servers = health.get("servers", {})
    print(f"combiner ok  ({_base_url(args)}, boot {health.get('boot_id', '?')[:8]})")
    if not servers:
        print("  no servers configured")
        return 0
    width = max(len(n) for n in servers)
    for name in sorted(servers):
        info = servers[name]
        state = info.get("state", "?")
        glyph = _STATE_GLYPH.get(state, "?")
        transport = info.get("transport", "?")
        print(f"  {glyph} {name:<{width}}  {state:<12} {transport}")
    pending = health.get("pending_oauth") or []
    if pending:
        print(f"  pending oauth: {', '.join(pending)}")
    return 0


async def cmd_health(args: argparse.Namespace) -> int:
    code, health = await _rest(args, "GET", "/health")
    _emit(args, health)
    return 0 if code == 200 else 1


async def cmd_enable(args: argparse.Namespace) -> int:
    _emit(args, await _call_meta(args, "combiner__enable_server", {"server_name": args.server}))
    return 0


async def cmd_disable(args: argparse.Namespace) -> int:
    _emit(args, await _call_meta(args, "combiner__disable_server", {"server_name": args.server}))
    return 0


async def cmd_restart_server(args: argparse.Namespace) -> int:
    """Restart a single MCP upstream (like :MCPRestartServer) — never the combiner."""
    _emit(args, await _call_meta(args, "combiner__restart_server", {"server_name": args.server}))
    return 0


async def cmd_reload(args: argparse.Namespace) -> int:
    _emit(args, await _call_meta(args, "combiner__reload_config", {}))
    return 0


async def cmd_call(args: argparse.Namespace) -> int:
    tool_args = json.loads(args.args) if args.args else {}
    _emit(args, await _call_meta(args, args.tool, tool_args))
    return 0


async def cmd_tools(args: argparse.Namespace) -> int:
    from fastmcp import Client

    async with Client(f"{_base_url(args)}/mcp") as client:
        tools = await client.list_tools()
    if args.json:
        _emit(args, [{"name": t.name, "description": t.description} for t in tools])
    else:
        for t in sorted(tools, key=lambda t: str(t.name)):
            print(t.name)
    return 0


async def cmd_session(args: argparse.Namespace) -> int:
    token = args.token
    if args.session_op == "status":
        if token:
            code, data = await _rest(args, "GET", f"/sessions/token/{token}/filter")
        else:
            code, data = await _rest(args, "GET", "/sessions")
        _emit(args, data)
        return 0 if code == 200 else 1

    if not token:
        print("session enable/disable/allow/clear require --token", file=sys.stderr)
        return 2

    if args.session_op == "enable":
        code, data = await _rest(
            args, "POST", f"/sessions/token/{token}/filter", {"enable": args.server}
        )
    elif args.session_op == "disable":
        code, data = await _rest(
            args, "POST", f"/sessions/token/{token}/filter", {"disable": args.server}
        )
    elif args.session_op == "allow":
        allowed = [s for s in (args.servers or "").split(",") if s]
        code, data = await _rest(
            args, "POST", f"/sessions/token/{token}/filter", {"allowed_servers": allowed}
        )
    elif args.session_op == "clear":
        code, data = await _rest(args, "DELETE", f"/sessions/token/{token}/filter")
    else:  # pragma: no cover - argparse restricts choices
        return 2
    _emit(args, data)
    return 0 if code == 200 else 1


def add_ctl_parsers(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Attach the control-plane subcommands to the top-level parser."""

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--url", default=None, help="Combiner base URL (overrides host/port)")
        p.add_argument("--host", default="127.0.0.1", help="Combiner host (default: 127.0.0.1)")
        p.add_argument("--port", type=int, default=9741, help="Combiner port (default: 9741)")
        p.add_argument("--json", action="store_true", help="Raw JSON output")

    # start / stop — process lifecycle via sharedserver, refcounted to the
    # calling shell (not this CLI). Idempotent: a second `start` from another
    # shell just adds a reference; the combiner stops after the last `stop`
    # (or the last referencing shell exits) plus the grace period.
    p = subparsers.add_parser(
        "start",
        help="Launch the combiner via sharedserver, attached to the calling shell "
        "(refcounted). Idempotent — extra starts just add references.",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to servers.json (default: $MCP_COMBINER_CONFIG, "
        "$CLAUDE_MCP_COMBINER_CONFIG, then standard locations)",
    )
    p.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=9741, help="Port to bind (default: 9741)")
    p.add_argument("--name", default="mcp-combiner", help="sharedserver name to register under")
    p.add_argument(
        "--grace-period",
        dest="grace_period",
        default="30m",
        help="Keep the combiner alive this long after the last client detaches (default: 30m)",
    )
    p.add_argument(
        "--pid",
        type=int,
        default=None,
        help="Client PID to attach the reference to (default: the calling shell)",
    )
    p.add_argument(
        "--log-file",
        dest="log_file",
        default=None,
        help="Combiner stdout/stderr capture path (default: "
        "~/.local/state/mcp-combiner/mcp-combiner.log; 'none' disables)",
    )
    p.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Poll /health until the combiner is ready (default: wait)",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print the sharedserver command instead of running it",
    )
    p.add_argument(
        "serve_args",
        nargs=argparse.REMAINDER,
        help="Extra serve args passed through to the combiner after `--` "
        "(e.g. -- --no-output-validation --schema-fix empty_object)",
    )
    p.set_defaults(func=cmd_start, url=None)

    p = subparsers.add_parser(
        "stop",
        help="Drop this shell's reference to the combiner (decref). It stops when "
        "the last reference is gone and the grace period lapses.",
    )
    p.add_argument("--name", default="mcp-combiner", help="sharedserver name it was started under")
    p.add_argument(
        "--pid",
        type=int,
        default=None,
        help="Client PID whose reference to drop (default: the calling shell — this CLI's parent)",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print the sharedserver command instead of running it",
    )
    p.set_defaults(func=cmd_stop, url=None)

    p = subparsers.add_parser(
        "restart",
        help="Restart the whole combiner: bounce the process (like :MCPRestart). "
        "Use --force when other clients are attached. For one upstream, use restart-server.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Bounce the combiner even if other clients are attached (they reconnect)",
    )
    p.add_argument(
        "--config",
        default=None,
        help="servers.json for the fresh combiner. Omitted (like --host/--port): the "
        "running combiner's registered command is reused verbatim, preserving flags "
        "this CLI cannot reconstruct. Passing any of the three rebuilds from scratch "
        "(config default: $MCP_COMBINER_CONFIG, $CLAUDE_MCP_COMBINER_CONFIG, then "
        "standard locations)",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Host to bind (default: the reused command's, else 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind (default: the reused command's, else 9741)",
    )
    p.add_argument("--name", default="mcp-combiner", help="sharedserver name")
    p.add_argument(
        "--grace-period",
        dest="grace_period",
        default="30m",
        help="Grace period for the fresh process (default: 30m)",
    )
    p.add_argument(
        "--pid",
        type=int,
        default=None,
        help="Client PID to attach the fresh process to (default: the calling shell)",
    )
    p.add_argument(
        "--log-file",
        dest="log_file",
        default=None,
        help="Combiner stdout/stderr capture path (default: "
        "~/.local/state/mcp-combiner/mcp-combiner.log; 'none' disables)",
    )
    p.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Poll /health until the combiner is ready (default: wait)",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print the sharedserver commands instead of running them",
    )
    p.set_defaults(func=cmd_restart_combiner, url=None)

    p = subparsers.add_parser("status", help="Show combiner + per-server status")
    _common(p)
    p.set_defaults(func=cmd_status)

    p = subparsers.add_parser("health", help="Dump the raw /health payload")
    _common(p)
    p.set_defaults(func=cmd_health)

    for name, fn, help_ in (
        ("enable", cmd_enable, "Enable a server (mount + connect)"),
        ("disable", cmd_disable, "Disable a server (unmount + disconnect)"),
        (
            "restart-server",
            cmd_restart_server,
            "Restart a single MCP upstream (hard bounce for owned processes) — "
            "like :MCPRestartServer. Does NOT restart the combiner.",
        ),
    ):
        p = subparsers.add_parser(name, help=help_)
        p.add_argument("server", help="Server name from the combiner config")
        _common(p)
        p.set_defaults(func=fn)

    p = subparsers.add_parser("reload", help="Re-read the config file and apply the diff")
    _common(p)
    p.set_defaults(func=cmd_reload)

    p = subparsers.add_parser("call", help="Call any tool through the combiner")
    p.add_argument("tool", help="Namespaced tool name (e.g. myserver_echo)")
    p.add_argument("--args", default=None, help="Tool arguments as a JSON object")
    _common(p)
    p.set_defaults(func=cmd_call)

    p = subparsers.add_parser("tools", help="List the tools the combiner advertises")
    _common(p)
    p.set_defaults(func=cmd_tools)

    p = subparsers.add_parser(
        "session",
        help="Per-chat session filters (by token) — WIP: status works; "
        "enable/disable/allow are recorded but not yet applied (see QUESTIONS.md Q1/Q4)",
    )
    p.add_argument(
        "session_op",
        choices=("status", "enable", "disable", "allow", "clear"),
        help="status: list sessions / one token's filter; enable/disable: one server; "
        "allow: comma-separated allow-list; clear: drop the filter",
    )
    p.add_argument("server", nargs="?", default=None, help="Server name (enable/disable)")
    p.add_argument("--token", default=None, help="Chat token (UUID) naming the target session")
    p.add_argument("--servers", default=None, help="Comma-separated server names (for 'allow')")
    _common(p)
    p.set_defaults(func=cmd_session)


def run(args: argparse.Namespace) -> int:
    """Run the selected control command."""
    try:
        result: int = asyncio.run(args.func(args))
        return result
    except httpx.ConnectError:
        print(f"cannot reach combiner at {_base_url(args)}", file=sys.stderr)
        return 1
    except Exception as e:  # surfaced as a clean CLI error, not a traceback
        print(f"error: {e}", file=sys.stderr)
        return 1
