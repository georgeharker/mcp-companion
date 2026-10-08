// RUNNER-STALE GUARD : pi 1.0 invalidates the extension runner at the TOP of
// AgentSession.reload() and replaces it at the END (after settings + resource
// reloads — often seconds). Any guarded ctx read from a callback inside that
// window throws "This extension ctx is stale after session replacement or
// reload…" (pi#10599). Two hazards reach us, both handled extension-side:
//
//   1. `ctx?.hasUI` guards against null, NOT against a THROWING getter — a tool
//      execute that reads ctx AFTER awaiting upstream work can turn a COMPLETED
//      call into a rejection, and a model retry would then re-execute it.
//      That is why reads here happen ONCE at execute entry and never re-read
//      past an await — the same rule the stale-ctx sweep applied to timers.
//   2. When such a throw reaches the tool result anyway (pi's emitToolCall has
//      no per-handler catch — pi#10599 issue 2), the raw stale text gives the
//      model nothing to act on. The hint-wrappers below name what happened and
//      when retrying is safe.
//
// Retry policy for this class: a retry is safe ONLY when the first attempt
// provably executed nothing (throw-before-upstream). Our tool executes do not
// touch guarded ctx before the upstream call, so this module intentionally
// ships NO automatic retry helper — classification + de-fatalizing reads +
// hint-wrapping only. (The combiner-connection layer has its own retry for its
// own staleness class; these are different failures.)

export type HasUiCtx = { hasUI?: boolean } | undefined

/** Read ctx.hasUI without ever letting the guarded getter throw. For
 *  execute-time reads; call BEFORE any await and pass the boolean down. */
export function safeHasUi(ctx: HasUiCtx): boolean {
    try {
        return Boolean(ctx?.hasUI)
    } catch {
        return false
    }
}

/** Intersected with HasUiCtx (shared `hasUI`) so pi's context types — which don't
 *  declare `model` — satisfy TS's weak-type rule (no overlapping properties). */
export type ModelCtx = HasUiCtx & { model?: { input?: string[] } } | undefined

/** Read ctx.model (image support + input limits source, read-tool parity) without
 *  ever letting the guarded getter throw. Same execute-time rule as safeHasUi: call
 *  BEFORE any await, pass the value down. */
export function safeModel(ctx: ModelCtx): { input?: string[] } | undefined {
    try {
        return ctx?.model
    } catch {
        return undefined
    }
}

/** True when the error is pi's runner-staleness (pi#10599), whether raw or
 *  wrapped by our own catch-rethrows (the stable substring survives both). */
export function isRunnerStaleError(e: unknown): boolean {
    const msg = e instanceof Error ? e.message : String(e)
    return /ctx is stale after session replacement or reload/i.test(msg)
}

/** Wrap a runner-stale failure in a hint that names what happened and when
 *  retrying is safe — instead of surfacing pi's raw assertion text. */
export function runnerStaleHint(toolLabel: string): string {
    return (
        `${toolLabel}: pi reloaded mid-call and this attempt hit the reload window. ` +
        `If the tool did not run, retry it — upstream state is unchanged; wait a few ` +
        `seconds so the reload finishes first.`
    )
}