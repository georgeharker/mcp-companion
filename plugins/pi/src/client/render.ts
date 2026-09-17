// describe/call output shaping: TS-shape schema rendering, result guarding, compact
// text rendering. Lean versions of pi-mcp-adapter's tool-metadata / mcp-output-guard /
// tool-result-renderer trio (MIT, © 2026 Nico Bailon) — one server, text-first.

import { renderSchemaSignature } from "./schema-signature.js"

import type { TextBlock, ToolResult } from "../pi.js"

/** Wrap guarded text as a pi tool result (content-blocks contract). */
export function textResult(text: string, details?: unknown): ToolResult {
    const content: TextBlock[] = [{ type: "text", text }]
    return details === undefined ? { content } : { content, details }
}

/** First text block of a tool result, for tests and internal use. */
export function resultText(result: { content?: unknown }): string {
    const blocks = result.content
    if (!Array.isArray(blocks)) return ""
    return blocks
        .map((b) =>
            typeof b === "object" && b !== null && (b as TextBlock).type === "text" ? (b as TextBlock).text : "",
        )
        .join("\n")
}

export type ToolInfo = {
    name: string
    description?: string
    inputSchema?: unknown
}

export const MAX_RESULT_CHARS = 16 * 1024

/** Fallback describe-rendering of a JSON schema when the signature renderer bails (union-heavy,
 *  conditional schemas): one level of properties with type + required markers. */
function renderJsonSchemaShape(schema: unknown, indent = 0): string {
    if (typeof schema !== "object" || schema === null) return "unknown"
    const s = schema as Record<string, unknown>
    const pad = " ".repeat(indent)
    if (Array.isArray(s.enum)) return s.enum.map((v) => JSON.stringify(v)).join(" | ")
    if (typeof s.type === "string" && s.type !== "object") {
        const desc = typeof s.description === "string" && s.description ? ` — ${s.description}` : ""
        return `${s.type}${desc}`
    }
    const props = s.properties
    if (typeof props !== "object" || props === null) return "{}"
    const required = new Set(Array.isArray(s.required) ? (s.required as unknown[]) : [])
    const lines: string[] = []
    for (const [name, prop] of Object.entries(props as Record<string, unknown>)) {
        const t =
            typeof prop === "object" && prop !== null && typeof (prop as Record<string, unknown>).type === "string"
                ? String((prop as Record<string, unknown>).type)
                : "unknown"
        lines.push(`${pad}${name}${required.has(name) ? "" : "?"}: ${t}`)
    }
    return lines.length ? `{\n${lines.join("\n")}\n${" ".repeat(Math.max(0, indent - 2))}}` : "{}"
}

/** Render a tool's full describe block. */
export function renderDescribe(tool: ToolInfo, server: string | undefined): string {
    const parts: string[] = [tool.name]
    if (server) parts.push(`server: ${server}`)
    if (tool.description) parts.push(tool.description.trim())
    const shape = renderSchemaSignature(tool.inputSchema) ?? renderJsonSchemaShape(tool.inputSchema)
    parts.push(`\nParameters:\n${shape}`)
    return parts.join("\n")
}

/** Render a one-line search hit. */
export function renderSearchHit(tool: ToolInfo): string {
    const desc = tool.description ? tool.description.trim().split("\n", 1)[0] : ""
    const line = desc.length > 120 ? `${desc.slice(0, 117)}…` : desc
    return line ? `${tool.name} — ${line}` : tool.name
}

/** MCP tool result content → guarded plain text for the LLM. */
/** Pretty-print small JSON payloads for readability; larger ones stay compact
 *  (token economy at scale). Non-JSON passes through untouched. */
function prettifyIfSmallJson(text: string, maxPrettyChars = 2048): string {
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

export function renderToolResult(result: unknown): string {
    const blocks = (result as { content?: unknown[] })?.content
    if (!Array.isArray(blocks) || blocks.length === 0) {
        // Structured-content fallback (adapter parity): tools returning only
        // structuredContent otherwise lose their payload to "(empty result)".
        const structured = (result as { structuredContent?: unknown })?.structuredContent
        if (structured !== undefined && structured !== null) {
            return renderToolResult({ content: [{ type: "text", text: JSON.stringify(structured) }] })
        }
        return "(empty result)"
    }
    const parts: string[] = []
    for (const b of blocks) {
        if (typeof b !== "object" || b === null) continue
        const block = b as Record<string, unknown>
        if (typeof block.text === "string") {
            parts.push(prettifyIfSmallJson(block.text))
        } else if (block.type === "image") {
            parts.push(`[image: ${typeof block.mimeType === "string" ? block.mimeType : "unknown"}]`)
        } else if (block.type === "resource" || block.type === "embedded_resource") {
            const resource = block.resource as Record<string, unknown> | undefined
            const uri = resource && typeof resource.uri === "string" ? resource.uri : "resource"
            parts.push(`[resource: ${uri}]`)
        } else if (block.type === "audio") {
            parts.push("[audio output]")
        }
    }
    const text = parts.join("\n").trim() || "(empty result)"
    if (text.length <= MAX_RESULT_CHARS) return text
    return `${text.slice(0, MAX_RESULT_CHARS)}\n\n… (output truncated at ${MAX_RESULT_CHARS} chars; narrow the call or paginate)`
}

/** Extract a short first line for compact call-result rendering in the transcript. */
export function resultFirstLine(text: string): string {
    const line = text.split("\n", 1)[0] ?? ""
    return line.length > 160 ? `${line.slice(0, 157)}…` : line
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
