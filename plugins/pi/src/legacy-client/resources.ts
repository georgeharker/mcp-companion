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

import { appendFileSync } from "node:fs"
import type { ToolDefinition } from "../pi.js"
import type { CombinerConnection, ResourceSummary } from "../client/connection.js"
import type { ServerFilter } from "../client/config-ladder.js"
// Canonical copies of the naming/interactive-resource helpers live in client/
// (survive the legacy deletion); re-exported here so the legacy tests keep working.
import { isInteractiveResource, openInBrowser, renderResourceResult } from "../client/widget-support.js"
import { resourceNameToToolName, resourceServer } from "../client/resource-naming.js"
import { textResult } from "./render.js"
import { callRenderer, resultRenderer } from "./renderers.js"

export { isInteractiveResource } from "../client/widget-support.js"
export { renderResourceResult, toolUiResourceUri } from "../client/widget-support.js"
export { resourceNameToToolName, resourceServer } from "../client/resource-naming.js"

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

/** Discover resources and register one read_* tool each. Idempotent; returns count.
 *
 *  `registered` is the toolName → resourceUri OWNERSHIP map (persisted by the caller
 *  across syncs). Collision rule: first registration keeps the plain name; a later
 *  resource whose derived name is taken by a DIFFERENT uri gets a server-qualified
 *  name (read_<server>_<name>) + a notice — most servers already self-qualify their
 *  resource names (todoist_task_list), so only bare names (e.g. a mock's "widget"
 *  shadowing svg-mcp's) reach this path. Same-uri re-syncs are no-ops. */
export async function syncResourceTools(
    register: (tool: ToolDefinition) => void,
    connection: CombinerConnection,
    registered: Map<string, string>,
    filter: ServerFilter | undefined,
    notify: (message: string, level: "info" | "warn" | "error") => void,
): Promise<number> {
    let resources: ResourceSummary[] = []
    const dbg = (s: string) => {
    }
    try {
        resources = filterResources(await connection.listResources(), filter)
        dbg(
            `syncResourceTools: ${resources.length} resource(s) after filter: ` +
                resources.map((r) => r.uri).slice(0, 12).join(", "),
        )
    } catch (e) {
        notify(`resource discovery failed (${e instanceof Error ? e.message : String(e)})`, "warn")
        return 0
    }
    for (const resource of resources) {
        const label = resource.name ?? resource.uri
        let toolName = `read_${resourceNameToToolName(label)}`
        const existingOwner = registered.get(toolName)
        if (existingOwner === resource.uri) continue // ours, already registered
        if (existingOwner !== undefined) {
            // Name collision across resources: first registration keeps the plain
            // name; this resource gets a server-qualified one (see docstring).
            const server = resourceServer(resource) ?? "mcp"
            toolName = `read_${resourceNameToToolName(`${server}_${label}`)}`
            if (registered.get(toolName) === resource.uri) continue
            dbg(`syncResourceTools: collision on "${label}" → disambiguated as ${toolName}`)
            notify(
                `resource "${label}" (${server}) collided with another resource's read tool; ` +
                    `registered as ${toolName} instead`,
                "info",
            )
        }
        registered.set(toolName, resource.uri)
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
