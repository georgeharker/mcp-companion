// NativeCombinerConnection retry/race semantics — the review's three regression cases
// (docs/reviews/2026-10-05-dev-native-mcp-review.md items 1–3), driven over pi-mcp's
// in-memory transport pair instead of HTTP:
//
//   1. a stale session (typed McpSessionExpiredError) retries ONCE and succeeds
//   2. a tool isError result containing "not found" must NOT retry (no destructive
//      re-run: the tool already executed server-side)
//   3. a reset() during an in-flight connect must not publish the laggard client
//
// HARNESS NOTE: InMemoryTransport.close() closes the server PEER too (the pair is
// one logical session), and a combiner bounce = a dead session — so the fake
// combiner mints a FRESH pair per connect (`FakeCombiner.attach()`), the same way
// the real server would accept a new HTTP session. Call counters live at the
// FakeCombiner level and therefore survive across re-connects.
import { describe, expect, it } from "vitest"
import { McpSessionExpiredError, type JsonRpcMessage, type McpTransport } from "@earendil-works/pi-mcp"
import { createInMemoryTransportPair, InMemoryTransport } from "@earendil-works/pi-mcp/testing"
import { NativeCombinerConnection, type ResolvedConnection } from "../src/native/combiner-connection.js"
import type { LogFn } from "../src/client/types.js"

type RpcEnvelope = { id?: unknown; method?: string; params?: unknown }
type RpcResponse = { result?: unknown; error?: { code: number; message: string } }

class FakeCombiner {
    /** Every tools/call the fake server actually received (across re-connects). */
    received = new Set<string>()

    constructor(private respond: (method: string, params: unknown) => RpcResponse) {}

    /** Mint one connect's transport: a fresh pair with this fake server on it. */
    attach(): InMemoryTransport {
        const pair = createInMemoryTransportPair()
        const server = pair.server
        server.onMessage((raw) => this.handle(server, raw as RpcEnvelope))
        void server.start()
        return pair.client
    }

    private handle(server: InMemoryTransport, msg: RpcEnvelope): void {
        if (typeof msg.id !== "number" || typeof msg.method !== "string") return
        if (msg.method.endsWith("initialized")) return
        if (msg.method === "initialize") {
            // SAFETY: initialize response must be wrapped in a `result` field — a bare
            // envelope leaves the client's pending initialize unsettled forever.
            void server.send({
                jsonrpc: "2.0",
                id: msg.id,
                result: {
                    // Echo the client's requested protocol version (it is in the
                    // supported list).
                    protocolVersion: (msg.params as { protocolVersion?: string } | undefined)?.protocolVersion,
                    capabilities: {},
                    serverInfo: { name: "fake-combiner", version: "0" },
                },
            } as unknown as JsonRpcMessage)
            return
        }
        if (msg.method === "tools/call") {
            this.received.add(String((msg.params as { name?: string } | undefined)?.name))
        }
        const r = this.respond(msg.method, msg.params)
        void server.send(
            { jsonrpc: "2.0", id: msg.id, ...(r.error ? { error: r.error } : { result: r.result }) } as unknown as JsonRpcMessage,
        )
    }
}

/** Wraps one pair's client leg; optionally fails the FIRST send of a given method
 *  with a typed stale error — the HTTP-404-with-session-id simulation (the real
 *  McpSessionExpiredError is raised by StreamableHttpTransport, which the in-memory
 *  pair cannot stand in for). The one-shot flag is test-level so a RETRY's fresh
 *  transport delivers instead of failing forever. */
class ScriptedTransport implements McpTransport {
    constructor(
        private readonly clientLeg: InMemoryTransport,
        private readonly failNext?: (method: string) => boolean,
    ) {}

    start(): Promise<void> {
        return this.clientLeg.start()
    }
    send(message: JsonRpcMessage): Promise<void> {
        const method = (message as unknown as RpcEnvelope).method
        if (method && this.failNext?.(method)) {
            return Promise.reject(new McpSessionExpiredError())
        }
        return this.clientLeg.send(message)
    }
    close(): Promise<void> {
        return this.clientLeg.close()
    }
    onMessage(l: Parameters<InMemoryTransport["onMessage"]>[0]) {
        return this.clientLeg.onMessage(l)
    }
    onError(l: Parameters<InMemoryTransport["onError"]>[0]) {
        return this.clientLeg.onError(l)
    }
    onClose(l: Parameters<InMemoryTransport["onClose"]>[0]) {
        return this.clientLeg.onClose(l)
    }
}

const connInputs: ResolvedConnection = { baseUrl: "http://127.0.0.1:9741/mcp" }
const quietLog: LogFn = () => undefined

function makeConnection(
    server: FakeCombiner,
    wrap?: (leg: InMemoryTransport) => McpTransport,
): NativeCombinerConnection {
    return new NativeCombinerConnection(connInputs, quietLog, {
        transportFactory: () => (wrap ? wrap(server.attach()) : server.attach()),
    })
}

const toolResult = { content: [{ type: "text", text: "ok" }], isError: false }

describe("NativeCombinerConnection retry semantics (review regression cases)", () => {
    it("retries ONCE on a typed stale-session error and succeeds", async () => {
        const server = new FakeCombiner((method) => (method === "tools/call" ? { result: toolResult } : { result: {} }))
        let staleFires = 1 // ONE stale failure, test-wide
        const conn = makeConnection(server, (leg) => new ScriptedTransport(leg, (m) => m === "tools/call" && staleFires-- > 0))
        conn.bindUi({ hasUI: false })
        conn.setToken("tok-stale")
        const result = (await conn.callTool("mock_echo", { message: "hi" })) as { content: Array<{ text: string }> }
        expect(result.content[0]?.text).toBe("ok")
        // The first attempt died in the transport BEFORE reaching the server; the
        // retry delivered exactly once.
        expect(server.received.size).toBe(1)
        expect(conn.state).toBe("connected")
        await conn.reset("test end")
    })

    it("does NOT retry a tool isError result containing stale-ish text", async () => {
        const server = new FakeCombiner((method) =>
            method === "tools/call"
                ? {
                      result: {
                          content: [{ type: "text", text: "widget not found (session scoped)" }],
                          isError: true,
                      },
                  }
                : { result: {} },
        )
        const conn = makeConnection(server)
        conn.bindUi({ hasUI: false })
        conn.setToken("tok-err")
        await expect(conn.callTool("mock_widget", {})).rejects.toThrow(/combiner tool error \(mock_widget\): widget not found/)
        // The tool executed EXACTLY once — its error text must never look like
        // transport staleness (a re-run would repeat a destructive action).
        expect(server.received.size).toBe(1)
        await conn.reset("test end")
    })

    it("does not publish a client whose connect was superseded by a reset", async () => {
        let release!: () => void
        const released = new Promise<void>((r) => {
            release = r
        })
        const server = new FakeCombiner((method) => (method === "tools/call" ? { result: toolResult } : { result: {} }))
        let starts = 0
        const conn = makeConnection(server, (leg) => {
            const t = new ScriptedTransport(leg)
            const orig = t.start.bind(t)
            t.start = async () => {
                starts++
                if (starts === 1) await released // gate the FIRST connect mid-flight
                return orig()
            }
            return t
        })
        conn.setToken("tok-race-first")
        const pending = conn.ensureConnected()
        // Token change mid-connect → reset() bumps the generation.
        conn.setToken("tok-race-second")
        release()
        await expect(pending).rejects.toThrow(/superseded/)
        // The laggard client from the OLD token was never published.
        expect((conn as unknown as { client?: unknown }).client).toBeUndefined()
        // A fresh connect on the new token works.
        await conn.ensureConnected()
        expect(conn.state).toBe("connected")
        await conn.reset("test end")
    })
})