// Ported conformance cases: pi-mcp-adapter __tests__/prompts.test.ts (MIT,
// © 2026 Nico Bailon). Our prompts.ts ports parsePromptArgs / resolvePromptArgs /
// formatPromptResult / sanitizePromptName near-verbatim — these cases run against
// our module with fixtures adapted to our PromptSummary shape.
//
// KNOWN DIVERGENCE (documented, not ported): their sanitizeServerPrefix hex-encodes
// spaces ("agent board" → "agent_20_board"); ours collapses to "agent_board" —
// lossy but the combiner's server names are config keys (no spaces in practice).

import { describe, expect, it } from "vitest"
import {
    formatPromptResult,
    parsePromptArgs,
    promptCommandName,
    resolvePromptArgs,
    sanitizePromptName,
} from "../src/client/prompts.js"
import type { PromptSummary } from "../src/client/connection.js"

function meta(overrides: Partial<PromptSummary> = {}): PromptSummary {
    return {
        name: "demo_brief",
        description: "Daily brief",
        arguments: [
            { name: "topic", required: true, description: "Topic" },
            { name: "date", required: false },
        ],
        ...overrides,
    }
}

describe("prompt command naming", () => {
    it("mirrors the mcp__<server>__<prompt> convention", () => {
        expect(promptCommandName("agent-board_plan", "mcp")).toBe("mcp__agent-board__plan")
    })

    it("splits the combiner's <server>_<prompt> combined name at the first underscore", () => {
        expect(promptCommandName("todoist_productivity-analysis", "mcp")).toBe("mcp__todoist__productivity-analysis")
    })

    it("honors a custom prefix (coexistence mode)", () => {
        expect(promptCommandName("demo_brief", "combiner")).toBe("combiner__demo__brief")
    })

    it("sanitizes prompt names with unusual characters", () => {
        expect(sanitizePromptName("weekly.report")).toBe("weekly_report")
        expect(sanitizePromptName("with spaces & symbols")).toBe("with_spaces_symbols")
        expect(sanitizePromptName("123-start")).toBe("_123-start")
        expect(sanitizePromptName("---")).toBe("prompt")
    })
})

describe("parsePromptArgs", () => {
    it("splits positional args on whitespace", () => {
        expect(parsePromptArgs("today weather")).toEqual({
            positional: ["today", "weather"],
            named: {},
        })
    })

    it("preserves double-quoted phrases", () => {
        expect(parsePromptArgs('"important tasks" today')).toEqual({
            positional: ["important tasks", "today"],
            named: {},
        })
    })

    it("recognizes key=value tokens as named args", () => {
        expect(parsePromptArgs("topic=demo date=today")).toEqual({
            positional: [],
            named: { topic: "demo", date: "today" },
        })
    })

    it("allows quoted values in key=value tokens", () => {
        expect(parsePromptArgs('topic="demo of the day" date=today')).toEqual({
            positional: [],
            named: { topic: "demo of the day", date: "today" },
        })
    })

    it("mixes positional and named", () => {
        expect(parsePromptArgs("today topic=demo")).toEqual({
            positional: ["today"],
            named: { topic: "demo" },
        })
    })
})

describe("resolvePromptArgs", () => {
    it("returns positional args mapped by declared order", () => {
        const result = resolvePromptArgs(meta(), { positional: ["ai", "today"], named: {} })
        expect(result.ok).toBe(true)
        if (result.ok) expect(result.args).toEqual({ topic: "ai", date: "today" })
    })

    it("prefers named args over positional when both are provided", () => {
        const result = resolvePromptArgs(meta(), {
            positional: ["fallback"],
            named: { topic: "ai" },
        })
        expect(result.ok).toBe(true)
        if (result.ok) expect(result.args).toEqual({ topic: "ai", date: "fallback" })
    })

    it("rejects missing required args with a usage hint", () => {
        const result = resolvePromptArgs(meta(), { positional: [], named: {} })
        expect(result.ok).toBe(false)
        if (!result.ok) expect(result.error).toContain("Missing required argument")
    })

    it("allows undeclared named args through for permissive server schemas", () => {
        const result = resolvePromptArgs(meta({ arguments: [] }), {
            positional: [],
            named: { extra: "value" },
        })
        expect(result.ok).toBe(true)
        if (result.ok) expect(result.args).toEqual({ extra: "value" })
    })
})

describe("formatPromptResult", () => {
    it("returns a single user message verbatim", () => {
        expect(formatPromptResult({ messages: [{ role: "user", content: { type: "text", text: "Hello" } }] })).toBe(
            "Hello",
        )
    })

    it("preserves role attribution for multi-turn prompts", () => {
        expect(
            formatPromptResult({
                messages: [
                    { role: "user", content: { type: "text", text: "Hi" } },
                    { role: "assistant", content: { type: "text", text: "Hello" } },
                ],
            }),
        ).toBe("[user] Hi\n\n[assistant] Hello")
    })

    it("summarizes embedded resource content", () => {
        expect(
            formatPromptResult({
                messages: [
                    { role: "user", content: { type: "resource", resource: { uri: "file:///x.txt", text: "body" } } },
                ],
            }),
        ).toBe("[resource file:///x.txt]\nbody")
    })

    it("images still produce a placeholder marker so the model sees the intent", () => {
        expect(
            formatPromptResult({
                messages: [{ role: "user", content: { type: "image", data: "...", mimeType: "image/png" } }],
            }),
        ).toBe("[image image/png (embedded)]")
    })
})
