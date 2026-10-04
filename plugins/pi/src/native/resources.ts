// NATIVE RESOURCE TOOLS : read_* tools for
// INTERACTIVE (mcp-app / ui://) resources — the parity half of legacy-client/
// resources.ts that pi's built-in read_mcp_resource deliberately does NOT cover
// (it skips ui:// listings and renders nothing).
//
// Parity contract with legacy read_<resource> tools:
//   - SAME naming: read_<resourceNameToToolName(resource.name ?? uri)>
//   - SAME attribution + project filtering: resourceServer() + `<server>_`/
//     `<server>-` prefix matching against the per-project ServerFilter
//   - SAME read behavior: connection.readResource(uri) → renderResourceResult
//     (markup as text, json prettified, blobs as byte notes, 16KB guard)
//   - SAME surfacing: append the combiner UI-host URL (this session's token),
//     auto-open in the browser when a dialog-capable UI exists (uiAutoOpen)
//   - idempotent registration; re-syncs on resources/list_changed (the
//     connection's onToolsChanged hook covers both list-change notifications)
//
// TEXT/image resources are NOT re-registered here — pi's built-in
// read_mcp_resource + tool_search discovery covers them in native mode; that is
// the deliberate (documented) divergence from legacy, which registered one
// zero-arg tool per resource of every kind.

import type { ExtensionAPI, ToolDefinition } from "../pi.js"
import type { NativeCombinerConnection, ResourceSummary } from "./combiner-connection.js"
import { isInteractiveResource, openInBrowser, renderResourceResult } from "../client/widget-support.js"
import { resourceNameToToolName, resourceServer } from "../client/resource-naming.js"
import type { ServerFilter } from "../client/config-ladder.js"

export type NativeResourceOptions = {
    connection: NativeCombinerConnection
    /** Per-project server filter, same semantics as legacy filterResources. */
    serverFilter?: ServerFilter
    /** Auto-open the widget URL on read (index.ts's uiAutoOpen setting). */
    uiAutoOpen?: boolean
    log: (level: "info" | "warn" | "error", message: string) => void
}

/** Project server filter for resources — mirrors legacy filterResources
 *  attribution semantics (uri host first, `<server>_` name-prefix fallback). */
export function filterInteractiveResources(
    resources: ResourceSummary[],
    filter: ServerFilter | undefined,
): ResourceSummary[] {
    const interactive = resources.filter(isInteractiveResource)
    if (!filter) return interactive
    const matches = (r: ResourceSummary, entry: string): boolean =>
        resourceServer(r) === entry ||
        (r.name ?? "").startsWith(`${entry}_`) ||
        (r.name ?? "").startsWith(`${entry}-`)
    if (filter.allow?.length) return interactive.filter((r) => filter.allow!.some((e) => matches(r, e)))
    if (filter.deny?.length) return interactive.filter((r) => !filter.deny!.some((e) => matches(r, e)))
    return interactive
}

function toReadTool(toolName: string, resource: ResourceSummary, opts: NativeResourceOptions): ToolDefinition {
    const label = resource.name ?? resource.uri
    const url = opts.connection.uiUrlFor(resource.uri)
    return {
        name: toolName,
        label: `MCP resource: ${label}`,
        description: [
            resource.description ?? `Read MCP resource ${resource.uri}`,
            `uri: ${resource.uri}`,
            resource.mimeType ? `type: ${resource.mimeType}` : undefined,
            "interactive app resource — markup as text; the interactive widget opens from the URL appended to results",
        ]
            .filter(Boolean)
            .join(" | "),
        promptSnippet: `Read MCP resource ${label}.`,
        parameters: { type: "object", properties: {} },
        exposure: "direct",
        namespace: { name: "mcp_combiner", description: "mcp-combiner aggregated tools" },
        execute: async (_toolCallId, _params, _signal, _onUpdate, ctx) => {
            try {
                const result = await opts.connection.readResource(resource.uri)
                let text = renderResourceResult(result)
                if (url) {
                    text = `${text}\n\ninteractive: ${url}`
                    if (opts.uiAutoOpen !== false && ctx?.hasUI) openInBrowser(url)
                }
                return { content: [{ type: "text", text }], details: { mode: "resource", uri: resource.uri, url: url || undefined } }
            } catch (e) {
                throw new Error(`${toolName}: resource read failed (${e instanceof Error ? e.message : String(e)})`)
            }
        },
    }
}

/** Register read_* tools for the interactive resources (idempotent, like legacy).
 *  Returns the tool names registered this pass. */
export async function activateNativeResources(
    pi: ExtensionAPI,
    opts: NativeResourceOptions,
    registered: Set<string>,
): Promise<string[]> {
    let resources: ResourceSummary[] = []
    try {
        resources = filterInteractiveResources(await opts.connection.listResources(true), opts.serverFilter)
    } catch (e) {
        opts.log("warn", `interactive resource discovery failed (${e instanceof Error ? e.message : String(e)})`)
        return []
    }
    const added: string[] = []
    for (const resource of resources) {
        const toolName = `read_${resourceNameToToolName(resource.name ?? resource.uri)}`
        if (registered.has(toolName)) continue
        registered.add(toolName)
        pi.registerTool(toReadTool(toolName, resource, opts))
        added.push(toolName)
    }
    if (added.length) opts.log("info", `native resources: ${added.length} interactive resource tool(s) registered`)
    return added
}