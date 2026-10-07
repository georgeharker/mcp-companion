// Pins the runner-stale guard (pi#10599 semantics): the guarded ctx getters
// THROW rather than return — `?.` does not protect against a throwing getter.
import { describe, expect, it } from "vitest"
import { isRunnerStaleError, runnerStaleHint, safeHasUi } from "../src/client/runner-stale.js"

function throwingGetCtx(): { hasUI: boolean } {
    return new Proxy(
        {},
        { get: () => { throw new Error("This extension ctx is stale after session replacement or reload. Do not use a captured pi or command ctx after ctx.newSession(), fork(), or switchSession() — resolve the ctx from the current event instead.") } },
    ) as unknown as { hasUI: boolean }
}

describe("runner-stale guard", () => {
    it("safeHasUi: a throwing getter degrades to false, never throws", () => {
        expect(safeHasUi({ hasUI: true })).toBe(true)
        expect(safeHasUi(undefined)).toBe(false)
        expect(() => safeHasUi(throwingGetCtx() as never)).not.toThrow()
        expect(safeHasUi(throwingGetCtx() as never)).toBe(false)
    })
    it("isRunnerStaleError: recognizes raw and wrapped forms, rejects others", () => {
        expect(isRunnerStaleError(new Error("This extension ctx is stale after session replacement or reload. …"))).toBe(true)
        expect(isRunnerStaleError(new Error("combiner tool error (echo): This extension ctx is stale after session replacement or reload"))).toBe(true)
        expect(isRunnerStaleError(new Error("MCP session expired"))).toBe(false)
        expect(isRunnerStaleError("fetch failed")).toBe(false)
    })
    it("runnerStaleHint: names what happened and when retrying is safe", () => {
        const h = runnerStaleHint("read_widget")
        expect(h).toContain("read_widget")
        expect(h).toContain("reload")
        expect(h).toContain("If the tool did not run, retry it")
    })
})
