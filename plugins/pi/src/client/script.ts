// The <toolName>Script batching tool — API parity with pi-mcp-adapter's
// mcpScript (MIT, © 2026 Nico Bailon; mcp-code.ts), reimplemented lean:
// scriptMode: `{code, timeoutMs?}` executing trusted JavaScript with
// `await tools.search/describe/call()` and `emit(value)` for user-visible output.
//
// Lean port: pi-mcp-adapter runs the code in a worker thread (mcp-code.ts) for hard
// timeout termination; we evaluate in-process with an AsyncFunction and race a timer.
// The trade-off is deliberate — trusted code, one loopback server — an abandoned
// call finishes harmlessly combiner-side, and the API surface (what agents write)
// is identical. Default timeout 30s, matching the adapter.

import type { ToolDefinition } from "../pi.js"
import type { CombinerConnection, ToolSummary } from "./connection.js"
import { rankTools, regexMatches, compileSafeRegex } from "./ranking.js"
import { renderDescribe, renderSearchHit, renderToolResult, textResult } from "./render.js"
import { callRenderer, resultRenderer } from "./renderers.js"

export const DEFAULT_SCRIPT_TIMEOUT_MS = 30_000
const MAX_SCRIPT_TIMEOUT_MS = 120_000
const MAX_EMITTED_BLOCKS = 200
const MAX_EMIT_CHARS = 16 * 1024

export type ScriptToolDeps = {
    connection: CombinerConnection
    /** Tool name for the batching tool — `${toolName}Script` ("mcpScript" in parity). */
    name: string
}

const AsyncFunction = Object.getPrototypeOf(async () => {
    /* capture */
}).constructor as new (...args: string[]) => (tools: unknown, emit: (v: unknown) => void) => Promise<unknown>

type ToolsApi = {
    search: (query?: string, opts?: { server?: string; limit?: number; offset?: number; regex?: boolean }) => unknown
    describe: (name: string) => unknown
    call: (name: string, args?: Record<string, unknown>) => unknown
}

function serverOf(name: string): string {
    return name.split("_", 1)[0] ?? ""
}

function formatValue(value: unknown): string {
    if (typeof value === "string") return value
    try {
        return JSON.stringify(value, null, 2) ?? String(value)
    } catch {
        return "[unserializable value]"
    }
}

export function createScriptTool(deps: ScriptToolDeps): ToolDefinition {
    const { connection, name } = deps

    return {
        name,
        label: "MCP script (combiner)",
        description: [
            "Run trusted JavaScript that makes multiple MCP calls in one request.",
            "API: const r = await tools.search('query', {limit}); await tools.describe('name');",
            "await tools.call('name', {args}); emit(value) for user-visible output.",
            "The final return value (or the last emitted value) becomes the tool result.",
        ].join(" "),
        promptSnippet: "Batch multiple MCP calls with trusted JavaScript (tools.search/describe/call + emit).",
        parameters: {
            type: "object",
            properties: {
                code: { type: "string", description: "JavaScript to run (await allowed)" },
                timeoutMs: { type: "number", description: `Timeout in ms (default ${DEFAULT_SCRIPT_TIMEOUT_MS})` },
            },
            required: ["code"],
        },
        renderCall: callRenderer(name, (args) => {
            const code = typeof args.code === "string" ? (args.code.trim().split("\n", 1)[0] ?? "") : ""
            return code.length > 60 ? `${code.slice(0, 59)}…` : code
        }),
        renderResult: resultRenderer(() => name, name),
        execute: async (_toolCallId, params) => {
            const p = params as { code?: unknown; timeoutMs?: unknown }
            if (typeof p.code !== "string" || !p.code.trim()) {
                throw new Error(`${name}: code must be a non-empty string`)
            }
            const timeoutMs =
                typeof p.timeoutMs === "number" && p.timeoutMs > 0
                    ? Math.min(MAX_SCRIPT_TIMEOUT_MS, p.timeoutMs)
                    : DEFAULT_SCRIPT_TIMEOUT_MS

            const emitted: string[] = []
            let emittedChars = 0
            const emit = (value: unknown): void => {
                if (emitted.length >= MAX_EMITTED_BLOCKS) return
                const text = formatValue(value).slice(0, MAX_EMIT_CHARS)
                emittedChars += text.length
                emitted.push(text)
            }

            const tools: ToolsApi = {
                search: (query, opts) => {
                    const q = typeof query === "string" ? query : ""
                    const server = opts?.server
                    return connection
                        .listTools()
                        .then((all) => {
                            const pool = server ? all.filter((t) => serverOf(t.name) === server) : all
                            if (opts?.regex) {
                                const { re, error } = compileSafeRegex(q)
                                if (!re) throw new Error(`bad regex: ${error}`)
                                return regexMatches(pool, re, opts?.limit ?? 12, opts?.offset ?? 0).map((m) => m.tool)
                            }
                            return rankTools(pool, q, opts?.limit ?? 12, opts?.offset ?? 0).map((m) => m.tool)
                        })
                        .then((tools) => tools.map((t) => renderSearchHit(t)))
                        .then((lines) => lines.join("\n"))
                },
                describe: (toolName) =>
                    connection
                        .listTools()
                        .then((all) => all.find((t) => t.name === toolName))
                        .then((t: ToolSummary | undefined) => {
                            if (!t) throw new Error(`no tool "${toolName}" — use tools.search first`)
                            return renderDescribe(t, serverOf(t.name))
                        }),
                call: (toolName, args) => connection.callTool(toolName, args).then((r) => renderToolResult(r)),
            }

            try {
                const fn = new AsyncFunction("tools", "emit", p.code)
                const timer = new Promise<never>((_, reject) =>
                    setTimeout(() => reject(new Error(`${name} timed out after ${timeoutMs}ms`)), timeoutMs),
                )
                let result: unknown
                try {
                    result = await Promise.race([fn(tools as unknown, emit), timer])
                } finally {
                    // nothing to clean — the timer's reject is inert once the race settled
                }
                const resultText = result === undefined ? "" : formatValue(result)
                const text = [...emitted, resultText].filter((s) => s.length > 0).join("\n\n")
                return textResult(text || "(no output — emit(value) or return a value)", {
                    mode: "script",
                    emitted: emitted.length,
                })
            } catch (e) {
                const msg = e instanceof Error ? e.message : String(e)
                const text = emitted.length ? `${emitted.join("\n\n")}\n\n${name}: ${msg}` : `${name}: ${msg}`
                throw new Error(text)
            }
        },
    }
}
