"""CLI entry point for mcp-combiner.

``mcp-combiner <command>`` is a control CLI for a running combiner
(status/enable/disable/restart/reload/session/call/tools — see ctl.py);
``mcp-combiner --mcp --config …`` runs the combiner server itself (the
historical bare invocation, which still works with a deprecation warning).
"""

from __future__ import annotations

import argparse
import atexit
import logging
import signal
import sys
import types

import uvicorn

from mcp_combiner import ctl
from mcp_combiner.asgi import ServeOptions, create_app
from mcp_combiner.schemafix import SCHEMA_FIXES
from mcp_combiner.sharedserver import cleanup as cleanup_sharedservers

logger = logging.getLogger(__name__)


def _signal_handler(signum: int, frame: types.FrameType | None) -> None:
    """Handle termination signals.

    This stays installed even though uvicorn replaces it while serving: uvicorn's
    ``capture_signals()`` *saves* our handler, installs its own ``handle_exit``
    for the duration of ``serve()``, and on exit restores ours and re-raises the
    captured signal back to it (uvicorn server.py). So when sharedserver SIGTERMs
    us, uvicorn drives the graceful shutdown (whose lifespan ``finally`` already
    runs ss_manager.stop_all() / decref), then hands the signal back here for a
    clean ``sys.exit(0)``. cleanup is idempotent, so the double call is a no-op.
    """
    logger.info("Received signal %d, cleaning up...", signum)
    cleanup_sharedservers()
    sys.exit(0)


def _add_serve_args(parser: argparse.ArgumentParser) -> None:
    from mcp_combiner import __version__

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Run the combiner MCP server (serve mode). Without this flag, "
        "mcp-combiner is a control CLI — see the subcommands.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to servers.json config file (serve mode)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9741,
        help="Port to listen on (default: 9741)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )

    # OAuth token-caching overrides (both override the config-file 'oauth' section)
    oauth_group = parser.add_mutually_exclusive_group()
    oauth_group.add_argument(
        "--oauth-cache",
        dest="oauth_cache",
        action="store_true",
        default=None,
        help="Enable OAuth disk token caching (overrides config; this is the default)",
    )
    oauth_group.add_argument(
        "--no-oauth-cache",
        dest="oauth_cache",
        action="store_false",
        help=(
            "Disable OAuth disk token caching — tokens kept in memory only "
            "and lost on restart (overrides config)"
        ),
    )
    parser.add_argument(
        "--oauth-token-dir",
        metavar="PATH",
        default=None,
        help=(
            "Directory for OAuth token files "
            "(default: ~/.cache/mcp-combiner/oauth-tokens; overrides config)"
        ),
    )
    parser.add_argument(
        "--normalize-schema",
        dest="normalize_schema",
        action="store_true",
        help="Back-compat alias for --schema-fix anyof_type_hoist",
    )
    parser.add_argument(
        "--schema-fix",
        dest="schema_fix",
        action="append",
        choices=SCHEMA_FIXES,
        default=None,
        metavar="FIX",
        help=(
            "Enable a named tool-schema fix (repeatable). "
            "Choices: " + ", ".join(SCHEMA_FIXES) + ". "
            "anyof_type_hoist hoists a sibling 'type' into anyOf items "
            "(Moonshot/Kimi); empty_object fills missing type/properties so {} never "
            "serializes to [] (Copilot/Joplin); drop_invalid_required drops a "
            "non-list 'required'. Off by default."
        ),
    )
    parser.add_argument(
        "--input-validation",
        dest="input_validation",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Tri-state JSON-schema validation of tool *input* arguments. "
            "--input-validation forces it on; --no-input-validation forces it "
            "off; omit to leave the combiner default (off — inputs are coerced, "
            "not strictly validated)."
        ),
    )
    parser.add_argument(
        "--output-validation",
        dest="output_validation",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Tri-state JSON-schema validation of tool *output*. "
            "--no-output-validation forces it off (the upstream server already "
            "validated its structured output, so re-validating here is redundant "
            "per-call work — measurably slow for large responses); "
            "--output-validation forces it on; omit to leave the default (on for "
            "tools that declare an outputSchema)."
        ),
    )
    parser.add_argument(
        "--stale-tool-grace",
        dest="stale_tool_grace",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "How long a disconnected server keeps serving its last-known tools "
            "before they are dropped (rides out a transient reconnect). Default "
            "30s. Lower it to drop a killed server's tools sooner."
        ),
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        default=None,
        help="Write logs to this file in addition to stderr (default: none)",
    )
    parser.add_argument(
        "--log-level",
        choices=["trace", "debug", "info", "warn", "error"],
        default="info",
        help=(
            "Verbosity for the combiner logger and httpx/mcp-client loggers "
            "(default: info).  Use 'debug' to capture OAuth metadata-discovery, "
            "token refresh, and httpx request/response detail."
        ),
    )


def _setup_logging(log_level: str, log_file: str | None) -> None:
    # Resolve --log-level to a stdlib logging numeric level.
    # "trace" is treated as DEBUG since stdlib has no TRACE.
    _level_map = {
        "trace": logging.DEBUG,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }
    level = _level_map[log_level]

    # Stderr handler on the combiner logger.  Without this only WARNING+ would
    # appear because Python's root logger defaults to WARNING.
    combiner_logger = logging.getLogger("mcp-combiner")
    combiner_logger.setLevel(level)
    if not combiner_logger.handlers:
        stderr_handler = logging.StreamHandler()
        stderr_handler.setLevel(level)
        stderr_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        combiner_logger.addHandler(stderr_handler)
        combiner_logger.propagate = False  # avoid duplicate messages via root

    # Configure file logging if requested.  File handler always runs at the
    # requested level (decoupled from the file's presence so you can pick
    # INFO+file or DEBUG+stderr-only independently).
    if log_file:
        import pathlib

        log_path = pathlib.Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        # Root catches non-combiner loggers (fastmcp, mcp.client.auth, httpx, …)
        logging.getLogger().addHandler(file_handler)
        logging.getLogger().setLevel(level)
        # propagate=False on combiner_logger means the root handler won't see
        # its messages — attach explicitly.
        combiner_logger.addHandler(file_handler)
        logger.info("Logging to %s at level %s", log_path, log_level)
    else:
        # No file — still apply level globally so DEBUG-on-stderr works.
        logging.getLogger().setLevel(level)

    # At DEBUG, also turn on the SDK loggers that carry the OAuth flow detail.
    if level <= logging.DEBUG:
        for name in ("httpx", "httpcore", "mcp.client.auth", "fastmcp.client.auth"):
            logging.getLogger(name).setLevel(logging.DEBUG)


def _serve(args: argparse.Namespace) -> None:
    options = ServeOptions(
        config=args.config,
        host=args.host,
        port=args.port,
        oauth_cache=args.oauth_cache,
        oauth_token_dir=args.oauth_token_dir,
        normalize_schema=args.normalize_schema,
        schema_fixes=args.schema_fix or [],
        input_validation=args.input_validation,
        output_validation=args.output_validation,
        stale_tool_grace=args.stale_tool_grace,
        log_file=args.log_file,
        log_level=args.log_level,
    )

    _setup_logging(options.log_level, options.log_file)

    # Register cleanup handlers. uvicorn temporarily swaps these out while it
    # serves, but restores them and re-raises the captured signal back to us on
    # shutdown (see _signal_handler), so they DO run. atexit is the backstop for
    # the normal-return path; all three call the same idempotent cleanup.
    atexit.register(cleanup_sharedservers)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Single worker - async handles concurrency
    app = create_app(options)
    uvicorn.run(
        app,
        host=options.host,
        port=options.port,
        log_level="info",
        # Bound the graceful-shutdown drain. The combiner holds long-lived MCP
        # streamable-http / SSE connections that never close on their own, so the
        # uvicorn default (None = wait forever) makes SIGTERM hang — and our
        # supervisor (sharedserver) only waits 5s before escalating to SIGKILL.
        # On SIGKILL the ASGI lifespan shutdown never runs, so we never `unuse`
        # (decref) our downstream sharedservers and they orphan. A short drain
        # lets any genuinely in-flight tool call finish, then force-closes the
        # persistent connections so the lifespan shutdown (stop_all) runs well
        # inside the 5s window. (FastMCP's own run_http_async sets 0 here; we run
        # uvicorn ourselves via http_app(), so we must set it ourselves.)
        timeout_graceful_shutdown=2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-combiner",
        description=(
            "MCP combiner — control CLI for a running combiner, and (with --mcp) "
            "the combiner server itself, aggregating multiple MCP servers behind "
            "one endpoint"
        ),
    )
    _add_serve_args(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    ctl.add_ctl_parsers(subparsers)

    args = parser.parse_args()

    # Control subcommand → ctl.
    if args.command:
        sys.exit(ctl.run(args))

    # Serve mode: --mcp, or the historical bare `--config …` invocation.
    if args.config:
        if not args.mcp:
            print(
                "warning: running the combiner server without --mcp is deprecated; "
                "bare `mcp-combiner` is now the control CLI. Add --mcp to this "
                "invocation (launcher configs: command args gain one '--mcp').",
                file=sys.stderr,
            )
        _serve(args)
        return

    if args.mcp:
        parser.error("serve mode (--mcp) requires --config")

    parser.print_help()
    sys.exit(2)


if __name__ == "__main__":
    main()
