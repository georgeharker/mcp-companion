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


_STATE_GLYPH = {
    "ready": "●",
    "connected": "◐",
    "disabled": "○",
    "auth_failed": "✗",
    "disconnected": "✗",
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


async def cmd_restart(args: argparse.Namespace) -> int:
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

    p = subparsers.add_parser("status", help="Show combiner + per-server status")
    _common(p)
    p.set_defaults(func=cmd_status)

    p = subparsers.add_parser("health", help="Dump the raw /health payload")
    _common(p)
    p.set_defaults(func=cmd_health)

    for name, fn, help_ in (
        ("enable", cmd_enable, "Enable a server (mount + connect)"),
        ("disable", cmd_disable, "Disable a server (unmount + disconnect)"),
        ("restart", cmd_restart, "Restart a server (hard bounce for owned processes)"),
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
