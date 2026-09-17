// The /mcp-combiner panel — a lean interactive TUI over the combiner state, in the
// pi-mcp-adapter /mcp panel idiom (overlay via ctx.ui.custom, Component with
// handleInput, matchesKey routing — MIT, © 2026 Nico Bailon) but combiner-shaped and aligned with the
// Neovim plugin's :MCPStatus UX (lua/mcp_companion/ui/init.lua): the same state
// glyphs (● ○ ⊘ ✗ ◌), the per-server tools/resources/prompts counts trio, the
// [session off] label for servers hidden from THIS chat by the project filter,
// padded columns, and `e` to toggle enable/disable (driven through the combiner__
// meta-tools). MIT-credited idioms from pi-mcp-adapter's mcp-panel.ts.

import { spawn } from "node:child_process"
import { Container, Text, matchesKey, truncateToWidth, visibleWidth, type Component } from "@earendil-works/pi-tui"
import type { CombinerConnection, PromptSummary, ResourceSummary, ToolSummary } from "./connection.js"
import { metaToolCall } from "./control.js"
import { resourceServer } from "./resources.js"

type RenderTheme = { fg: (color: string, text: string) => string }

type TuiLike = { requestRender(): void }

export type PanelDeps = {
    connection: CombinerConnection
    toolName: string
    /** This session's grouping token (identity row). */
    getToken: () => string | undefined
    /** This session's project server filter, if any ([session off] labels). */
    getFilter: () => { allow?: string[]; deny?: string[] } | undefined
    notify: (message: string, level?: "info" | "warn" | "error") => void
}

type HealthServer = { state?: string; status?: string; disabled?: boolean; transport?: string }
type Health = { servers?: Record<string, HealthServer> }

type ServerRow = {
    kind: "server"
    name: string
    state: string
    transport: string
    disabled: boolean
    sessionOff: boolean
    tools: ToolSummary[]
    prompts: PromptSummary[]
    resources: ResourceSummary[]
    expanded: boolean
}
type ToolRow = { kind: "tool"; server: string; name: string; desc: string; tokens: number }
type HintRow = { kind: "hint"; server: string; text: string }
type Row = ServerRow | ToolRow | HintRow

// State glyphs mirror the Neovim plugin's status_icon() table; "meta" marks the
// combiner's own combiner__* tools group (nvim's combiner_on hexagon).
const STATE_GLYPHS: Record<string, string> = {
    ready: "●",
    connected: "●",
    connecting: "◌",
    starting: "◌",
    disconnected: "○",
    idle: "○",
    disabled: "⊘",
    failed: "✗",
    error: "✗",
    meta: "⬢",
}
// Nerd-font codepoints (no emoji, matching the footer's plug convention).
const ICON_TOOL = "\uF0E7" // nf-fa-bolt
const ICON_RESOURCE = "\uF15B" // nf-fa-file
const ICON_PROMPT = "\uF075" // nf-fa-comment

const MAX_VISIBLE = 24
const PANEL_WIDTH = 92
const NAME_PAD = 18
const TOOL_PAD = 30

function stateGlyph(state: string): string {
    return STATE_GLYPHS[state.toLowerCase()] ?? "?"
}

function stateColor(state: string): string {
    if (state === "meta") return "toolTitle"
    if (state === "ready" || state === "connected") return "success"
    if (state === "failed" || state === "error") return "error"
    if (state === "disabled") return "muted"
    return "muted"
}

function pad(s: string, w: number): string {
    const vis = visibleWidth(s)
    return vis >= w ? s : s + " ".repeat(w - vis)
}

/** Rough token estimate per tool, adapter-style (chars/4 + base). */
function estimateTokens(tool: ToolSummary): number {
    const schemaLen = JSON.stringify(tool.inputSchema ?? {}).length
    return Math.ceil((tool.name.length + (tool.description?.length ?? 0) + schemaLen) / 4) + 10
}

function serverOf(name: string): string {
    return name.split("_", 1)[0] ?? ""
}

/** Attribute a tool/prompt name to its server against the KNOWN server list,
 *  longest-match-first — server names may themselves contain underscores
 *  (gws_georgeharker_tools belongs to gws_georgeharker, not a phantom "gws").
 *  Falls back to the first segment for names no known server prefixes. */
function groupServer(name: string, knownServers: string[]): string {
    let best: string | undefined
    for (const s of knownServers) {
        if (!name.startsWith(`${s}_`)) continue
        if (best === undefined || s.length > best.length) best = s
    }
    return best ?? serverOf(name)
}

/** Copy text to the system clipboard; notifies as fallback when no tool exists. */
function copyToClipboard(text: string, notify: PanelDeps["notify"]): void {
    const cmd = process.platform === "darwin" ? ["pbcopy"] : ["xclip", "-selection", "clipboard"]
    try {
        const child = spawn(cmd[0], cmd.slice(1), { stdio: ["pipe", "ignore", "ignore"] })
        child.on("error", () => notify(`tool name: ${text}`))
        child.stdin?.end(text)
        notify(`copied: ${text}`)
    } catch {
        notify(`tool name: ${text}`)
    }
}

class CombinerPanel implements Component {
    private readonly container = new Container()
    private readonly deps: PanelDeps
    private readonly tui: TuiLike
    private readonly theme: RenderTheme | undefined
    private readonly done: () => void
    private rows: Row[] = []
    private visible: Row[] = []
    private cursor = 0
    private offset = 0
    private query = ""
    private searchActive = false
    private notice: string | null = null
    private loading = true
    private expandedServer: string | null = null

    constructor(deps: PanelDeps, tui: TuiLike, theme: RenderTheme | undefined, done: () => void) {
        this.deps = deps
        this.tui = tui
        this.theme = theme
        this.done = done
        void this.refresh()
    }

    dispose(): void {
        // nothing held beyond closures
    }

    /** pi-tui Component contract — our render rebuilds from scratch (Container.clear),
     *  so there is no cached rendering to drop. */
    invalidate(): void {
        // no-op by design
    }

    private fg(color: string, text: string): string {
        return this.theme?.fg ? this.theme.fg(color, text) : text
    }

    private async refresh(): Promise<void> {
        this.loading = true
        this.tui.requestRender()
        const servers = new Map<string, HealthServer>()
        try {
            const health = (await this.deps.connection.health()) as Health
            for (const [name, s] of Object.entries(health.servers ?? {})) servers.set(name, s)
        } catch (e) {
            this.notice = `/health unreachable: ${e instanceof Error ? e.message : String(e)}`
        }
        const [tools, prompts, resources] = await Promise.all([
            this.deps.connection.listTools(true).catch(() => [] as ToolSummary[]),
            this.deps.connection.listPrompts(true).catch(() => [] as PromptSummary[]),
            this.deps.connection.listResources(true).catch(() => [] as ResourceSummary[]),
        ])
        const toolsBy = new Map<string, ToolSummary[]>()
        const promptsBy = new Map<string, PromptSummary[]>()
        const resourcesBy = new Map<string, ResourceSummary[]>()
        const push = <K, V>(map: Map<K, V[]>, key: K, value: V) => {
            const list = map.get(key)
            if (list) list.push(value)
            else map.set(key, [value])
        }
        const known = [...servers.keys()].sort((a, b) => b.length - a.length)
        for (const t of tools) {
            const s = groupServer(t.name, known)
            if (s) push(toolsBy, s, t)
        }
        for (const p of prompts) {
            const s = groupServer(p.name, known)
            if (s) push(promptsBy, s, p)
        }
        for (const r of resources) {
            const s = resourceServer(r)
            if (s) push(resourcesBy, s, r)
        }

        const prev = new Map(this.rows.filter((r) => r.kind === "server").map((r) => [r.name, r]))
        const build = (name: string, h: HealthServer): ServerRow => {
            const p = prev.get(name)
            // The fallback group holding the combiner's own combiner__* tools has no
            // /health entry — mark it "meta" rather than "unknown" so it renders as
            // the combiner itself (⬢), not a broken (?) server.
            const isMeta = name === "combiner" && !servers.has(name)
            return {
                kind: "server",
                name,
                state: isMeta ? "meta" : String(h.state ?? h.status ?? "unknown"),
                transport: typeof h.transport === "string" ? h.transport : "",
                disabled: h.disabled === true,
                sessionOff: this.sessionOff(name),
                tools: toolsBy.get(name) ?? [],
                prompts: promptsBy.get(name) ?? [],
                resources: resourcesBy.get(name) ?? [],
                expanded: p?.expanded ?? false,
            }
        }
        const next: Row[] = [...servers.keys()].sort().map((n) => build(n, servers.get(n) ?? {}))
        // tools/prompts/resources whose server has no /health entry still get a group
        for (const name of new Set([...toolsBy.keys(), ...promptsBy.keys(), ...resourcesBy.keys()])) {
            if (!servers.has(name)) next.push(build(name, {}))
        }
        this.rows = next
        this.loading = false
        this.applyFilter()
        this.tui.requestRender()
    }

    private sessionOff(server: string): boolean {
        const filter = this.deps.getFilter()
        if (!filter) return false
        if (filter.allow?.length) return !filter.allow.includes(server)
        if (filter.deny?.length) return filter.deny.includes(server)
        return false
    }

    /** Expand rows into the visible list honoring the search query. An expanded
     *  server contributes its tool rows beneath it (token estimates included). */
    private applyFilter(): void {
        const q = this.query.trim().toLowerCase()
        const out: Row[] = []
        for (const row of this.rows) {
            if (row.kind !== "server") continue
            const serverMatch = !q || row.name.toLowerCase().includes(q) || String(row.tools.length).includes(q)
            if (!serverMatch) continue
            out.push(row)
            if (!row.expanded) continue
            if (row.tools.length === 0) {
                const hint = row.sessionOff
                    ? "(session off — tools hidden from this chat)"
                    : row.disabled
                      ? "(press e to enable)"
                      : "(no tools advertised — disconnected or none)"
                out.push({ kind: "hint", server: row.name, text: hint })
                continue
            }
            for (const t of row.tools) {
                const toolMatch =
                    !q || t.name.toLowerCase().includes(q) || (t.description ?? "").toLowerCase().includes(q)
                if (!toolMatch) continue
                out.push({
                    kind: "tool",
                    server: row.name,
                    name: t.name,
                    desc: t.description ?? "",
                    tokens: estimateTokens(t),
                })
            }
        }
        this.visible = out
        if (this.cursor >= this.visible.length) this.cursor = Math.max(0, this.visible.length - 1)
    }

    // ── rendering ──

    render(width: number): string[] {
        const w = Math.min(PANEL_WIDTH, Math.max(40, width))
        const inner = w - 4
        this.container.clear()
        const line = (text: string) => this.container.addChild(new Text(text, 0, 0))

        line(
            `${this.fg("muted", "╭─ ")}${this.fg("toolTitle", "mcp-combiner")}${this.fg("muted", ` ${"─".repeat(Math.max(0, w - 17))}╮`)}`,
        )
        const port = (() => {
            try {
                return new URL(this.deps.connection.controlOrigin()).port || "80"
            } catch {
                return "?"
            }
        })()
        this.row(
            `${this.fg(stateColor(this.deps.connection.state), stateGlyph(this.deps.connection.state))} connection ${this.deps.connection.state}  ·  :${port}  ·  token ${this.deps.getToken() ?? "-"}`,
            inner,
        )
        const filter = this.deps.getFilter()
        this.row(
            filter
                ? `exposure ${filter.allow ? `allow: ${filter.allow.join(", ")}` : `deny: ${filter.deny?.join(", ") ?? ""}`}`
                : "exposure all servers",
            inner,
        )
        let searchLine: string
        if (this.searchActive) {
            searchLine = `◎  search: ${this.query}▏`
        } else if (this.query) {
            searchLine = `◎  ${this.query}  (esc clears)`
        } else {
            searchLine = `◎  ${this.fg("muted", "type / to filter")}`
        }
        this.row(searchLine, inner)
        if (this.notice) this.row(this.fg("error", this.notice), inner)
        line(this.fg("muted", "├" + "─".repeat(w - 2) + "┤"))

        if (this.loading) {
            this.row(this.fg("muted", "loading…"), inner)
        } else {
            const slice = this.visible.slice(this.offset, this.offset + MAX_VISIBLE)
            slice.forEach((row, i) => {
                const sel = this.offset + i === this.cursor
                const mark = sel ? this.fg("toolTitle", "›") : " "
                if (row.kind === "server") {
                    const g = this.fg(stateColor(row.state), stateGlyph(row.state))
                    let head = `${mark} ${row.expanded ? "▾" : "▸"} ${g} ${pad(row.name, NAME_PAD)}`
                    if (row.disabled) {
                        head += this.fg("muted", " [disabled]")
                    } else {
                        head += this.fg(
                            "muted",
                            `  ${row.tools.length} ${ICON_TOOL}  ${row.resources.length} ${ICON_RESOURCE}  ${row.prompts.length} ${ICON_PROMPT}`,
                        )
                    }
                    if (row.transport) head += this.fg("muted", `  [${row.transport}]`)
                    if (row.sessionOff) head += this.fg("error", " [session off]")
                    this.row(head, inner)
                } else if (row.kind === "hint") {
                    this.row(`    ${this.fg("muted", row.text)}`, inner)
                } else {
                    const body = truncateToWidth(
                        `${pad(row.name, TOOL_PAD)}${row.desc.split("\n", 1)[0] ?? ""}`,
                        inner - 12,
                        "…",
                    )
                    const tail = this.fg("muted", `~${row.tokens}t`)
                    this.row(
                        `${sel ? body : this.fg("muted", body)}${" ".repeat(Math.max(1, inner - 12 - visibleWidth(body) - 6))}${tail}`,
                        inner,
                    )
                }
            })
            const total = this.visible.length
            if (total > MAX_VISIBLE) this.row(this.fg("muted", `… ${total - MAX_VISIBLE} more (↑/↓)`), inner)
        }

        line(this.fg("muted", "├" + "─".repeat(w - 2) + "┤"))
        this.row(
            this.fg("muted", "↑↓ move · enter expand · e enable/disable · c copy · / filter · r refresh · q close"),
            inner,
        )
        line(this.fg("muted", "╰" + "─".repeat(w - 2) + "╯"))
        return this.container.render(w)
    }

    /** Add a row with muted side rails, padded to the panel width. */
    private row(text: string, inner: number): void {
        const padded = Math.max(0, inner - visibleWidth(text))
        const rail = this.fg("muted", "│")
        this.container.addChild(new Text(`${rail} ${text}${" ".repeat(padded)} ${rail}`, 0, 0))
    }

    // ── input ──

    handleInput(data: string): void {
        if (matchesKey(data, "ctrl+c") || matchesKey(data, "escape")) {
            if (this.searchActive || this.query) {
                this.searchActive = false
                this.query = ""
                this.applyFilter()
                this.tui.requestRender()
                return
            }
            this.done()
            return
        }
        if (matchesKey(data, "q")) {
            this.done()
            return
        }
        if (matchesKey(data, "r")) {
            void this.refresh()
            return
        }
        if (this.searchActive) {
            if (matchesKey(data, "enter")) {
                this.searchActive = false
                return
            }
            if (matchesKey(data, "backspace")) {
                this.query = this.query.slice(0, -1)
            } else if (data.length === 1 && data >= " " && data <= "~") {
                this.query += data
            }
            this.applyFilter()
            this.tui.requestRender()
            return
        }
        if (matchesKey(data, "/")) {
            this.searchActive = true
            this.tui.requestRender()
            return
        }
        const move = (delta: number) => {
            this.cursor = Math.min(this.visible.length - 1, Math.max(0, this.cursor + delta))
            if (this.cursor < this.offset) this.offset = this.cursor
            if (this.cursor >= this.offset + MAX_VISIBLE) this.offset = this.cursor - MAX_VISIBLE + 1
            this.tui.requestRender()
        }
        if (matchesKey(data, "up") || matchesKey(data, "k")) return move(-1)
        if (matchesKey(data, "down") || matchesKey(data, "j")) return move(1)

        const row = this.visible[this.cursor]
        if (matchesKey(data, "enter") || matchesKey(data, "space")) {
            if (row?.kind === "server") {
                this.expandedServer = this.expandedServer === row.name ? null : row.name
                for (const r of this.rows) if (r.kind === "server" && r.name === row.name) r.expanded = !r.expanded
                this.applyFilter()
                this.tui.requestRender()
            } else if (row?.kind === "tool") {
                copyToClipboard(row.name, this.deps.notify)
            }
            return
        }
        if (matchesKey(data, "c") && row?.kind === "tool") {
            copyToClipboard(row.name, this.deps.notify)
            return
        }
        if ((matchesKey(data, "e") || matchesKey(data, "d")) && row?.kind === "server") {
            const verb = row.disabled ? "enable" : "disable"
            this.notice = `${verb} ${row.name}…`
            this.tui.requestRender()
            void metaToolCall(this.deps.connection, verb, row.name)
                .then((msg) => {
                    this.notice = msg
                    return this.refresh()
                })
                .catch((e) => {
                    this.notice = `${verb} failed: ${e instanceof Error ? e.message : String(e)}`
                    this.tui.requestRender()
                })
        }
    }
}

/** Open the panel as a centered overlay; resolves when closed. */
export async function openCombinerPanel(deps: PanelDeps, ui: unknown): Promise<void> {
    const custom = (ui as { custom?: unknown } | undefined | null)?.custom as
        | ((
              factory: (
                  tui: TuiLike,
                  theme: RenderTheme,
                  keybindings: unknown,
                  done: (result: void) => void,
              ) => Component & { dispose?(): void },
              options?: { overlay?: boolean; overlayOptions?: { anchor?: string; width?: number } },
          ) => Promise<void>)
        | undefined
    if (!custom) {
        deps.notify("mcp-combiner: panel requires a TUI session (ctx.ui.custom)", "warn")
        return
    }
    await custom((tui, theme, _keybindings, done) => new CombinerPanel(deps, tui, theme, () => done(undefined)), {
        overlay: true,
        overlayOptions: { anchor: "center", width: PANEL_WIDTH },
    })
}
