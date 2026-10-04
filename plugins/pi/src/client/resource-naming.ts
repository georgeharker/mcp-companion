// Resource naming helpers — extracted from legacy-client/resources.ts during the
// Extracted during the legacy-client factoring so surfaces that survive the native migration (panel, widget
// resource attribution) don't import the deprecated legacy-client/ directory.
//
// These name the SAME way legacy read_<resource> tools always did (ported verbatim
// from pi-mcp-adapter, MIT © Nico Bailon); native widget resource support reuses the
// attribution logic, so panel counts and native read_* tools agree on server keys.

import type { ResourceSummary } from "./types.js"

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