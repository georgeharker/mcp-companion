// Widget/interactive-resource support — extracted from legacy-client/render.ts,
// proxy-tool.ts, and resources.ts during the legacy-client factoring.
//
// These pieces are NOT proxy-surface specific: they are the combiner UI-host,
// mcp-app-resource, and browser-open mechanics that SURVIVE into the native mode
// (widget holds via the connection's onWidgetUrl log-tap; read_* tools for ui://
// resources, which pi's built-in read_mcp_resource deliberately does NOT cover).
// When legacy-client/ is deleted, this file keeps them alive; legacy/resources.ts
// imports them from here in the meantime.

import { spawn } from "node:child_process"
import type { ResourceSummary } from "./types.js"

// The shared guarded resource read (pretty-print small JSON, spill over the cap)
// lives in render.ts — re-exported so the native resource surface keeps importing
// from here until the legacy-client factoring completes.
export { renderResourceResult } from "./render.js"

export const MCP_APP_MIME = "text/html;profile=mcp-app"

/** True for interactive ext-apps resources: read_* returns their markup AND
 *  surfaces the combiner UI-host URL (which serves the live widget). */
export function isInteractiveResource(r: ResourceSummary): boolean {
    return r.mimeType === MCP_APP_MIME || r.uri.startsWith("ui://")
}

/** Platform browser open — best-effort; failures are fine (the URL is in the
 *  result text anyway). Spawned detached so pi退出 doesn't kill the browser. */
export function openInBrowser(url: string): void {
    const cmd = process.platform === "darwin" ? ["open"] : ["xdg-open"]
    try {
        spawn(cmd[0], [...cmd.slice(1), url], { stdio: "ignore", detached: true }).unref?.()
    } catch {
        // no opener available — the URL is in the result text anyway
    }
}

/** The ui:// resource a tool result carries (port of the adapter's
 * getToolUiResourceUri): `_meta["ui/resourceUri"]` or `_meta.ui.resourceUri`. */
export function toolUiResourceUri(result: unknown): string | undefined {
    const meta = (result as { _meta?: unknown })?._meta
    if (typeof meta !== "object" || meta === null) return undefined
    const m = meta as Record<string, unknown>
    const nested = (m.ui as Record<string, unknown> | undefined)?.resourceUri
    const uri = typeof nested === "string" ? nested : m["ui/resourceUri"]
    return typeof uri === "string" && uri.startsWith("ui://") ? uri : undefined
}

// NOTE: the guarded resource read (pretty-print small JSON, spill over the cap)
// lives in render.ts and is re-exported above — the old copies here and in the
// legacy render.ts are unified; nothing remains here.