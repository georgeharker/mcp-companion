// Ported conformance cases: pi-mcp-adapter __tests__/search-ranking.test.ts (MIT,
// © 2026 Nico Bailon), adapted from their single-tool scoreToolMatch API to our
// batch rankTools. Cases for their searchKeywords feature are not ported — we
// deliberately don't implement keyword maps (the combiner owns server-side
// toolFilter; per-server keyword tuning was out of scope).

import { describe, expect, it } from "vitest"
import { rankTools, type SearchableTool } from "../src/client/ranking.js"

const tool = (name: string, description: string): SearchableTool => ({ name, description })
const names = (tools: SearchableTool[], query: string, limit = 10, offset = 0) =>
    rankTools(tools, query, limit, offset).map((m) => m.tool.name)

describe("search ranking (ported semantics)", () => {
    const tools = [tool("search_records", "Find records"), tool("find_records", "Search records")]

    it("ranks an exact name above a description match", () => {
        const ranked = names(tools, "search")
        expect(ranked[0]).toBe("search_records")
        expect(ranked).toContain("find_records")
    })

    it("drops partial two-token matches", () => {
        expect(names(tools, "search missing")).toEqual([])
    })

    it("ignores single-letter possessive tokens instead of stem-matching them", () => {
        // "project's" tokenizes to ["project", "s"]; a bare "s" must not match "simulator".
        const possessive = [tool("sync_icon", "Add an icon to your project's icons file.")]
        expect(names(possessive, "simulator")).toEqual([])
        // Real stems still match: "sync" (4+ chars) may prefix-match "synchronize".
        expect(names([tool("sync_icon", "Sync an icon.")], "synchronize")).toContain("sync_icon")
    })

    it("honors the coverage floor for short queries", () => {
        // ≤2-token queries need 100% coverage; "search completely-unrelated" only
        // covers one token against these tools.
        expect(names(tools, "search completely-unrelated")).toEqual([])
    })

    it("paginates: offset beyond the result set is empty", () => {
        expect(rankTools(tools, "search", 10, 5)).toEqual([])
        const first = rankTools(tools, "search", 1)
        const second = rankTools(tools, "search", 1, 1)
        expect(first[0].tool.name).toBe("search_records")
        expect(second[0].tool.name).toBe("find_records")
    })
})
