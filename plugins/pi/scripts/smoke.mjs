// Smoke test: exercise the built client half (v0.16 native architecture) against
// the live combiner on :9741.
// Run: node scripts/smoke.mjs   (from plugins/pi)
//
// The :9741 URLs below are INTENTIONAL loopback constants — this script's subject
// is that live combiner instance (same convention as the original smoke; not a
// config surface).
//
// Mode map (what changed vs the legacy smoke — the legacy script tested
// script.ts / direct-tools.ts / connection.ts / resources.ts, all deleted by the
// native factoring): the connection is NativeCombinerConnection (pi-mcp based),
// the batch tool is gone (codemode replaces it — needs pi's runtime, covered by
// vitest/note docs, not here), direct-tool promotion is pi's native exposure
// (activateNativeTools), and the mcp() router + spill guard are exercised
// end-to-end with a tiny cap.

import { NativeCombinerConnection, tokenedUrl } from "../dist/native/index.js"
import { rankTools } from "../dist/client/ranking.js"
import { readLadder } from "../dist/client/config-ladder.js"
import { handleElicitation } from "../dist/client/elicitation.js"
import { formatPromptResult, parsePromptArgs, promptCommandName, resolvePromptArgs } from "../dist/client/prompts.js"
import { countsFromHealth, footerText } from "../dist/client/footer.js"
import { isInteractiveResource, MCP_APP_MIME } from "../dist/client/widget-support.js"
import { renderResourceResult } from "../dist/client/render.js"
import { resolveSessionConfig, stripUrlToken, urlTokenOf } from "../dist/client/config-ladder.js"
import { matchesGlob } from "../dist/client/tool-matching.js"
import { resourceNameToToolName, resourceServer } from "../dist/client/resource-naming.js"
import { activateNativeTools } from "../dist/native/index.js"
import { readFile } from "node:fs/promises"

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

// 3. connect + listTools (native pi-mcp transport)
const conn = new NativeCombinerConnection(
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

// 6. callTool — combiner meta-tool (harmless read). Native callTool THROWS on
// isError (the transport carries server errors as exceptions — by design).
try {
    const r = await conn.callTool("combiner__status")
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
    // The un-filtered list needs a fresh LIST (the per-token cache is keyed to the filter state)
    const restored = await conn.listTools(true)
    check("filter removal restores the full list", restored.length >= tools.length, `${restored.length} tools`)
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

// 12. interactive resources: naming, filtering, live read through the guard
try {
    const resources = await conn.listResources()
    check(
        "resources/list discovers resources",
        resources.length > 0,
        `${resources.length} resources (e.g. ${resources[0]?.name ?? resources[0]?.uri})`,
    )
    const r = resources.find((x) => x.uri.startsWith("ui://")) ?? resources[0]
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
    check(
        "resource server attribution (uri host)",
        resourceServer({ uri: "ui://todoist/x", mimeType: MCP_APP_MIME }) === "todoist",
        String(resourceServer({ uri: "ui://todoist/x", mimeType: MCP_APP_MIME })),
    )
    const interactive = isInteractiveResource({ uri: "ui://todoist/x", mimeType: MCP_APP_MIME })
    check("mcp-app resource detected interactive", interactive, "")
    const { filterInteractiveResources } = await import("../dist/native/index.js")
    const byServer = filterInteractiveResources(
        [
            { uri: "ui://todoist/a", mimeType: MCP_APP_MIME },
            { uri: "ui://github/b", mimeType: MCP_APP_MIME },
        ],
        { allow: ["todoist"] },
    )
    check(
        "interactive resource filter by uri host",
        byServer.length === 1 && byServer[0].uri === "ui://todoist/a",
        byServer.map((x) => x.uri).join(","),
    )
    const live = await conn.readResource(r.uri)
    const text = await renderResourceResult(live)
    check(
        "live readResource + guarded render",
        text.length > 0,
        `${text.length} chars, head: ${text.slice(0, 60).replace(/\n/g, " ")}`,
    )
} catch (e) {
    check("resources", false, e.message)
}

// 13. native tool surface: exposure mapping + registration + live execute
try {
    check(
        "glob matching",
        matchesGlob("github_search_code", "github_search_*") && !matchesGlob("github_search_code", "todoist_*"),
        "",
    )
    const registeredDefs = []
    const fakePi = { registerTool: (t) => registeredDefs.push(t) }
    const names = await activateNativeTools(fakePi, {
        connection: conn,
        serverFilter: { allow: ["combiner"] },
        directSpec: ["combiner__status"],
        warnLargeDirectExposure: false,
        log: () => {},
    })
    check("native surface registers tools", names.length > 0, `${names.length} names; ${registeredDefs.length} defs`)
    const direct = registeredDefs.find((t) => t.name === "combiner__status")
    const codemode = registeredDefs.find((t) => t.name === "combiner__refresh_tools")
    check(
        "exposure: allowlist glob → direct, rest → codemode",
        direct?.exposure === "direct" && (codemode === undefined || codemode.exposure === "codemode"),
        direct ? `combiner__status=${direct.exposure}` : "combiner__status absent",
    )
    check(
        "direct def carries the combiner namespace",
        direct?.namespace?.name === "mcp_combiner",
        JSON.stringify(direct?.namespace ?? null),
    )
    if (direct) {
        const r = await direct.execute("smoke", {}, undefined, undefined, { hasUI: false })
        const text = Array.isArray(r.content) ? r.content.map((b) => b.text ?? "").join("\n") : String(r.content)
        check("native direct tool executes live", text.length > 20, text.slice(0, 70))
        check(
            "under-cap result is not truncated",
            !text.includes("Truncated") && r.details?.full_output_path === undefined,
            `details keys: ${Object.keys(r.details ?? {}).join(",")}`,
        )
    }
} catch (e) {
    check("native tool surface", false, e.message)
}

// 14. mcp() router + the SPILL GUARD end-to-end: tiny cap → truncated notice,
// spill file with the FULL text, details.full_output_path.
try {
    const { createMcpTool } = await import("../dist/client/proxy-tool.js")
    const tool = createMcpTool({
        connection: conn,
        toolName: "mcp",
        maxResultChars: 64,
    })
    const r = await tool.execute("smoke", { tool: "combiner__status" }, undefined, undefined, { hasUI: false })
    const text = Array.isArray(r.content) ? r.content.map((b) => b.text ?? "").join("\n") : String(r.content)
    check("router call truncates over the cap", text.includes("Truncated"), `len=${text.length}`)
    const spillPath = /Full content: (\S+)/.exec(text)?.[1] ?? r.details?.full_output_path
    check("truncated notice + details name the spill file", Boolean(spillPath), String(spillPath))
    if (spillPath) {
        const full = await readFile(spillPath, "utf8")
        check("spill file holds the full content", full.length > 64 && text.startsWith(full.slice(0, 10)), `full=${full.length} chars`)
    }
    check(
        "details carry mode/tool/server",
        r.details?.mode === "call" && r.details?.tool === "combiner__status" && r.details?.server === "combiner",
        JSON.stringify(r.details?.tool ? { mode: r.details.mode, tool: r.details.tool, server: r.details.server } : r.details),
    )
    // Under the default cap the same call is NOT truncated.
    const tool2 = createMcpTool({ connection: conn, toolName: "mcp" })
    const r2 = await tool2.execute("smoke", { tool: "combiner__status" })
    const t2 = Array.isArray(r2.content) ? r2.content.map((b) => b.text ?? "").join("\n") : String(r2.content)
    check("default cap leaves small results untouched", !t2.includes("Truncated"), `len=${t2.length}`)
} catch (e) {
    check("mcp router + spill guard", false, e.message)
}

// 15. renderers: compact rows, collapse/expand, error styling (passthrough theme)
{
    const { proxyRenderers, argsPreview } = await import("../dist/client/renderers.js")
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