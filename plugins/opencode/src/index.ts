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
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

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
}

type LogFn = (level: "info" | "warn" | "error", message: string) => void
type ToastFn = (variant: "success" | "warning" | "error", message: string) => void
/** The OpenCode client handed to the plugin (from PluginInput). */
type OcClient = Parameters<Plugin>[0]["client"]

const DEFAULT_PORT = 9741
const DEFAULT_NAME = "mcp-combiner"
const DEFAULT_GRACE = "30m"
// The `--mcp` serve flag was introduced in combiner 0.8.0; older versions serve
// with a bare `--config`. Version-gated so this plugin works across the boundary.
const MIN_MCP_VERSION: [number, number, number] = [0, 8, 0]

// ── sharedserver binary resolution (ported from opencode-sharedserver) ──

const CANDIDATE_BINARIES = [
    "sharedserver",
    join(homedir(), ".cargo", "bin", "sharedserver"),
    join(homedir(), ".local", "bin", "sharedserver"),
    "/usr/local/bin/sharedserver",
    "/opt/homebrew/bin/sharedserver",
]

function resolveBinary(override: string | undefined, env: NodeJS.ProcessEnv): string | undefined {
    const candidates = [override, env.SHAREDSERVER_BIN, ...CANDIDATE_BINARIES].filter(
        (v): v is string => typeof v === "string" && v.length > 0,
    )
    for (const candidate of candidates) {
        if (candidate.includes("/")) {
            if (existsSync(candidate)) return candidate
            continue
        }
        const probe = spawnSync(candidate, ["--version"], { stdio: "ignore", env })
        if (probe.status === 0) return candidate
    }
    return undefined
}

// ── combiner command resolution ────────────────────────────────────

type Command = { cmd: string; args: string[] }

function onPath(cmd: string, env: NodeJS.ProcessEnv): boolean {
    return spawnSync(cmd, ["--version"], { stdio: "ignore", env }).status === 0
}

function splitArgs(value: string | undefined): string[] {
    if (!value) return []
    return value.split(/\s+/).filter((s) => s.length > 0)
}

/** Resolve how to invoke the combiner, mirroring the Claude mcp-combiner plugin's priority:
 *  explicit option → env command → `mcp-combiner` on PATH →
 *  `uv run --project <checkout> python -m mcp_combiner`. */
function resolveCombiner(opts: Options, env: NodeJS.ProcessEnv): Command | undefined {
    const extra = opts.args ?? []
    if (opts.command) return { cmd: opts.command, args: extra }
    if (env.OPENCODE_MCP_COMBINER_COMMAND) {
        return {
            cmd: env.OPENCODE_MCP_COMBINER_COMMAND,
            args: [...splitArgs(env.OPENCODE_MCP_COMBINER_ARGS), ...extra],
        }
    }
    if (onPath("mcp-combiner", env)) return { cmd: "mcp-combiner", args: extra }
    const checkout = opts.checkout ?? env.OPENCODE_MCP_COMBINER_CHECKOUT
    if (checkout && onPath("uv", env)) {
        return {
            cmd: "uv",
            args: ["run", "--project", checkout, "python", "-m", "mcp_combiner", ...extra],
        }
    }
    return undefined
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

/** True if the combiner supports (needs) the `--mcp` serve flag. Unknown version
 *  → assume yes (current releases are ≥0.8.0). */
function combinerNeedsMcpFlag(cmd: Command, env: NodeJS.ProcessEnv): boolean {
    const r = spawnSync(cmd.cmd, [...cmd.args, "--version"], { env })
    if (r.status !== 0) return true
    const ver = parseVersion(`${r.stdout?.toString() ?? ""}${r.stderr?.toString() ?? ""}`)
    return ver ? gte(ver, MIN_MCP_VERSION) : true
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

    const port = opts.port ?? DEFAULT_PORT
    const mcpName = opts.mcpName ?? DEFAULT_NAME
    const name = opts.name ?? DEFAULT_NAME
    const register = opts.register !== false
    const hostOwnedUrl = process.env.MCP_COMPANION_COMBINER_URL
    const url = opts.url ?? hostOwnedUrl ?? `http://127.0.0.1:${port}/mcp`

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
    const hooks = { config: configHook }

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

    const binary = resolveBinary(opts.binary, env)
    if (!binary) {
        const msg = "sharedserver binary not found; set `binary`/`$SHAREDSERVER_BIN`, or use manage:false"
        log("error", msg)
        toast("error", msg)
        return hooks
    }
    const combiner = resolveCombiner(opts, env)
    if (!combiner) {
        const msg =
            "mcp-combiner command not found; install `mcp-combiner`, set `command`/`checkout`, " +
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
    const wrapped: Command = { cmd: combiner.cmd, args: [...combiner.args, ...serve] }

    const useArgs = [
        "use",
        name,
        "--pid",
        String(process.pid),
        "--grace-period",
        opts.gracePeriod ?? DEFAULT_GRACE,
        "--metadata",
        `opencode-${process.pid}`,
    ]
    if (opts.logFile) useArgs.push("--log-file", opts.logFile)
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
