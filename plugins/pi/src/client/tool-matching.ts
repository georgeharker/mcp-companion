// Shared tool-name matching helpers — extracted from legacy-client/direct-tools.ts
// during the legacy-client factoring so the NATIVE tool surface (src/native/) and the
// client half (src/client/: config-ladder, panel) can use them without importing the
// deprecated legacy-client/ directory. When legacy-client/ is deleted, this file
// survives untouched.
//
// These are the stable contract:
//   - tool names arrive with the combiner's baked `<server>_` prefix
//   - server attribution is PREFIX-based (underscore-safe), not string equality

/** A combined-tool allowlist spec from the ladder's `combiner.directTools` key:
 *  a glob allowlist, or "search" (legacy adapter semantics — deprecated in the
 *  native mode, where tool_search replaces it). */
export type DirectToolsSpec = string[] | "search"

/** Names that must never be shadowed by a promoted/registered tool (pi builtins).
 *  Callers add their own tool names on top. */
export const RESERVED_TOOL_NAMES = ["read", "bash", "edit", "write", "grep", "find", "ls", "mcp", "glob"] as const

/** Apply a server allow/deny filter to a combined tool list. Server names may
 *  contain underscores (gws_georgeharker), so matching is by PREFIX — a tool
 *  belongs to entry E iff its name starts `E_` (subsumes first-segment equality). */
export function toolServerMatches(toolName: string, serverEntry: string): boolean {
    return toolName.startsWith(`${serverEntry}_`)
}

/** Apply a server allow/deny filter to a combined tool list (`<server>_` prefix,
 *  underscore-safe via toolServerMatches). */
export function applyServerFilter(
    tools: ToolSummary[],
    filter: { allow?: string[]; deny?: string[] } | undefined,
): ToolSummary[] {
    if (!filter) return tools
    if (filter.allow?.length) {
        return tools.filter((t) => filter.allow!.some((e) => toolServerMatches(t.name, e)))
    }
    if (filter.deny?.length) {
        return tools.filter((t) => !filter.deny!.some((e) => toolServerMatches(t.name, e)))
    }
    return tools
}

/** fnmatch-lite: `*` → any run, `?` → one char, everything else literal. */
export function globToRegExp(pattern: string): RegExp {
    const escaped = pattern
        .replace(/[.+^${}()|[\]\\]/g, "\\$&")
        .replace(/\*/g, ".*")
        .replace(/\?/g, ".")
    return new RegExp(`^${escaped}$`, "i")
}

export function matchesGlob(name: string, pattern: string): boolean {
    return globToRegExp(pattern).test(name)
}

// Local shape of a combiner tool listing (kept structural — the two connection
// implementations share it; defined here so both src/client and src/native can
// import it without a connection.ts dependency).
export type ToolSummary = {
    name: string
    description?: string
    inputSchema?: unknown
    annotations?: {
        readOnlyHint?: boolean
        destructiveHint?: boolean
        idempotentHint?: boolean
        openWorldHint?: boolean
    }
}