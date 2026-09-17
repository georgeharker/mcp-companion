// The combiner MCP client: one streamable-HTTP connection, one token, reconnect.
//
// This is the piece that makes the extension the adapter: it owns the Client +
// StreamableHTTPClientTransport pair against the combiner's /mcp endpoint, mints the
// per-Pi-session grouping token into the URL path (URL form beats headers combiner-
// side, so the token rides the path), sends the optional inbound bearer, bridges
// elicitation to Pi's UI, and keeps a tools/list cache fresh via tools/list_changed.
//
// Lifecycle: lazy by default (connect on first mcp() call), `eager` connects at
// session_start. A session reset (new/resume/fork/reload) rebinds the token and
// drops the client; a transport close or stale-session 404 triggers a one-shot
// reconnect-and-retry on the next call. The combiner's handover means the same token
// resumes its parked isolated upstream sessions after a combiner restart.

import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client"
import type { ElicitResult } from "@modelcontextprotocol/client"
import { handleElicitation, type ElicitUi } from "./elicitation.js"
import type { ServerFilter } from "./config-ladder.js"

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

const CLIENT_INFO = { name: "pi-mcp-combiner", version: "0.1.0" }
const CONNECT_TIMEOUT_MS = 8_000

function isRecord(v: unknown): v is Record<string, unknown> {
    return typeof v === "object" && v !== null && !Array.isArray(v)
}

/** Pull {message, requestedSchema} out of an elicitation/create request params,
 *  tolerating both form-mode and legacy shapes. Returns undefined when unusable. */
function toElicitParams(raw: unknown): { message?: string; requestedSchema?: unknown } | undefined {
    const p = isRecord(raw) ? raw : undefined
    if (!p) return undefined
    const out: { message?: string; requestedSchema?: unknown } = {}
    if (typeof p.message === "string") out.message = p.message
    if (isRecord(p.requestedSchema)) out.requestedSchema = p.requestedSchema
    return out
}

/** Build the tokened endpoint URL: <origin>/mcp/<token> (combiner's URL form). */
export function tokenedUrl(baseUrl: string, token: string): string {
    try {
        const u = new URL(baseUrl)
        // Normalize to bare /mcp (strip any existing path segments below it), append token.
        const path = u.pathname.replace(/\/mcp(?:\/[^/]*)*\/?$/, "/mcp")
        u.pathname = `${path.replace(/\/$/, "")}/${encodeURIComponent(token)}`
        return u.toString()
    } catch {
        return baseUrl
    }
}

export class CombinerConnection {
    private conn: ResolvedConnection
    private readonly log: LogFn
    /** Latest dialog-capable UI, refreshed each session_start. */
    private elicitUi: ElicitUi = { hasUI: false }
    private prompts: PromptSummary[] | undefined
    private promptsFetchedAt = 0
    private resources: ResourceSummary[] | undefined
    private resourcesFetchedAt = 0
    private token: string | undefined
    private client: Client | undefined
    private connecting: Promise<Client> | undefined
    private tools: ToolSummary[] | undefined
    private toolsFetchedAt = 0
    private hooks: ConnectionHooks = {}
    state: ConnectionState = "disconnected"
    lastError: string | undefined

    constructor(conn: ResolvedConnection, log: LogFn) {
        this.conn = conn
        this.log = log
    }

    /** Bind lifecycle hooks (tools changed / state transitions). */
    setHooks(hooks: ConnectionHooks): void {
        this.hooks = hooks
    }

    /** Effective grouping token for this session (explicit URL token wins). */
    get sessionToken(): string | undefined {
        return this.effectiveToken()
    }

    /** Bind the UI slice used by elicitation (called from session_start). */
    bindUi(ui: ElicitUi): void {
        this.elicitUi = ui
    }

    /** Swap connection inputs (base URL / bearer env) when a session re-resolves a
     *  different combiner URL (e.g. a worktree branch pinning its own combiner).
     *  No-op when nothing changed; otherwise drops the client for a fresh connect. */
    rebind(conn: ResolvedConnection): boolean {
        if (
            conn.baseUrl === this.conn.baseUrl &&
            conn.bearerTokenEnv === this.conn.bearerTokenEnv &&
            conn.urlToken === this.conn.urlToken
        ) {
            return false
        }
        this.conn = conn
        void this.reset("connection rebound (session config changed)")
        return true
    }

    /** Set the grouping token for this session; resets any open connection. */
    setToken(token: string): void {
        if (this.token === token) return
        this.token = token
        void this.reset("token changed")
    }

    /** Drop the connection + caches (session reset / shutdown). Never throws. */
    async reset(reason: string): Promise<void> {
        const client = this.client
        this.client = undefined
        this.connecting = undefined
        this.tools = undefined
        this.state = "disconnected"
        try {
            await client?.close()
        } catch {
            // best-effort
        }
        if (reason) this.log("info", `connection reset (${reason})`)
    }

    /** Effective token: explicit URL token wins over the minted session token. */
    private effectiveToken(): string | undefined {
        return this.conn.urlToken ?? this.token
    }

    /** Connect (idempotent, single-flight). Throws with a human message on failure. */
    async ensureConnected(): Promise<Client> {
        if (this.client) return this.client
        if (this.connecting) return this.connecting

        const token = this.effectiveToken()
        if (!token) throw new Error("no grouping token set (session not started)")
        const url = tokenedUrl(this.conn.baseUrl, token)
        let endpoint: URL
        try {
            endpoint = new URL(url)
        } catch {
            throw new Error(`combiner URL "${url}" is not parseable; set a valid url (…/mcp) in settings or mcp.json`)
        }
        const bearer = this.conn.bearerTokenEnv ? process.env[this.conn.bearerTokenEnv] : undefined

        this.state = "connecting"
        this.connecting = (async () => {
            const client = new Client(CLIENT_INFO, { capabilities: { elicitation: {} } })
            const transport = new StreamableHTTPClientTransport(endpoint, {
                authProvider: bearer ? { token: async () => bearer } : undefined,
            })
            // Elicitation bridge — the combiner's permission gate depends on it.
            client.setRequestHandler("elicitation/create", (req): ElicitResult | Promise<ElicitResult> => {
                const params = toElicitParams(isRecord(req) ? req.params : undefined)
                if (!params) return { action: "decline" }
                return handleElicitation(params, this.elicitUi)
            })
            client.setNotificationHandler("notifications/tools/list_changed", () => {
                this.tools = undefined // refetch on next list
                this.prompts = undefined
                this.resources = undefined
                try {
                    this.hooks.onToolsChanged?.()
                } catch {
                    // hook errors must never break the notification path
                }
            })
            client.setNotificationHandler("notifications/resources/list_changed", () => {
                this.resources = undefined
                try {
                    this.hooks.onToolsChanged?.()
                } catch {
                    // hook errors must never break the notification path
                }
            })
            // Stage 2: the widget hold announces its UI URL via a log
            // notification ("Interactive UI ready: <url>") while the agent's
            // call is in flight — open it so the user sees the widget at once.
            client.setNotificationHandler("notifications/message", (n) => {
                const data = (n.params as { data?: unknown } | undefined)?.data
                if (typeof data !== "string") return
                const m = data.match(/Interactive UI ready: (\S+)/)
                if (m) {
                    try {
                        this.hooks.onWidgetUrl?.(m[1])
                    } catch {
                        // hook errors must never break the notification path
                    }
                }
            })
            client.onclose = () => {
                if (this.client === client) {
                    this.client = undefined
                    this.state = "disconnected"
                }
            }

            await Promise.race([
                client.connect(transport),
                new Promise((_, reject) => setTimeout(() => reject(new Error("connect timeout")), CONNECT_TIMEOUT_MS)),
            ])

            this.client = client
            this.state = "connected"
            this.lastError = undefined
            this.hooks.onStateChange?.(this.state)
            return client
        })()

        try {
            return await this.connecting
        } catch (e) {
            this.state = "failed"
            this.lastError = e instanceof Error ? e.message : String(e)
            this.log("warn", `combiner connect failed (${this.lastError})`)
            this.hooks.onStateChange?.(this.state)
            throw new Error(`cannot reach the combiner at ${this.conn.baseUrl}: ${this.lastError}`)
        } finally {
            this.connecting = undefined
        }
    }

    /** tools/list, cached until invalidated (list_changed / reset / reconnect). */
    async listTools(force = false): Promise<ToolSummary[]> {
        if (!force && this.tools && Date.now() - this.toolsFetchedAt < 5_000) return this.tools
        const client = await this.ensureConnected()
        const res = await client.listTools()
        this.tools = (res.tools ?? []) as ToolSummary[]
        this.toolsFetchedAt = Date.now()
        return this.tools
    }

    /** prompts/list, cached alongside tools (combiner namespaces `<server>_<name>`). */
    async listPrompts(force = false): Promise<PromptSummary[]> {
        if (!force && this.prompts && Date.now() - this.promptsFetchedAt < 5_000) return this.prompts
        const client = await this.ensureConnected()
        const res = await client.listPrompts()
        this.prompts = (res.prompts ?? []) as PromptSummary[]
        this.promptsFetchedAt = Date.now()
        return this.prompts
    }

    /** prompts/get through the combiner. */
    async getPrompt(name: string, args: Record<string, string>): Promise<unknown> {
        const client = await this.ensureConnected()
        return client.getPrompt({ name, arguments: args })
    }

    /** resources/list, cached alongside tools/prompts. */
    async listResources(force = false): Promise<ResourceSummary[]> {
        if (!force && this.resources && Date.now() - this.resourcesFetchedAt < 5_000) return this.resources
        const client = await this.ensureConnected()
        const res = await client.listResources()
        this.resources = (res.resources ?? []) as ResourceSummary[]
        this.resourcesFetchedAt = Date.now()
        return this.resources
    }

    /** resources/read through the combiner. */
    async readResource(uri: string): Promise<unknown> {
        const client = await this.ensureConnected()
        return client.readResource({ uri })
    }

    /** callTool with a single stale-session retry: on transport/stale errors we reset
     *  and reconnect once (the combiner may have bounced; handover keeps our token). */
    async callTool(name: string, args: Record<string, unknown> | undefined): Promise<unknown> {
        const attempt = async (): Promise<unknown> => {
            const client = await this.ensureConnected()
            return client.callTool({ name, arguments: args ?? {} })
        }
        try {
            return await attempt()
        } catch (e) {
            const msg = e instanceof Error ? e.message : String(e)
            const stale = /404|stale|session|not found|closed|fetch failed/i.test(msg)
            if (!stale) throw e
            this.log("info", `call "${name}" went stale (${msg}); reconnecting once`)
            await this.reset("stale session")
            return await attempt()
        }
    }

    /** Control-plane origin for the combiner's REST routes (/health, /sessions/*). */
    controlOrigin(): string {
        try {
            return new URL(this.conn.baseUrl).origin
        } catch {
            return this.conn.baseUrl
        }
    }

    /** The interactive-UI URL for a resource (the combiner's UI host serves the
     *  widget for THIS session's token), or empty when no token is set. */
    uiUrlFor(resourceUri: string): string {
        const token = this.effectiveToken()
        if (!token || !resourceUri) return ""
        return `${this.controlOrigin()}/ui/${encodeURIComponent(token)}/?resource=${encodeURIComponent(resourceUri)}`
    }

    /** Headers for control-plane calls: the same inbound bearer as /mcp (the combiner
     *  gates its mutating routes — /sessions*, /handover* — with it; /health is open
     *  but presenting anyway is harmless). */
    private controlHeaders(): Record<string, string> {
        const token = this.conn.bearerTokenEnv ? process.env[this.conn.bearerTokenEnv] : undefined
        return token ? { authorization: `Bearer ${token}` } : {}
    }

    /** POST the project-derived server filter for our token. The combiner accepts
     *  pending filters pre-connect, so this is safe before the first connect. */
    async applyFilter(filter: ServerFilter): Promise<void> {
        const token = this.effectiveToken()
        if (!token) throw new Error("no grouping token set (session not started)")
        const body: Record<string, unknown> = {}
        if (filter.allow?.length) body.allowed_servers = filter.allow
        else if (filter.deny?.length) body.disabled_servers = filter.deny
        else return
        const res = await fetch(`${this.controlOrigin()}/sessions/token/${encodeURIComponent(token)}/filter`, {
            method: "POST",
            headers: { "content-type": "application/json", ...this.controlHeaders() },
            body: JSON.stringify(body),
        })
        if (!res.ok) throw new Error(`filter apply failed: ${res.status} ${await res.text().catch(() => "")}`)
        this.log("info", `session server filter applied (${filter.allow ? "allow" : "deny"}-list)`)
    }

    /** GET /health — the control-plane health snapshot. */
    async health(): Promise<unknown> {
        const res = await fetch(`${this.controlOrigin()}/health`, { headers: this.controlHeaders() })
        if (!res.ok) throw new Error(`/health returned ${res.status}`)
        return res.json()
    }
}
