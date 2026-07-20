// OpenCode plugin: run the `mcp-combiner` MCP aggregator via the `sharedserver`
// CLI and register its HTTP endpoint with OpenCode.
//
// Two independent responsibilities (mirrors the Claude Code plugin, which uses
// SessionStart hooks + a static .mcp.json):
//   1. Process — drive `sharedserver use … -- <combiner> --mcp --config … --port …`
//      so the combiner is running and refcounted (shared with other clients).
//   2. Registration — inject a `type: "remote"` entry into OpenCode's `mcp`
//      config via the `config` hook (OpenCode has no static .mcp.json).
//
// The two halves are independent: registration is unconditional (OpenCode
// surfaces connection state via McpStatus and reconnects), the launch is the
// best-effort "make sure something is listening there" half. See README.md.

import { spawnSync } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import type { Plugin } from "@opencode-ai/plugin"
import { resolveSharedserver } from "./sharedserver-resolve.js"

type Options = {
    // ── Registration ──────────────────────────────────────────────
    /** Key under OpenCode's `mcp` config. Default `"mcp-combiner"`. */
    mcpName?: string
    /** Explicit MCP URL to register. Default `http://127.0.0.1:<port>/mcp`
     *  (or `$MCP_COMPANION_COMBINER_URL` when the host owns the combiner). */
    url?: string
    /** Register the MCP endpoint with OpenCode. Default `true`. */
    register?: boolean

    // ── Process management ────────────────────────────────────────
    /** Launch/attach the combiner via sharedserver. Default `true`.
     *  `false` → registration only (assume something else runs it). */
    manage?: boolean
    /** Explicit path to the `sharedserver` binary. */
    binary?: string
    /** Override SHAREDSERVER_LOCKDIR for child invocations. */
    lockdir?: string
    /** sharedserver instance name. Default `"mcp-combiner"`. */
    name?: string
    /** sharedserver grace period, e.g. "30m", "1h". Default `"30m"`. */
    gracePeriod?: string
    /** Capture the combiner's stdout/stderr to this path (sharedserver `--log-file`). */
    logFile?: string

    // ── Combiner invocation ───────────────────────────────────────
    /** Override the combiner command (else auto-resolved, see resolveCombiner). */
    command?: string
    /** Extra args passed to the combiner before the serve args. */
    args?: string[]
    /** Path to a combiner checkout for `uv run --project <checkout> python -m mcp_combiner`. */
    checkout?: string
    /** Path to the combiner's servers.json (else auto-probed). */
    config?: string
    /** HTTP port the combiner serves on. Default `9741`. */
    port?: number
    /** HTTP host the combiner binds. Default `127.0.0.1`. */
    host?: string

    /** Show TUI toasts for attach/health outcomes. Default `true`. */
    notify?: boolean
    /** Append the tool-discovery directive to the system prompt. Default `true`. */
    instructions?: boolean
}

type LogFn = (level: "info" | "warn" | "error", message: string) => void
type ToastFn = (variant: "success" | "warning" | "error", message: string) => void
/** The OpenCode client handed to the plugin (from PluginInput). */
type OcClient = Parameters<Plugin>[0]["client"]

const DEFAULT_PORT = 9741
const DEFAULT_NAME = "mcp-combiner"
const DEFAULT_GRACE = "30m"
// The `--mcp` serve flag was introduced in combiner 0.8.0; older versions serve with a
// bare `--config`. That same boundary is the floor for trusting a PATH install: below it
// we would rather fetch a known-good release via uvx than limp along on a stale one.
// Deliberately NOT the lockstep release version — it moves only when this plugin starts
// depending on newer combiner behaviour.
const MIN_COMBINER_VERSION: [number, number, number] = [0, 8, 0]

// ── the tool-discovery directive ───────────────────────────────────
// Appended to the system prompt so the agent knows combined tools arrive under a
// `<server>_` prefix, and looks (the tool list, `combiner__status`) before deciding a
// capability is absent. The analogue of the Claude Code plugin's SessionStart
// additionalContext, which reads the same text. Canonical source: CLAUDE.md.example at
// the repo root (plugins/claude/instructions.txt symlinks it). A release-time `prepack`
// copies that file to this package's root as instructions.txt (see package.json
// `prepack`/`files`); we read the copy ONCE here so the published npm package is
// self-contained without duplicating the text in source. A dev/unbuilt run (no copy
// present) falls back to an empty string and simply injects nothing.
const COMBINER_DIRECTIVE: string = (() => {
    try {
        // dist/index.js lives in dist/; the packed copy ships at the package root.
        const here = dirname(fileURLToPath(import.meta.url))
        return readFileSync(join(here, "..", "instructions.txt"), "utf8")
    } catch {
        return ""
    }
})()

// ── sharedserver binary resolution (ported from opencode-sharedserver) ──

// Resolution lives in a module vendored byte-identical from georgeharker/sharedserver
// (scripts/sync-vendored.sh), so the Claude hook's bin/sharedserver and this plugin
// answer "which sharedserver, and why" identically. Floor-only against the latest
// release: mcp-companion consumes sharedserver rather than shipping it.
const SHAREDSERVER_MIN_VERSION = "0.6.7"

function resolveBinary(
    override: string | undefined,
    env: NodeJS.ProcessEnv,
    log?: LogFn,
    toast?: ToastFn,
): string | undefined {
    return resolveSharedserver(
        {
            label: "mcp-combiner",
            minVersion: SHAREDSERVER_MIN_VERSION,
            installerUrl:
                "https://github.com/georgeharker/sharedserver/releases/latest/download/sharedserver-installer.sh",
        },
        override,
        env,
        log,
        toast,
    )
}

// ── combiner command resolution ────────────────────────────────────

/** How to invoke the combiner. `args` are the STRUCTURAL arguments that identify what to
 *  run (`mcp-combiner@0.9.4` for uvx, `run --project … -m mcp_combiner` for a checkout) —
 *  the user's own extras are appended only at spawn time, never during probing. `version`
 *  caches what resolution already learned, so the serve-flag gate need not re-probe. */
type Command = {
    cmd: string
    /** Structural args identifying WHAT to run. Probed with; never omitted. */
    args: string[]
    /** The user's own extra args. Appended at spawn time only — never probed with. */
    extra?: string[]
    version?: [number, number, number]
}

/** Probe a command once for BOTH facts we need: is it runnable, and which version.
 *
 *  Presence is "spawn did not fail", NOT "exited 0": a pre-0.8.0 mcp-combiner exits 2 on
 *  `--version` (the flag did not exist yet), so an exit-status test reported *absent* for
 *  precisely the stale install MIN_COMBINER_VERSION exists to catch. spawnSync sets `error`
 *  (ENOENT) when the command cannot be resolved and EACCES when it is present but not
 *  executable; both mean unusable, so any error counts as absent. Resolution is left to
 *  spawn — hand-rolling a PATH walk means reimplementing PATHEXT, exec bits and symlink
 *  following, for no gain.
 *
 *  Probing uses ONLY the structural args. Folding the user's extras in here meant a single
 *  arg the CLI rejects in that position made a healthy install look stale AND broke the uvx
 *  fallbacks that carry the same extras — turning one bad option into "could not be found
 *  or fetched". */
function probe(cmd: Command, env: NodeJS.ProcessEnv): { present: boolean; version?: [number, number, number] } {
    const r = spawnSync(cmd.cmd, [...cmd.args, "--version"], { env })
    if (r.error) return { present: false }
    if (r.status !== 0) return { present: true }
    return { present: true, version: parseVersion(`${r.stdout?.toString() ?? ""}${r.stderr?.toString() ?? ""}`) }
}

/** Probe, and accept only a combiner at or above the floor. Used for every auto-detected
 *  source so the guarantee is enforced rather than assumed — including the uvx tail, whose
 *  whole job is to be the known-good option. */
function probeUsable(cmd: Command, env: NodeJS.ProcessEnv): Command | undefined {
    const { version } = probe(cmd, env)
    if (!version || !gte(version, MIN_COMBINER_VERSION)) return undefined
    return { ...cmd, version }
}

function splitArgs(value: string | undefined): string[] {
    if (!value) return []
    return value.split(/\s+/).filter((s) => s.length > 0)
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

/** This plugin's own version, which lockstep releases keep equal to the published PyPI
 *  mcp-combiner (scripts/bump-version.sh writes both). Used as the uvx pin — derived from
 *  the package manifest rather than duplicated as a constant. */
const PLUGIN_VERSION: string | undefined = (() => {
    try {
        const here = dirname(fileURLToPath(import.meta.url))
        const pkg = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8")) as {
            version?: string
        }
        return pkg.version
    } catch {
        return undefined
    }
})()

/** Resolve how to invoke the combiner, mirroring the Claude plugin's priority:
 *  explicit option → env command → `mcp-combiner` on PATH (only when new enough) →
 *  `uv run --project <checkout> python -m mcp_combiner` → a pinned release via uvx.
 *  The uvx tail is what makes a bare plugin install work with nothing installed by hand.
 *  Unlike the Claude plugin there is no legacy `mcp-bridge` branch — this package post-dates
 *  the rename, so nobody can be upgrading into it from that name. */
function resolveCombiner(
    opts: Options,
    env: NodeJS.ProcessEnv,
    log?: LogFn,
    toast?: ToastFn,
): Command | undefined {
    const extra = opts.args ?? []
    if (opts.command) return { cmd: opts.command, args: [], extra }
    if (env.OPENCODE_MCP_COMBINER_COMMAND) {
        // $…_ARGS is part of the command spec (structural); opts.args is the user's extra.
        return {
            cmd: env.OPENCODE_MCP_COMBINER_COMMAND,
            args: splitArgs(env.OPENCODE_MCP_COMBINER_ARGS),
            extra,
        }
    }

    // A PATH install wins outright when it is new enough: explicit user choice, no fetch.
    // Too old and we fall through rather than limp — the uvx tail gets a known-good
    // release, so staleness self-heals instead of lingering forever.
    const onPathCandidate: Command = { cmd: "mcp-combiner", args: [] }
    const pathProbe = probe(onPathCandidate, env)
    if (pathProbe.present) {
        if (pathProbe.version && gte(pathProbe.version, MIN_COMBINER_VERSION)) {
            return { ...onPathCandidate, extra, version: pathProbe.version }
        }
        // Toast as well as log: this one is actionable — the user installed an
        // mcp-combiner that we are now declining to use, and silently routing around it
        // would be the confusing outcome. The uvx fallback below keeps them working
        // meanwhile, so this is a nudge rather than an error.
        const stale =
            `mcp-combiner on PATH reports ${pathProbe.version?.join(".") ?? "a pre-0.8.0 version"}, ` +
            `older than ${MIN_COMBINER_VERSION.join(".")} — ignoring it and fetching a pinned ` +
            "release instead. Upgrade with: uv tool install --upgrade mcp-combiner"
        log?.("warn", stale)
        toast?.("warning", stale)
    }

    const checkout = opts.checkout ?? env.OPENCODE_MCP_COMBINER_CHECKOUT
    if (checkout && existsSync(checkout) && probe({ cmd: "uv", args: [] }, env).present) {
        return {
            cmd: "uv",
            args: ["run", "--project", checkout, "python", "-m", "mcp_combiner"],
            extra,
        }
    }

    // Out-of-the-box path: no install required, fetch from PyPI. Pinned to this plugin's
    // version so the pair moves in lockstep; if that exact release is missing (a failed
    // publish, say) fall back to latest rather than failing outright. The probe both
    // validates the pin and warms uv's cache, so the real spawn is a cache hit.
    if (probe({ cmd: "uvx", args: [] }, env).present) {
        if (PLUGIN_VERSION) {
            const pinned = probeUsable({ cmd: "uvx", args: [`mcp-combiner@${PLUGIN_VERSION}`] }, env)
            if (pinned) return { ...pinned, extra }
            log?.("warn", `pinned mcp-combiner@${PLUGIN_VERSION} unavailable or too old; trying latest from PyPI`)
        }
        const latest = probeUsable({ cmd: "uvx", args: ["mcp-combiner"] }, env)
        if (latest) return { ...latest, extra }
    }

    return undefined
}

/** True if the combiner supports (needs) the `--mcp` serve flag. Unknown version →
 *  assume NO, matching the Claude plugin's start.sh: a version we cannot read is a
 *  pre-0.8.0 build with no --version flag, and those serve on a bare --config. Bare
 *  --config still serves on newer releases (with a deprecation warning), so guessing
 *  low degrades gracefully whereas guessing high does not. */
function combinerNeedsMcpFlag(cmd: Command, env: NodeJS.ProcessEnv): boolean {
    // Auto-detected sources already recorded their version during resolution; only the
    // explicit command/env override arrives unprobed, and it probes once here. The probe
    // keeps cmd.args (structural) and drops cmd.extra: dropping the structural args would
    // reduce `uv run --project … -m mcp_combiner` to a bare `uv --version` and read uv's
    // OWN version as the combiner's, while keeping the extras is what made healthy
    // installs look stale.
    const ver = cmd.version ?? probe({ cmd: cmd.cmd, args: cmd.args }, env).version
    return ver ? gte(ver, MIN_COMBINER_VERSION) : false
}

// ── servers.json resolution (mirrors the Claude mcp-combiner plugin probing) ──

function resolveConfig(opts: Options, env: NodeJS.ProcessEnv): string | undefined {
    if (opts.config) return opts.config
    if (env.OPENCODE_MCP_COMBINER_CONFIG) return env.OPENCODE_MCP_COMBINER_CONFIG
    const user = env.USER ?? env.LOGNAME ?? ""
    const candidates = [
        join(homedir(), ".cache", "secrets", `${user}.mcpservers.json`),
        join(homedir(), ".config", "mcp-combiner", "servers.json"),
        join(homedir(), ".config", "mcp", "servers.json"),
    ]
    return candidates.find(existsSync)
}

// ── sharedserver lifecycle (ported) ────────────────────────────────

type PreState = "active" | "grace" | "stopped" | "unknown"

function preCheck(binary: string, name: string, env: NodeJS.ProcessEnv): PreState {
    const result = spawnSync(binary, ["check", name], { stdio: "ignore", env })
    switch (result.status) {
        case 0:
            return "active"
        case 1:
            return "grace"
        case 2:
            return "stopped"
        default:
            return "unknown"
    }
}

type ServerInfo = { pid?: number; state?: string }

function readServerInfo(binary: string, name: string, env: NodeJS.ProcessEnv): ServerInfo | undefined {
    const result = spawnSync(binary, ["info", name, "--json"], { env })
    if (result.status !== 0) return undefined
    try {
        return JSON.parse(result.stdout.toString()) as ServerInfo
    } catch {
        return undefined
    }
}

function isPidAlive(pid: number): boolean {
    try {
        process.kill(pid, 0)
        return true
    } catch {
        return false
    }
}

type Attached = { binary: string; name: string; env: NodeJS.ProcessEnv }

const attached: Attached[] = []
let cleanupInstalled = false

function installCleanup() {
    if (cleanupInstalled) return
    cleanupInstalled = true

    const drain = () => {
        while (attached.length) {
            const s = attached.pop()!
            spawnSync(s.binary, ["unuse", s.name, "--pid", String(process.pid)], {
                stdio: "ignore",
                env: s.env,
            })
        }
    }

    process.on("exit", drain)
    for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as NodeJS.Signals[]) {
        process.on(sig, () => {
            drain()
            process.kill(process.pid, sig)
        })
    }
}

// ── health checks ──────────────────────────────────────────────────

/** Process-level: confirm the wrapped combiner is still alive (sharedserver info). */
function scheduleProcessHealthCheck(
    binary: string,
    name: string,
    env: NodeJS.ProcessEnv,
    log: LogFn,
    toast: ToastFn,
    delayMs: number,
) {
    setTimeout(() => {
        const info = readServerInfo(binary, name, env)
        if (!info) {
            log("warn", `${name}: process health check returned no data`)
            return
        }
        if (info.state && info.state !== "active") {
            const msg = `${name}: not active after start (state: ${info.state})`
            log("error", msg)
            toast("error", msg)
            return
        }
        if (info.pid && !isPidAlive(info.pid)) {
            const msg = `${name}: PID ${info.pid} died shortly after start`
            log("error", msg)
            toast("error", msg)
            return
        }
        log("info", `${name}: process healthy (pid=${info.pid}, state=${info.state})`)
    }, delayMs).unref()
}

/** OpenCode-level: confirm OpenCode actually connected to the combiner MCP server. */
function scheduleMcpHealthCheck(
    client: OcClient,
    mcpName: string,
    log: LogFn,
    toast: ToastFn,
    delayMs: number,
) {
    setTimeout(() => {
        client.mcp
            .status()
            .then((res) => {
                const st = res.data?.[mcpName]
                if (!st) {
                    log("warn", `${mcpName}: not present in OpenCode mcp status yet`)
                    return
                }
                switch (st.status) {
                    case "connected":
                        log("info", `${mcpName}: connected`)
                        toast("success", `${mcpName}: connected`)
                        break
                    case "failed":
                        toast("error", `${mcpName}: failed — ${st.error ?? "unknown error"}`)
                        break
                    case "needs_auth":
                    case "needs_client_registration":
                        toast("warning", `${mcpName}: ${st.status}`)
                        break
                    default:
                        log("info", `${mcpName}: status ${st.status}`)
                }
            })
            .catch((err: unknown) => {
                log("warn", `${mcpName}: mcp status check failed: ${err instanceof Error ? err.message : String(err)}`)
            })
    }, delayMs).unref()
}

// ── plugin ─────────────────────────────────────────────────────────

const McpCombinerPlugin: Plugin = async ({ client }, options) => {
    const opts = (options ?? {}) as Options
    const notify = opts.notify !== false

    const log: LogFn = (level, message) => {
        client.app.log({ body: { service: "mcp-combiner", level, message } }).catch(() => {})
    }
    const toast: ToastFn = (variant, message) => {
        if (!notify) return
        // Deferred: the plugin runs inside InstanceBootstrap, before the TUI bus
        // subscribers are wired up. Best-effort (no-op if headless).
        setTimeout(() => {
            client.tui.showToast({ body: { title: "mcp-combiner", message, variant } }).catch(() => {})
        }, 1500).unref()
    }

    // Env knobs mirror the Claude plugin's CLAUDE_MCP_COMBINER_* set, so a user running
    // both clients configures one namespace per client rather than options here and env
    // vars there. Explicit plugin options still win over the environment.
    const envPortRaw = process.env.OPENCODE_MCP_COMBINER_PORT
    const envPort = Number(envPortRaw)
    const portExplicit = opts.port !== undefined || (envPortRaw !== undefined && envPortRaw !== "")
    if (envPortRaw !== undefined && envPortRaw !== "" && !(Number.isInteger(envPort) && envPort > 0)) {
        log("warn", `OPENCODE_MCP_COMBINER_PORT=${envPortRaw} is not a positive integer; using ${DEFAULT_PORT}`)
    }
    let port = opts.port ?? (Number.isInteger(envPort) && envPort > 0 ? envPort : DEFAULT_PORT)
    const mcpName = opts.mcpName ?? DEFAULT_NAME
    const name = opts.name ?? process.env.OPENCODE_MCP_COMBINER_NAME ?? DEFAULT_NAME
    const register = opts.register !== false

    // A set MCP_COMPANION_COMBINER_URL normally means the host editor owns the combiner
    // and we only register. But if a port was ALSO named explicitly, the user is saying
    // "I picked this port, launch here" — the same distinction the Claude plugin draws,
    // kept identical so one environment behaves the same in both clients. The host never
    // sets a port, so this cannot change its behaviour.
    const combinerUrl = process.env.MCP_COMPANION_COMBINER_URL
    const hostOwnedUrl = combinerUrl && !portExplicit ? combinerUrl : undefined
    if (combinerUrl && portExplicit) {
        // The URL wins where they disagree: it is what gets registered with OpenCode, so
        // serving anywhere else would be unreachable.
        let urlPort = NaN
        try {
            urlPort = Number(new URL(combinerUrl).port)
        } catch {
            // Malformed URL — fall through to the no-usable-port branch below, which
            // reports it. Never throw here: this runs at plugin init and would take
            // the whole session down over a typo'd env var.
        }
        if (!Number.isInteger(urlPort) || urlPort <= 0) {
            const msg = `MCP_COMPANION_COMBINER_URL=${combinerUrl} has no explicit port; serving on ${port}. Use an explicit port in the URL (e.g. http://127.0.0.1:${port}/mcp).`
            log("warn", msg)
            toast("warning", msg)
        } else if (urlPort !== port) {
            const msg = `port ${port} disagrees with MCP_COMPANION_COMBINER_URL=${combinerUrl}; the URL is what gets registered, so serving on ${port} would be unreachable — using ${urlPort} instead. Set them to the same port.`
            log("warn", msg)
            toast("warning", msg)
            port = urlPort
        }
    }
    const url = opts.url ?? hostOwnedUrl ?? combinerUrl ?? `http://127.0.0.1:${port}/mcp`

    // The registration half — always applied (unless disabled). OpenCode surfaces
    // connection state itself, so a briefly-absent endpoint recovers.
    const configHook = async (cfg: { mcp?: Record<string, unknown> }) => {
        if (!register) return
        cfg.mcp ??= {}
        if (cfg.mcp[mcpName]) {
            log("info", `mcp "${mcpName}" already configured by the user; leaving as-is`)
            return
        }
        cfg.mcp[mcpName] = { type: "remote", url, enabled: true }
        log("info", `registered mcp "${mcpName}" → ${url}`)
    }
    // The directive: appended to the system prompt each session (analogue of the CC
    // plugin's SessionStart additionalContext). Contributed unconditionally — the
    // combiner's tools are reachable whether or not this plugin owns the process.
    const wantInstructions = opts.instructions !== false
    const systemHook = async (_input: unknown, output: { system: string[] }) => {
        if (!wantInstructions || !COMBINER_DIRECTIVE) return
        output.system.push(COMBINER_DIRECTIVE)
    }

    const hooks = {
        config: configHook,
        "experimental.chat.system.transform": systemHook,
    }

    const env: NodeJS.ProcessEnv = { ...process.env }
    if (opts.lockdir) env.SHAREDSERVER_LOCKDIR = opts.lockdir

    // The process half — skipped when the host owns the combiner or manage=false.
    const manage = opts.manage !== false
    if (hostOwnedUrl) {
        log("info", `host owns the combiner ($MCP_COMPANION_COMBINER_URL=${hostOwnedUrl}); registering only`)
        scheduleMcpHealthCheck(client, mcpName, log, toast, 5000)
        return hooks
    }
    if (!manage) {
        log("info", `manage=false; registering ${url} only (assuming the combiner is started elsewhere)`)
        scheduleMcpHealthCheck(client, mcpName, log, toast, 5000)
        return hooks
    }

    const binary = resolveBinary(opts.binary, env, log, toast)
    if (!binary) {
        const msg = "sharedserver binary not found; set `binary`/`$SHAREDSERVER_BIN`, or use manage:false"
        log("error", msg)
        toast("error", msg)
        return hooks
    }
    const combiner = resolveCombiner(opts, env, log, toast)
    if (!combiner) {
        const msg =
            "mcp-combiner could not be found or fetched; install uv and it will be fetched from " +
            "PyPI on demand, or install `mcp-combiner`, set `command`/`checkout`, " +
            "or set $OPENCODE_MCP_COMBINER_COMMAND / $OPENCODE_MCP_COMBINER_CHECKOUT"
        log("error", msg)
        toast("error", msg)
        return hooks
    }
    const cfgPath = resolveConfig(opts, env)
    if (!cfgPath) {
        const msg =
            "no combiner servers.json found; set `config` or $OPENCODE_MCP_COMBINER_CONFIG " +
            "(probed ~/.cache/secrets/<user>.mcpservers.json, ~/.config/mcp-combiner/servers.json, ~/.config/mcp/servers.json)"
        log("error", msg)
        toast("error", msg)
        return hooks
    }

    // Assemble the wrapped command: <combiner> [--mcp] --config <cfg> --port <port> [--host <host>]
    const serve: string[] = []
    if (combinerNeedsMcpFlag(combiner, env)) serve.push("--mcp")
    serve.push("--config", cfgPath, "--port", String(port))
    if (opts.host) serve.push("--host", opts.host)
    // Structural args, then the user's extras, then the serve args — the extras sit
    // "before the serve args" as the `args` option documents.
    const wrapped: Command = {
        cmd: combiner.cmd,
        args: [...combiner.args, ...(combiner.extra ?? []), ...serve],
    }

    const useArgs = [
        "use",
        name,
        "--pid",
        String(process.pid),
        "--grace-period",
        opts.gracePeriod ?? process.env.OPENCODE_MCP_COMBINER_GRACE ?? DEFAULT_GRACE,
        "--metadata",
        `opencode-${process.pid}`,
    ]
    const logFile = opts.logFile ?? process.env.OPENCODE_MCP_COMBINER_LOG
    if (logFile) useArgs.push("--log-file", logFile)
    useArgs.push("--", wrapped.cmd, ...wrapped.args)

    installCleanup()
    const pre = preCheck(binary, name, env)
    const result = spawnSync(binary, useArgs, { stdio: "pipe", env })

    if (result.error) {
        const msg = `${name}: failed to spawn sharedserver (${result.error.message})`
        log("error", msg)
        toast("error", msg)
        return hooks
    }
    if (result.status !== 0) {
        const stderr = result.stderr?.toString().trim()
        const msg = `${name}: sharedserver use exited ${result.status}${stderr ? ` (${stderr})` : ""}`
        log("error", msg)
        toast("error", msg)
        return hooks
    }

    attached.push({ binary, name, env })
    if (pre === "stopped" || pre === "unknown") {
        log("info", `started combiner "${name}" (${wrapped.cmd} ${wrapped.args.join(" ")})`)
    } else {
        log("info", `attached to running combiner "${name}" (was ${pre})`)
    }

    scheduleProcessHealthCheck(binary, name, env, log, toast, 2500)
    scheduleMcpHealthCheck(client, mcpName, log, toast, 5000)
    return hooks
}

export default McpCombinerPlugin
