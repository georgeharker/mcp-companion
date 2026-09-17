// Narrow, local typing for the slice of Pi's extension API this plugin uses.
//
// Pi (badlogic/pi-mono, earendil-works/pi) ships its ExtensionAPI types with the
// harness rather than as a standalone npm package we can depend on, so we declare
// exactly the surface we touch — three lifecycle events, `registerCommand`, `exec`,
// and `sendMessage`. Kept deliberately minimal: a wider mirror would rot against a
// moving upstream. Signatures follow the published extension docs
// (https://pi.dev/docs/latest/extensions). Compiled with the package tsconfig
// (plugins/pi/tsconfig.json) — `npx tsc --noEmit` is the authoritative gate.

export type SessionStartReason = "startup" | "reload" | "new" | "resume" | "fork"
export type SessionShutdownReason = "quit" | "reload" | "new" | "resume" | "fork"

export type SessionStartEvent = { reason: SessionStartReason; previousSessionFile?: string }
export type SessionShutdownEvent = { reason: SessionShutdownReason; targetSessionFile?: string }
export type BeforeAgentStartEvent = { systemPrompt: string }
export type BeforeAgentStartResult = { systemPrompt?: string } | void

export type ExtensionContext = {
    cwd: string
    mode: "tui" | "rpc" | "json" | "print"
    hasUI: boolean
    signal?: AbortSignal
    ui?: ExtensionUIContext
    sessionManager?: {
        getSessionId(): string | undefined
        getSessionFile(): string | undefined
    }
}

/** The dialog-capable UI slice Pi hands extensions (TUI and RPC modes). Used by the
 *  elicitation bridge and the mcp() tool's interactive fallbacks. */
export type ExtensionUIContext = {
    notify?: (message: string, level?: "info" | "warn" | "error") => void
    select?: (title: string, options: string[], opts?: unknown) => Promise<string | undefined>
    confirm?: (title: string, message: string, opts?: unknown) => Promise<boolean>
    input?: (title: string, placeholder?: string, opts?: unknown) => Promise<string | undefined>
    /** Set footer/status-bar text; undefined clears the slot. */
    setStatus?: (key: string, text: string | undefined) => void
}

/** Context handed to a command handler. Superset of ExtensionContext in practice; we
 *  only read `signal`, `ui`, and `hasUI`. */
export type ExtensionCommandContext = ExtensionContext

export type AutocompleteItem = { value: string; label?: string }

export type ExecResult = { stdout: string; stderr: string; code: number; killed: boolean }
export type ExecOptions = { signal?: AbortSignal; timeout?: number; cwd?: string; env?: NodeJS.ProcessEnv }

/** An LLM-visible message injected from a command. `display:true` shows it in the TUI;
 *  `{triggerTurn:true, deliverAs:"steer"}` makes the model act on it this turn. */
export type SendMessage = {
    customType: string
    content: string
    display?: boolean
    details?: Record<string, unknown>
}
export type SendMessageOptions = { triggerTurn?: boolean; deliverAs?: "steer" | "followUp" }

/** Minimal JSON-schema-ish parameter type for registerTool. Pi uses typebox TSchema;
 *  we hand it plain JSON Schema objects, which its loader accepts. */
export type ToolParameters = Record<string, unknown>

export type ToolCallContext = ExtensionContext & {
    onUpdate?: (update: { level: "info" | "warn" | "error"; message: string }) => void
}

export type TextBlock = { type: "text"; text: string }

/** pi's TUI Component contract (pi-tui's Component). invalidate() is REQUIRED,
 *  not optional: MouseRegion.invalidate calls it unconditionally, and a component
 *  missing it crashes pi with "this.child.invalidate is not a function" the first
 *  time the transcript invalidates (session restore, theme change, resize).
 *  Renderers must at minimum no-op it / drop any width cache. */
export type TuiComponent = {
    render(width: number): string[]
    invalidate(): void
}

/** The theme slice renderers receive (pi passes its live theme). */
export type RenderTheme = { fg: (color: string, text: string) => string; bold?: (text: string) => string }

/** Rendering options pi passes to renderResult. */
export type ToolRenderResultOptions = { expanded: boolean; isPartial: boolean }

/** pi's real tool-result contract: content is an ARRAY of blocks; the error flag is
 *  set by THROWING from execute, never by returning a property. */
export type ToolResult = {
    content: TextBlock[]
    details?: unknown
    terminate?: boolean
}

export type ToolDefinition = {
    name: string
    label: string
    description: string
    promptSnippet?: string
    parameters: ToolParameters
    execute: (
        toolCallId: string,
        params: Record<string, unknown>,
        signal: AbortSignal | undefined,
        onUpdate: ((update: { level: "info" | "warn" | "error"; message: string }) => void) | undefined,
        ctx: ToolCallContext | undefined,
    ) => ToolResult | Promise<ToolResult>
    /** Custom rendering (pi contract): each returns a TUI Component — render(width)
     *  yields styled lines. Provided by client/renderers.ts. */
    renderCall?: (
        args: Record<string, unknown>,
        theme: RenderTheme,
        context: { toolCallId: string; [key: string]: unknown },
    ) => TuiComponent
    renderResult?: (
        result: { content?: unknown; isError?: boolean; details?: unknown },
        options: ToolRenderResultOptions,
        theme: RenderTheme,
        context: { [key: string]: unknown },
    ) => TuiComponent
}

export type CommandSpec = {
    description: string
    handler: (args: string, ctx: ExtensionCommandContext) => void | Promise<void>
    getArgumentCompletions?: (prefix: string) => AutocompleteItem[] | null
}

export interface ExtensionAPI {
    on(event: "session_start", handler: (event: SessionStartEvent, ctx: ExtensionContext) => void | Promise<void>): void
    on(
        event: "session_shutdown",
        handler: (event: SessionShutdownEvent, ctx: ExtensionContext) => void | Promise<void>,
    ): void
    on(
        event: "before_agent_start",
        handler: (
            event: BeforeAgentStartEvent,
            ctx: ExtensionContext,
        ) => BeforeAgentStartResult | Promise<BeforeAgentStartResult>,
    ): void
    registerCommand(name: string, spec: CommandSpec): void
    /** Register an LLM-callable tool. Pi also accepts richer fields (renderers,
     *  prepareArguments); we declare only what we use. */
    registerTool(tool: ToolDefinition): void
    /** Send a message into the conversation as if the user typed it (prompt delivery). */
    sendUserMessage(text: string): void
    exec(command: string, args: string[], options?: ExecOptions): Promise<ExecResult>
    sendMessage(message: SendMessage, options?: SendMessageOptions): void
}
