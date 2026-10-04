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
): { dispose(): void } {
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
        void activateNativeResources(pi, resourceOpts, resourceRegistered).catch((e) =>
            opts.log("warn", `native resource surface failed: ${e instanceof Error ? e.message : String(e)}`),
        )
    }
    run()
    opts.connection.setHooks({
        onToolsChanged: run,
    })
    return {
        dispose() {
            active = false
        },
    }
}

// NOTE on codemode activation: pi auto-activates the codemode tool when an MCP server
// with codemode exposure connects. Extension-registered codemode tools may not trigger
// that activation (verify against pi 1.0.0); the documented fallback is
//   settings.json → "defaultTools": ["+codemode"]
// which keeps the story one-line even in the non-auto case.
// Verified live (pi 1.0.x): registerTool accepts exposure/namespace/annotations;
// the peer-dep import resolves from the host's installed package tree. Still
// unverified: codemode auto-activation for extension-registered tools — the
// documented fallback is settings.json → "defaultTools": ["+codemode"].
export type { ExtensionContext }