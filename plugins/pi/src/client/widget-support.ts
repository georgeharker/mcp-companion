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
import type { ResourceSummary } from "./connection.js"

export const MAX_RESULT_CHARS = 16 * 1024

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

const maxPrettyCharsDefault = 2048

function prettifyIfSmallJson(text: string, maxPrettyChars = maxPrettyCharsDefault): string {
    const trimmed = text.trim()
    if (trimmed.length === 0 || trimmed.length > maxPrettyChars) return text
    if (!(trimmed.startsWith("{") || trimmed.startsWith("["))) return text
    try {
        const parsed: unknown = JSON.parse(trimmed)
        if (typeof parsed !== "object" || parsed === null) return text
        return JSON.stringify(parsed, null, 2)
    } catch {
        return text
    }
}

/** MCP readResource result → guarded plain text: text contents pass through
 *  (pretty-printed when small JSON; with the shared size guard), images become
 *  markers, base64 blobs become byte notes. */
export function renderResourceResult(result: unknown): string {
    const contents = (result as { contents?: unknown[] })?.contents
    if (!Array.isArray(contents) || contents.length === 0) return "(empty resource)"
    const parts: string[] = []
    for (const c of contents) {
        if (typeof c !== "object" || c === null) continue
        const item = c as Record<string, unknown>
        const mime = typeof item.mimeType === "string" ? item.mimeType : "unknown"
        if (typeof item.text === "string") {
            parts.push(prettifyIfSmallJson(item.text))
        } else if (typeof item.blob === "string") {
            const bytes = Math.floor((item.blob.length * 3) / 4) // base64 → approx decoded size
            parts.push(`[binary ${mime}, ~${bytes} bytes — uri ${item.uri ?? "?"}]`)
        } else if (mime.startsWith("image/")) {
            parts.push(`[image: ${mime} — uri ${item.uri ?? "?"}]`)
        } else {
            parts.push(`[unreadable content: ${mime} — uri ${item.uri ?? "?"}]`)
        }
    }
    const text = parts.join("\n").trim() || "(empty resource)"
    if (text.length <= MAX_RESULT_CHARS) return text
    return `${text.slice(0, MAX_RESULT_CHARS)}\n\n… (resource truncated at ${MAX_RESULT_CHARS} chars)`
}