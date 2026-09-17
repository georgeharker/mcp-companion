// The `mcp()` proxy tool — the agent's single entry point to every combiner tool.
//
// One ~200-token tool instead of hundreds of definitions: the agent searches,
// describes, then calls. Calling convention mirrors pi-mcp-adapter's mcp tool (so
// existing muscle memory and prompts stay accurate); verbs the combiner makes moot
// (auth actions, install, ui-messages) are absent. Lean reimplementation — search
// ranking in ranking.ts, describe/guard in render.ts.
//
// Pi tool contract: results carry content BLOCKS (textResult helper); failures are
// signalled by THROWING (pi sets isError from the throw — a returned property never
// does). No custom renderers — pi's default tool rendering.

import { spawn } from "node:child_process"
import type { ToolCallContext, ToolDefinition } from "../pi.js"
import type { CombinerConnection, ToolSummary } from "./connection.js"
import { compileSafeRegex, rankTools, regexMatches, type SearchableTool } from "./ranking.js"
import { renderDescribe, renderSearchHit, renderToolResult, textResult, toolUiResourceUri } from "./render.js"
import { proxyRenderers } from "./renderers.js"

export type ProxyToolDeps = {
    connection: CombinerConnection
    /** Human label for error messages, e.g. "mcp". */
    toolName: string
    /** Client-side server filter (mirrors the combiner-side token filter) — a GETTER
     *  so per-session re-resolution (worktrees, project switch) applies mid-flight. */
    getServerFilter?: () => { allow?: string[]; deny?: string[] } | undefined
    /** Search-mode directTools hook: given ranked matches, activate them as direct
     *  tools and return the names (empty/undefined when nothing new activated). */
    onSearchMatches?: (tools: ToolSummary[]) => string[]
    /** Whether interactive-UI results auto-open the browser (default true). */
    uiAutoOpen?: boolean
}

/** Open a URL in the user's browser (macOS/xdg); silent no-op elsewhere. */
export function openInBrowser(url: string): void {
    const cmd = process.platform === "darwin" ? ["open"] : ["xdg-open"]
    try {
        spawn(cmd[0], [...cmd.slice(1), url], { stdio: "ignore", detached: true }).unref?.()
    } catch {
        // no opener available — the URL is in the result text anyway
    }
}

/** Append the interactive-UI line + auto-open for results carrying a ui:// resource. */
function withUiResource(
    connection: CombinerConnection,
    deps: { uiAutoOpen?: boolean },
    text: string,
    result: unknown,
    ctx: ToolCallContext | undefined,
): string {
    const uri = toolUiResourceUri(result)
    if (!uri) return text
    const url = connection.uiUrlFor(uri)
    if (!url) return text
    if (deps.uiAutoOpen !== false && ctx?.hasUI) openInBrowser(url)
    return `${text}\n\ninteractive: ${url}${deps.uiAutoOpen !== false && ctx?.hasUI ? " (opened in your browser)" : ""}`
}

const PARAMETERS: Record<string, unknown> = {
    type: "object",
    properties: {
        search: { type: "string", description: "Search tools by name/description" },
        regex: { type: "boolean", description: "Treat search as a regex (validated)" },
        describe: { type: "string", description: "Tool name to describe (parameters + description)" },
        tool: { type: "string", description: "Tool name to call (as listed, e.g. github_search_code)" },
        args: {
            type: ["object", "string"],
            description: "Arguments for the call — object preferred, JSON string accepted",
        },
        server: { type: "string", description: "Filter search results / disambiguate by server prefix" },
        list: { type: "boolean", description: "List tools (all, or one server's via server)" },
        status: { type: "boolean", description: "Combiner + upstream server status" },
        connect: { type: "string", description: "Ensure the connection is up (returns status)" },
        limit: { type: "number", description: "Max search results (default 12)" },
        offset: { type: "number", description: "Search result pagination offset" },
    },
}

/** Server prefix of a combiner tool name ("<server>_<tool>"). */
function serverOf(name: string): string {
    return name.split("_", 1)[0] ?? ""
}

/** Apply the client-side allow/deny filter to a tool list (underscore-safe via
 *  prefix matching — see toolServerMatches). */
function filterTools<T extends SearchableTool>(
    tools: T[],
    filter: { allow?: string[]; deny?: string[] } | undefined,
): T[] {
    if (!filter) return tools
    if (filter.allow?.length) {
        return tools.filter((t) => filter.allow!.some((e) => t.name.startsWith(`${e}_`)))
    }
    if (filter.deny?.length) {
        return tools.filter((t) => !filter.deny!.some((e) => t.name.startsWith(`${e}_`)))
    }
    return tools
}

/** Failures throw — pi marks the result as an error for the model. */
function fail(toolName: string, message: string): never {
    throw new Error(`${toolName}: ${message}`)
}

/** Parse `args` — object passes through, string gets JSON.parse'd once. */
function parseArgs(raw: unknown): { args?: Record<string, unknown>; error?: string } {
    if (raw === undefined || raw === null) return {}
    if (typeof raw === "object" && !Array.isArray(raw)) return { args: raw as Record<string, unknown> }
    if (typeof raw === "string") {
        const s = raw.trim()
        if (!s) return {}
        try {
            const parsed: unknown = JSON.parse(s)
            if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed))
                return { args: parsed as Record<string, unknown> }
            return { error: "args string must parse to a JSON object" }
        } catch (e) {
            return { error: `args is not valid JSON (${e instanceof Error ? e.message : String(e)})` }
        }
    }
    return { error: "args must be an object or a JSON object string" }
}

export function createMcpTool(deps: ProxyToolDeps): ToolDefinition {
    const { connection, toolName } = deps
    const serverFilter = (): { allow?: string[]; deny?: string[] } | undefined => deps.getServerFilter?.()

    const description = [
        "Search, describe, and call MCP tools aggregated by the mcp-combiner.",
        "Tools are named <server>_<tool> (e.g. github_search_code).",
        "Two calls instead of dozens of definitions: search, describe, then call.",
        "Discover before assuming a capability is absent.",
    ].join(" ")

    return {
        name: toolName,
        label: "MCP (combiner)",
        description,
        promptSnippet: "Search, describe, and call MCP tools (mcp({search}), mcp({describe}), mcp({tool, args})).",
        parameters: PARAMETERS,
        ...proxyRenderers(toolName),
        execute: async (_toolCallId, params, _signal, _onUpdate, ctx) => {
            const p = params as Record<string, unknown>
            // ── call ──
            if (typeof p.tool === "string" && p.tool) {
                const { args, error } = parseArgs(p.args)
                if (error) fail(toolName, error)
                // An explicit server on a call is a consistency claim: the model said
                // WHICH server it meant. Honor it — a mismatch is a mistake worth
                // surfacing, not silently executing. (It also feeds pi-permission-
                // system's server hint, so honoring it keeps gating honest.)
                if (typeof p.server === "string" && p.server && serverOf(p.tool) !== p.server) {
                    fail(toolName, `tool "${p.tool}" does not belong to server "${p.server}"`)
                }
                const listed = filterTools(await connection.listTools(), serverFilter())
                const match = listed.find((t) => t.name === p.tool)
                if (!match) {
                    const near = rankTools(listed, p.tool, 5).map((m) => m.tool.name)
                    fail(
                        toolName,
                        `no tool "${p.tool}". ${near.length ? `Closest: ${near.join(", ")}. ` : ""}Use search first.`,
                    )
                }
                const result = await connection.callTool(match!.name, args)
                const raw = renderToolResult(result)
                const text = withUiResource(connection, deps, raw, result, ctx)
                if ((result as { isError?: boolean })?.isError) throw new Error(text)
                return textResult(text, { mode: "call", tool: match!.name, server: serverOf(match!.name) })
            }

            // ── describe ──
            if (typeof p.describe === "string" && p.describe) {
                const listed = filterTools(await connection.listTools(), serverFilter())
                const match = listed.find((t) => t.name === p.describe)
                if (!match) fail(toolName, `no tool "${p.describe}". Use search first.`)
                return textResult(renderDescribe(match!, serverOf(match!.name)), {
                    mode: "describe",
                    tool: match!.name,
                })
            }

            // ── search ──
            if (typeof p.search === "string" && p.search) {
                const listed = filterTools(await connection.listTools(), serverFilter())
                const server = typeof p.server === "string" && p.server ? p.server : undefined
                const pool = server ? listed.filter((t) => serverOf(t.name) === server) : listed
                const limit = typeof p.limit === "number" && p.limit > 0 ? Math.min(50, p.limit) : 12
                const offset = typeof p.offset === "number" && p.offset >= 0 ? p.offset : 0

                let matches
                if (p.regex === true) {
                    const { re, error: rxErr } = compileSafeRegex(p.search)
                    if (!re) fail(toolName, `bad regex: ${rxErr}`)
                    matches = regexMatches(pool, re, limit, offset)
                } else {
                    matches = rankTools(pool, p.search, limit, offset)
                }
                const total = matches.length + offset
                const lines = matches.map((m) => renderSearchHit(m.tool))
                const header = `${lines.length} of ~${total} matches for "${p.search}"${server ? ` (server ${server})` : ""}`
                const more =
                    matches.length === limit ? `\n\nMore: mcp({search:"${p.search}", offset:${offset + limit}})` : ""
                // Search-mode directTools: first match promotes these to first-class tools.
                let activated = ""
                if (deps.onSearchMatches) {
                    const names = deps.onSearchMatches(matches.map((m) => m.tool as ToolSummary))
                    if (names.length) {
                        activated = `\n\nActivated as direct tools (call them by name now): ${names.join(", ")}`
                    }
                }
                return textResult(
                    `${header}\n${lines.join("\n")}\n\nDescribe next: mcp({describe:"<name>"})${activated}${more}`,
                    { mode: "search", query: p.search, count: lines.length },
                )
            }

            // ── list ──
            if (p.list === true || (typeof p.server === "string" && p.server && Object.keys(p).length === 1)) {
                const listed = filterTools(await connection.listTools(), serverFilter())
                const server = typeof p.server === "string" && p.server ? p.server : undefined
                const pool = server ? listed.filter((t) => serverOf(t.name) === server) : listed
                return textResult(pool.map((t) => renderSearchHit(t)).join("\n") || "(no tools)", {
                    mode: "list",
                    server: server ?? null,
                    count: pool.length,
                })
            }

            // ── connect (no-op connectively; combiner owns upstreams) ──
            if (typeof p.connect === "string") {
                await connection.ensureConnected()
                return textResult(statusText(connection), { mode: "connect" })
            }

            // ── status (default) ──
            if (p.status === true || Object.keys(p).length === 0) {
                let servers: string[] = []
                try {
                    servers = [
                        ...new Set(
                            filterTools(await connection.listTools(), serverFilter()).map((t) => serverOf(t.name)),
                        ),
                    ].sort()
                } catch {
                    // not connected — status still reports that honestly below
                }
                return textResult(statusText(connection, servers), { mode: "status" })
            }

            fail(toolName, "unsupported arguments — use search | describe | tool | list | status | connect")
        },
    }
}

/** One-line status for status/connect results (no async fetches — cached state only). */
function statusText(connection: CombinerConnection, servers?: string[]): string {
    const lines = [`combiner: ${connection.state}${connection.lastError ? ` (${connection.lastError})` : ""}`]
    if (servers?.length) lines.push(`servers: ${servers.join(", ")}`)
    return lines.join("\n")
}
