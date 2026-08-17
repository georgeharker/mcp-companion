// Pi extension: run the `mcp-combiner` MCP aggregator via the `sharedserver` CLI so it
// is available to Pi through `pi-mcp-adapter`.
//
// Pi has no MCP of its own; `pi-mcp-adapter` (a separate Pi package) is what actually
// talks MCP, reading its own `mcp.json`. This extension is the OTHER half — the exact
// counterpart of the process side of the Claude Code and OpenCode plugins:
//
//   1. Process — drive `sharedserver use … -- <combiner> --mcp --config … --port …` so
//      the combiner is running and refcounted, SHARED with any other client (Claude
//      Code, OpenCode, Neovim) that uses the same sharedserver name. Launched in
//      `session_start` (Pi forbids background startup from the factory) and released in
//      `session_shutdown` — but only on `reason === "quit"`, since reload/new/resume/
//      fork keep the same Pi process alive and a fresh `session_start` re-attaches.
//   2. Instructions — append the combiner's tool-discovery directive to the system
//      prompt via `before_agent_start` (analogue of the CC plugin's SessionStart
//      additionalContext and the OpenCode plugin's system.transform hook). The combiner
//      also serves the same text as its MCP `instructions`, which pi-mcp-adapter
//      surfaces on connect — this is the guaranteed, client-native belt to that braces.
//
// Registration itself (pointing pi-mcp-adapter at the combiner) is a single `mcp.json`
// entry — see mcp.json.example and the README. That is static, CC-style, by design;
// this extension deliberately does not write another extension's config.
//
// The sharedserver resolution/fetch and the combiner command ladder are ported
// faithfully from plugins/opencode/src/index.ts — same floor, same warnings, same
// degrade-rather-than-die behaviour, so a user running several clients gets one answer
// to "which combiner / which sharedserver am I on, and why".

import { spawnSync } from "node:child_process"
import { existsSync, mkdirSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import type {
    AutocompleteItem,
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    SessionShutdownEvent,
} from "./pi.js"
import { resolveSharedserver } from "./sharedserver-resolve.js"

const DEFAULT_PORT = 9741
const DEFAULT_NAME = "mcp-combiner"
const DEFAULT_GRACE = "30m"
// The `--mcp` serve flag arrived in combiner 0.8.0; older versions serve with a bare
// `--config`. That boundary is also the floor for trusting a PATH install — below it we
// fetch a known-good release via uvx rather than limp along. NOT the lockstep release
// version; it moves only when this extension depends on newer combiner behaviour.
const MIN_COMBINER_VERSION: [number, number, number] = [0, 8, 0]
// Floor-only against sharedserver's latest release: this repo consumes sharedserver
// rather than shipping it. Kept equal to the OpenCode plugin's value (they resolve the
// same binary the same way).
const SHAREDSERVER_MIN_VERSION = "0.6.7"

type LogFn = (level: "info" | "warn" | "error", message: string) => void

// ── the tool-discovery directive ───────────────────────────────────
// Appended to the system prompt so the agent knows combined tools arrive under a
// `<server>_` prefix (and, through pi-mcp-adapter, are reached via its `mcp()` proxy or
// promoted `directTools`), and looks before deciding a capability is absent. Canonical
// source: CLAUDE.md.example at the repo root; a release-time `prepack` copies it to this
// package's root as instructions.txt (see package.json). A dev/unbuilt run without the
// copy falls back to empty and simply injects nothing.
const COMBINER_DIRECTIVE: string = (() => {
    try {
        const here = dirname(fileURLToPath(import.meta.url))
        // dist/index.js and the packed instructions.txt both sit one level up from here
        // when built (dist/), and the source layout mirrors it (src/ → package root).
        return readFileSync(join(here, "..", "instructions.txt"), "utf8")
    } catch {
        return ""
    }
})()
// First line of the directive — used to detect an already-appended prompt so repeated
// `before_agent_start` turns do not stack duplicate copies.
const DIRECTIVE_MARKER = COMBINER_DIRECTIVE.split("\n", 1)[0] ?? ""

// ── env configuration ──────────────────────────────────────────────
// The PI_MCP_COMBINER_* namespace mirrors the Claude plugin's CLAUDE_MCP_COMBINER_* and
// the OpenCode plugin's OPENCODE_MCP_COMBINER_*, so a user running several clients keeps
// one namespace per client rather than options in one place and env vars in another.

function env(name: string): string | undefined {
    const v = process.env[name]
    return v !== undefined && v !== "" ? v : undefined
}

function splitArgs(value: string | undefined): string[] {
    if (!value) return []
    return value.split(/\s+/).filter((s) => s.length > 0)
}

// ── combiner command resolution (ported from the OpenCode plugin) ──

type Command = {
    cmd: string
    /** Structural args identifying WHAT to run. Probed with; never omitted. */
    args: string[]
    /** The user's own extra args. Appended at spawn time only — never probed with. */
    extra?: string[]
    version?: [number, number, number]
}

function parseVersion(text: string): [number, number, number] | undefined {
    const m = text.match(/(\d+)\.(\d+)\.(\d+)/)
    if (!m) return undefined
    return [Number(m[1]), Number(m[2]), Number(m[3])]
}

function gte(a: [number, number, number], b: [number, number, number]): boolean {
    for (let i = 0; i < 3; i++) {
        if (a[i] !== b[i]) return a[i] > b[i]
    }
    return true
}

/** Probe a command once for BOTH facts: is it runnable, and which version. Presence is
 *  "spawn did not fail", NOT "exited 0" — a pre-0.8.0 combiner exits non-zero on
 *  `--version` (the flag did not exist), which is exactly the stale install the floor
 *  exists to catch. Probes with ONLY the structural args; the user's extras are never
 *  folded in, so one arg the CLI rejects cannot make a healthy install look absent. */
function probe(cmd: Command): { present: boolean; version?: [number, number, number] } {
    const r = spawnSync(cmd.cmd, [...cmd.args, "--version"], { env: process.env })
    if (r.error) return { present: false }
    if (r.status !== 0) return { present: true }
    return { present: true, version: parseVersion(`${r.stdout?.toString() ?? ""}${r.stderr?.toString() ?? ""}`) }
}

function probeUsable(cmd: Command): Command | undefined {
    const { version } = probe(cmd)
    if (!version || !gte(version, MIN_COMBINER_VERSION)) return undefined
    return { ...cmd, version }
}

/** This extension's own version, kept equal to the published PyPI mcp-combiner by
 *  lockstep releases (scripts/bump-version.sh). Used as the uvx pin — read from the
 *  manifest, never duplicated as a constant. */
const PLUGIN_VERSION: string | undefined = (() => {
    try {
        const here = dirname(fileURLToPath(import.meta.url))
        const pkg = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8")) as { version?: string }
        return pkg.version
    } catch {
        return undefined
    }
})()

/** Resolve how to invoke the combiner, mirroring the sibling plugins' priority:
 *  explicit env command → `mcp-combiner` on PATH (only when new enough) →
 *  `uv run --project <checkout> python -m mcp_combiner` → a pinned release via uvx. The
 *  uvx tail is what makes a bare install work with nothing installed by hand. */
function resolveCombiner(log: LogFn): Command | undefined {
    const extra = splitArgs(env("PI_MCP_COMBINER_ARGS"))

    const command = env("PI_MCP_COMBINER_COMMAND")
    if (command) {
        return { cmd: command, args: [], extra }
    }

    const onPath: Command = { cmd: "mcp-combiner", args: [] }
    const pathProbe = probe(onPath)
    if (pathProbe.present) {
        if (pathProbe.version && gte(pathProbe.version, MIN_COMBINER_VERSION)) {
            return { ...onPath, extra, version: pathProbe.version }
        }
        log(
            "warn",
            `mcp-combiner on PATH reports ${pathProbe.version?.join(".") ?? "a pre-0.8.0 version"}, older than ` +
                `${MIN_COMBINER_VERSION.join(".")} — ignoring it and fetching a pinned release instead. ` +
                "Upgrade with: uv tool install --upgrade mcp-combiner",
        )
    }

    const checkout = env("PI_MCP_COMBINER_CHECKOUT")
    if (checkout && existsSync(checkout) && probe({ cmd: "uv", args: [] }).present) {
        return { cmd: "uv", args: ["run", "--project", checkout, "python", "-m", "mcp_combiner"], extra }
    }

    // Out-of-the-box path: no install required, fetch from PyPI. Pinned to this
    // extension's version so the pair moves in lockstep; if that exact release is
    // missing, fall back to latest rather than failing. The probe both validates the pin
    // and warms uv's cache, so the real spawn is a cache hit.
    if (probe({ cmd: "uvx", args: [] }).present) {
        if (PLUGIN_VERSION) {
            const pinned = probeUsable({ cmd: "uvx", args: [`mcp-combiner@${PLUGIN_VERSION}`] })
            if (pinned) return { ...pinned, extra }
            log("warn", `pinned mcp-combiner@${PLUGIN_VERSION} unavailable or too old; trying latest from PyPI`)
        }
        const latest = probeUsable({ cmd: "uvx", args: ["mcp-combiner"] })
        if (latest) return { ...latest, extra }
    }

    return undefined
}

/** True if the resolved combiner supports (needs) the `--mcp` serve flag. Unknown
 *  version → assume NO: a version we cannot read is a pre-0.8.0 build serving on a bare
 *  `--config`, and a bare `--config` still serves on newer releases (with a deprecation
 *  warning), so guessing low degrades gracefully whereas guessing high does not. */
function combinerNeedsMcpFlag(cmd: Command): boolean {
    const ver = cmd.version ?? probe({ cmd: cmd.cmd, args: cmd.args }).version
    return ver ? gte(ver, MIN_COMBINER_VERSION) : false
}

// ── servers.json resolution (mirrors the sibling plugins) ──

function resolveConfig(log: LogFn): string | undefined {
    const explicit = env("PI_MCP_COMBINER_CONFIG")
    if (explicit) return explicit
    const user = process.env.USER ?? process.env.LOGNAME ?? ""
    const candidates = [
        join(homedir(), ".cache", "secrets", `${user}.mcpservers.json`),
        join(homedir(), ".config", "mcp-combiner", "servers.json"),
        join(homedir(), ".config", "mcp", "servers.json"),
    ]
    const found = candidates.find(existsSync)
    if (!found) {
        log(
            "error",
            "no combiner servers.json found; set $PI_MCP_COMBINER_CONFIG (probed " +
                "~/.cache/secrets/<user>.mcpservers.json, ~/.config/mcp-combiner/servers.json, " +
                "~/.config/mcp/servers.json)",
        )
    }
    return found
}

// ── sharedserver lifecycle ─────────────────────────────────────────

/** Attach state for exactly one live refcount. Guards `detach()` so a
 *  `session_shutdown("quit")` and a process-exit handler cannot double-`unuse` (which
 *  would over-decrement sharedserver's refcount). */
type Attachment = { binary: string; name: string }
let attachment: Attachment | null = null
let cleanupInstalled = false

function installProcessCleanup() {
    if (cleanupInstalled) return
    cleanupInstalled = true
    // Belt to the session_shutdown("quit") braces: if Pi is killed hard enough that
    // session_shutdown never fires, still release the refcount. Idempotent via `detach`.
    process.on("exit", () => detach())
    for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as NodeJS.Signals[]) {
        process.on(sig, () => {
            detach()
            process.kill(process.pid, sig)
        })
    }
}

function detach() {
    if (!attachment) return
    const { binary, name } = attachment
    attachment = null
    spawnSync(binary, ["unuse", name, "--pid", String(process.pid)], { stdio: "ignore", env: process.env })
}

// ── the extension ──────────────────────────────────────────────────

export default function mcpCombiner(pi: ExtensionAPI): void {
    const notify = env("PI_MCP_COMBINER_NOTIFY") !== "false"
    const wantInstructions = env("PI_MCP_COMBINER_INSTRUCTIONS") !== "false"
    const manage = env("PI_MCP_COMBINER_MANAGE") !== "false"

    // Host-owned mode: a set $MCP_COMPANION_COMBINER_URL means an editor/host (e.g.
    // Neovim) already owns and refcounts the combiner. Then we NEVER launch — only the
    // instructions half runs. The host never sets a port var, mirroring the sibling
    // plugins' distinction.
    const hostOwned = env("MCP_COMPANION_COMBINER_URL") !== undefined && env("PI_MCP_COMBINER_PORT") === undefined

    // ── instructions: appended every turn (analogue of the sibling plugins) ──
    pi.on("before_agent_start", (event) => {
        if (!wantInstructions || !COMBINER_DIRECTIVE) return
        // Do not stack duplicates across turns: the chained prompt carries forward.
        if (DIRECTIVE_MARKER && event.systemPrompt.includes(DIRECTIVE_MARKER)) return
        return { systemPrompt: `${event.systemPrompt}\n\n${COMBINER_DIRECTIVE}` }
    })

    // ── /mcp-combiner command: inspect the extension (verb: system-prompt) ──
    pi.registerCommand("mcp-combiner", {
        description: "mcp-combiner extension — verb: system-prompt (show the injected directive)",
        getArgumentCompletions: (prefix) => completeVerbs(prefix),
        handler: (args, ctx) => {
            const verb = args.trim()
            if (verb === "" || verb === "system-prompt") {
                showDirective(ctx, "mcp-combiner", COMBINER_DIRECTIVE, wantInstructions)
                return
            }
            ctx.ui?.notify?.(`mcp-combiner: unknown verb "${verb}". Try: system-prompt`, "warn")
        },
    })

    if (hostOwned || !manage) {
        // Registration is static (mcp.json) and the combiner is someone else's to run:
        // nothing to launch. The instructions handler above still applies.
        return
    }

    // ── process: launch on session_start, release on session_shutdown("quit") ──
    // Pi forbids starting background resources from the factory, so all of this is
    // deferred to session_start. session_start fires again on reload/new/resume/fork
    // within the same process; the `attachment` guard makes re-entry a no-op attach.
    pi.on("session_start", (_event, ctx) => {
        if (attachment) return // already attached in this process

        const log = makeLog(ctx, notify)
        const binary = resolveSharedserver(
            {
                label: "mcp-combiner",
                minVersion: SHAREDSERVER_MIN_VERSION,
                installerUrl:
                    "https://github.com/georgeharker/sharedserver/releases/latest/download/sharedserver-installer.sh",
            },
            env("SHAREDSERVER_BIN"),
            process.env,
            log,
        )
        if (!binary) {
            log("error", "sharedserver binary not found; set $SHAREDSERVER_BIN, or PI_MCP_COMBINER_MANAGE=false")
            return
        }

        const combiner = resolveCombiner(log)
        if (!combiner) {
            log(
                "error",
                "mcp-combiner could not be found or fetched; install uv and it is fetched from PyPI on demand, " +
                    "or install mcp-combiner, or set $PI_MCP_COMBINER_COMMAND / $PI_MCP_COMBINER_CHECKOUT",
            )
            return
        }

        const cfgPath = resolveConfig(log)
        if (!cfgPath) return // resolveConfig already logged the specifics

        const name = env("PI_MCP_COMBINER_NAME") ?? DEFAULT_NAME
        const port = resolvePort(log)
        const grace = env("PI_MCP_COMBINER_GRACE") ?? DEFAULT_GRACE

        // Assemble: <combiner> [--mcp] --config <cfg> --port <port> [--host <host>]
        const serve: string[] = []
        const modern = combinerNeedsMcpFlag(combiner)
        if (modern) serve.push("--mcp")
        serve.push("--config", cfgPath, "--port", String(port))
        const host = env("PI_MCP_COMBINER_HOST")
        if (host) serve.push("--host", host)

        // Logging parity with the sibling plugins' two-file scheme: sharedserver captures
        // raw stdout/stderr (--log-file on `use`), and the combiner writes its own
        // --log-file. Both default under $XDG_STATE_HOME/mcp-combiner; "none" disables.
        // Combiner-side flags ride the same version gate as --mcp.
        const logDir = join(process.env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "mcp-combiner")
        const pyLogFile = env("PI_MCP_COMBINER_PYLOG") ?? join(logDir, "mcp-combiner-py.log")
        if (modern) {
            if (pyLogFile !== "none") serve.push("--log-file", pyLogFile)
            serve.push("--log-level", env("PI_MCP_COMBINER_LOG_LEVEL") ?? "info")
        }

        const wrappedArgs = [...combiner.args, ...(combiner.extra ?? []), ...serve]
        const useArgs = [
            "use",
            name,
            "--pid",
            String(process.pid),
            "--grace-period",
            grace,
            "--metadata",
            `pi-${process.pid}`,
        ]
        const logFile = env("PI_MCP_COMBINER_LOG") ?? join(logDir, "mcp-combiner.log")
        if (logFile !== "none") {
            try {
                mkdirSync(dirname(logFile), { recursive: true })
            } catch {
                // best-effort — a failed mkdir just means sharedserver may drop the capture
            }
            useArgs.push("--log-file", logFile)
        }
        useArgs.push("--", combiner.cmd, ...wrappedArgs)

        installProcessCleanup()
        const result = spawnSync(binary, useArgs, { stdio: "pipe", env: process.env })
        if (result.error) {
            log("error", `${name}: failed to spawn sharedserver (${result.error.message})`)
            return
        }
        if (result.status !== 0) {
            const stderr = result.stderr?.toString().trim()
            log("error", `${name}: sharedserver use exited ${result.status}${stderr ? ` (${stderr})` : ""}`)
            return
        }

        attachment = { binary, name }
        log("info", `combiner "${name}" attached on port ${port} (${combiner.cmd} ${wrappedArgs.join(" ")})`)
    })

    pi.on("session_shutdown", (event: SessionShutdownEvent) => {
        // Only "quit" means the Pi process is actually leaving. reload/new/resume/fork
        // keep the process alive and a fresh session_start re-attaches — releasing the
        // refcount on those would needlessly drop (and re-take) the shared combiner.
        if (event.reason === "quit") detach()
    })
}

// ── helpers ────────────────────────────────────────────────────────

// The verbs the extension's slash command understands. `system-prompt` shows the
// directive this extension injects — the show-command pattern from pi-custom-system-prompt,
// since `before_agent_start` injections are per-turn and never appear in Pi's own
// `/system-prompt` (which reports the base prompt only).
const COMMAND_VERBS = ["system-prompt"]
function completeVerbs(prefix: string): AutocompleteItem[] | null {
    const p = prefix.trim()
    const matches = COMMAND_VERBS.filter((v) => v.startsWith(p))
    return matches.length ? matches.map((v) => ({ value: v, label: v })) : null
}

const SHOW_LIMIT = 1600
function showDirective(ctx: ExtensionCommandContext, label: string, directive: string, enabled: boolean): void {
    if (!directive) {
        ctx.ui?.notify?.(`${label}: no directive bundled (instructions.txt missing)`, "warn")
        return
    }
    const head = enabled
        ? `${label} directive — injected into the system prompt on every turn (before_agent_start):`
        : `${label} directive — injection is DISABLED this session; it would be:`
    const body =
        directive.length > SHOW_LIMIT
            ? `${directive.slice(0, SHOW_LIMIT)}\n\n… (${directive.length} chars total)`
            : directive
    ctx.ui?.notify?.(`${head}\n\n${body}`, "info")
}

function makeLog(ctx: ExtensionContext, notify: boolean): LogFn {
    return (level, message) => {
        const line = `mcp-combiner: ${message}`
        // Pi has no structured plugin log sink like OpenCode's client.app.log; surface
        // through the UI when there is one (and the user has not opted out), else stderr.
        if (notify && ctx.hasUI && ctx.ui?.notify) {
            ctx.ui.notify(line, level === "error" ? "error" : level === "warn" ? "warn" : "info")
        } else if (level === "error" || level === "warn") {
            process.stderr.write(`${line}\n`)
        }
    }
}

/** The port to serve on, reconciling PI_MCP_COMBINER_PORT with any explicit
 *  $MCP_COMPANION_COMBINER_URL (which is what pi-mcp-adapter registers, so serving
 *  anywhere else would be unreachable — the URL wins, same rule as the sibling plugins). */
function resolvePort(log: LogFn): number {
    const raw = env("PI_MCP_COMBINER_PORT")
    let port = DEFAULT_PORT
    if (raw !== undefined) {
        const n = Number(raw)
        if (Number.isInteger(n) && n > 0) port = n
        else log("warn", `PI_MCP_COMBINER_PORT=${raw} is not a positive integer; using ${DEFAULT_PORT}`)
    }
    const url = env("MCP_COMPANION_COMBINER_URL")
    if (url && raw !== undefined) {
        let urlPort = Number.NaN
        try {
            urlPort = Number(new URL(url).port)
        } catch {
            // malformed URL — fall through to the no-usable-port warning below
        }
        if (!Number.isInteger(urlPort) || urlPort <= 0) {
            log(
                "warn",
                `MCP_COMPANION_COMBINER_URL=${url} has no explicit port; serving on ${port}. ` +
                    `Use an explicit port in the URL (e.g. http://127.0.0.1:${port}/mcp).`,
            )
        } else if (urlPort !== port) {
            log(
                "warn",
                `PI_MCP_COMBINER_PORT=${port} disagrees with MCP_COMPANION_COMBINER_URL=${url}; the URL is what ` +
                    `pi-mcp-adapter registers, so serving on ${port} would be unreachable — using ${urlPort}. ` +
                    "Set them to the same port.",
            )
            port = urlPort
        }
    }
    return port
}
