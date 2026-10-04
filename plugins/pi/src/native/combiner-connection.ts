// NATIVE CONNECTION : the combiner MCP client rebuilt
// on @earendil-works/pi-mcp (peer dependency on pi's own published MCP package) so the
// connection is OURS and therefore the elicitation capability is OURS to declare:
//
//     capabilities: { elicitation: {} }          // advertised, because we CAN answer
//     client.setRequestHandler("elicitation/create", …)   // elicitation.ts bridge
//
// Everything else falls through to pi-mcp: transports, reconnection of dropped SSE
// streams, protocol negotiation, tool/resource listing with pagination, roots. We add
// only what the combiner needs on top: the per-Pi-session grouping token in the URL
// path, the optional inbound bearer, tools/prompts/resources caches, one stale-session
// retry, and the widget log-notification tap.
//
// CONTRACT: this class matches client/connection.ts's public surface structurally, so
// existing consumers (prompts.ts, resources.ts, footer.ts, panel.ts, control.ts) work
// unchanged and index.ts's mode gate can pick either implementation. The model-facing
// difference from the SDK client half is registration: see native/tool-surface.ts —
// tools are declared to pi NATIVELY (pi.registerTool with exposure/namespace), there is
// no mcp() proxy and no script tool.
//
// Why instantiate instead of wrapping: pi's built-in MCP extension owns its client
// instances privately; no hook reaches them (verified 0.99→1.0). Owning the connection
// is the only sanctioned way to advertise elicitation today. The two-switch upstream
// design (per-server config + handler-availability negotiation) remains the ecosystem
// endgame; this class is what the extension ships until and after that lands.

import { McpClient, McpSessionExpiredError, StreamableHttpTransport } from "@earendil-works/pi-mcp"
import type { CallToolResult } from "@earendil-works/pi-mcp"
import { handleElicitation, type ElicitResponse, type ElicitUi } from "../client/elicitation.js"
import type { ServerFilter } from "../client/config-ladder.js"

// ── shared contracts (kept aligned with client/connection.ts by the callers) ────────

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

export type ToolSummary = {
    name: string
    description?: string
    inputSchema?: unknown
    /** MCP tool annotations, carried straight through to pi.registerTool so pi's
     *  permission extensions can classify calls. */
    annotations?: ToolAnnotationsSummary
}
export type ToolAnnotationsSummary = {
    readOnlyHint?: boolean
    destructiveHint?: boolean
    idempotentHint?: boolean
    openWorldHint?: boolean
}

export type PromptArgument = { name: string; description?: string; required?: boolean }
export type PromptSummary = { name: string; title?: string; description?: string; arguments?: PromptArgument[] }
export type ResourceSummary = {
    uri: string
    name?: string
    description?: string
    mimeType?: string
    _meta?: Record<string, unknown>
}

export type ConnectionHooks = {
    onToolsChanged?: () => void
    onStateChange?: (state: ConnectionState) => void
    onWidgetUrl?: (url: string) => void
}

// pi-mcp doesn't (yet) export an ElicitResult type — shapes are protocol-stable:
// accept/decline/cancel with an optional content record. Tracked as a tiny upstream
// export PR candidate.
export type ElicitResult = ElicitResponse

const CLIENT_INFO = { name: "pi-mcp-combiner", version: "0.14.3" }
const CONNECT_TIMEOUT_MS = 8_000
const LIST_CACHE_MS = 5_000

function isRecord(v: unknown): v is Record<string, unknown> {
    return typeof v === "object" && v !== null && !Array.isArray(v)
}

/** Build the tokened endpoint URL: <origin>/mcp/<token> (identical to client/connection.ts). */
export function tokenedUrl(baseUrl: string, token: string): string {
    try {
        const u = new URL(baseUrl)
        const path = u.pathname.replace(/\/mcp(?:\/[^/]*)*\/?$/, "/mcp")
        u.pathname = `${path.replace(/\/$/, "")}/${encodeURIComponent(token)}`
        return u.toString()
    } catch {
        return baseUrl
    }
}

export class NativeCombinerConnection {
    private conn: ResolvedConnection
    private readonly log: LogFn
    private elicitUi: ElicitUi = { hasUI: false }
    private token: string | undefined
    private client: McpClient | undefined
    private connecting: Promise<McpClient> | undefined
    private tools: ToolSummary[] | undefined
    private toolsFetchedAt = 0
    private prompts: PromptSummary[] | undefined
    private promptsFetchedAt = 0
    private resources: ResourceSummary[] | undefined
    private resourcesFetchedAt = 0
    private hooks: ConnectionHooks = {}
    state: ConnectionState = "disconnected"
    lastError: string | undefined

    constructor(conn: ResolvedConnection, log: LogFn) {
        this.conn = conn
        this.log = log
    }

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

    /** Swap connection inputs (base URL / bearer env); drops the client when changed. */
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
        this.prompts = undefined
        this.resources = undefined
        this.state = "disconnected"
        try {
            await client?.close()
        } catch {
            // best-effort
        }
        if (reason) this.log("info", `connection reset (${reason})`)
    }

    private effectiveToken(): string | undefined {
        return this.conn.urlToken ?? this.token
    }

    /** Connect (idempotent, single-flight). THE elicitation-relevant site: the
     *  capability + request handler live HERE, on a client we own. */
    async ensureConnected(): Promise<McpClient> {
        if (this.client) return this.client
        if (this.connecting) return this.connecting

        const token = this.effectiveToken()
        if (!token) throw new Error("no grouping token set (session not started)")
        const endpoint = tokenedUrl(this.conn.baseUrl, token)
        let url: URL
        try {
            url = new URL(endpoint)
        } catch {
            throw new Error(`combiner URL "${endpoint}" is not parseable; set a valid url (…/mcp) in settings or mcp.json`)
        }
        const bearer = this.conn.bearerTokenEnv ? process.env[this.conn.bearerTokenEnv] : undefined

        this.state = "connecting"
        this.connecting = (async () => {
            const client = new McpClient({
                name: CLIENT_INFO.name,
                version: CLIENT_INFO.version,
                // Advertised elicitation — the combiner's permission gate depends on it,
                // and our handler below CAN answer (decline when no dialog UI exists).
                capabilities: { elicitation: {} },
                // Widget holds keep a call in flight up to MCP_COMBINER_UI_HOLD_TIMEOUT
                // (default 50s combiner-side); 90s headroom so the native call outlives it.
                requestTimeoutMs: 90_000,
            })

            client.setRequestHandler(
                "elicitation/create",
                async (raw: unknown, { signal }: { signal: AbortSignal }): Promise<ElicitResult> => {
                    const params = isRecord(raw) ? raw : undefined
                    const message = typeof params?.message === "string" ? params.message : undefined
                    const requestedSchema = isRecord(params?.requestedSchema) ? params?.requestedSchema : undefined
                    if (!message && !requestedSchema) return { action: "decline" }
                    const result = await handleElicitation({ message, requestedSchema }, this.elicitUi, signal)
                    return result as ElicitResult
                },
            )

            // pi-mcp's onNotification listeners receive the notification PARAMS directly
            // (not the raw envelope) — params shape for notifications/message is
            // { level, logger, data }.
            client.onNotification("notifications/tools/list_changed", () => {
                this.tools = undefined
                this.prompts = undefined
                this.resources = undefined
                try {
                    this.hooks.onToolsChanged?.()
                } catch {
                    // hook errors must never break the notification path
                }
            })
            client.onNotification("notifications/resources/list_changed", () => {
                this.resources = undefined
                try {
                    this.hooks.onToolsChanged?.()
                } catch {
                    // hook errors must never break the notification path
                }
            })
            // Widget hold announces its UI URL via a log notification mid-flight.
            client.onNotification("notifications/message", (params: unknown) => {
                const data = (isRecord(params) ? params.data : undefined)
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
            client.onClose(() => {
                if (this.client === client) {
                    this.client = undefined
                    this.state = "disconnected"
                    this.hooks.onStateChange?.(this.state)
                }
            })

            const transport = new StreamableHttpTransport({
                url,
                authProvider: bearer ? { token: async () => bearer } : undefined,
            })
            await Promise.race([
                client.connect(transport),
                new Promise((_, reject) => setTimeout(() => reject(new Error("connect timeout")), CONNECT_TIMEOUT_MS)),
            ])

            this.client = client
            this.state = "connected"
            this.lastError = undefined
            // Catch-up after (re)connect — same rationale as client/connection.ts:
            // MCP notifications are lossy for disconnected clients, so re-sync every
            // surface the moment the stream is live again. Idempotent upstream.
            try {
                this.hooks.onToolsChanged?.()
            } catch {
                // hook errors must never break the connect path
            }
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
            throw new Error(`cannot reach the combiner at ${this.conn.baseUrl}: ${this.lastError} — the combiner may have restarted; this call did not run and is safe to retry (carried state: grants, filters, parked sessions survives restarts)`)
        } finally {
            this.connecting = undefined
        }
    }

    /** tools/list, cached until invalidated (list_changed / reset / reconnect).
     *  pi-mcp returns the tools array directly (pagination already resolved). */
    async listTools(force = false): Promise<ToolSummary[]> {
        if (!force && this.tools && Date.now() - this.toolsFetchedAt < LIST_CACHE_MS) return this.tools
        const client = await this.ensureConnected()
        const tools: ToolSummary[] = (await client.listTools()).map((t) => ({
            name: t.name,
            description: t.description,
            inputSchema: t.inputSchema,
            annotations: t.annotations as ToolAnnotationsSummary | undefined,
        }))
        this.tools = tools
        this.toolsFetchedAt = Date.now()
        return tools
    }

    /** prompts/list, cached alongside tools. pi-mcp has no prompts convenience (yet);
     *  the generic request() is the sanctioned path for server-initiated methods the
     *  client doesn't model. */
    async listPrompts(force = false): Promise<PromptSummary[]> {
        if (!force && this.prompts && Date.now() - this.promptsFetchedAt < LIST_CACHE_MS) return this.prompts
        const client = await this.ensureConnected()
        const res = await client.request<{ prompts: PromptSummary[] }>("prompts/list")
        this.prompts = res.prompts ?? []
        this.promptsFetchedAt = Date.now()
        return this.prompts
    }

    /** prompts/get through the combiner. */
    async getPrompt(name: string, args: Record<string, string>): Promise<unknown> {
        const client = await this.ensureConnected()
        return client.request("prompts/get", { name, arguments: args })
    }

    /** resources/list, cached alongside tools/prompts.
     *  SAFETY: pi-mcp's Resource adds typed `_meta` variance the combiner surfaces as
     *  Record<string, unknown> — the shapes agree on the fields we consume
     *  (uri/name/description/mimeType), so the cast narrows only metadata typing. */
    async listResources(force = false): Promise<ResourceSummary[]> {
        if (!force && this.resources && Date.now() - this.resourcesFetchedAt < LIST_CACHE_MS) return this.resources
        const client = await this.ensureConnected()
        const resources: ResourceSummary[] = ((await client.listResources()) as unknown) as ResourceSummary[]
        this.resources = resources
        this.resourcesFetchedAt = Date.now()
        return resources
    }

    /** resources/read through the combiner. */
    async readResource(uri: string): Promise<unknown> {
        const client = await this.ensureConnected()
        return client.readResource(uri)
    }

    /** callTool with one stale-session retry (combiner bounce; handover keeps token).
     *  A server-marked error (CallToolResult.isError) THROWS so pi records the call
     *  as a tool error — same convention the rest of the extension uses. */
    async callTool(name: string, args: Record<string, unknown> | undefined): Promise<CallToolResult> {
        const attempt = async (): Promise<CallToolResult> => {
            const client = await this.ensureConnected()
            const result = await client.callTool(name, args)
            if (result.isError) {
                const text = (result.content ?? [])
                    .map((b) => (isRecord(b) && b.type === "text" && typeof b.text === "string" ? b.text : JSON.stringify(b)))
                    .filter(Boolean)
                    .join("\n")
                throw new Error(`combiner tool error (${name}): ${text}`)
            }
            return result
        }
        try {
            return await attempt()
        } catch (e) {
            // pi-mcp raises a TYPED McpSessionExpiredError on 404-with-session-id (the
            // combiner's stale-session 404) — catch it first-class, exactly like pi's
            // own builtin does ("retry once on a new session; the old client is
            // detached, not closed, so other in-flight calls get their own 404 and
            // retry the same way"). The message regex stays as a belt for anything
            // the transport doesn't classify.
            const typedStale = e instanceof McpSessionExpiredError
            const msg = e instanceof Error ? e.message : String(e)
            const stale = typedStale || /404|stale|session|not found|closed|fetch failed|illegal/i.test(msg)
            if (!stale) throw e
            this.log(
                "info",
                `call "${name}" went stale (${typedStale ? "session expired (typed)" : msg}); reconnecting once`,
            )
            await this.reset("stale session")
            return await attempt()
        }
    }

    // ── control plane (unchanged from client/connection.ts) ─────────────────────────

    controlOrigin(): string {
        try {
            return new URL(this.conn.baseUrl).origin
        } catch {
            return this.conn.baseUrl
        }
    }

    uiUrlFor(resourceUri: string): string {
        const token = this.effectiveToken()
        if (!token || !resourceUri) return ""
        return `${this.controlOrigin()}/ui/${encodeURIComponent(token)}/?resource=${encodeURIComponent(resourceUri)}`
    }

    private controlHeaders(): Record<string, string> {
        const token = this.conn.bearerTokenEnv ? process.env[this.conn.bearerTokenEnv] : undefined
        return token ? { authorization: `Bearer ${token}` } : {}
    }

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

    async health(): Promise<unknown> {
        const res = await fetch(`${this.controlOrigin()}/health`, { headers: this.controlHeaders() })
        if (!res.ok) throw new Error(`/health returned ${res.status}`)
        return res.json()
    }
}