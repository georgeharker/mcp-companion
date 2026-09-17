// Pi-specific settings for the mcp-combiner extension's client half.
//
// Standard file: `$PI_CODING_AGENT_DIR/extensions/mcp-combiner.json` — one place for
// the knobs that are about THIS extension in Pi, deliberately separate from the shared
// MCP config ladder (`.pi/mcp.json` & co. hold connection + per-project exposure; see
// config-ladder.ts). Read-only at extension load; changes take effect on /reload.
//
// Precedence: PI_MCP_COMBINER_* env > this file > built-in defaults.

import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

export type AdapterMode = boolean | "auto"

export type FooterStatus = "full" | "compact" | "off"

export type CombinerSettings = {
    /** Tool name registered with Pi. Default "mcp" (parity with pi-mcp-adapter);
     *  rename (e.g. "combiner") to coexist while both are installed. */
    toolName: string
    /** "lazy" (default): connect on first mcp() call. "eager": connect at session_start. */
    lazy: "lazy" | "eager"
    /** Client-half gate. `true`: we are the MCP tool. `false`: legacy mode — the
     *  process + instructions halves only, pi-mcp-adapter (if installed) owns MCP.
     *  `"auto"` (default): OFF when pi-mcp-adapter is detected installed, ON otherwise —
     *  so existing adapter users upgrade with zero behaviour change, while fresh
     *  installs get the one-package experience. Env PI_MCP_COMBINER_ADAPTER wins. */
    adapter: AdapterMode
    /** Expose combiner resources as read_* tools. */
    exposeResources: boolean
    /** Register combiner prompts as slash commands (adapter naming:
     *  mcp__<server>__<name>; prefixed with our toolName when renamed). */
    prompts: boolean
    /** Footer status line: "full" = "N servers enabled (M ready) · T tools", "compact" = "MCP M/N",
     *  "off" = none. Same values as pi-mcp-adapter's mcpFooterStatus. */
    mcpFooterStatus: FooterStatus
    /** The ctx.ui.setStatus key the footer publishes under. Default "mcp" — the
     *  slot pi-mcp-adapter wrote and footers (oh-my-posh's PI_STATUS aggregate)
     *  already surface; rename only if something else claims it. */
    mcpFooterKey: string
    /** Register the trusted batching tool (<toolName>Script). Default true. */
    scriptMode: boolean
    /** Auto-open interactive widget URLs (Stage 2 holds + resource reads).
     *  Default true. */
    uiAutoOpen: boolean
    /** Explicit combiner base URL (e.g. "http://127.0.0.1:9741/mcp"). Env wins. */
    url?: string
    /** Notify (vs stderr-only) for connection lifecycle messages. */
    notify: boolean
}

export const DEFAULT_SETTINGS: CombinerSettings = {
    toolName: "mcp",
    lazy: "lazy",
    adapter: "auto",
    exposeResources: true,
    prompts: true,
    mcpFooterStatus: "full",
    mcpFooterKey: "mcp",
    scriptMode: true,
    uiAutoOpen: true,
    notify: true,
}

/** Pi's agent dir: `$PI_CODING_AGENT_DIR` when set, else the first existing of the
 *  known defaults (~/.config/pi/agent on newer layouts, ~/.pi/agent on older). */
export function agentDir(): string {
    const env = process.env.PI_CODING_AGENT_DIR
    if (env && env.trim()) return env
    const candidates = [join(homedir(), ".config", "pi", "agent"), join(homedir(), ".pi", "agent")]
    for (const c of candidates) if (existsSync(c)) return c
    return candidates[0]
}

/** The settings file path this extension reads. */
export function settingsPath(): string {
    return join(agentDir(), "extensions", "mcp-combiner.json")
}

/** Load + validate the settings file; missing file or bad shape → defaults.
 *  Unknown keys are ignored (forward compat). */
export function loadSettings(path = settingsPath()): CombinerSettings {
    const out: CombinerSettings = { ...DEFAULT_SETTINGS }
    if (!existsSync(path)) return out
    let doc: unknown
    try {
        doc = JSON.parse(readFileSync(path, "utf8"))
    } catch {
        return out // malformed JSON → defaults; noisy parse errors help nobody at load time
    }
    if (typeof doc !== "object" || doc === null || Array.isArray(doc)) return out
    const d = doc as Record<string, unknown>

    if (typeof d.toolName === "string" && /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(d.toolName)) out.toolName = d.toolName
    if (d.lazy === "lazy" || d.lazy === "eager") out.lazy = d.lazy
    if (d.adapter === "auto" || typeof d.adapter === "boolean") out.adapter = d.adapter
    if (typeof d.exposeResources === "boolean") out.exposeResources = d.exposeResources
    if (typeof d.prompts === "boolean") out.prompts = d.prompts
    if (d.mcpFooterStatus === "full" || d.mcpFooterStatus === "compact" || d.mcpFooterStatus === "off") {
        out.mcpFooterStatus = d.mcpFooterStatus
    }
    if (typeof d.mcpFooterKey === "string" && d.mcpFooterKey.trim()) out.mcpFooterKey = d.mcpFooterKey.trim()
    if (typeof d.uiAutoOpen === "boolean") out.uiAutoOpen = d.uiAutoOpen
    if (typeof d.scriptMode === "boolean") out.scriptMode = d.scriptMode
    if (typeof d.url === "string" && d.url.trim()) out.url = d.url.trim()
    if (typeof d.notify === "boolean") out.notify = d.notify
    return out
}
