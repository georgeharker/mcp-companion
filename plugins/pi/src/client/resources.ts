// MCP resources → read_* Pi tools, compatible with pi-mcp-adapter's conventions.
//
// A resource is server-exposed content addressed by URI (the GET to tools' POST);
// each becomes a zero-parameter tool the model can call directly, named
// `read_<sanitized>` (resourceNameToToolName ported verbatim). Reads go through the
// combiner's proxied resources/read; text is guarded, binaries noted by size.
//
// Interactive mcp-app resources (mimeType `text/html;profile=mcp-app`, `ui://` URIs)
// are flagged in the tool description — read_* returns their markup as text; the
// interactive browser experience is the ext-apps host (later, see adapter-design.md).

import type { ToolDefinition } from "../pi.js"
import type { CombinerConnection, ResourceSummary } from "./connection.js"
import type { ServerFilter } from "./config-ladder.js"
import { renderResourceResult, textResult, toolUiResourceUri } from "./render.js"
import { openInBrowser } from "./proxy-tool.js"
import { callRenderer, resultRenderer } from "./renderers.js"

/** Ported verbatim from pi-mcp-adapter's resource-tools.ts (MIT, © 2026 Nico Bailon). */
export function resourceNameToToolName(name: string): string {
    let result = name
        .replace(/[^a-zA-Z0-9]/g, "_")
        .replace(/_+/g, "_")
        .replace(/^_+/, "") // Remove leading underscores
        .replace(/_+$/, "") // Remove trailing underscores
        .toLowerCase()

    // Ensure we have a valid name
    if (!result || /^\d/.test(result)) {
        result = "resource" + (result ? "_" + result : "")
    }

    return result
}

export const MCP_APP_MIME = "text/html;profile=mcp-app"

/** True for interactive ext-apps resources: read_* returns their markup AND
 *  surfaces the combiner UI-host URL (which serves the live widget). */
export function isInteractiveResource(r: ResourceSummary): boolean {
    return r.mimeType === MCP_APP_MIME || r.uri.startsWith("ui://")
}

/** Attribute a resource to an upstream server for filtering: ui:// URIs carry the
 *  server as the host component (ui://todoist/…); otherwise fall back to the name's
 *  leading word before a dash/underscore. */
export function resourceServer(r: ResourceSummary): string | undefined {
    const m = r.uri.match(/^[a-z][a-z0-9+.-]*:\/\/([^/?#]+)/i)
    if (m?.[1]) return m[1].split(".")[0]?.toLowerCase()
    const name = r.name ?? r.uri
    const head = name.split(/[-_]/, 1)[0]
    return head ? head.toLowerCase() : undefined
}

/** Apply the project server filter to a resource list (mirrors tool filtering;
 *  attribution by uri host, with `_`/`-` name-prefix fallback for underscore-named
 *  servers whose resources carry the full prefixed name). */
export function filterResources(resources: ResourceSummary[], filter: ServerFilter | undefined): ResourceSummary[] {
    if (!filter) return resources
    const matches = (r: ResourceSummary, entry: string): boolean =>
        resourceServer(r) === entry || (r.name ?? "").startsWith(`${entry}_`) || (r.name ?? "").startsWith(`${entry}-`)
    if (filter.allow?.length) {
        return resources.filter((r) => filter.allow!.some((e) => matches(r, e)))
    }
    if (filter.deny?.length) {
        return resources.filter((r) => !filter.deny!.some((e) => matches(r, e)))
    }
    return resources
}

/** Discover resources and register one read_* tool each. Idempotent; returns count. */
export async function syncResourceTools(
    register: (tool: ToolDefinition) => void,
    connection: CombinerConnection,
    registered: Set<string>,
    filter: ServerFilter | undefined,
    notify: (message: string, level: "info" | "warn" | "error") => void,
): Promise<number> {
    let resources: ResourceSummary[] = []
    try {
        resources = filterResources(await connection.listResources(), filter)
    } catch (e) {
        notify(`resource discovery failed (${e instanceof Error ? e.message : String(e)})`, "warn")
        return 0
    }
    for (const resource of resources) {
        const label = resource.name ?? resource.uri
        const toolName = `read_${resourceNameToToolName(label)}`
        if (registered.has(toolName)) continue
        registered.add(toolName)
        const interactive = isInteractiveResource(resource)
        const description = [
            resource.description ?? `Read MCP resource ${resource.uri}`,
            `uri: ${resource.uri}`,
            resource.mimeType ? `type: ${resource.mimeType}` : undefined,
            interactive
                ? "interactive app resource — markup as text; the interactive widget opens from the URL appended to results"
                : undefined,
        ]
            .filter(Boolean)
            .join(" | ")
        register({
            name: toolName,
            label: `MCP resource: ${label}`,
            description,
            promptSnippet: `Read MCP resource ${label}.`,
            parameters: { type: "object", properties: {} },
            renderCall: callRenderer(toolName, () => resource.uri),
            renderResult: resultRenderer(() => toolName, toolName),
            execute: async (
                _toolCallId: string,
                _params: Record<string, unknown>,
                _signal: AbortSignal | undefined,
                _onUpdate: unknown,
                ctx: { hasUI: boolean } | undefined,
            ) => {
                try {
                    const result = await connection.readResource(resource.uri)
                    let text = renderResourceResult(result)
                    // Interactive resources: the combiner's UI host serves the
                    // widget for this session's token — surface + auto-open.
                    if (interactive) {
                        const url = connection.uiUrlFor(resource.uri)
                        if (url) {
                            text = `${text}\n\ninteractive: ${url}`
                            if (ctx?.hasUI) openInBrowser(url)
                        }
                    }
                    return textResult(text, { mode: "resource", uri: resource.uri })
                } catch (e) {
                    throw new Error(`${toolName}: resource read failed (${e instanceof Error ? e.message : String(e)})`)
                }
            },
        })
    }
    return resources.length
}
