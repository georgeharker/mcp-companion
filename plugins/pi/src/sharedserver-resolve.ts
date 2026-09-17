// Resolve the `sharedserver` binary, fetching it when none is usable.
//
// VENDORED FILE. This is copied byte-identical into the consuming OpenCode plugins
// (cribsheet, svg-mcp) by their scripts/sync-vendored.sh, so the drift check is a plain
// diff. There is no way to share it as a dependency without coupling their versions to
// sharedserver's — see plugins/claude/bin/sharedserver for the same reasoning on the
// shell side. Edit it HERE; consumers re-sync.
//
// Everything repo-specific arrives through ResolveConfig rather than being hardcoded,
// mirroring bin/sharedserver.conf.

import { spawnSync } from "node:child_process"
import { existsSync, mkdirSync, rmSync, statSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

export type LogFn = (level: "info" | "warn" | "error", message: string) => void
export type ToastFn = (variant: "success" | "warning" | "error", message: string) => void

export type ResolveConfig = {
    /** Prefix for user-facing messages. Default "sharedserver". */
    label?: string
    /** Minimum acceptable version. Defaults to pkgVersion (lockstep). */
    minVersion?: string
    /** Installer to fetch. Defaults to the release pinned to pkgVersion. */
    installerUrl?: string
    /** The host package's own version — only meaningful in lockstep mode. */
    pkgVersion?: string
}

/** Oldest release these plugins are correct against: 0.5.0 added the PID-reuse guard,
 *  without which a recycled client PID can hold a server open indefinitely. */
const HARDCODED_FLOOR = "0.5.0"

const CANDIDATE_BINARIES = [
    "sharedserver",
    join(homedir(), ".cargo", "bin", "sharedserver"),
    join(homedir(), ".local", "bin", "sharedserver"),
    "/usr/local/bin/sharedserver",
    "/opt/homebrew/bin/sharedserver",
]

function parseVersion(text: string): [number, number, number] | undefined {
    const m = text.match(/(\d+)\.(\d+)\.(\d+)/)
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : undefined
}

function gte(a: [number, number, number], b: [number, number, number]): boolean {
    for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i]
    return true
}

function versionOf(candidate: string, env: NodeJS.ProcessEnv): [number, number, number] | undefined {
    const r = spawnSync(candidate, ["--version"], { env })
    if (r.error) return undefined
    return parseVersion(`${r.stdout?.toString() ?? ""}${r.stderr?.toString() ?? ""}`)
}

/** Synchronous sleep — this path runs before anything can be awaited, so the lock wait
 *  cannot use timers. Atomics.wait blocks without spinning. */
function sleepSync(ms: number): void {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms)
}

/** First sharedserver found at ANY version, or undefined. */
function probeAny(override: string | undefined, env: NodeJS.ProcessEnv): string | undefined {
    const candidates = [override, env.SHAREDSERVER_BIN, ...CANDIDATE_BINARIES].filter(
        (v): v is string => typeof v === "string" && v.length > 0,
    )
    for (const candidate of candidates) {
        if (candidate.includes("/")) {
            if (existsSync(candidate)) return candidate
            continue
        }
        // Presence is "spawn did not fail", NOT "exited 0" — a build that rejects some
        // flag is still present.
        if (!spawnSync(candidate, ["--version"], { stdio: "ignore", env }).error) return candidate
    }
    return undefined
}

/** Download and run the cargo-dist installer, then re-probe. */
function installPinned(
    cfg: Required<Pick<ResolveConfig, "label">> & ResolveConfig,
    url: string,
    what: string,
    env: NodeJS.ProcessEnv,
    log?: LogFn,
    toast?: ToastFn,
): string | undefined {
    // The SAME lock directory as the shell shim — cross-client coordination, so a
    // Claude session and an OpenCode session starting together do not race.
    const lockdir = join(env.TMPDIR || "/tmp", ".sharedserver-install.lock")
    let haveLock = false
    try {
        mkdirSync(lockdir)
        haveLock = true
    } catch {
        const deadline = Date.now() + 20_000
        while (existsSync(lockdir) && Date.now() < deadline) sleepSync(250)
        const after = probeAny(undefined, env)
        if (after) return after
        try {
            mkdirSync(lockdir)
            haveLock = true
        } catch {
            log?.("warn", `${cfg.label}: another process is installing sharedserver and did not finish; remove the stale lock if this persists: ${lockdir}`)
            return undefined
        }
    }

    try {
        // Re-probe under the lock: the winner may have finished while we waited.
        const already = probeAny(undefined, env)
        const floor = parseVersion(cfg.minVersion ?? HARDCODED_FLOOR)
        if (already) {
            const v = versionOf(already, env)
            if (v && floor && gte(v, floor)) return already
        }

        // Download to a file, then run it. `curl … | sh` would report success on a 404,
        // since a pipeline's status is sh's; spawnSync uses no shell, so this is exact.
        const script = join(lockdir, "installer.sh")
        const dl = spawnSync("curl", ["--proto", "=https", "--tlsv1.2", "-LsSf", url, "-o", script], { env })
        if (dl.error || dl.status !== 0) {
            const msg = `${cfg.label}: could not download the sharedserver installer (${what})`
            log?.("warn", msg)
            toast?.("warning", msg)
            return undefined
        }
        if (!existsSync(script) || statSync(script).size === 0) {
            log?.("warn", `${cfg.label}: the downloaded sharedserver installer was empty`)
            return undefined
        }

        // cargo-dist embeds the expected sha256 but looks up only `sha256sum` and SKIPS
        // verification when missing — i.e. always, on macOS. Give it one (identical
        // output) rather than execute an unverified binary.
        let runEnv = env
        if (spawnSync("sh", ["-c", "command -v sha256sum"], { stdio: "ignore" }).status !== 0) {
            if (spawnSync("sh", ["-c", "command -v shasum"], { stdio: "ignore" }).status !== 0) {
                log?.("warn", `${cfg.label}: refusing to install sharedserver — no sha256sum or shasum to verify the download`)
                return undefined
            }
            writeFileSync(join(lockdir, "sha256sum"), '#!/bin/sh\nexec shasum -a 256 "$@"\n', { mode: 0o755 })
            runEnv = { ...env, PATH: `${lockdir}:${env.PATH ?? ""}` }
        }

        const run = spawnSync("sh", [script], { env: runEnv, stdio: "ignore" })
        if (run.error || run.status !== 0) {
            log?.("warn", `${cfg.label}: the sharedserver installer ran but failed`)
            return undefined
        }
        return probeAny(undefined, env)
    } finally {
        if (haveLock) {
            try {
                rmSync(lockdir, { recursive: true, force: true })
            } catch {
                /* best effort — a leaked lock costs the next run its bounded wait */
            }
        }
    }
}

/** Resolve the sharedserver binary, fetching a release when nothing usable is present.
 *
 *  Behaviourally identical to plugins/claude/bin/sharedserver — same ladder, floor,
 *  lock and degrade-rather-than-die fallback, so a user running both clients gets the
 *  same answer to "which sharedserver am I on, and why". */
export function resolveSharedserver(
    cfg: ResolveConfig,
    override: string | undefined,
    env: NodeJS.ProcessEnv,
    log?: LogFn,
    toast?: ToastFn,
): string | undefined {
    const label = cfg.label ?? "sharedserver"
    const minVersion = cfg.minVersion ?? cfg.pkgVersion ?? HARDCODED_FLOOR
    const url =
        cfg.installerUrl ??
        (cfg.pkgVersion
            ? `https://github.com/georgeharker/sharedserver/releases/download/v${cfg.pkgVersion}/sharedserver-installer.sh`
            : "https://github.com/georgeharker/sharedserver/releases/latest/download/sharedserver-installer.sh")
    const what = cfg.installerUrl || !cfg.pkgVersion ? "the latest release" : `v${cfg.pkgVersion}`
    const resolved = { ...cfg, label, minVersion }
    const floor = parseVersion(minVersion)

    // An explicit binary / SHAREDSERVER_BIN is never second-guessed: the user named a
    // specific one, so quietly downloading a different one is the wrong answer.
    const explicit = override ?? env.SHAREDSERVER_BIN
    if (explicit) {
        const present = explicit.includes("/")
            ? existsSync(explicit)
            : !spawnSync(explicit, ["--version"], { stdio: "ignore", env }).error
        if (present) {
            const v = versionOf(explicit, env)
            if (v && floor && !gte(v, floor)) {
                const msg = `${label}: sharedserver at ${explicit} is ${v.join(".")}; this plugin expects >= ${minVersion}. Using it anyway because you set it explicitly.`
                log?.("warn", msg)
                toast?.("warning", msg)
            }
            return explicit
        }
    }

    const found = probeAny(override, env)
    if (found) {
        const v = versionOf(found, env)
        if (v && floor && gte(v, floor)) return found
        // Too old: fetch, but remember this one — if the download fails we would rather
        // run the old binary than nothing.
        const msg = `${label}: sharedserver at ${found} is ${v?.join(".") ?? "an unknown version"}; this plugin expects >= ${minVersion}. Fetching ${what}. To keep your own build, set SHAREDSERVER_BIN=${found}`
        log?.("warn", msg)
        toast?.("warning", msg)
        return installPinned(resolved, url, what, env, log, toast) ?? found
    }

    log?.("info", `${label}: no usable sharedserver found; fetching ${what} (one time). This needs no Rust toolchain.`)
    return installPinned(resolved, url, what, env, log, toast)
}
