// Direct tool promotion: allowlist + search-mode, per the agreed design
// (docs/adapter-design.md §Now/Later). Behavior follows pi-mcp-adapter's
// directTools modes (MIT, © 2026 Nico Bailon).
//
// `combiner.directTools` on the shared-file entry is either a glob allowlist over
// combined tool names — registered once at session start, cache-stable — or
// "search": nothing registered upfront; the first mcp({search}) match activates
// those tools as first-class Pi tools for the rest of the session (the adapter's
// directTools:"search" semantics, on our own mid-session registerTool path).
//
// `directTools: true` is deliberately NOT offered: at combiner scale (hundreds of
// tools) it burns the context window the proxy exists to save. Allowlists larger
// than ~50 tools draw a loud warning and still register — an explicit list is the
// user's call.

import type { ToolDefinition } from "../pi.js"
import type { CombinerConnection } from "../client/connection.js"
// Shared helpers (matching/attribution) moved to client/tool-matching.ts during the
// Factoring — re-exported here so index.ts's wiring and tests keep their
// import paths while this module waits for deletion.
import { globToRegExp, type ToolSummary } from "../client/tool-matching.js"
import { renderToolResult, textResult } from "./render.js"
import { directToolRenderers } from "./renderers.js"

export { RESERVED_TOOL_NAMES, applyServerFilter, matchesGlob, toolServerMatches } from "../client/tool-matching.js"
export type { DirectToolsSpec } from "../client/tool-matching.js"

/** Resolve the allowlist spec against the combined tool list (server filter applied
 *  by the caller). Returns matching tools in list order. */
export function resolveAllowlist(tools: ToolSummary[], patterns: string[]): ToolSummary[] {
    const regexes = patterns.map(globToRegExp)
    return tools.filter((t) => regexes.some((re) => re.test(t.name)))
}

/** Ensure a usable parameters object: pass the advertised JSON schema through when it
 *  is an object; repair anything else to an empty object schema. */
function normalizeInputSchema(schema: unknown): Record<string, unknown> {
    if (typeof schema === "object" && schema !== null && !Array.isArray(schema)) {
        return schema as Record<string, unknown>
    }
    return { type: "object", properties: {} }
}

export type DirectToolDeps = {
    connection: CombinerConnection
    /** Names already registered (shared across syncs + search activation). */
    registered: Set<string>
    /** Reserved names to refuse (builtins + this extension's own tool names). */
    reserved: Set<string>
    register: (tool: ToolDefinition) => void
    log: (level: "info" | "warn" | "error", message: string) => void
}

/** Build the Pi tool definition for one promoted combiner tool. */
export function buildDirectTool(tool: ToolSummary, deps: DirectToolDeps): ToolDefinition {
    return {
        name: tool.name,
        label: `MCP: ${tool.name}`,
        description: tool.description ?? `Combiner tool ${tool.name}`,
        parameters: normalizeInputSchema(tool.inputSchema),
        ...directToolRenderers(tool.name),
        execute: async (_toolCallId: string, params: Record<string, unknown>) => {
            const result = await deps.connection.callTool(tool.name, params)
            const text = renderToolResult(result)
            if ((result as { isError?: boolean })?.isError) throw new Error(text)
            return textResult(text)
        },
    }
}

/** Register one tool if new and not reserved. Returns the name, or undefined when skipped. */
export function registerDirectTool(tool: ToolSummary, deps: DirectToolDeps): string | undefined {
    if (deps.registered.has(tool.name)) return undefined
    if (deps.reserved.has(tool.name)) {
        deps.log("warn", `directTools: refusing to register "${tool.name}" — reserved name`)
        deps.registered.add(tool.name) // don't re-warn on every sync
        return undefined
    }
    deps.registered.add(tool.name)
    deps.register(buildDirectTool(tool, deps))
    return tool.name
}

/** Allowlist sync: register every matching, not-yet-registered tool. */
export function syncAllowlistTools(tools: ToolSummary[], patterns: string[], deps: DirectToolDeps): number {
    const matches = resolveAllowlist(tools, patterns)
    if (matches.length > 50) {
        deps.log(
            "warn",
            `directTools: allowlist promotes ${matches.length} tools — every schema rides in every ` +
                "request. Consider trimming (the mcp() proxy covers the rest).",
        )
    }
    let added = 0
    for (const tool of matches) {
        if (registerDirectTool(tool, deps)) added++
    }
    return added
}

/** Search-mode activation: register matched tools (bounded per activation). Returns
 *  the newly activated names — empty when everything matched was already live. */
export function activateFromSearch(matches: ToolSummary[], deps: DirectToolDeps, max = 10): string[] {
    const activated: string[] = []
    for (const tool of matches) {
        if (activated.length >= max) break
        if (deps.registered.has(tool.name)) continue
        if (registerDirectTool(tool, deps)) activated.push(tool.name)
    }
    return activated
}
