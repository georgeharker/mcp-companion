// Shared connection contract: the types every surface (router, panel, footer,
// prompts, resources, the extension wiring) types against. The concrete class
// lives in native/combiner-connection.ts (the pi-mcp transport); this module
// owns the INTERFACE so no surface depends on the implementation.

import type { ServerFilter } from "./config-ladder.js"
import type { ElicitUi } from "./elicitation.js"

export type LogFn = (level: "info" | "warn" | "error", message: string) => void

export type ResolvedConnection = {
    /** Base URL including /mcp, WITHOUT any token path (e.g. http://127.0.0.1:9741/mcp). */
    baseUrl: string
    /** Env var holding the inbound bearer token (read at connect time), if any. */
    bearerTokenEnv?: string
    /** Explicit token already present in the configured URL path (user override). */
    urlToken?: string
}

export type ConnectionState = "disconnected" | "connecting" | "connected" | "failed"

export type ToolSummary = { name: string; description?: string; inputSchema?: unknown }

export type PromptArgument = { name: string; description?: string; required?: boolean }

export type PromptSummary = { name: string; title?: string; description?: string; arguments?: PromptArgument[] }

export type ResourceSummary = {
    uri: string
    name?: string
    description?: string
    mimeType?: string
    _meta?: Record<string, unknown>
}

/** Optional callbacks fired by the connection (set from index.ts wiring). */
export type ConnectionHooks = {
    /** tools/list_changed arrived — caches invalidated; refresh derived surfaces. */
    onToolsChanged?: () => void
    /** connection state transitioned — refresh footer/status surfaces. */
    onStateChange?: (state: ConnectionState) => void
    /** Stage 2: the combiner announced a widget UI URL while an agent tool call
     *  is held in flight — the client should open it (gated by uiAutoOpen). */
    onWidgetUrl?: (url: string) => void
}

/** The connection surface every module types against — the duck-parity contract
 *  both the retired SDK connection and the native pi-mcp connection implemented. */
export interface CombinerConnection {
    state: ConnectionState
    lastError: string | undefined
    sessionToken: string | undefined
    setToken(token: string): void
    rebind(conn: ResolvedConnection): boolean
    reset(reason: string): Promise<void>
    ensureConnected(): Promise<unknown>
    listTools(force?: boolean): Promise<ToolSummary[]>
    listPrompts(force?: boolean): Promise<PromptSummary[]>
    getPrompt(name: string, args: Record<string, string>): Promise<unknown>
    listResources(force?: boolean): Promise<ResourceSummary[]>
    readResource(uri: string): Promise<unknown>
    /** Call a combiner tool. `options.signal` rides into the transport — pi's cancel
     *  aborts upstream work and widget holds. Throws on transport failure (with a
     *  recovery hint) and on server-marked tool errors (result.isError). */
    callTool(name: string, args: Record<string, unknown> | undefined, options?: { signal?: AbortSignal }): Promise<unknown>
    controlOrigin(): string
    uiUrlFor(resourceUri: string): string
    applyFilter(filter: ServerFilter): Promise<void>
    health(): Promise<unknown>
    setHooks(hooks: ConnectionHooks): void
    bindUi(ui: ElicitUi): void
}

/** Build the tokened endpoint URL: <origin>/mcp/<token> (combiner's URL form). */
export function tokenedUrl(baseUrl: string, token: string): string {
    try {
        const u = new URL(baseUrl)
        u.pathname = `${u.pathname.replace(/\/+$/, "")}/${token}`
        return u.toString()
    } catch {
        return `${baseUrl.replace(/\/+$/, "")}/${token}`
    }
}
