// Ported/derived conformance cases: guard truncation + describe + ui-resource
// extraction, per pi-mcp-adapter's mcp-output-guard.test.ts / tool-metadata.test.ts
// / resource-tool tests (MIT, © 2026 Nico Bailon). The adapter's temp-file
// spillover is now ADOPTED (not excluded): oversized results spill the full text
// to a 0600 temp file and the notice names it; images survive as real blocks on
// the blocks path.

import { access, readFile, stat } from "node:fs/promises"
import { constants } from "node:fs"

import { describe, expect, it } from "vitest"
import {
    guardedText,
    renderResourceResult,
    renderToolResult,
    renderToolResultBlocks,
    toolUiResourceUri,
} from "../src/client/render.js"
import { resourceNameToToolName } from "../src/client/resource-naming.js"

const TIGHT = { maxResultChars: 1024 }
const IMAGE_MIME = "image/png"
const IMAGE_DATA = "iVBORw0KGgoAAAANSUhEUg=="

describe("tool result guard (ported semantics, scoped)", () => {
    it("passes short text through verbatim", async () => {
        expect(await renderToolResult({ content: [{ type: "text", text: "ok" }] })).toBe("ok")
    })

    it("truncates oversized text with an explicit marker + spill file", async () => {
        const big = "x".repeat(3 * 1024)
        const out = await renderToolResult({ content: [{ type: "text", text: big }] }, TIGHT)
        expect(out.length).toBeLessThan(big.length)
        expect(out).toContain("Truncated")
        expect(out).toContain("Full content:")
        // The spill path is real, readable, 0600, and holds the FULL text.
        const path = /Full content: (\S+)/.exec(out)?.[1]
        expect(path).toBeDefined()
        const spilled = await readFile(path!, "utf8")
        expect(spilled).toBe(big)
        const mode = (await stat(path!)).mode & 0o777
        expect(mode).toBe(0o600)
    })

    it("under the cap there is no spill path", async () => {
        const { spillPath } = await guardedText("small", TIGHT, "t")
        expect(spillPath).toBeUndefined()
    })

    it("cut is surrogate-safe (no lone high surrogate at the head end)", async () => {
        // "😀" is a surrogate pair: 0x1F600 → 0xD83D 0xDE00. Cut exactly at the
        // boundary must not leave the high surrogate trailing in the head.
        const text = "a".repeat(TIGHT.maxResultChars - 1) + "😀"
        const head = text.slice(0, TIGHT.maxResultChars) // the raw cut WOULD split the pair
        expect(head.charCodeAt(head.length - 1)).toBe(0xd83d)
        const { text: out } = await guardedText(text, TIGHT, "t")
        const body = out.split("\n\n[")[0]
        expect(body.charCodeAt(body.length - 1)).toBe(0x61)
        expect(body).not.toContain(String.fromCharCode(0xd83d))
    })

    it("empty content reports honestly", async () => {
        expect(await renderToolResult({ content: [] })).toBe("(empty result)")
        expect(await renderToolResult({})).toBe("(empty result)")
    })

    it("string API: image blocks stay placeholder markers", async () => {
        expect(await renderToolResult({ content: [{ type: "image", mimeType: IMAGE_MIME }] })).toBe(
            "[image: image/png]",
        )
    })

    it("blocks API: images stay REAL blocks (the model sees them)", async () => {
        const out = await renderToolResultBlocks({
            content: [
                { type: "text", text: "done" },
                { type: "image", data: IMAGE_DATA, mimeType: IMAGE_MIME },
            ],
        })
        expect(out.text).toBe("done")
        expect(out.content[0]).toEqual({ type: "text", text: "done" })
        expect(out.content[1]).toEqual({ type: "image", data: IMAGE_DATA, mimeType: IMAGE_MIME })
    })

    it("blocks API + non-vision model: images become omission notes", async () => {
        const out = await renderToolResultBlocks(
            { content: [{ type: "image", data: IMAGE_DATA, mimeType: IMAGE_MIME }] },
            { model: { input: ["text"] } },
        )
        expect(out.content[1]).toMatchObject({ type: "text", text: expect.stringContaining("does not support images") })
        expect(JSON.stringify(out.content)).not.toContain(IMAGE_DATA)
    })

    it("blocks API: image-only results get an (image result) text", async () => {
        const out = await renderToolResultBlocks({ content: [{ type: "image", data: IMAGE_DATA, mimeType: IMAGE_MIME }] })
        expect(out.text).toBe("(image result)")
        expect(out.content).toHaveLength(2)
    })

    it("resource results: text passes, blobs become size notes", async () => {
        expect(await renderResourceResult({ contents: [{ uri: "u", text: "hi" }] })).toBe("hi")
        expect(
            await renderResourceResult({ contents: [{ uri: "u", blob: "aGVsbG8=", mimeType: "application/octet-stream" }] }),
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
    it("falls back to structuredContent when content is empty (adapter parity)", async () => {
        const out = await renderToolResult({ structuredContent: { ok: true, count: 2 } })
        expect(out).toContain('"ok": true')
        expect(out).toContain('"count": 2')
    })

    it("pretty-prints small single-line JSON for readability", async () => {
        const out = await renderToolResult({ content: [{ type: "text", text: '{"a":1,"b":[2,3]}' }] })
        expect(out).toBe('{\n  "a": 1,\n  "b": [\n    2,\n    3\n  ]\n}')
    })

    it("keeps large JSON compact (token economy at scale)", async () => {
        const big = JSON.stringify({ data: "y".repeat(3000) })
        const out = await renderToolResult({ content: [{ type: "text", text: big }] })
        expect(out).toBe(big) // untouched — over the 2 KB pretty budget
    })

    it("leaves non-JSON text alone", async () => {
        expect(await renderToolResult({ content: [{ type: "text", text: "{not json" }] })).toBe("{not json")
    })

    it("resource contents pretty-print too", async () => {
        expect(
            await renderResourceResult({ contents: [{ uri: "u", mimeType: "application/json", text: '{"k":[1]}' }] }),
        ).toBe('{\n  "k": [\n    1\n  ]\n}')
    })

    it("oversized JSON spills as full text (never mid-JSON with no recovery)", async () => {
        const big = JSON.stringify({ data: "y".repeat(3 * 1024) })
        const out = await renderToolResult({ content: [{ type: "text", text: big }] }, TIGHT)
        expect(out).toContain("Truncated")
        const path = /Full content: (\S+)/.exec(out)?.[1]
        expect(path).toBeDefined()
        await expect(access(path!, constants.F_OK)).resolves.toBeUndefined()
        expect(await readFile(path!, "utf8")).toBe(big)
    })
})