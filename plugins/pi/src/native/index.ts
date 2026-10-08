// NATIVE MODE : activation entry for the
// pi-mcp-based combiner connection + native tool surface. This file exists so the
// wire-in stays a few lines in src/index.ts:
//
//   const conn = new NativeCombinerConnection(…, clientLog)   // implements CombinerConnection
//   const nativeSurface = activateNativeMode(pi, { connection: conn, … })
//   … nativeSurface.run() at session_start (post-setToken) and on list_changed
//
// The FIRST run is deferred by design (see activateNativeMode): the grouping token
// only exists after session_start. Configuration (directTools spec, server filter)
// rides as GETTERS so every pass resolves the CURRENT session's values — worktree
// and project switches re-resolve session config, and a load-time snapshot would
// apply the wrong allow/deny set to later sessions (review finding #5).

import type { ExtensionAPI, ExtensionContext } from "../pi.js"
import type { ServerFilter } from "../client/config-ladder.js"
import { NativeCombinerConnection } from "./combiner-connection.js"
import { activateNativeTools, type NativeToolSurfaceState } from "./tool-surface.js"
import { activateNativeResources } from "./resources.js"

export { NativeCombinerConnection } from "./combiner-connection.js"
export { activateNativeTools, type NativeToolSurfaceOptions, type NativeToolSurfaceState } from "./tool-surface.js"
export { tokenedUrl } from "./combiner-connection.js"
export { activateNativeResources, filterInteractiveResources, type NativeResourceOptions } from "./resources.js"

export type NativeActivationOptions = {
    connection: NativeCombinerConnection
    /** Per-project directTools spec, resolved PER PASS (not a load-time snapshot). */
    directSpec?: () => string[] | "search" | undefined
    /** Per-project server filter, resolved PER PASS (same reason). */
    serverFilter?: () => ServerFilter | undefined
    /** Register the interactive read_<resource> tools (index.ts's exposeResources
     *  setting); default true. */
    exposeResources?: boolean
    /** Auto-open widget URLs (index.ts's uiAutoOpen setting); default true. */
    uiAutoOpen?: boolean
    /** Warn when the direct-exposure set grows large; settings kill-switch
     *  warnLargeDirectExposure disables it (default true, i.e. warn). */
    warnLargeDirectExposure?: boolean
    /** Cap on guarded tool-result text before spill-to-file (maxResultChars setting);
     *  forwarded to the tool surface and the interactive read_* tools. */
    maxResultChars?: number
    log: (level: "info" | "warn" | "error", message: string) => void
}

/** Register the declared tool surface + interactive-resource read_* tools + the
 *  list_changed re-registration handler. Called once at session_start; the
 *  list_changed hook re-syncs both surfaces on the same names. One shared
 *  NativeToolSurfaceState diff across all passes — unchanged declarations are
 *  skipped, removed ones re-registered hidden (see activateNativeTools). */
export function activateNativeMode(
    pi: ExtensionAPI,
    opts: NativeActivationOptions,
): { run(): void; dispose(): void } {
    let active = true
    const toolState: NativeToolSurfaceState = { names: new Set(), signatures: new Map() }
    // Cross-pass memory for the read_* tools (skip re-registration of unchanged
    // names — pi.registerTool replaces-by-name with a warning).
    const resourceRegistered = new Set<string>()
    const run = () => {
        if (!active) return
        void activateNativeTools(pi, {
            connection: opts.connection,
            serverFilter: opts.serverFilter,
            directSpec: opts.directSpec,
            state: toolState,
            warnLargeDirectExposure: opts.warnLargeDirectExposure,
            maxResultChars: opts.maxResultChars,
            log: opts.log,
        }).catch((e) =>
            opts.log("warn", `native tool surface failed: ${e instanceof Error ? e.message : String(e)}`),
        )
        if (opts.exposeResources !== false) {
            void activateNativeResources(
                pi,
                {
                    connection: opts.connection,
                    serverFilter: opts.serverFilter,
                    directSpec: opts.directSpec,
                    uiAutoOpen: opts.uiAutoOpen,
                    maxResultChars: opts.maxResultChars,
                    log: opts.log,
                },
                resourceRegistered,
            ).catch((e) => opts.log("warn", `native resource surface failed: ${e instanceof Error ? e.message : String(e)}`))
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