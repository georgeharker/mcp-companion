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
// CONTRACT: this class implements client/types.ts's CombinerConnection interface, so
// every surface (prompts.ts, resources.ts, footer.ts, panel.ts, control.ts) types
// against the interface, not against this file. The model-facing difference in native
// mode is registration: see native/tool-surface.ts — tools are declared to pi NATIVELY
// (pi.registerTool with exposure/namespace/annotations), and the mcp() router
// (createMcpTool in index.ts) ALSO rides this same connection — there is no script tool.
//
// Why instantiate instead of wrapping: pi's built-in MCP extension owns its client
// instances privately; no hook reaches them (verified 0.99→1.0). Owning the connection
// is the only sanctioned way to advertise elicitation today. The two-switch upstream
// design (per-server config + handler-availability negotiation) remains the ecosystem
// endgame; this class is what the extension ships until and after that lands.

import { appendFileSync } from "node:fs"
import {
    McpClient,
    McpConnectionClosedError,
    McpSessionExpiredError,
    McpTimeoutError,
    StreamableHttpTransport,
    type CallToolResult,
    type McpRequestOptions,
    type McpTransport,
} from "@earendil-works/pi-mcp"
import { handleElicitation, type ElicitResponse, type ElicitUi } from "../client/elicitation.js"
import type { CombinerConnection } from "../client/types.js"
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


// Debug logging is OPT-IN (env PI_MCP_COMBINER_NC_DBG=1): it writes tool names, session
// ids and error text to /tmp/pi-combiner-dbg.log. Ships disabled — never on by default.
const ncDbgEnabled = process.env.PI_MCP_COMBINER_NC_DBG === "1"
function ncDbg(s: string): void {
    if (!ncDbgEnabled) return
    try {
        appendFileSync("/tmp/pi-combiner-dbg.log", `${new Date().toISOString()} ${s}\n`)
    } catch {
        // debug only
    }
}

// Keep in sync with plugins/pi/package.json "version": scripts/bump-version.sh stamps
// this line alongside the package files (the string is what this client declares).
const CLIENT_INFO = { name: "pi-mcp-combiner", version: "0.16.0" }
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

export class NativeCombinerConnection implements CombinerConnection {
    private conn: ResolvedConnection
    private readonly log: LogFn
    private elicitUi: ElicitUi = { hasUI: false }
    private token: string | undefined
    private client: McpClient | undefined
    /** Bumped by every reset: in-flight connects check it before publishing a client
     *  (a connect that started before the reset must never win the publish race). */
    private generation = 0
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

    /** Test seam: build the transport for one connect (defaults to
     *  StreamableHttpTransport). Injection lets tests drive the connection over an
     *  in-memory pair instead of HTTP. */
    private readonly transportFactory: ((url: URL) => McpTransport) | undefined

    constructor(conn: ResolvedConnection, log: LogFn, opts?: { transportFactory?: (url: URL) => McpTransport }) {
        this.conn = conn
        this.log = log
        this.transportFactory = opts?.transportFactory
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
        this.generation++
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
        const genAtStart = this.generation
        const connectPromise = (async (): Promise<McpClient> => {
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

            const transport = this.transportFactory
                ? this.transportFactory(url)
                : new StreamableHttpTransport({
                      url,
                      authProvider: bearer ? { token: async () => bearer } : undefined,
                  })
            let timer: ReturnType<typeof setTimeout> | undefined
            const connectP = client.connect(transport)
            const settle = connectP.then((r) => r)
            // Late rejections (close beats connect, timeout raced a slow success) must
            // not become unhandled rejections — pi crashes on those.
            settle.catch(() => undefined)
            try {
                await Promise.race([
                    settle,
                    new Promise((_, reject) =>
                        setTimeout(
                            () => reject(new Error(`connect timeout after ${CONNECT_TIMEOUT_MS}ms`)),
                            CONNECT_TIMEOUT_MS,
                        ),
                    ),
                ])
            } catch (e) {
                // The client may STILL complete its connect after this failure (the
                // timeout raced a slow success): close it best-effort, non-blocking, so
                // no untracked SSE stream/handlers compete for the token's elicitation
                // routing (the zombie class the reload teardown also guards).
                void client.close().catch(() => undefined)
                throw e
            } finally {
                if (timer) clearTimeout(timer)
            }

            if (this.generation !== genAtStart) {
                // A reset() (setToken/rebind/session switch) ran while this connect was
                // in flight: never publish the laggard — it would resurrect the old
                // token/routing the reset just dropped. Close it; the NEXT
                // ensureConnected builds its own.
                void client.close().catch(() => undefined)
                throw new Error("connect superseded by a connection reset (token/config changed)")
            }

            this.client = client
            // SAFETY: pi-mcp's McpClient session id lives only on concrete internals —
            // the casts read it for debug output only; unknown means "unexposed".
            ncDbg(
                `ensureConnected: CONNECTED session=${(client as unknown as { sessionId?: string }).sessionId ?? "?"} state=${(client as unknown as { transport?: { sessionId?: string } }).transport?.sessionId ?? "?"}`,
            )
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
        this.connecting = connectPromise

        try {
            return await connectPromise
        } catch (e) {
            const superseded = this.generation !== genAtStart
            if (!superseded) {
                this.state = "failed"
                this.lastError = e instanceof Error ? e.message : String(e)
                this.log("warn", `combiner connect failed (${this.lastError})`)
                this.hooks.onStateChange?.(this.state)
            }
            throw superseded
                ? e
                : new Error(`cannot reach the combiner at ${this.conn.baseUrl}: ${this.lastError} — the combiner may have restarted; this call did not run and is safe to retry (carried state: grants, filters, parked sessions survives restarts)`)
        } finally {
            // Identity check: a reset + newer connect may have repopulated
            // this.connecting while we awaited — never wipe the newer single-flight.
            if (this.connecting === connectPromise) this.connecting = undefined
        }
    }

    /** One-shot stale-session retry shared by EVERY request method — a combiner
     *  bounce invalidates the transport session, and the first operation to notice
     *  can be any of them (the router's listTools lookup runs BEFORE callTool;
     *  without this it surfaced raw McpSessionExpiredError and never reset the
     *  client, wedging the connection until reload).
     *  Classification is TYPE-based over pi-mcp's exported taxonomy; the message
     *  regex remains only as a belt for raw transport failures pi-mcp does not wrap
     *  (e.g. fetch failed). callTool passes retryStaleOnly: only
     *  McpSessionExpiredError — which the server raises BEFORE executing anything —
     *  may trigger an automatic re-run there. */
    private async withStaleRetry<T>(
        label: string,
        op: (client: McpClient) => Promise<T>,
        opts?: { retryStaleOnly?: boolean },
    ): Promise<T> {
        const client = await this.ensureConnected()
        try {
            return await op(client)
        } catch (e) {
            const msg = e instanceof Error ? e.message : String(e)
            const staleOnly = opts?.retryStaleOnly === true
            const stale =
                e instanceof McpSessionExpiredError ||
                (!staleOnly &&
                    (e instanceof McpConnectionClosedError ||
                        e instanceof McpTimeoutError ||
                        /404|stale|session|not found|closed|fetch failed|illegal/i.test(msg)))
            if (!stale) throw e
            ncDbg(`${label} stale-retry FIRED typed=${e instanceof McpSessionExpiredError}`)
            this.log(
                "info",
                `${label} went stale (${e instanceof McpSessionExpiredError ? "session expired" : msg}); reconnecting once`,
            )
            // Reset ONLY if the failing client is still the current one — a concurrent
            // caller may already have reset and reconnected ("detach, don't close" was
            // pi's builtin wording); resetting here would close the FRESH client every
            // in-flight call just reconnected, cascading into another stale round.
            if (this.client === client) {
                await this.reset("stale session")
            } else {
                ncDbg(`${label} skipped reset — client already replaced by a concurrent reset`)
            }
            return await op(await this.ensureConnected())
        }
    }

    /** tools/list, cached until invalidated (list_changed / reset / reconnect).
     *  pi-mcp returns the tools array directly (pagination already resolved). */
    async listTools(force = false): Promise<ToolSummary[]> {
        if (!force && this.tools && Date.now() - this.toolsFetchedAt < LIST_CACHE_MS) return this.tools
        const tools: ToolSummary[] = (await this.withStaleRetry("listTools", (c) => c.listTools())).map((t) => ({
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
        const res = await this.withStaleRetry("listPrompts", (c) =>
            c.request<{ prompts: PromptSummary[] }>("prompts/list"),
        )
        this.prompts = res.prompts ?? []
        this.promptsFetchedAt = Date.now()
        return this.prompts
    }

    /** prompts/get through the combiner. */
    async getPrompt(name: string, args: Record<string, string>): Promise<unknown> {
        return this.withStaleRetry(`getPrompt ${name}`, (c) =>
            c.request("prompts/get", { name, arguments: args }),
        )
    }

    /** resources/list, cached alongside tools/prompts.
     *  SAFETY: pi-mcp's Resource adds typed `_meta` variance the combiner surfaces as
     *  Record<string, unknown> — the shapes agree on the fields we consume
     *  (uri/name/description/mimeType), so the cast narrows only metadata typing. */
    async listResources(force = false): Promise<ResourceSummary[]> {
        if (!force && this.resources && Date.now() - this.resourcesFetchedAt < LIST_CACHE_MS) return this.resources
        const resources: ResourceSummary[] = ((await this.withStaleRetry("listResources", (c) =>
            c.listResources(),
        )) as unknown) as ResourceSummary[]
        this.resources = resources
        this.resourcesFetchedAt = Date.now()
        return resources
    }

    /** resources/read through the combiner. */
    async readResource(uri: string): Promise<unknown> {
        return this.withStaleRetry(`readResource ${uri}`, (c) => c.readResource(uri))
    }

    /** callTool. Transport-level stale retry is STALE-ONLY (typed
     *  McpSessionExpiredError): that error is raised by the server BEFORE it
     *  executes the request, so one re-run is provably safe. Any other failure —
     *  closed/timeout/"fetch failed" AFTER delivery — may correspond to a request
     *  that already EXECUTED upstream; an automatic re-run could repeat a
     *  destructive tool, so those surface as errors (with the recovery hints) and
     *  the model/user decides whether to call again.
     *  A server-marked error (CallToolResult.isError) is checked AFTER the transport
     *  retry layer and THROWS, so pi records the call as a tool error — and crucially
     *  the tool's own error TEXT can never reach the retry classification (a
     *  "…not found" tool error must not look like transport staleness).
     *  `options.signal` rides into the transport: cancellation cancels upstream work
     *  and widget holds (notifications/cancelled). */
    async callTool(
        name: string,
        args: Record<string, unknown> | undefined,
        options?: McpRequestOptions,
    ): Promise<CallToolResult> {
        const result = await this.withStaleRetry(`callTool ${name}`, (c) => c.callTool(name, args, options), {
            retryStaleOnly: true,
        })
        if (!result.isError) return result
        const text = (result.content ?? [])
            .map((b) => (isRecord(b) && b.type === "text" && typeof b.text === "string" ? b.text : JSON.stringify(b)))
            .filter(Boolean)
            .join("\n")
        throw new Error(`combiner tool error (${name}): ${text}`)
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