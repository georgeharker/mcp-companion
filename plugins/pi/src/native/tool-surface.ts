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
// list_changed, which re-registers through this module again). Each pass diffs
// against the previous registration (schema signatures) so only CHANGED tools hit
// pi.registerTool (pi's replace-by-name path logs a warning per re-registration —
// a full re-register on every list_changed would churn hundreds of warnings);
// tools the server REMOVED are re-registered `hidden` so they stop being
// callable/declared rather than lingering as broken names.

import type { ExtensionAPI, ToolDefinition, ToolParameters } from "../pi.js"
import type { NativeCombinerConnection, ToolSummary } from "./combiner-connection.js"
import { applyServerFilter, matchesGlob, RESERVED_TOOL_NAMES } from "../client/tool-matching.js"
import { renderSchemaSignature } from "../client/schema-signature.js"
import { toLlmContent, type CallToolResult } from "@earendil-works/pi-mcp"
import type { ServerFilter } from "../client/config-ladder.js"

export type NativeToolSurfaceOptions = {
    /** The resolved combiner connection (owns elicitation, filters, caches). */
    connection: NativeCombinerConnection
    /** The per-project server filter (same object index.ts passes to applyFilter);
     *  applied defensively here too so a filter race never widens surface exposure.
     *  A GETTER resolves per call — worktrees/project switches re-resolve session
     *  config, and a load-time snapshot would apply the wrong allow/deny set. */
    serverFilter?: ServerFilter | (() => ServerFilter | undefined)
    /** The combiner block's directTools spec, as resolved from the ladder: a glob
     *  allowlist → "direct" exposure for matches; "search" → everything codemode.
     *  A getter resolves per call (same reason as serverFilter). */
    directSpec?: string[] | "search" | (() => string[] | "search" | undefined)
    /** Cross-pass registration state (see activateNativeTools). A fresh object on
     *  first call; the SAME object on every pass so the diffing + hidden-removal
     *  work. native/index.ts owns one per activation. */
    state?: NativeToolSurfaceState
    /** Warn when the direct-exposure set grows large (every declared schema rides in
     *  every request). Settings kill-switch: warnLargeDirectExposure (default true). */
    warnLargeDirectExposure?: boolean
    log: (level: "info" | "warn" | "error", message: string) => void
}

/** Registration memory across passes: currently-declared names + each tool's schema
 *  signature, so a changed schema re-registers and an unchanged one doesn't (pi's
 *  re-register = replace-with-warning; skipping unchanged declarations avoids the
 *  warning churn and keeps pi's tool identity stable). */
export type NativeToolSurfaceState = {
    names: Set<string>
    signatures: Map<string, string | null>
}

const COMBINER_NAMESPACE = { name: "mcp_combiner", description: "mcp-combiner aggregated tools" } as const

function resolveFilter(f: NativeToolSurfaceOptions["serverFilter"]): ServerFilter | undefined {
    return typeof f === "function" ? f() : f
}
function resolveDirectSpec(d: NativeToolSurfaceOptions["directSpec"]): string[] | "search" | undefined {
    return typeof d === "function" ? d() : d
}

/** Map a `CallToolResult` to pi's tool-result contract via pi-mcp's own converter —
 *  text stays text, images become real image blocks (base64 + mime), resource
 *  blocks flatten, structured-only results stringify. The full result rides
 *  `details` for renderers regardless. */
// SAFETY: LlmContent (pi-mcp) is { type:"text"; text } | { type:"image"; data; mimeType }
// — the same elementwise shape src/pi.ts's ToolResult content block union accepts;
// elementwise-identical, so the assignment is asserted (not a structural re-check).
type ToolResultContentBlock = { type: "text"; text: string } | { type: "image"; data: string; mimeType: string }
function toToolResult(call: Pick<CallToolResult, "content" | "structuredContent">): {
    content: ToolResultContentBlock[]
} {
    return { content: toLlmContent(call) as ToolResultContentBlock[] }
}

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
        namespace: COMBINER_NAMESPACE,
        annotations: sum.annotations,
        execute: async (_toolCallId, params, signal) => {
            // The abort signal rides into the transport (callTool's options.signal):
            // cancellation cancels upstream work, and widget holds respect it
            // (notifications/cancelled).
            try {
                const result = await conn.callTool(name, params, { signal })
                return { ...toToolResult(result), details: result }
            } catch (e) {
                // pi's error convention: THROWING marks the call an error (no isError
                // property on successful results).
                throw e instanceof Error ? e : new Error(String(e))
            }
        },
    }
}

/** Registration for a tool the combiner no longer offers: same name (replaces the old
 *  declaration), exposure `hidden` (not declared to the model/codemode; calling it
 *  surfaces a clear error instead of a stale upstream call). */
function removedToolDefinition(name: string): ToolDefinition {
    return {
        name,
        label: name,
        description: "(unavailable: the combiner no longer offers this tool)",
        parameters: { type: "object", properties: {} },
        exposure: "hidden",
        namespace: COMBINER_NAMESPACE,
        execute: async () => {
            throw new Error(`"${name}" is no longer offered by the combiner; use tool_search or the mcp discovery tool to find its replacement`)
        },
    }
}

/** Register the combiner's tools natively, DIFFING against `state` (which the caller
 *  reuses across passes): unchanged declarations are skipped (pi.registerTool
 *  replaces-by-name with a warning — churn on every list_changed would spam), changed
 *  schemas re-register, and removed tools are re-registered hidden. Returns the names
 *  currently active (for status/panel surfaces). */
export async function activateNativeTools(pi: ExtensionAPI, opts: NativeToolSurfaceOptions): Promise<string[]> {
    const state: NativeToolSurfaceState = opts.state ?? { names: new Set(), signatures: new Map() }
    const tools = applyServerFilter(await opts.connection.listTools(true), resolveFilter(opts.serverFilter))
    const allowlist = Array.isArray(resolveDirectSpec(opts.directSpec)) ? (resolveDirectSpec(opts.directSpec) as string[]) : []
    const reserved: ReadonlySet<string> = new Set(RESERVED_TOOL_NAMES)

    const currentNames = new Set(tools.map((t) => t.name))
    const directNames: string[] = []
    let fresh = 0
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
        // Diff: skip the re-register when the schema signature is unchanged (pi keeps
        // the existing declaration; re-registering would log its warning for nothing).
        const sig = renderSchemaSignature(sum.inputSchema)
        if (state.names.has(sum.name) && state.signatures.get(sum.name) === sig) {
            continue
        }
        pi.registerTool(toNativeTool(sum, opts.connection, exposure))
        state.signatures.set(sum.name, sig)
        state.names.add(sum.name)
        fresh++
        if (exposure === "direct") directNames.push(sum.name)
    }
    // Removals: hide them (never unregister silently — pi has no unregister; a hidden
    // re-register at the same name retires the declaration).
    let removed = 0
    for (const goneName of [...state.names]) {
        if (currentNames.has(goneName)) continue
        pi.registerTool(removedToolDefinition(goneName))
        state.names.delete(goneName)
        state.signatures.delete(goneName)
        removed++
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
    if (fresh || removed) {
        opts.log("info", `native tool surface: ${fresh} registered, ${removed} hidden (removed), ${state.names.size} active`)
    }
    return [...state.names]
}