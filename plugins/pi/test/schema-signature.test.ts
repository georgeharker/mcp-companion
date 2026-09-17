// Ported conformance cases: pi-mcp-adapter __tests__/ts-shape.test.ts (MIT, © 2026
// Nico Bailon) — the core renderer is a verbatim lift of theirs, so the cases run
// against our module unchanged.
import { describe, expect, it } from "vitest"
import { renderSchemaSignature } from "../src/client/schema-signature.js"

describe("renderSchemaSignature (ported cases)", () => {
    it("renders required and optional object properties", () => {
        expect(
            renderSchemaSignature({
                type: "object",
                properties: { query: { type: "string" }, limit: { type: "integer" } },
                required: ["query"],
            }),
        ).toBe("{ query: string; limit?: number; }")
    })

    it("renders enum unions", () => {
        expect(renderSchemaSignature({ enum: ["fast", "safe", null] })).toBe('"fast" | "safe" | null')
    })

    it("renders nested objects and arrays", () => {
        expect(
            renderSchemaSignature({
                type: "object",
                properties: {
                    config: {
                        type: "object",
                        properties: { tags: { type: "array", items: { type: "string" } } },
                        required: ["tags"],
                    },
                },
                required: ["config"],
            }),
        ).toBe("{ config: { tags: string[]; }; }")
    })

    it("hoists local references as named definitions", () => {
        expect(
            renderSchemaSignature({
                type: "object",
                properties: { address: { $ref: "#/$defs/Address" } },
                required: ["address"],
                $defs: {
                    Address: {
                        type: "object",
                        properties: { city: { type: "string" } },
                        required: ["city"],
                    },
                },
            }),
        ).toBe("type Address = { city: string; };\n\n{ address: Address; }")
    })

    it("ignores unsupported unreferenced definitions", () => {
        expect(
            renderSchemaSignature({
                type: "object",
                properties: { query: { type: "string" } },
                required: ["query"],
                $defs: {
                    Unused: { if: { type: "string" }, then: { type: "number" } },
                },
            }),
        ).toBe("{ query: string; }")
    })
})

// NEW (not in the adapter's suite): annotated rendering — descriptions threaded
// as comments, defaults inline. The reason this renderer exists (signature density)
// without its flaw (dropping the field guidance the model needs at describe-time).
describe("renderSchemaSignature — annotated (extension)", () => {
    it("threads field descriptions as comments and switches to multi-line", () => {
        expect(
            renderSchemaSignature({
                type: "object",
                properties: {
                    query: { type: "string", description: "The search query" },
                    limit: { type: "integer", description: "Max results, default 30" },
                },
                required: ["query"],
            }),
        ).toBe(`{
  query: string; // The search query
  limit?: number; // Max results, default 30
}`)
    })

    it("shows defaults inline before the description", () => {
        expect(
            renderSchemaSignature({
                type: "object",
                properties: {
                    sort: { type: "string", enum: ["created", "updated"], default: "created", description: "Sort key" },
                },
                required: [],
            }),
        ).toBe(`{
  sort?: "created" | "updated"; // default "created" — Sort key
}`)
    })

    it("stays single-line when nothing needs annotating (conformance)", () => {
        expect(
            renderSchemaSignature({
                type: "object",
                properties: { query: { type: "string" } },
                required: ["query"],
            }),
        ).toBe("{ query: string; }")
    })
})
