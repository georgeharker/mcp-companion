// /mcp-combiner control verbs: inspect and drive the combiner from a Pi session.
//
// The read side hits the combiner's control plane directly (GET /health, token
// filter routes); the write side drives the combiner__* meta-tools through our own
// MCP client — one auth path, no second protocol.

import type { CombinerConnection } from "./connection.js"

export type HealthServer = { name?: string; status?: string; state?: string; transport?: string }

export type HealthSnapshot = {
    servers?: Array<Record<string, unknown>>
    tool_count?: number
    [k: string]: unknown
}

const GLYPHS: Record<string, string> = {
    ready: "✓",
    connected: "✓",
    starting: "…",
    connecting: "…",
    failed: "✗",
    error: "✗",
    disabled: "–",
    idle: "○",
}

function glyph(state: string | undefined): string {
    if (!state) return "?"
    return GLYPHS[state.toLowerCase()] ?? "?"
}

/** `status` verb: health glyph table + our session's tool view. */
export async function statusText(connection: CombinerConnection, cwd: string): Promise<string> {
    const lines: string[] = []
    lines.push(`connection: ${connection.state}${connection.lastError ? ` (${connection.lastError})` : ""}`)
    try {
        const health = (await connection.health()) as HealthSnapshot
        // The combiner ships `servers` as an object keyed by name; tolerate arrays too.
        const raw = health.servers
        const entries: Array<[string, Record<string, unknown>]> = []
        if (Array.isArray(raw)) {
            for (const s of raw) {
                if (typeof s === "object" && s !== null) {
                    const rec = s as Record<string, unknown>
                    entries.push([typeof rec.name === "string" ? rec.name : "?", rec])
                }
            }
        } else if (typeof raw === "object" && raw !== null) {
            for (const [name, s] of Object.entries(raw as Record<string, unknown>)) {
                if (typeof s === "object" && s !== null) entries.push([name, s as Record<string, unknown>])
            }
        }
        for (const [name, s] of entries) {
            const state = typeof (s.status ?? s.state) === "string" ? String(s.status ?? s.state) : undefined
            const transport = typeof s.transport === "string" ? ` [${s.transport}]` : ""
            const dis = s.disabled === true ? " (disabled)" : ""
            lines.push(`${glyph(state)} ${name}${transport} (${state ?? "unknown"})${dis}`)
        }
    } catch (e) {
        lines.push(`/health unreachable: ${e instanceof Error ? e.message : String(e)}`)
    }
    try {
        const tools = await connection.listTools()
        const servers = [...new Set(tools.map((t) => t.name.split("_", 1)[0] ?? ""))].filter(Boolean).sort()
        lines.push(
            `visible to this session: ${tools.length} tools across ${servers.length} servers (${servers.join(", ")})`,
        )
    } catch {
        // connection state already reported above
    }
    lines.push(`cwd: ${cwd}`)
    return lines.join("\n")
}

/** `enable` / `disable` / `restart-server` verbs via the combiner meta-tools. */
export async function metaToolCall(
    connection: CombinerConnection,
    verb: "enable" | "disable" | "restart-server",
    server: string,
): Promise<string> {
    const map: Record<typeof verb, string> = {
        enable: "combiner__enable_server",
        disable: "combiner__disable_server",
        "restart-server": "combiner__restart_server",
    }
    const tool = map[verb]
    const result = (await connection.callTool(tool, { server_name: server })) as { content?: Array<{ text?: string }> }
    const text = (result?.content ?? [])
        .map((b) => b.text ?? "")
        .join("\n")
        .trim()
    return text || `${verb} ${server}: done`
}
