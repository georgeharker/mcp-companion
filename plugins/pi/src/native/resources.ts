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
import { isRunnerStaleError, runnerStaleHint, safeHasUi } from "../client/runner-stale.js"
import type { ServerFilter } from "../client/config-ladder.js"

export type NativeResourceOptions = {
    connection: NativeCombinerConnection
    /** Per-project server filter, same semantics as legacy filterResources; a getter
     *  resolves per call (worktrees/project switches re-resolve session config). */
    serverFilter?: ServerFilter | (() => ServerFilter | undefined)
    /** The directTools spec (same vocabulary as the tool surface): a read_* tool whose
     *  name matches the allowlist is declared "direct", otherwise codemode — consistent
     *  with how regular tools are exposed (a getter resolves per call). */
    directSpec?: string[] | "search" | (() => string[] | "search" | undefined)
    /** Auto-open the widget URL on read (index.ts's uiAutoOpen setting). */
    uiAutoOpen?: boolean
    log: (level: "info" | "warn" | "error", message: string) => void
}

function resolveFilter(f: NativeResourceOptions["serverFilter"]): ServerFilter | undefined {
    return typeof f === "function" ? f() : f
}
function resolveDirectSpec(d: NativeResourceOptions["directSpec"]): string[] | "search" | undefined {
    return typeof d === "function" ? d() : d
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

function resolveResourceExposure(
    opts: NativeResourceOptions,
    toolName: string,
): NonNullable<ToolDefinition["exposure"]> {
    const spec = resolveDirectSpec(opts.directSpec)
    return Array.isArray(spec) && spec.some((p) => matchesResourceGlob(toolName, p)) ? "direct" : "codemode"
}

function matchesResourceGlob(name: string, pattern: string): boolean {
    // Same glob vocabulary as the tool surface (matchesGlob from tool-matching).
    return name === pattern || (pattern.endsWith("*") && name.startsWith(pattern.slice(0, -1)))
}

function toReadTool(toolName: string, resource: ResourceSummary, opts: NativeResourceOptions): ToolDefinition {
    const label = resource.name ?? resource.uri
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
        exposure: resolveResourceExposure(opts, toolName),
        namespace: { name: "mcp_combiner", description: "mcp-combiner aggregated tools" },
        execute: async (_toolCallId, _params, _signal, _onUpdate, ctx) => {
            // SAFETY: guarded ctx read ONCE at entry, never re-read after the await
            // (client/runner-stale.ts — a mid-window read would reject a COMPLETED
            // resource read; the model retrying would re-read, which is at least
            // read-only, but the failure text helps nobody).
            const hasUi = safeHasUi(ctx)
            try {
                const result = await opts.connection.readResource(resource.uri)
                // Compute the URL PER CALL: the session token changes across /new and
                // resume, and the registered set skips re-registration — a
                // registration-time URL would carry a dead token after the first
                // session switch.
                const url = opts.connection.uiUrlFor(resource.uri)
                let text = renderResourceResult(result)
                if (url) {
                    text = `${text}\n\ninteractive: ${url}`
                    if (opts.uiAutoOpen !== false && hasUi) openInBrowser(url)
                }
                return { content: [{ type: "text", text }], details: { mode: "resource", uri: resource.uri, url: url || undefined } }
            } catch (e) {
                if (isRunnerStaleError(e)) {
                    throw new Error(runnerStaleHint(toolName))
                }
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
        resources = filterInteractiveResources(await opts.connection.listResources(true), resolveFilter(opts.serverFilter))
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