// Shared-MCP-config reading for the combiner entry — the `.pi/mcp.json` interplay.
//
// The extension READS the standard MCP files (pi-mcp-adapter's ladder, mirrored here)
// but never writes them. It recognizes "its" entry (`mcp-combiner` by name,
// `x-combiner: true`, or a url whose origin matches the resolved combiner origin) and
// honors:
//   - `url` (incl. an explicit path token like /mcp/pi-<name>, which wins over our
//     minted per-session token)
//   - `auth`/`bearerTokenEnv` (compat keys; we only ever send a static bearer)
//   - a namespaced `combiner` block for per-project exposure knobs:
//       { "servers": { "allow": [...] | "deny": [...] }, "exposeResources": bool, "prompts": bool }
//     Unknown keys pass through pi-mcp-adapter's lenient validator untouched, so this
//     file stays valid for both readers simultaneously.
//
// Later files win (adapter precedence order). Server DEFINITIONS stay global in the
// combiner's servers.json — the ladder only shapes connection + exposure.

import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { agentDir } from "./settings.js"
import type { DirectToolsSpec } from "./direct-tools.js"

export type ServerFilter = { allow?: string[]; deny?: string[] }

export type CombinerBlock = {
    servers?: ServerFilter
    exposeResources?: boolean
    prompts?: boolean
    /** Direct tool promotion: glob allowlist over combined tool names (e.g.
     *  ["combiner__status", "github_search_*"]), or "search" to register tools as
     *  first-class Pi tools the first time mcp({search}) matches them. */
    directTools?: string[] | "search"
}

export type CombinerEntry = {
    url?: string
    bearerTokenEnv?: string
    combiner?: CombinerBlock
}

export type LadderResult = {
    /** Merged combiner entry (later files win). Empty object when absent everywhere. */
    entry: CombinerEntry
    /** Non-combiner server names seen in the ladder — reported (not connected) in v1. */
    otherServers: string[]
    /** Files that existed and were read, in ladder order. */
    sources: string[]
}

/** The ladder, lowest precedence first. Mirrors pi-mcp-adapter's documented order. */
export function ladderPaths(cwd: string): string[] {
    return [
        join(homedir(), ".config", "mcp", "mcp.json"),
        join(homedir(), ".agents", "mcp.json"),
        join(homedir(), ".agents", "mcp", "mcp.json"),
        join(agentDir(), "mcp.json"),
        join(cwd, ".mcp.json"),
        join(cwd, ".pi", "mcp.json"),
    ]
}

/** Origin (scheme://host:port) of a URL string, or undefined when unparseable. */
function origin(url: string): string | undefined {
    try {
        return new URL(url).origin
    } catch {
        return undefined
    }
}

function isRecord(v: unknown): v is Record<string, unknown> {
    return typeof v === "object" && v !== null && !Array.isArray(v)
}

function stringArray(v: unknown): string[] | undefined {
    return Array.isArray(v) && v.every((x) => typeof x === "string") ? (v as string[]) : undefined
}

/** Recognize "our" entry: explicit name, explicit marker, or url origin match. */
export function isCombinerEntry(
    name: string,
    entry: Record<string, unknown>,
    combinerOrigin: string | undefined,
): boolean {
    if (name === "mcp-combiner") return true
    if (entry["x-combiner"] === true) return true
    const url = typeof entry.url === "string" ? entry.url : undefined
    if (url && combinerOrigin && origin(url) === combinerOrigin) return true
    return false
}

/** Parse one file's contribution: the combiner entry (if recognized) + other names. */
function readOne(path: string, combinerOrigin: string | undefined): { entry?: CombinerEntry; others: string[] } {
    let doc: unknown
    try {
        doc = JSON.parse(readFileSync(path, "utf8"))
    } catch {
        return { others: [] } // absent or malformed → contributes nothing
    }
    if (!isRecord(doc)) return { others: [] }
    const servers = doc.mcpServers ?? doc["mcp-servers"]
    if (!isRecord(servers)) return { others: [] }

    let entry: CombinerEntry | undefined
    const others: string[] = []
    for (const [name, raw] of Object.entries(servers)) {
        if (!isRecord(raw)) continue
        if (isCombinerEntry(name, raw, combinerOrigin)) {
            // Later ladder files replace (not deep-merge) the entry, matching the
            // adapter's later-wins semantics; the `combiner` block merges per-key.
            const prev = entry
            const block: CombinerBlock = { ...(prev?.combiner ?? {}) }
            const rawBlock = isRecord(raw.combiner) ? raw.combiner : undefined
            if (rawBlock) {
                const serversBlock = isRecord(rawBlock.servers) ? rawBlock.servers : undefined
                if (serversBlock) {
                    block.servers = {
                        allow: stringArray(serversBlock.allow) ?? block.servers?.allow,
                        deny: stringArray(serversBlock.deny) ?? block.servers?.deny,
                    }
                }
                if (typeof rawBlock.exposeResources === "boolean") block.exposeResources = rawBlock.exposeResources
                if (typeof rawBlock.prompts === "boolean") block.prompts = rawBlock.prompts
                if (rawBlock.directTools === "search") block.directTools = "search"
                else if (stringArray(rawBlock.directTools)?.length)
                    block.directTools = stringArray(rawBlock.directTools)
            }
            entry = {
                url: typeof raw.url === "string" && raw.url ? raw.url : prev?.url,
                bearerTokenEnv:
                    typeof raw.bearerTokenEnv === "string" && raw.bearerTokenEnv
                        ? raw.bearerTokenEnv
                        : prev?.bearerTokenEnv,
                combiner: Object.keys(block).length ? block : prev?.combiner,
            }
        } else {
            others.push(name)
        }
    }
    return { entry, others }
}

/** Walk the ladder (cwd-relative project files included) and merge. */
export function readLadder(cwd: string, combinerOrigin: string | undefined): LadderResult {
    const out: LadderResult = { entry: {}, otherServers: [], sources: [] }
    for (const path of ladderPaths(cwd)) {
        if (!existsSync(path)) continue
        out.sources.push(path)
        const { entry, others } = readOne(path, combinerOrigin)
        if (entry) out.entry = entry
        for (const name of others) if (!out.otherServers.includes(name)) out.otherServers.push(name)
    }
    return out
}

// ── session config resolution ──────────────────────────────────────
// The per-session view of everything the ladder shapes, resolved against a session
// cwd (NOT process.cwd()): worktree subagents and project switches get their own
// project layers (.mcp.json / .pi/mcp.json), while the global layers and env are
// cwd-independent. Computed at factory time as a pre-session default, re-resolved
// at every session_start with ctx.cwd.

export type SessionConfig = {
    /** Token-stripped base URL incl. /mcp. */
    baseUrl: string
    bearerTokenEnv: string | undefined
    /** Explicit token from the configured URL path (user override), if any. */
    urlToken: string | undefined
    serverFilter: ServerFilter | undefined
    directSpec: DirectToolsSpec | undefined
    entry: CombinerEntry
    otherServers: string[]
    sources: string[]
}

export type ResolveSessionConfigOptions = {
    /** Session working directory (project layers read from here). */
    cwd: string
    /** Explicit env URL (host-owned or PI_MCP_COMBINER_URL), highest precedence. */
    envUrl: string | undefined
    /** Settings-file URL, next precedence. */
    settingsUrl: string | undefined
    /** Serve-side default (host:port/mcp) for origin matching and the fallback. */
    defaultUrl: string
}

/** Extract an explicit /mcp/<token> path token from a URL, if present. */
export function urlTokenOf(url: string): string | undefined {
    const m = url.match(/\/mcp\/([^/?#]+)/)
    return m?.[1]
}

/** Normalize a URL back to bare /mcp (strip any token path). */
export function stripUrlToken(url: string): string {
    return url.replace(/(\/mcp)\/[^/?#]+/, "$1")
}

function originOf(url: string): string | undefined {
    try {
        return new URL(url).origin
    } catch {
        return undefined
    }
}

/** Resolve the full per-session config. Pure — no env or settings reads inside. */
export function resolveSessionConfig(opts: ResolveSessionConfigOptions): SessionConfig {
    const ladder = readLadder(opts.cwd, originOf(opts.defaultUrl))
    const url = opts.envUrl ?? opts.settingsUrl ?? ladder.entry.url ?? opts.defaultUrl
    return {
        baseUrl: stripUrlToken(url),
        bearerTokenEnv: ladder.entry.bearerTokenEnv,
        urlToken: urlTokenOf(url),
        serverFilter: ladder.entry.combiner?.servers,
        directSpec: ladder.entry.combiner?.directTools,
        entry: ladder.entry,
        otherServers: ladder.otherServers,
        sources: ladder.sources,
    }
}
