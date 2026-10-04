// NATIVE TOOL SURFACE : declares the combiner's tools
// to Pi FIRST-CLASS, via pi.registerTool with exposure/namespace/annotations — the
// replacement for client/proxy-tool.ts + client/script.ts + direct-tools.ts promotion.
//
// What this buys (the whole point of the native path):
//
//   - every tool is declared under its own name (`github_search_code`), one hop,
//     no mcp() wrapper and no mcpScript
//   - exposure IS pi's native mechanism: `direct` for the daily drivers (declared to
//     the model verbatim), `codemode` for the rest (callable from codemode scripts,
//     listed only as a namespace — 4%-token cost since pi 1.0), so the combiner's
//     671 tools stay out of the declaration budget by default
//   - annotations (readOnly/destructive hints) ride along, so pi-permission-system
//     and other tool_call gates classify combiner calls without any adapter glue
//   - tool_search / codemode searchTools() find undeclared ones by BM25, replacing
//     directTools:"search" promotion with pi's own discovery
//
// Exposure mapping reuses the existing config vocabulary: the `combiner.directTools`
// allowlist in the mcp.json ladder means "these are direct" here; everything else is
// `codemode`. `directTools: "search"` degenerates to plain codemode (pi's tool_search
// replaces it). Per-project allow/deny keeps flowing combiner-side via applyFilter.
//
// Wire shape: register once at session_start (tools list is stable modulo
// list_changed, which re-registers on the same names). Tools the combiner offers
// during a session surface via the connection's onToolsChanged hook → activate again.

import type { ExtensionAPI, ToolDefinition, ToolParameters } from "../pi.js"
import type { NativeCombinerConnection, ToolSummary } from "./combiner-connection.js"
import { applyServerFilter, matchesGlob, RESERVED_TOOL_NAMES } from "../client/tool-matching.js"
import type { ServerFilter } from "../client/config-ladder.js"

function isRecord(v: unknown): v is Record<string, unknown> {
    return typeof v === "object" && v !== null && !Array.isArray(v)
}

export type NativeToolSurfaceOptions = {
    /** The resolved combiner connection (owns elicitation, filters, caches). */
    connection: NativeCombinerConnection
    /** The per-project server filter (same object index.ts passes to applyFilter);
     *  applied defensively here too so a filter race never widens surface exposure. */
    serverFilter?: ServerFilter
    /** The combiner block's directTools spec, as resolved from the ladder: a glob
     *  allowlist → "direct" exposure for matches; "search" → everything codemode. */
    directSpec?: string[] | "search"
    /** Warn when the direct-exposure set grows large (every declared schema rides in
     *  every request). Settings kill-switch: warnLargeDirectExposure (default true). */
    warnLargeDirectExposure?: boolean
    /** Namespace shown in codemode's grouped listing (pi normalizes -/­_ alike). */
    namespaceName?: string
    /** Names that must never be shadowed by a combiner tool (pi builtins + ours). */
    reserved?: readonly string[]
    log: (level: "info" | "warn" | "error", message: string) => void
}

/** Map a CallToolResult content block to pi's text-block contract. pi tool results
 *  are arrays of text blocks today; image/other blocks are summarized to text and the
 *  full result rides `details` for renderers (same shape the SDK client half uses). */
function textBlockOf(b: unknown): { type: "text"; text: string } {
    const block = isRecord(b) ? b : undefined
    if (block && block.type === "text") {
        return { type: "text", text: String(block.text) }
    }
    const kind = block?.type ?? (block ? "content" : b)
    return { type: "text", text: `[${String(kind)} block]` }
}

function toToolResult(call: { content?: unknown; structuredContent?: unknown }): {
    content: { type: "text"; text: string }[]
} {
    const blocks = Array.isArray(call.content) ? call.content : []
    return { content: blocks.map((b) => textBlockOf(b)) }
}

/** Register the combiner's tools natively. */
const DIRECT_EXPOSURE_WARN_THRESHOLD = 50

/** Turn one combiner ToolSummary into pi's ToolDefinition, plus the native-only fields
 *  pi 1.0 accepts on registerTool (exposure/namespace/annotations). */
function toNativeTool(sum: ToolSummary, conn: NativeCombinerConnection, exposure: ToolDefinition["exposure"]): ToolDefinition {
    const name = sum.name
    return {
        name,
        label: name,
        description: sum.description ?? name,
        parameters: (sum.inputSchema as ToolParameters) ?? { type: "object", properties: {} },
        // NATIVE-ONLY fields (pi ≥ 1.0 host contract): exposure decides how the tool is
        // declared; namespace groups it in codemode's listing; annotations feed
        // pi-permission-system's classification. Kept optional on ToolDefinition so the
        // extension stays load-compatible with pi 0.99.x where these fields are ignored.
        exposure,
        namespace: { name: "mcp_combiner", description: "mcp-combiner aggregated tools" },
        annotations: sum.annotations,
        execute: async (_toolCallId, params, signal) => {
            // The abort signal rides into the transport; the combiner can cancel
            // upstream work, and widget holds respect it (notifications/cancelled).
            void signal
            try {
                const result = await conn.callTool(name, params)
                return { ...toToolResult(result), details: result }
            } catch (e) {
                // pi's error convention: THROWING marks the call an error (no isError
                // property on successful results).
                throw e instanceof Error ? e : new Error(String(e))
            }
        },
    }
}

/** Register the combiner's tools natively. Returns the names it registered (for
 *  status/panel surfaces). Idempotent by name — pi.registerTool replaces by name with
 *  a warning, so re-registration on list_changed must pass the same names. */
export async function activateNativeTools(pi: ExtensionAPI, opts: NativeToolSurfaceOptions): Promise<string[]> {
    const registered = [] as string[]
    const cachedNames: string[] = registered
    const tools = applyServerFilter(await opts.connection.listTools(true), opts.serverFilter)
    const allowlist = Array.isArray(opts.directSpec) ? opts.directSpec : []
    const reserved = new Set<string>([...(opts.reserved ?? []), ...RESERVED_TOOL_NAMES])

    const directNames: string[] = []
    for (const sum of tools) {
        if (reserved.has(sum.name)) {
            opts.log("warn", `skipping combiner tool "${sum.name}" — collides with a reserved name`)
            continue
        }
        // Allowlist glob match → declared verbatim ("direct"); everything else rides
        // codemode: callable from scripts, discoverable via tool_search/searchTools(),
        // near-zero declaration cost. "search" degrades to the same codemode mapping
        // (pi's tool_search replaces the adapter's search-promote semantics).
        const exposure = allowlist.some((p) => matchesGlob(sum.name, p)) ? "direct" : "codemode"
        if (exposure === "direct") directNames.push(sum.name)
        pi.registerTool(toNativeTool(sum, opts.connection, exposure))
        cachedNames.push(sum.name)
    }
    if (
        directNames.length > DIRECT_EXPOSURE_WARN_THRESHOLD &&
        opts.warnLargeDirectExposure !== false
    ) {
        opts.log(
            "warn",
            `native mode: ${directNames.length} tools declared "direct" — every schema rides in every ` +
                `request. Consider trimming the allowlist (directTools) or disabling this warning ` +
                `with "warnLargeDirectExposure": false in extensions/mcp-combiner.json.`,
        )
    }
    opts.log("info", `native tool surface: ${cachedNames.length} tools registered`)
    return cachedNames
}