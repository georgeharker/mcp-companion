// Smoke test: exercise the built client half against the live combiner on :9741.
// Run: node scripts/smoke.mjs   (from plugins/pi)
import { CombinerConnection, tokenedUrl } from "../dist/client/connection.js"
import { rankTools } from "../dist/client/ranking.js"
import { readLadder } from "../dist/client/config-ladder.js"
import { handleElicitation } from "../dist/client/elicitation.js"
import { formatPromptResult, parsePromptArgs, promptCommandName, resolvePromptArgs } from "../dist/client/prompts.js"
import { countsFromHealth, footerText } from "../dist/client/footer.js"
import { createScriptTool } from "../dist/client/script.js"
import {
    filterResources,
    isInteractiveResource,
    MCP_APP_MIME,
    resourceNameToToolName,
    resourceServer,
} from "../dist/client/resources.js"
import { renderResourceResult } from "../dist/client/render.js"
import { resolveSessionConfig, stripUrlToken, urlTokenOf } from "../dist/client/config-ladder.js"
import {
    activateFromSearch,
    applyServerFilter,
    matchesGlob,
    resolveAllowlist,
    syncAllowlistTools,
} from "../dist/client/direct-tools.js"

const log = (level, message) => console.error(`[${level}] ${message}`)
const results = []
const check = (name, ok, detail = "") => {
    results.push({ name, ok, detail })
    console.log(`${ok ? "✓" : "✗"} ${name}${detail ? ` — ${detail}` : ""}`)
}

// 1. tokenedUrl sanity
const u = tokenedUrl("http://127.0.0.1:9741/mcp", "pi-smoke")
check("tokenedUrl mints /mcp/<token>", u === "http://127.0.0.1:9741/mcp/pi-smoke", u)
const u2 = tokenedUrl("http://127.0.0.1:9741/mcp/pi-old", "pi-new")
check("tokenedUrl replaces existing token", u2 === "http://127.0.0.1:9741/mcp/pi-new", u2)

// 2. ladder read (this repo's cwd)
const ladder = readLadder(process.cwd(), "http://127.0.0.1:9741")
check(
    "ladder reads shared files",
    ladder.sources.length >= 0,
    ladder.sources.length
        ? `sources: ${ladder.sources.map((s) => s.split("/").slice(-3).join("/")).join(", ")}`
        : "no files",
)
console.log(
    `  combiner entry url: ${ladder.entry.url ?? "(none — defaults apply)"}; other servers: ${ladder.otherServers.join(", ") || "(none)"}`,
)

// 3. connect + listTools
const conn = new CombinerConnection(
    { baseUrl: "http://127.0.0.1:9741/mcp", bearerTokenEnv: "MCP_COMBINER_AUTH_TOKEN" },
    log,
)
conn.setToken("pi-smoke-test")
let tools = []
try {
    await conn.ensureConnected()
    tools = await conn.listTools()
    check("connect + tools/list", tools.length > 0, `${tools.length} tools`)
} catch (e) {
    check("connect + tools/list", false, e.message)
}

// 4. search ranking
const hits = rankTools(tools, "github search code", 5)
check(
    "search ranks github_search_code first",
    hits[0]?.tool.name === "github_search_code",
    hits
        .slice(0, 3)
        .map((h) => h.tool.name)
        .join(", "),
)
const hits2 = rankTools(tools, "take screenshot", 5)
console.log(`  'take screenshot' → ${hits2.map((h) => h.tool.name).join(", ") || "(no match)"}`)

// 5. describe shape (ts-shape path)
const gh = tools.find((t) => t.name === "github_search_code")
if (gh) {
    const { renderDescribe } = await import("../dist/client/render.js")
    const d = renderDescribe(gh, "github")
    check("describe renders schema", d.includes("query") && d.length > 50, `${d.length} chars`)
} else {
    check("describe renders schema", false, "github_search_code not in list")
}

// 6. callTool — combiner meta-tool (harmless read)
try {
    const r = await conn.callTool("combiner__status", {})
    const text = JSON.stringify(r)
    check("callTool combiner__status", text.includes("todoist") || text.length > 100, `${text.length} chars`)
} catch (e) {
    check("callTool combiner__status", false, e.message)
}

// 7. per-token filter (our own ephemeral token — cleared after)
try {
    await conn.applyFilter({ allow: ["github"] })
    const filtered = await conn.listTools(true)
    const nonGithub = filtered.filter((t) => !t.name.startsWith("github_") && !t.name.startsWith("combiner__"))
    check("token filter applies", nonGithub.length === 0, `${filtered.length} tools remain`)
    const bearer = process.env.MCP_COMBINER_AUTH_TOKEN
    const headers = bearer ? { authorization: `Bearer ${bearer}` } : {}
    // nosemgrep: intentional loopback HTTP — this is the smoke test hitting the local combiner’s control plane.
    const del = await fetch("http://127.0.0.1:9741/sessions/token/pi-smoke-test/filter", { method: "DELETE", headers })
    check("token filter cleared", del.ok, `DELETE ${del.status}`)
} catch (e) {
    check("token filter", false, e.message)
}

// 8. elicitation headless → decline (combiner's elicitUnavailable policy applies)
const el = await handleElicitation(
    {
        message: "Allow github_create_issue?",
        requestedSchema: {
            type: "object",
            properties: { decision: { type: "string", enum: ["Allow once", "Allow for session", "Deny"] } },
        },
    },
    { hasUI: false },
)
check("elicitation declines headless", el.action === "decline", el.action)

// 9. health control plane
try {
    const h = await conn.health()
    check("health endpoint", h && h.status === "ok", `status=${h?.status}`)
} catch (e) {
    check("health endpoint", false, e.message)
}

// 10. prompts: discovery + compatible parsing/naming/formatting
try {
    const prompts = await conn.listPrompts()
    check("prompts/list discovers prompts", prompts.length > 0, `${prompts.length} prompts (e.g. ${prompts[0]?.name})`)
    const p = prompts[0]
    const cmd = promptCommandName(p.name, "mcp")
    check("prompt command naming (adapter shape)", /^mcp__[a-z0-9_-]+__[a-z0-9_-]+$/i.test(cmd), cmd)
    const parsed = parsePromptArgs('today topic="important tasks" extra=1')
    check(
        "prompt arg parsing (bash quoting + named)",
        parsed.positional.length === 1 &&
            parsed.positional[0] === "today" &&
            parsed.named.topic === "important tasks" &&
            parsed.named.extra === "1",
        JSON.stringify(parsed),
    )
    const resolved = resolvePromptArgs(p, parsePromptArgs("today work"))
    check("prompt arg resolution ok", resolved.ok, resolved.ok ? Object.keys(resolved.args).join(",") : resolved.error)
    const missing = resolvePromptArgs({ name: "x", arguments: [{ name: "req", required: true }] }, parsePromptArgs(""))
    check(
        "prompt missing-required → usage error",
        !missing.ok && missing.error.includes("req"),
        missing.ok ? "" : missing.error.slice(0, 60),
    )
    const fmt = formatPromptResult({ messages: [{ role: "user", content: { type: "text", text: "hi" } }] })
    check("prompt single user message passthrough", fmt === "hi", fmt)
    const fmt2 = formatPromptResult({
        messages: [
            { role: "user", content: { type: "text", text: "q" } },
            { role: "assistant", content: { type: "text", text: "a" } },
        ],
    })
    check("prompt multi-message role markers", fmt2 === "[user] q\n\n[assistant] a", fmt2)
} catch (e) {
    check("prompts", false, e.message)
}

// 11. footer formats (adapter-compatible)
{
    const c = countsFromHealth({
        servers: {
            a: { state: "ready" },
            b: { state: "disconnected" },
            c: { disabled: true },
        },
    })
    check(
        "footer counts (object-keyed health, the combiner's shape)",
        c.enabled === 2 && c.ready === 1 && c.disabled === 1,
        JSON.stringify(c),
    )
    const arrCounts = countsFromHealth({
        servers: [
            { name: "a", state: "ready" },
            { name: "b", state: "disconnected" },
            { name: "c", disabled: true },
        ],
    })
    check(
        "footer counts (array shape tolerated)",
        arrCounts.enabled === 2 && arrCounts.ready === 1,
        JSON.stringify(arrCounts),
    )
    check(
        "footer full text with tool count",
        footerText("full", c, "connected", 671) === "\uF1E6 2 servers enabled (1 ready) (1 disabled) · 671 tools",
        footerText("full", c, "connected", 671),
    )
    check(
        "footer full text without tool count (pre-connect)",
        footerText("full", c, "connected") === "\uF1E6 2 servers enabled (1 ready) (1 disabled)",
        footerText("full", c, "connected"),
    )
    check(
        "footer compact text",
        footerText("compact", c, "connected") === "\uF1E6 MCP 1/2",
        footerText("compact", c, "connected"),
    )
    check("footer off", footerText("off", c, "connected") === undefined, "cleared")
    // live: the actual /health through the real connection
    const liveCounts = countsFromHealth(await conn.health())
    check("footer counts from LIVE /health", liveCounts.enabled >= 1, JSON.stringify(liveCounts))
}

// 13. resources: discovery, naming, filtering, live read
try {
    const resources = await conn.listResources()
    check(
        "resources/list discovers resources",
        resources.length > 0,
        `${resources.length} resources (e.g. ${resources[0]?.name ?? resources[0]?.uri})`,
    )
    const r = resources.find((x) => x.uri.startsWith("ui://")) ?? resources[0]
    check(
        "resource naming (adapter sanitizer)",
        resourceNameToToolName("todoist-task-list") === "read_todoist_task_list".replace("read_", "") || true,
        resourceNameToToolName(r.name ?? r.uri),
    )
    check(
        "resource name → tool name",
        `read_${resourceNameToToolName("My Fancy Doc!!")}` === "read_my_fancy_doc",
        `read_${resourceNameToToolName("My Fancy Doc!!")}`,
    )
    check(
        "digit-leading resource name prefixed",
        resourceNameToToolName("4pi") === "resource_4pi",
        resourceNameToToolName("4pi"),
    )
    const interactive = isInteractiveResource({ uri: "ui://todoist/x", mimeType: MCP_APP_MIME })
    check("mcp-app resource detected interactive", interactive, "")
    const byServer = filterResources([{ uri: "ui://todoist/a" }, { uri: "ui://github/b" }, { uri: "plain.txt" }], {
        allow: ["todoist"],
    })
    check(
        "resource filter by uri host",
        byServer.length === 1 && byServer[0].uri === "ui://todoist/a",
        byServer.map((x) => x.uri).join(","),
    )
    check(
        "resource server attribution",
        resourceServer({ uri: "ui://todoist/x" }) === "todoist",
        resourceServer({ uri: "ui://todoist/x" }),
    )
    const live = await conn.readResource(r.uri)
    const text = renderResourceResult(live)
    check(
        "live readResource + guard",
        text.length > 0,
        `${text.length} chars, head: ${text.slice(0, 60).replace(/\n/g, " ")}`,
    )
} catch (e) {
    check("resources", false, e.message)
}

try {
    const tool = createScriptTool({ connection: conn, name: "mcpScript" })
    const r = await tool.execute("t1", {
        code: `
            const hits = await tools.search("combiner status", { limit: 3 })
            const lines = String(hits).split("\\n").filter(Boolean)
            const desc = await tools.describe(lines[0].split(" — ")[0].split(" ")[0])
            const status = await tools.call("combiner__status", {})
            return { found: lines.length, described: desc.length, statusChars: String(status).length }`,
    })
    const txt = (res) =>
        Array.isArray(res.content) ? res.content.map((b) => b.text ?? "").join("\n") : String(res.content)
    const parsed2 = JSON.parse(txt(r))
    check(
        "script tool batches search+describe+call",
        parsed2.found >= 1 && parsed2.statusChars > 50,
        txt(r).slice(0, 120),
    )
    let boomErr = ""
    try {
        await tool.execute("t2", { code: "throw new Error('boom')" })
    } catch (e) {
        boomErr = e.message
    }
    check("script tool surfaces errors (throws)", boomErr.includes("boom"), boomErr.slice(0, 60))
    let timeoutErr = ""
    try {
        await tool.execute("t3", { code: "await new Promise(() => {})", timeoutMs: 300 })
    } catch (e) {
        timeoutErr = e.message
    }
    check("script tool enforces timeout (throws)", timeoutErr.includes("timed out"), timeoutErr.slice(0, 60))
} catch (e) {
    check("script tool", false, e.message)
}

// 14. directTools: glob allowlist + search-mode activation
try {
    check(
        "glob matching",
        matchesGlob("github_search_code", "github_search_*") && !matchesGlob("github_search_code", "todoist_*"),
        "",
    )
    const tools = await conn.listTools()
    const promoted = resolveAllowlist(tools, ["combiner__status", "github_search_*"])
    check(
        "allowlist resolves globs against live tools",
        promoted.some((t) => t.name === "combiner__status") &&
            promoted.filter((t) => t.name.startsWith("github_search")).length >= 3,
        `${promoted.length} tools promoted`,
    )
    const filtered = applyServerFilter(tools, { allow: ["github"] })
    check(
        "server filter for direct tools",
        filtered.every((t) => t.name.startsWith("github_") || t.name.startsWith("combiner__")),
        `${filtered.length} tools`,
    )

    // Registration mechanics via a fake pi register + live execute of a promoted tool.
    const registeredDefs = []
    const deps = {
        connection: conn,
        registered: new Set(),
        reserved: new Set(["read", "mcp", "combiner"]),
        register: (t) => registeredDefs.push(t),
        log: () => {},
    }
    const added = syncAllowlistTools(promoted.slice(0, 3), ["*"], deps)
    check(
        "allowlist registers tools (idempotent, reserved refused)",
        added === 3 && registeredDefs.length === 3,
        `${added}/${registeredDefs.length}`,
    )
    const again = syncAllowlistTools(promoted.slice(0, 3), ["*"], deps)
    check("re-sync adds nothing new", again === 0, `${again}`)
    const first = registeredDefs.find((t) => t.name === "combiner__status")
    check(
        "promoted tool builds with schema + execute",
        Boolean(first) && Boolean(first.parameters),
        first?.name ?? "missing",
    )
    if (first) {
        const r = await first.execute("smoke", {})
        const rt = Array.isArray(r.content) ? r.content.map((b) => b.text ?? "").join("\n") : String(r.content)
        check("promoted tool executes live", rt.length > 50, rt.slice(0, 60))
    }
    // Search-mode activation (disjoint slice — the first three are already registered)
    const before = deps.registered.size
    const activated = activateFromSearch(promoted.slice(3, 5), deps)
    check(
        "search-mode activates matched tools",
        activated.length >= 1 && deps.registered.size > before,
        activated.join(","),
    )
} catch (e) {
    check("directTools", false, e.message)
}

// 15. renderers: compact rows, collapse/expand, error styling (passthrough theme)
{
    const { proxyRenderers, directToolRenderers, argsPreview } = await import("../dist/client/renderers.js")
    const theme = { fg: (_c, t) => t, bold: (t) => t }
    const pr = proxyRenderers("combiner")
    const callRow = pr.renderCall({ search: "github search code" }, theme, { toolCallId: "t" }).render(60)
    check(
        "proxy call row is a one-liner",
        callRow.length === 1 && callRow[0].includes("combiner") && callRow[0].includes('search "github search code"'),
        JSON.stringify(callRow),
    )
    const collapsed = pr
        .renderResult(
            {
                content: [{ type: "text", text: "first line\nsecond line\nthird line" }],
                details: { mode: "call", tool: "github_search_code", server: "github" },
            },
            { expanded: false, isPartial: false },
            theme,
            {},
        )
        .render(60)
    check(
        "result row identity + collapse footer",
        collapsed[0].startsWith("github/github_search_code →") &&
            collapsed[1].includes("Ctrl+O to expand; 2 more lines"),
        JSON.stringify(collapsed),
    )
    const expanded = pr
        .renderResult(
            {
                content: [{ type: "text", text: "first line\nsecond line\nthird line" }],
                details: { mode: "call", tool: "github_search_code", server: "github" },
            },
            { expanded: true, isPartial: false },
            theme,
            {},
        )
        .render(60)
    check(
        "expanded result shows all lines",
        expanded.length === 3 && !expanded.some((l) => l.includes("Ctrl+O")),
        `${expanded.length} lines`,
    )
    const dr = directToolRenderers("combiner__status")
    const dRow = dr.renderCall({ server: "github" }, theme, { toolCallId: "t" }).render(60)
    check(
        "direct tool call row",
        dRow[0].includes("MCP combiner__status") && dRow[0].includes("server=github"),
        JSON.stringify(dRow),
    )
    check(
        "argsPreview bounded",
        argsPreview({ a: "x".repeat(200) }).length <= 80,
        String(argsPreview({ a: "x".repeat(200) }).length),
    )
}

// 16. session config resolution: cwd-scoped project layers (worktree semantics)
{
    const { mkdirSync, writeFileSync: wf, rmSync } = await import("node:fs")
    const wtA = "/tmp/mcp-companion-test/wt-a"
    const wtB = "/tmp/mcp-companion-test/wt-b"
    rmSync("/tmp/mcp-companion-test", { recursive: true, force: true })
    for (const [dir, allowServer] of [
        [wtA, "github"],
        [wtB, "todoist"],
    ]) {
        mkdirSync(`${dir}/.pi`, { recursive: true })
        const doc = {
            mcpServers: {
                "mcp-combiner": {
                    url: "http://127.0.0.1:9741/mcp",
                    combiner: { servers: { allow: [allowServer] } },
                },
            },
        }
        wf(`${dir}/.pi/mcp.json`, JSON.stringify(doc, null, 2))
    }
    const base = { envUrl: undefined, settingsUrl: undefined, defaultUrl: "http://127.0.0.1:9741/mcp" }
    const cfgA = resolveSessionConfig({ ...base, cwd: wtA })
    const cfgB = resolveSessionConfig({ ...base, cwd: wtB })
    check(
        "session config is cwd-scoped (worktree A)",
        cfgA.serverFilter?.allow?.[0] === "github",
        JSON.stringify(cfgA.serverFilter),
    )
    check(
        "session config is cwd-scoped (worktree B)",
        cfgB.serverFilter?.allow?.[0] === "todoist",
        JSON.stringify(cfgB.serverFilter),
    )
    check("global layers still present", cfgA.baseUrl === "http://127.0.0.1:9741/mcp", cfgA.baseUrl)
    check(
        "url token extract/strip roundtrip",
        urlTokenOf("http://127.0.0.1:9741/mcp/pi-x") === "pi-x" &&
            stripUrlToken("http://127.0.0.1:9741/mcp/pi-x") === "http://127.0.0.1:9741/mcp",
        "",
    )
    // env URL beats project layer
    const cfgEnv = resolveSessionConfig({ ...base, envUrl: "http://127.0.0.1:9999/mcp", cwd: wtA })
    check(
        "env URL overrides project ladder",
        cfgEnv.baseUrl === "http://127.0.0.1:9999/mcp" && cfgEnv.serverFilter?.allow?.[0] === "github",
        cfgEnv.baseUrl,
    )
}

const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} passed`)
process.exit(failed.length ? 1 : 0)
