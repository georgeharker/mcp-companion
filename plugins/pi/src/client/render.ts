// describe/call output shaping: TS-shape schema rendering, result guarding, compact
// text rendering. Lean versions of pi-mcp-adapter's tool-metadata / mcp-output-guard /
// tool-result-renderer trio (MIT, © 2026 Nico Bailon) — one server, text-first.
//
// Oversized results SPILL rather than silently cut (the pi bash/read/codemode
// pattern): the head stays LLM-visible, the full text goes to a 0600 temp file and
// the truncated notice names it, so the call stays recoverable via `read` instead of
// ending mid-JSON with no continuation story. Images survive as REAL image blocks on
// the blocks path (pi normalizes + resizes them into history; provider adapters drop
// them for non-vision models with an omission note).

import { mkdtemp, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { renderSchemaSignature } from "./schema-signature.js"

import type { ImageBlock, TextBlock, ToolResult } from "../pi.js"

/** Wrap guarded text as a pi tool result (content-blocks contract). */
export function textResult(text: string, details?: unknown): ToolResult {
    const content: (TextBlock | ImageBlock)[] = [{ type: "text", text }]
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

/** Default cap on guarded text before spill (kept in one place; the live cap is
 *  the `maxResultChars` setting, threaded through from index.ts). */
export const MAX_RESULT_CHARS = 16 * 1024

/** Options for the guarded renderers. */
export type RenderOptions = {
    /** Char cap before spilling (default MAX_RESULT_CHARS; min 1024). */
    maxResultChars?: number
    /** Active model — when it cannot see images, image blocks become omission notes
     *  instead (read-tool parity). Omit to forward images unconditionally. */
    model?: { input?: string[] }
}

/** Clamp a caller-provided char cap into a sane band. */
function limitFor(max: number | undefined): number {
    if (typeof max !== "number" || !Number.isFinite(max)) return MAX_RESULT_CHARS
    return Math.min(4 * 1024 * 1024, Math.max(1024, Math.floor(max)))
}

/** UTF-16 surrogate-safe cut: never end the head with a lone high surrogate, so
 *  the truncated notice never glues itself into a split code point. */
function safeCut(text: string, at: number): string {
    if (at <= 0 || at >= text.length) return at >= text.length ? text : ""
    const last = text.charCodeAt(at - 1)
    return last >= 0xd800 && last < 0xdc00 ? text.slice(0, at - 1) : text.slice(0, at)
}

/** Spill full text to a 0600 temp file (pi's truncated-output convention —
 *  user-readable only, like the bash tool's spill and codemode's images). */
export async function spillResultText(text: string, toolName: string): Promise<string | undefined> {
    try {
        const dir = await mkdtemp(join(tmpdir(), "pi-mcp-"))
        const safe = toolName.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 60) || "tool"
        const path = join(dir, `${safe}.txt`)
        await writeFile(path, text, { mode: 0o600 })
        return path
    } catch {
        return undefined // spill failure degrades to the head-only result
    }
}

export type GuardedText = {
    /** Head (LLM-visible) + truncated notice when over the cap. */
    text: string
    /** Set when the full text was spilled to a temp file. */
    spillPath?: string
}

/** Guard a rendered text against the char cap: under the cap → verbatim; over →
 *  surrogate-safe head + notice naming the spill file. Never cuts mid-JSON without
 *  a recovery story: the notice line tells the caller where the rest lives. */
export async function guardedText(text: string, opts: RenderOptions, label: string): Promise<GuardedText> {
    const max = limitFor(opts.maxResultChars)
    if (text.length <= max) return { text }
    const spillPath = await spillResultText(text, label)
    const head = safeCut(text, max)
    const notice = spillPath
        ? `\n\n[Truncated: showing first ${head.length} of ${text.length} chars. Full content: ${spillPath} (read with offset/limit)]`
        : `\n\n[Truncated: showing first ${head.length} of ${text.length} chars — full contents could not be saved; narrow the call or paginate]`
    return { text: head + notice, spillPath }
}

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

export type RenderedToolResult = {
    /** Joined, guarded text (what the string API and error paths surface). */
    text: string
    /** Full content for a pi tool result: the guarded text block plus REAL image
     *  blocks (dropped to omission notes when the active model is non-vision). */
    content: (TextBlock | ImageBlock)[]
    /** Spill file path when the text was truncated. */
    spillPath?: string
}

/** Non-vision gate (read-tool parity): pi's provider adapters also omit image
 *  blocks for text-only models, but the explicit note keeps the model informed. */
function nonVision(model: RenderOptions["model"]): boolean {
    return Boolean(model && Array.isArray(model.input) && !model.input.includes("image"))
}

/** MCP tool result → guarded text AND content blocks. Text joins, pretty-prints
 *  (small JSON), and spills when oversized; images stay REAL blocks so the model
 *  sees them and codemode scripts can `image(block)` them. */
export async function renderToolResultBlocks(
    result: unknown,
    opts: RenderOptions = {},
    label = "tool",
): Promise<RenderedToolResult> {
    const { text: joined, images } = renderToolTextParts(result)
    const guarded = await guardedText(joined, opts, label)
    const content: (TextBlock | ImageBlock)[] = [{ type: "text", text: guarded.text }]
    for (const block of images) {
        content.push(nonVision(opts.model) ? { type: "text", text: NON_VISION_NOTE } : block)
    }
    const out: RenderedToolResult = { text: guarded.text, content }
    if (guarded.spillPath) out.spillPath = guarded.spillPath
    return out
}

/** String API (script tool's tools.call, resource reads): guarded text only; images
 *  stay one-line placeholders. */
export async function renderToolResult(result: unknown, opts: RenderOptions = {}, label = "tool"): Promise<string> {
    return (await renderToolResultBlocks(result, opts, label)).text
}

/** Joined text + surviving image blocks from an MCP tool result's content. */
function renderToolTextParts(result: unknown): { text: string; images: ImageBlock[] } {
    const blocks = (result as { content?: unknown[] })?.content
    if (!Array.isArray(blocks) || blocks.length === 0) {
        // Structured-content fallback (adapter parity): tools returning only
        // structuredContent otherwise lose their payload to "(empty result)".
        const structured = (result as { structuredContent?: unknown })?.structuredContent
        if (structured !== undefined && structured !== null) {
            return renderToolTextParts({ content: [{ type: "text", text: JSON.stringify(structured) }] })
        }
        return { text: "(empty result)", images: [] }
    }
    const parts: string[] = []
    const images: ImageBlock[] = []
    for (const b of blocks) {
        if (typeof b !== "object" || b === null) continue
        const block = b as Record<string, unknown>
        if (typeof block.text === "string") {
            parts.push(prettifyIfSmallJson(block.text))
        } else if (block.type === "image" && typeof block.data === "string") {
            const mimeType = typeof block.mimeType === "string" ? block.mimeType : "unknown"
            images.push({ type: "image", data: block.data, mimeType })
        } else if (block.type === "image") {
            // No data (or an upstream placeholder) — keep a marker so the block isn't lost.
            parts.push(`[image: ${typeof block.mimeType === "string" ? block.mimeType : "unknown"}]`)
        } else if (block.type === "resource" || block.type === "embedded_resource") {
            const resource = block.resource as Record<string, unknown> | undefined
            const uri = resource && typeof resource.uri === "string" ? resource.uri : "resource"
            parts.push(`[resource: ${uri}]`)
        } else if (block.type === "audio") {
            parts.push("[audio output]")
        }
    }
    return { text: parts.join("\n").trim() || (images.length ? "(image result)" : "(empty result)"), images }
}

const NON_VISION_NOTE = "[Current model does not support images. The image will be omitted from this request.]"

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
 *  (pretty-printed when small JSON; spill-to-file guard shared with tool results),
 *  images become markers, base64 blobs become byte notes. */
export async function renderResourceResult(result: unknown, opts: RenderOptions = {}, label = "resource"): Promise<string> {
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
    return (await guardedText(text, opts, label)).text
}
