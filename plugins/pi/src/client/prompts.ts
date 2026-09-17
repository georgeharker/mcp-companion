// MCP prompts → Pi slash commands, compatible with pi-mcp-adapter's conventions
// (MIT, © 2026 Nico Bailon) — argument parsing and resolution are ported verbatim
// from its prompts.ts; result formatting is adapted (our rendering),
// discovery/registration are ours.
//
// The combiner namespaces prompts `<server>_<name>` (same convention as tools); we
// register commands as `<prefix>__<server>__<sanitized>` where <prefix> is `mcp` in
// parity mode or our renamed toolName in coexistence mode — the adapter's
// formatPromptCommandName shape (`mcp__<server>__<name>`).
//
// Argument handling (positional + `name=value`, bash-style quoting, named-wins,
// undeclared named preserved, usage message on missing required) and result
// formatting (`[role]` markers, resource/image inline markers, single-user-message
// passthrough) are ported from pi-mcp-adapter's prompts.ts (MIT, © 2026 Nico Bailon).

import type { ExtensionAPI } from "../pi.js"
import type { CombinerConnection, PromptSummary } from "./connection.js"

// ── naming ──

/** Adapter-compatible prompt-name sanitizer: collapse invalid runs to `_`, trim,
 *  `prompt` when empty, leading `_` when it starts with a digit. */
export function sanitizePromptName(name: string): string {
    const cleaned = name.replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^[_-]+|[_-]+$/g, "")
    if (!cleaned) return "prompt"
    return /^[0-9]/.test(cleaned) ? `_${cleaned}` : cleaned
}

/** Command name for a combiner prompt (`<server>_<name>`), in adapter shape. */
export function promptCommandName(prefixedPromptName: string, prefix: string): string {
    const server = prefixedPromptName.split("_", 1)[0] ?? "server"
    const rest = prefixedPromptName.slice(server.length + 1) || prefixedPromptName
    return `${prefix}__${sanitizePromptName(server)}__${sanitizePromptName(rest)}`
}

// ── argument parsing (ported near-verbatim) ──

export function parsePromptArgs(input: string): { positional: string[]; named: Record<string, string> } {
    const positional: string[] = []
    const named: Record<string, string> = {}
    for (const token of tokenizeArgs(input)) {
        const eq = findUnquotedEquals(token)
        if (eq > 0) {
            const key = token.slice(0, eq).trim()
            const value = stripQuotes(token.slice(eq + 1).trim())
            if (key) {
                named[key] = value
                continue
            }
        }
        positional.push(stripQuotes(token))
    }
    return { positional, named }
}

function tokenizeArgs(input: string): string[] {
    const tokens: string[] = []
    let current = ""
    let quote: '"' | "'" | null = null
    let escaped = false
    for (const char of input) {
        if (escaped) {
            current += char
            escaped = false
            continue
        }
        if (char === "\\" && quote !== "'") {
            escaped = true
            continue
        }
        if (quote) {
            current += char
            if (char === quote) quote = null
            continue
        }
        if (char === '"' || char === "'") {
            quote = char
            current += char
            continue
        }
        if (/\s/.test(char)) {
            if (current.length > 0) {
                tokens.push(current)
                current = ""
            }
            continue
        }
        current += char
    }
    if (current.length > 0) tokens.push(current)
    return tokens
}

function findUnquotedEquals(token: string): number {
    let quote: '"' | "'" | null = null
    for (let i = 0; i < token.length; i++) {
        const ch = token[i]
        if (quote) {
            if (ch === quote) quote = null
            continue
        }
        if (ch === '"' || ch === "'") quote = ch
        else if (ch === "=") return i
    }
    return -1
}

function stripQuotes(value: string): string {
    if (value.length >= 2 && (value.startsWith('"') || value.startsWith("'")) && value.endsWith(value.charAt(0))) {
        return value.slice(1, -1)
    }
    return value
}

// ── argument resolution (ported near-verbatim) ──

export type ResolvedPromptArgs = { ok: true; args: Record<string, string> } | { ok: false; error: string }

export function resolvePromptArgs(
    prompt: PromptSummary,
    parsed: { positional: string[]; named: Record<string, string> },
): ResolvedPromptArgs {
    const args: Record<string, string> = {}
    const declared = prompt.arguments ?? []
    let positionalIndex = 0
    for (const argDef of declared) {
        const value = parsed.named[argDef.name] ?? parsed.positional[positionalIndex++]
        if (value !== undefined && value !== "") {
            args[argDef.name] = value
        }
    }
    // Preserve named arguments the prompt did not declare (MCP allows arbitrary
    // string key/values in prompts/get params.arguments).
    for (const [key, value] of Object.entries(parsed.named)) {
        if (!(key in args)) args[key] = value
    }
    const missing = declared.filter((a) => a.required && !args[a.name])
    if (missing.length > 0) {
        const usage = declared.map((a) => (a.required ? `<${a.name}>` : `[${a.name}]`)).join(" ")
        const missingList = missing.map((a) => a.name).join(", ")
        return {
            ok: false,
            error: `Missing required argument${missing.length > 1 ? "s" : ""}: ${missingList}. Usage: <args> ${usage}`.trim(),
        }
    }
    return { ok: true, args }
}

// ── result formatting (ported near-verbatim) ──

type PromptResult = { messages?: Array<{ role?: string; content?: Record<string, unknown> }> }

/** Flatten a prompts/get result into one string for sendUserMessage: role markers
 *  when multi-message, bare text for a single user message; non-text content
 *  becomes inline `[resource uri]` / `[image mime]` / `[audio mime]` markers. */
export function formatPromptResult(result: unknown): string {
    const messages = (result as PromptResult)?.messages ?? []
    const lines: string[] = []
    for (const message of messages) {
        const text = extractMessageText(message?.content)
        if (!text) continue
        if (message?.role === "user" && messages.length === 1) {
            lines.push(text)
        } else {
            lines.push(`[${message?.role ?? "unknown"}] ${text}`)
        }
    }
    return lines.join("\n\n").trim()
}

function extractMessageText(content: Record<string, unknown> | undefined): string {
    if (!content || typeof content !== "object") return ""
    switch (content.type) {
        case "text":
            return typeof content.text === "string" ? content.text : ""
        case "resource": {
            const resource = content.resource as Record<string, unknown> | undefined
            if (!resource) return ""
            if (typeof resource.text === "string") return `[resource ${resource.uri}]\n${resource.text}`
            return `[resource ${resource.uri}]`
        }
        case "resource_link":
            return `[resource_link ${content.uri ?? ""}${content.name ? ` — ${String(content.name)}` : ""}]`
        case "image":
            return `[image ${content.mimeType ?? "unknown"}${content.data ? " (embedded)" : ""}]`
        case "audio":
            return `[audio ${content.mimeType ?? "unknown"}]`
        default:
            return ""
    }
}

// ── command surface sync ──

/** Discover prompts and register one command each. Idempotent: prompts already
 *  registered are left alone; new ones are added. Returns the count seen. */
export async function syncPromptCommands(
    pi: ExtensionAPI,
    connection: CombinerConnection,
    prefix: string,
    registered: Set<string>,
    notify: (message: string, level: "info" | "warn" | "error") => void,
): Promise<number> {
    let prompts: PromptSummary[] = []
    try {
        prompts = await connection.listPrompts()
    } catch (e) {
        notify(`prompt discovery failed (${e instanceof Error ? e.message : String(e)})`, "warn")
        return 0
    }
    for (const prompt of prompts) {
        const commandName = promptCommandName(prompt.name, prefix)
        if (registered.has(commandName)) continue
        registered.add(commandName)
        pi.registerCommand(commandName, {
            description: buildDescription(prompt),
            handler: (args, ctx) => {
                void (async () => {
                    const parsed = parsePromptArgs(args ?? "")
                    const resolved = resolvePromptArgs(prompt, parsed)
                    if (!resolved.ok) {
                        ctx.ui?.notify?.(resolved.error, "error")
                        return
                    }
                    try {
                        const result = await connection.getPrompt(prompt.name, resolved.args)
                        const text = formatPromptResult(result)
                        if (!text) {
                            ctx.ui?.notify?.(`mcp-combiner: prompt "${prompt.name}" returned no text content`, "warn")
                            return
                        }
                        pi.sendUserMessage(text)
                    } catch (e) {
                        ctx.ui?.notify?.(
                            `mcp-combiner: prompt "${prompt.name}" failed (${e instanceof Error ? e.message : String(e)})`,
                            "error",
                        )
                    }
                })()
            },
        })
    }
    return prompts.length
}

function buildDescription(prompt: PromptSummary): string {
    const declared = prompt.arguments ?? []
    const usage = declared.map((a) => (a.required ? `<${a.name}>` : `[${a.name}]`)).join(" ")
    const desc = (prompt.description ?? prompt.title ?? "").split("\n", 1)[0]
    return `MCP prompt${desc ? `: ${desc}` : ""}${usage ? ` — args: ${usage}` : ""}`
}
