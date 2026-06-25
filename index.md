# mcp-companion

An MCP aggregator (combiner) and editor integration: aggregate multiple
[Model Context Protocol](https://modelcontextprotocol.io) servers behind a
single HTTP endpoint, with first-class
[CodeCompanion.nvim](https://github.com/olimorris/codecompanion.nvim) support.
The Python combiner runs standalone — any MCP-aware client can connect — while
the Lua plugin layer adds Neovim features: tool registration, editor context,
slash commands, ACP forwarding, and a status UI.

Everything lives in the [overview and quick start](README.md):

| I want to… | Section |
|------------|---------|
| Run the combiner standalone | [MCP Combiner (standalone)](README.md#mcp-combiner-standalone) |
| Write a servers config | [MCP Server Config](README.md#mcp-server-config) |
| Set up OAuth / bearer auth | [Authentication](README.md#authentication) |
| Install the Neovim plugin | [Neovim Integration](README.md#neovim-integration) |
| Let an agent drive my Neovim | [Controlling Neovim from an agent](docs/neovim-control.md) |
| Scope servers per project or session | [Per-session server gating](README.md#per-session-server-gating) |
