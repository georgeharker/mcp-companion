"""Simple test combiner with direct tools only (no proxy) to test multi-session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import JSONResponse

combiner = FastMCP("test-combiner")


@combiner.tool()
def echo(message: str) -> str:
    """Echo back the message."""
    return f"Echo: {message}"


@combiner.tool()
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return f"Sum: {a + b}"


@combiner.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    from starlette.responses import JSONResponse as _JSONResponse

    return _JSONResponse({"status": "ok"})


if __name__ == "__main__":
    combiner.run(transport="http", host="127.0.0.1", port=9742)
