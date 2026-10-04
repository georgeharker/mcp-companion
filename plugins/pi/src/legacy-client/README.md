# legacy-client/ — the DEPRECATED tool surface

These are the client-half modules that the native mode (src/native/) replaces.
**This whole directory is the deprecation set**: when the native mode becomes the
default and proves out, delete this directory, the `test/legacy/` tests, and the
four imports in src/index.ts marked "LEGACY CLIENT HALF".

| file | what it did | native replacement |
|---|---|---|
| proxy-tool.ts | `mcp()` search/describe/call proxy tool | every tool registered natively via `pi.registerTool`; discovery via codemode `searchTools()`/`describeNamespace()` + `tool_search` |
| script.ts | `mcpScript` batch tool | built-in `codemode` tool (superset: parallel `Promise.allSettled`, full pi tool set) |
| direct-tools.ts | allowlist/"search" promotion | `exposure` + `toolExposure` mapping in native/tool-surface.ts (`direct` vs `codemode`) |
| resources.ts | `read_<resource>` tools incl. text resources | **interactive (`ui://`/mcp-app) reads now covered by native/resources.ts** (same naming, attribution, renderResourceResult, URL-append + auto-open, built on client/widget-support.ts); text/image resources → built-in `read_mcp_resource` (deliberate divergence: pi's generic tool + tool_search replace the legacy zero-arg per-resource tools) |
| ranking.ts | BM25 ranking for mcp() search | pi's own `searchTools()` ranking |
| render.ts | schema-signature + call/result renders for proxy surfaces | native calls render natively (`server/tool`, args shown); only `toolUiResourceUri` is native-relevant (widget URL helper) |
| renderers.ts | TUI components for proxy/direct-tools results | native native-rendered; unused in native mode |
| schema-signature.ts | proxy describe schema rendering | unused in native mode (describeNative = tool description) |

Shared helpers this directory still needs live in src/client/ (they SURVIVE the
deletion — do not delete these when removing the dir):
- client/tool-matching.ts (glob/server matching, reserved names, ToolSummary shape)
- client/resource-naming.ts (resourceServer, resourceNameToToolName)
- client/widget-support.ts (mcp-app/interactive resources, openInBrowser, toolUiResourceUri,
  renderResourceResult — the widget-hold mechanics native mode reuses)
- client/elicitation.ts (the elicitation bridge — the native connection uses it directly)
- client/connection.ts, config-ladder, settings, footer, panel, control, prompts

Status: still ACTIVE — index.ts's default `client` mode wires it. The mode gate
(`integration: client | nativeTools | auto`) at the top of index.ts decides.