// NATIVE MODE : activation entry for the
// pi-mcp-based combiner connection + native tool surface. This file exists so the
// wire-in stays a few lines in src/index.ts's mode gate while the shape is reviewed:
//
//   const native = settings.integration === "nativeTools"   // new tri-state member
//   const conn = native ? new NativeCombinerConnection(…, clientLog)   // same contract
//                        : new CombinerConnection(…, clientLog)
//   if (native) void activateNativeTools(pi, { connection: conn, ... }) as needed
//
// Mode matrix (the native-migration plan item):: extension-owned connection (peer dep on @earendil-works/pi-mcp) + native
// tool declaration via pi.registerTool(exposure/namespace/annotations) = native
// exposure with elicitation STILL ANSWERED (capability advertised on the owned
// connection) — no mcp() proxy, no script tool, no upstream PR required for v1.

import type { ExtensionAPI, ExtensionContext } from "../pi.js"
import type { ServerFilter } from "../client/config-ladder.js"
import { NativeCombinerConnection } from "./combiner-connection.js"
import { activateNativeTools } from "./tool-surface.js"
import { activateNativeResources } from "./resources.js"

export { NativeCombinerConnection } from "./combiner-connection.js"
export { activateNativeTools, type NativeToolSurfaceOptions } from "./tool-surface.js"
export { tokenedUrl } from "./combiner-connection.js"
export { activateNativeResources, filterInteractiveResources, type NativeResourceOptions } from "./resources.js"

export type NativeActivationOptions = {
    connection: NativeCombinerConnection
    directSpec?: string[] | "search"
    serverFilter?: ServerFilter
    /** Register the interactive read_<resource> tools (index.ts's exposeResources
     *  setting); default true. */
    exposeResources?: boolean
    /** Auto-open widget URLs (index.ts's uiAutoOpen setting); default true. */
    uiAutoOpen?: boolean
    /** Warn when the direct-exposure set grows large; settings kill-switch
     *  warnLargeDirectExposure disables it (default true, i.e. warn). */
    warnLargeDirectExposure?: boolean
    log: (level: "info" | "warn" | "error", message: string) => void
}

/** Register the declared tool surface + interactive-resource read_* tools + the
 *  list_changed re-registration handler. Called once at session_start; the
 *  list_changed hook re-syncs both surfaces on the same names. */
export function activateNativeMode(
    pi: ExtensionAPI,
    opts: NativeActivationOptions,
): { run(): void; dispose(): void } {
    const resourceRegistered = new Set<string>()
    let active = true
    const resourceOpts = {
        connection: opts.connection,
        serverFilter: opts.serverFilter,
        uiAutoOpen: opts.uiAutoOpen,
        log: opts.log,
    }
    const run = () => {
        if (!active) return
        void activateNativeTools(pi, {
            connection: opts.connection,
            serverFilter: opts.serverFilter,
            directSpec: opts.directSpec,
            warnLargeDirectExposure: opts.warnLargeDirectExposure,
            log: opts.log,
        }).catch((e) =>
            opts.log("warn", `native tool surface failed: ${e instanceof Error ? e.message : String(e)}`),
        )
        if (opts.exposeResources !== false) {
            void activateNativeResources(pi, resourceOpts, resourceRegistered).catch((e) =>
                opts.log("warn", `native resource surface failed: ${e instanceof Error ? e.message : String(e)}`),
            )
        }
    }
    // The FIRST run is the caller's to schedule: the grouping token only exists
    // after session_start (the connection refuses to connect without it), so a
    // factory-time run() would fail with "no grouping token set" — the startup
    // warnings this signature used to emit. index.ts calls run() at
    // session_start (post-setToken) and from its own onToolsChanged hook; we
    // deliberately do NOT setHooks here — a second setHooks call from the
    // wiring would replace ours wholesale (last-wins), so the re-sync lives in
    // ONE hook set, index.ts's.
    return {
        run,
        dispose() {
            active = false
        },
    }
}

// NOTE on codemode activation (verified live, pi 1.0.x): pi does NOT
// auto-activate the codemode tool for extension-registered codeme tools (only
// for connected MCP servers) — enable it with settings.json →
//   "defaultTools": ["+codemode"]
// (defaultTools applies at a FRESH session's creation; resumed sessions keep
// their recorded tool set). With codemode on, our registered tools are
// first-class script members under their bare names (`tools.mock_echo(...)`),
// searchTools() discovers them (embedding their TS declarations), and
// pi's codemode globals are text()/image()/console.log/return (no emit).
// Also verified: registerTool accepts exposure/namespace/annotations, and
// the peer-dep import resolves from the host's installed package tree.
export type { ExtensionContext }