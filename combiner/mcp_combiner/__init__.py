"""mcp-combiner — a FastMCP MCP aggregator: fronts multiple MCP servers behind one endpoint."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mcp-combiner")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0.dev0"
