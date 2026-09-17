// Elicitation bridge: MCP server→client elicitation → Pi's ctx.ui dialogs.
//
// The structured dialogs (select/confirm/input) are the right model: each mode
// (interactive, RPC, print) provides its own implementation, so a gate question
// renders as a native TUI dialog locally AND extends over the wire as
// extension_ui_request frames in RPC mode — remote clients (e.g. un-bien's
// paired app) render them natively. Custom TUI overlays would be interactive-
// mode-only and would not reach remote clients; pi-ask's flow is a separate
// protocol we deliberately don't couple to.
//
// Load-bearing for the combiner's permissions gate, which elicits
// (Allow once / Allow for session / Deny) for tools whose policy resolves to elicit;
// its secure fallback is elicitUnavailable: deny. So when no dialog-capable UI exists
// we return "decline" rather than hanging — the combiner then denies the call.
//
// Simplified from pi-mcp-adapter's elicitation-handler.ts (MIT, © 2026 Nico Bailon):
// form schemas map to sequential per-property dialogs (enum→select, boolean→confirm,
// string/number→input); there is no URL-elicitation mode (no OAuth here).
// Options are returned VERBATIM — the combiner string-matches the gate choice.

import type { ExtensionUIContext } from "../pi.js"

export type ElicitUi = { hasUI: boolean; ui?: ExtensionUIContext }

export type ElicitRequest = {
    message?: string
    requestedSchema?: unknown
}

export type ElicitResponse =
    { action: "accept"; content: Record<string, ElicitValue> } | { action: "decline" | "cancel" }

const REFUSE: ElicitResponse = { action: "decline" }

/** Primitive value an elicitation form can collect (the SDK's accepted value set,
 *  incl. string[] for array-typed form fields). */
export type ElicitValue = string | number | boolean | string[]

function isRecord(v: unknown): v is Record<string, unknown> {
    return typeof v === "object" && v !== null && !Array.isArray(v)
}

/** Coerce raw dialog input to the declared primitive; undefined when it doesn't fit. */
function coerce(raw: string, type: string | undefined): ElicitValue | undefined {
    if (type === "number" || type === "integer") {
        const n = Number(raw)
        if (!Number.isFinite(n)) return undefined
        return type === "integer" ? Math.trunc(n) : n
    }
    return raw
}

/** Present one property's dialog; returns the value or undefined (cancelled/invalid). */
async function askProperty(
    ui: ExtensionUIContext,
    label: string,
    prop: Record<string, unknown>,
): Promise<ElicitValue | undefined> {
    const type = typeof prop.type === "string" ? prop.type : "string"
    const desc = typeof prop.description === "string" && prop.description ? ` — ${prop.description}` : ""

    if (Array.isArray(prop.enum) && prop.enum.every((v) => typeof v === "string")) {
        const options = prop.enum as string[]
        const picked = await ui.select?.(`${label}${desc}`, options)
        return picked
    }
    if (type === "boolean") {
        return await ui.confirm?.(label, `${prop.description ?? "Allow this request?"}`, undefined as never)
    }
    if (type === "array") {
        // Best-effort arrays: comma-separated input (v1 — no repeat-prompt loop yet).
        const raw = await ui.input?.(`${label}${desc}`, "a,b,c (comma-separated)")
        if (raw === undefined) return undefined
        const items = raw
            .split(",")
            .map((s) => s.trim())
            .filter((s) => s.length > 0)
        return items
    }
    const raw = await ui.input?.(`${label}${desc}`, type === "string" ? "string" : type)
    if (raw === undefined) return undefined
    const value = coerce(raw, type)
    return value
}

/** Handle an elicitation request against Pi's UI. Never throws — a UI that can't
 *  answer declines, and the combiner applies its elicitUnavailable policy. */
export async function handleElicitation(req: ElicitRequest, elicitUi: ElicitUi): Promise<ElicitResponse> {
    if (!elicitUi.hasUI || !elicitUi.ui?.select || !elicitUi.ui.input) return REFUSE

    const schema = isRecord(req.requestedSchema) ? req.requestedSchema : undefined
    const props = schema && isRecord(schema.properties) ? schema.properties : undefined
    const header = typeof req.message === "string" && req.message ? req.message : "MCP server request"

    // No schema → boolean-shaped consent question (covers plain allow/deny elicits).
    if (!props || Object.keys(props).length === 0) {
        const ok = await elicitUi.ui.confirm?.(header, "Allow?", undefined as never)
        if (ok === undefined) return REFUSE
        return ok ? { action: "accept", content: {} } : REFUSE
    }

    // Sequential per-property dialogs — each element rides the mode-implemented
    // UI surface, so multi-property forms work over the wire too. A single-
    // property form (the consent-gate shape) asks under the bare message; only
    // multi-property forms prefix the property name.
    const entries = Object.entries(props).filter(([, v]) => isRecord(v))
    const content: Record<string, ElicitValue> = {}
    for (const [name, raw] of entries) {
        const label = entries.length === 1 ? header : `${header}: ${name}`
        const value = await askProperty(elicitUi.ui, label, raw as Record<string, unknown>)
        if (value === undefined) return REFUSE
        content[name] = value
    }
    return { action: "accept", content }
}
