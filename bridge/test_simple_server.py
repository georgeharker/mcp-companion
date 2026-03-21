"""Simple test bridge with direct tools only (no proxy) to test multi-session."""

from fastmcp import FastMCP

bridge = FastMCP("test-bridge")


@bridge.tool()
def echo(message: str) -> str:
    """Echo back the message."""
    return f"Echo: {message}"


@bridge.tool()
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return f"Sum: {a + b}"


@bridge.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    bridge.run(transport="http", host="127.0.0.1", port=9742)
