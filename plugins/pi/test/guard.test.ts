// Ported/derived conformance cases: guard truncation + describe + ui-resource
// extraction, per pi-mcp-adapter's mcp-output-guard.test.ts / tool-metadata.test.ts
// / resource-tool tests (MIT, © 2026 Nico Bailon). Scoped to what we implement:
// the adapter's temp-file spillover, line-count budgeting, and details bounding
// are deliberately excluded (we cap at 16 KB inline text).

import { describe, expect, it } from "vitest"
import { renderResourceResult, renderToolResult, toolUiResourceUri } from "../src/client/render.js"
import { resourceNameToToolName } from "../src/client/resources.js"

describe("tool result guard (ported semantics, scoped)", () => {
    it("passes short text through verbatim", () => {
        expect(renderToolResult({ content: [{ type: "text", text: "ok" }] })).toBe("ok")
    })

    it("truncates oversized text with an explicit marker", () => {
        const big = "x".repeat(17 * 1024)
        const out = renderToolResult({ content: [{ type: "text", text: big }] })
        expect(out.length).toBeLessThan(big.length)
        expect(out).toContain("truncated")
    })

    it("empty content reports honestly", () => {
        expect(renderToolResult({ content: [] })).toBe("(empty result)")
        expect(renderToolResult({})).toBe("(empty result)")
    })

    it("images become placeholder markers", () => {
        expect(renderToolResult({ content: [{ type: "image", mimeType: "image/png" }] })).toBe("[image: image/png]")
    })

    it("resource results: text passes, blobs become size notes", () => {
        expect(renderResourceResult({ contents: [{ uri: "u", text: "hi" }] })).toBe("hi")
        expect(
            renderResourceResult({ contents: [{ uri: "u", blob: "aGVsbG8=", mimeType: "application/octet-stream" }] }),
        ).toContain(
            "~6 bytes", // (len*3/4 estimate — padding slack makes it 6, decoded is 5)
        )
    })
})

describe("ui resource extraction (ported from getToolUiResourceUri)", () => {
    it("reads the nested meta form", () => {
        expect(toolUiResourceUri({ _meta: { ui: { resourceUri: "ui://srv/w" } } })).toBe("ui://srv/w")
    })

    it("reads the flat key form", () => {
        expect(toolUiResourceUri({ _meta: { "ui/resourceUri": "ui://srv/w" } })).toBe("ui://srv/w")
    })

    it("rejects non-ui schemes and absent meta", () => {
        expect(toolUiResourceUri({ _meta: { "ui/resourceUri": "https://srv/w" } })).toBeUndefined()
        expect(toolUiResourceUri({})).toBeUndefined()
        expect(toolUiResourceUri(undefined)).toBeUndefined()
    })
})

describe("resource name sanitizer (verbatim port cases)", () => {
    it("collapses non-alphanumerics and trims underscores", () => {
        expect(resourceNameToToolName("My Fancy Doc!!")).toBe("my_fancy_doc")
        expect(resourceNameToToolName("--weird--name--")).toBe("weird_name")
    })

    it("prefixes digit-leading or empty results", () => {
        expect(resourceNameToToolName("4pi")).toBe("resource_4pi")
        expect(resourceNameToToolName("!!!")).toBe("resource")
    })
})

// NEW (not in the adapter's suite): structured-content fallback + small-JSON
// pretty-printing — compact-preserving readability improvements.
describe("structured-content fallback + JSON prettify (extension)", () => {
    it("falls back to structuredContent when content is empty (adapter parity)", () => {
        const out = renderToolResult({ structuredContent: { ok: true, count: 2 } })
        expect(out).toContain('"ok": true')
        expect(out).toContain('"count": 2')
    })

    it("pretty-prints small single-line JSON for readability", () => {
        const out = renderToolResult({ content: [{ type: "text", text: '{"a":1,"b":[2,3]}' }] })
        expect(out).toBe('{\n  "a": 1,\n  "b": [\n    2,\n    3\n  ]\n}')
    })

    it("keeps large JSON compact (token economy at scale)", () => {
        const big = JSON.stringify({ data: "y".repeat(3000) })
        const out = renderToolResult({ content: [{ type: "text", text: big }] })
        expect(out).toBe(big) // untouched — over the 2 KB pretty budget
    })

    it("leaves non-JSON text alone", () => {
        expect(renderToolResult({ content: [{ type: "text", text: "{not json" }] })).toBe("{not json")
    })

    it("resource contents pretty-print too", () => {
        expect(
            renderResourceResult({ contents: [{ uri: "u", mimeType: "application/json", text: '{"k":[1]}' }] }),
        ).toBe('{\n  "k": [\n    1\n  ]\n}')
    })
})
