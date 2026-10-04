// Compact tool-call/result renderers — pi parity with pi-mcp-adapter's
// tool-result-renderer.ts (MIT, © 2026 Nico Bailon), lean port.
//
// Contract (pi's ToolDefinition): renderCall(args, theme, context) and
// renderResult(result, options, theme, context) each return a TUI Component — an
// object with render(width): string[]. @earendil-works/pi-tui is a PEER dependency:
// pi's extension loader aliases it to pi's own copy for every extension (verified in
// pi's loader getAliases()), so we never bundle a second TUI instance — we render
// with the same classes pi's TUI uses. devDependency only provides types/tests.
//
// Style ported from the adapter's compact mode: a one-line title row
// `<title> → <first output line>` with muted previews, collapsed body lines with a
// "(Ctrl+O to expand)" footer (pi's expand keybinding), red error rows.

import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui"
import type { RenderTheme, TuiComponent, ToolRenderResultOptions } from "../pi.js"

type Theme = RenderTheme

/** One styled line: `title <muted preview>` — the adapter's compact call row. */
class TitleRow implements TuiComponent {
    private title: string
    private preview: string
    private theme: Theme | undefined
    private cached: { width: number; lines: string[] } | null = null

    constructor(title: string, preview: string, theme: Theme | undefined) {
        this.title = title
        this.preview = preview
        this.theme = theme
    }

    /** pi-tui Component contract (invalidate is REQUIRED — MouseRegion calls it
     *  unconditionally): drop the width cache so the next render re-styles
     *  (theme changes). Same pattern as panel.ts. */
    invalidate(): void {
        this.cached = null
    }

    render(width: number): string[] {
        const safeWidth = Math.max(1, Math.floor(width))
        if (this.cached?.width === safeWidth) return this.cached.lines
        const fg = this.theme?.fg?.bind(this.theme)
        const title = fg ? fg("toolTitle", this.title) : this.title
        let line: string
        if (this.preview) {
            const available = safeWidth - visibleWidth(this.title) - 1
            const preview = available > 8 ? truncateToWidth(this.preview, available, "…") : ""
            line = preview ? `${title} ${fg ? fg("muted", preview) : preview}` : title
        } else {
            line = title
        }
        this.cached = { width: safeWidth, lines: [line] }
        return this.cached.lines
    }
}

/** Compact result: `identity → first line` plus a collapsed body (N lines) with an
 *  expand footer; full body when expanded; red when the result is an error. */
class CompactResult implements TuiComponent {
    private identity: string
    private lines: string[]
    private expanded: boolean
    private isError: boolean
    private theme: Theme | undefined
    private cached: { width: number; lines: string[] } | null = null

    constructor(
        identity: string,
        lines: string[],
        options: ToolRenderResultOptions,
        isError: boolean,
        theme: Theme | undefined,
    ) {
        this.identity = identity
        this.lines = lines
        this.expanded = options.expanded
        this.isError = isError
        this.theme = theme
    }

    /** pi-tui Component contract — see TitleRow.invalidate. */
    invalidate(): void {
        this.cached = null
    }

    render(width: number): string[] {
        const safeWidth = Math.max(1, Math.floor(width))
        if (this.cached?.width === safeWidth) return this.cached.lines
        const fg = this.theme?.fg?.bind(this.theme)
        const out: string[] = []
        const first = this.lines[0] ?? "(empty result)"
        const head = `${fg ? fg("toolTitle", this.identity) : this.identity} ${fg ? fg("muted", "→") : "→"} `
        const headWidth = visibleWidth(head)
        const body = this.isError && fg ? fg("error", first) : first
        out.push(`${head}${truncateToWidth(body, Math.max(1, safeWidth - headWidth), "…")}`)
        const rest = this.lines.slice(1)
        if (this.expanded) {
            for (const line of rest.slice(0, 400)) out.push(truncateToWidth(line, safeWidth, "…"))
        } else if (rest.length > 0) {
            out.push(
                fg
                    ? fg("muted", `(Ctrl+O to expand; ${rest.length} more line${rest.length === 1 ? "" : "s"})`)
                    : "(Ctrl+O to expand)",
            )
        }
        this.cached = { width: safeWidth, lines: out }
        return out
    }
}

/** Extract text lines from a pi tool result (content blocks). */
export function resultLines(result: { content?: unknown }): string[] {
    const blocks = result.content
    if (!Array.isArray(blocks)) return ["(empty result)"]
    const lines: string[] = []
    for (const b of blocks) {
        if (typeof b === "object" && b !== null && (b as { type?: string }).type === "text") {
            const text = (b as { text?: string }).text ?? ""
            for (const line of text.split("\n")) lines.push(line)
        } else if (typeof b === "object" && b !== null && (b as { type?: string }).type === "image") {
            lines.push(`[image: ${(b as { mimeType?: string }).mimeType ?? "unknown"}]`)
        }
    }
    return lines.length ? lines : ["(empty result)"]
}

/** Compact args preview: key=value pairs, JSON-compact, bounded. */
export function argsPreview(args: Record<string, unknown> | undefined, maxLen = 80): string {
    if (!args || typeof args !== "object") return ""
    const parts: string[] = []
    for (const [k, v] of Object.entries(args)) {
        if (v === undefined || v === null || v === "") continue
        const rendered =
            typeof v === "string"
                ? v
                : (() => {
                      try {
                          return JSON.stringify(v)
                      } catch {
                          return String(v)
                      }
                  })()
        parts.push(`${k}=${rendered}`)
    }
    const joined = parts.join(" ")
    return joined.length > maxLen ? `${joined.slice(0, maxLen - 1)}…` : joined
}

// ── factories ──

/** Call renderer: `title <args preview>` one-liner. */
export function callRenderer(title: string, preview: (args: Record<string, unknown>) => string) {
    return (args: Record<string, unknown>, theme?: Theme): TuiComponent =>
        new TitleRow(title, preview(args ?? {}), theme)
}

/** Result renderer: `identity → first line` + collapsed body. Identity comes from
 *  the result's details (mode/tool) with a static fallback. */
export function resultRenderer(identityOf: (result: { details?: unknown }) => string, fallbackIdentity: string) {
    return (
        result: { content?: unknown; isError?: boolean; details?: unknown },
        options: ToolRenderResultOptions,
        theme?: Theme,
    ): TuiComponent => {
        let identity = fallbackIdentity
        try {
            identity = identityOf(result) || fallbackIdentity
        } catch {
            // malformed details — static identity
        }
        return new CompactResult(identity, resultLines(result), options, Boolean(result.isError), theme)
    }
}

/** The proxy (mcp) tool's renderer pair — identity per verb, call preview per shape. */
export function proxyRenderers(toolName: string): {
    renderCall: ReturnType<typeof callRenderer>
    renderResult: ReturnType<typeof resultRenderer>
} {
    const preview = (args: Record<string, unknown>): string => {
        if (typeof args.tool === "string" && args.tool)
            return `${args.tool} ${argsPreview(args as Record<string, unknown>, 60)}`.trim()
        if (typeof args.search === "string" && args.search) return `search "${args.search}"`
        if (typeof args.describe === "string" && args.describe) return `describe ${args.describe}`
        if (typeof args.connect === "string") return `connect ${args.connect}`
        return argsPreview(args, 60)
    }
    const identity = (result: { details?: unknown }): string => {
        const d = result.details as { mode?: string; tool?: string; server?: string; query?: string } | undefined
        if (d?.mode === "call" && d.tool) return d.server ? `${d.server}/${d.tool}` : d.tool
        if (d?.mode === "search" && d.query) return `search "${d.query}"`
        if (d?.mode === "describe" && d.tool) return `describe ${d.tool}`
        return toolName
    }
    return {
        renderCall: callRenderer(toolName, preview),
        renderResult: resultRenderer(identity, toolName),
    }
}

/** Direct-tool renderer pair: `MCP <name>` title, first-line result row. */
export function directToolRenderers(name: string): {
    renderCall: ReturnType<typeof callRenderer>
    renderResult: ReturnType<typeof resultRenderer>
} {
    return {
        renderCall: callRenderer(`MCP ${name}`, (args) => argsPreview(args, 70)),
        renderResult: resultRenderer(() => name, `MCP ${name}`),
    }
}
