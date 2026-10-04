// Tool search ranking over the combiner's (already prefixed) tool list.
//
// Lean reimplementation of pi-mcp-adapter's search-ranking.ts (MIT, © 2026 Nico Bailon)
// — same field-weight philosophy (name > server > description), phrase multipliers,
// and token-coverage floor, without that package's cross-file state. The combiner
// prefixes every tool `<server>_`, so server scoring rides the name field for free.

export type SearchableTool = {
    /** Prefixed name as the combiner serves it, e.g. "github_search_code". */
    name: string
    description?: string
}

export type RankedMatch = {
    tool: SearchableTool
    score: number
}

const FIELD_WEIGHTS = { name: 12, server: 8, description: 5 } as const
/** Exact/startsWith/contains multipliers over a field's weighted base. */
const PHRASE = { exact: 3, starts: 2, contains: 1.5 } as const
/** Shortest token allowed to stem-match (prefix-match) a longer query token. */
const MIN_STEM = 4

function tokenize(s: string): string[] {
    return s
        .toLowerCase()
        .split(/[^a-z0-9_]+/)
        .filter((t) => t.length > 0)
}

function phraseScore(haystack: string, needle: string): number {
    if (haystack === needle) return PHRASE.exact
    if (haystack.startsWith(needle)) return PHRASE.starts
    if (haystack.includes(needle)) return PHRASE.contains
    return 0
}

function tokenScore(haystackTokens: string[], queryToken: string): number {
    let best = 0
    for (const ht of haystackTokens) {
        if (ht === queryToken) best = Math.max(best, 1)
        else if (
            queryToken.length >= MIN_STEM &&
            ht.length >= MIN_STEM &&
            (ht.startsWith(queryToken) || queryToken.startsWith(ht))
        )
            best = Math.max(best, 0.7)
        else if (ht.includes(queryToken) && queryToken.length >= MIN_STEM) best = Math.max(best, 0.4)
    }
    return best
}

/** Rank tools against a query. Coverage floor: ≥60% of query tokens must match
 *  somewhere (100% for ≤2-token queries) — the adapter's anti-noise rule. */
export function rankTools(tools: SearchableTool[], query: string, limit: number, offset = 0): RankedMatch[] {
    const q = query.trim().toLowerCase()
    if (!q) return []
    const qTokens = tokenize(q)
    const matches: RankedMatch[] = []

    for (const tool of tools) {
        const name = tool.name.toLowerCase()
        const desc = (tool.description ?? "").toLowerCase()
        const server = name.split("_", 1)[0] ?? ""
        const nameTokens = tokenize(name)
        const descTokens = tokenize(desc)
        const needed = qTokens.length <= 2 ? qTokens.length : Math.ceil(qTokens.length * 0.6)

        let score = 0
        let covered = 0
        // Whole-query phrase pass (the strong signal).
        const phrase = Math.max(
            phraseScore(name, q) * FIELD_WEIGHTS.name,
            phraseScore(server, q) * FIELD_WEIGHTS.server,
            phraseScore(desc, q) * FIELD_WEIGHTS.description,
        )
        if (phrase > 0) {
            covered = qTokens.length
            score += phrase
        }
        // Per-token pass.
        for (const [i, qt] of qTokens.entries()) {
            const nameHit = tokenScore(nameTokens, qt)
            const serverHit = tokenScore([server], qt)
            const descHit = tokenScore(descTokens, qt)
            const best = Math.max(
                nameHit * FIELD_WEIGHTS.name,
                serverHit * FIELD_WEIGHTS.server,
                descHit * FIELD_WEIGHTS.description,
            )
            if (best > 0) {
                covered++
                score += best
                // First query token landing on the name is the classic intent signal.
                if (i === 0 && nameHit > 0) score += 8
            }
        }
        if (covered < needed) continue
        if (score <= 0) continue
        matches.push({ tool, score })
    }

    matches.sort((a, b) => b.score - a.score || a.tool.name.localeCompare(b.tool.name))
    return matches.slice(offset, offset + limit)
}

/** Simple substring fallback for regex-mode search (pattern pre-validated by caller). */
export function regexMatches(tools: SearchableTool[], pattern: RegExp, limit: number, offset = 0): RankedMatch[] {
    const out: RankedMatch[] = []
    for (const tool of tools) {
        if (pattern.test(tool.name) || pattern.test(tool.description ?? "")) out.push({ tool, score: 1 })
        pattern.lastIndex = 0
    }
    return out.slice(offset, offset + limit)
}

/** Compile a user regex with a conservative safety net: anchored parsing + a size
 *  ceiling. Returns undefined (with reason) for anything that smells exponential. */
export function compileSafeRegex(pattern: string): { re?: RegExp; error?: string } {
    if (pattern.length > 200) return { error: "pattern too long" }
    try {
        const re = new RegExp(pattern, "i")
        // Reject obvious nested quantifiers (a+)+ style catastrophes.
        if (/\(([^)]*[+*][^)]*)\)[+*{]/.test(pattern)) return { error: "nested quantifiers not allowed" }
        return { re }
    } catch (e) {
        return { error: `invalid regex: ${e instanceof Error ? e.message : String(e)}` }
    }
}
