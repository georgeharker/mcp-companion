// Conformance for the elicitation bridge router — the combiner's consent gate
// rides this, so the enum round-trip (verbatim option strings) is load-bearing.
import { describe, expect, it } from "vitest"
import { handleElicitation, type ElicitUi } from "../src/client/elicitation.js"

type FakeUi = {
    select: (t: string, o: string[]) => Promise<string | undefined>
    confirm: () => Promise<boolean | undefined>
    input: (t: string) => Promise<string | undefined>
}

function fakeUi(overrides: Partial<FakeUi>): ElicitUi {
    // The bridge's capability guard requires select AND input to exist; defaults
    // here cancel/fail loudly if a test mis-routes.
    const base: FakeUi = {
        select: async () => undefined,
        confirm: async () => false,
        input: async () => undefined,
        ...overrides,
    }
    return { hasUI: true, ui: base as unknown as ElicitUi["ui"] }
}

const GATE_SCHEMA = {
    type: "object",
    properties: { value: { type: "string", enum: ["Allow once", "Allow for session", "Deny"] } },
    required: ["value"],
}

describe("handleElicitation", () => {
    it("routes the single-enum gate shape to select with the bare message title and verbatim options", async () => {
        const calls: Array<{ title: string; options: string[] }> = []
        const ui = fakeUi({
            select: async (title: string, options: string[]) => {
                calls.push({ title, options })
                return "Allow for session"
            },
        })
        const result = await handleElicitation(
            { message: "Allow 'github' to run tool 'create_issue'?", requestedSchema: GATE_SCHEMA },
            ui,
        )
        expect(calls).toHaveLength(1)
        expect(calls[0]!.title).toBe("Allow 'github' to run tool 'create_issue'?")
        expect(calls[0]!.options).toEqual(["Allow once", "Allow for session", "Deny"])
        expect(result).toEqual({ action: "accept", content: { value: "Allow for session" } })
    })

    it("declines when select is cancelled", async () => {
        const ui = fakeUi({ select: async () => undefined })
        const result = await handleElicitation({ message: "gate", requestedSchema: GATE_SCHEMA }, ui)
        expect(result).toEqual({ action: "decline" })
    })

    it("prefixes the property name only in multi-property forms", async () => {
        const titles: string[] = []
        const ui = fakeUi({
            input: async (title: string) => {
                titles.push(title)
                return "x"
            },
        })
        const result = await handleElicitation(
            {
                message: "provide",
                requestedSchema: {
                    type: "object",
                    properties: { a: { type: "string" }, b: { type: "string" } },
                },
            },
            ui,
        )
        expect(titles).toEqual(["provide: a", "provide: b"])
        expect(result).toEqual({ action: "accept", content: { a: "x", b: "x" } })
    })

    it("no-schema elicits map to confirm: true accepts, false/cancel declines", async () => {
        const yes = await handleElicitation({ message: "Allow?" }, fakeUi({ confirm: async () => true }))
        expect(yes).toEqual({ action: "accept", content: {} })
        const no = await handleElicitation({ message: "Allow?" }, fakeUi({ confirm: async () => false }))
        expect(no).toEqual({ action: "decline" })
        const cancelled = await handleElicitation({ message: "Allow?" }, fakeUi({ confirm: async () => undefined }))
        expect(cancelled).toEqual({ action: "decline" })
    })

    it("coerces number/integer inputs and parses comma-separated arrays", async () => {
        const int = await handleElicitation(
            { message: "n", requestedSchema: { type: "object", properties: { n: { type: "integer" } } } },
            fakeUi({ input: async () => "42" }),
        )
        expect(int).toEqual({ action: "accept", content: { n: 42 } })
        const arr = await handleElicitation(
            { message: "list", requestedSchema: { type: "object", properties: { tags: { type: "array" } } } },
            fakeUi({ input: async () => "a, b,,c" }),
        )
        expect(arr).toEqual({ action: "accept", content: { tags: ["a", "b", "c"] } })
    })

    it("declines without a dialog-capable UI", async () => {
        const result = await handleElicitation({ message: "gate", requestedSchema: GATE_SCHEMA }, { hasUI: false })
        expect(result).toEqual({ action: "decline" })
    })
})
