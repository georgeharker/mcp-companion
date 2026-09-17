"""HTML templates for the UI host — faithful Python port of pi-mcp-adapter's
host-html-template.ts + sandbox-proxy-template.ts (MIT, © 2026 Nico Bailon).

The injection contract is preserved exactly: the vendored app-bridge.bundle.js
expects the host page's inline module to define SESSION_TOKEN, SERVER_NAME,
TOOL_NAME, TOOL_ARGS, HOST_CONTEXT, ALLOW_ATTRIBUTE, consent flags,
SANDBOX_PROXY_URL, RESOURCE_CSP, RESOURCE_PERMISSIONS — and the sandbox relay
page to define EXPECTED_PARENT_ORIGIN and RESOURCE_PATH.
"""

# The embedded HTML/JS documents in this module are CONTENT (faithful ports of
# the TS templates' documents) — their lines legitimately exceed the code
# line-length limit and cannot be wrapped without diverging from the source.
# ruff: noqa: E501

from __future__ import annotations

import html
import json
from typing import Any

# Mirror sandbox-proxy-template.ts / host-html-template.ts constants.
APP_SANDBOX = "allow-scripts allow-forms allow-modals allow-popups allow-downloads"
APP_PROXY_SANDBOX = f"{APP_SANDBOX} allow-same-origin"
APP_INNER_SANDBOX = APP_PROXY_SANDBOX
SANDBOX_PROXY_PATH = "/sandbox"
SANDBOX_RESOURCE_PATH_PREFIX = "/resource/"
APP_BRIDGE_MODULE_URL = "/app-bridge.bundle.js"

SUPPORTED_PROTOCOL_VERSION = "2026-01-26"


def safe_inline_json(value: Any) -> str:
    """JSON for inline <script> embedding, escaped against </script> breakouts
    (</, &, U+2028/9) — ported verbatim from the TS safeInlineJSON."""
    out = json.dumps(value, ensure_ascii=False)
    return (
        out.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def escape_html(value: str) -> str:
    return html.escape(value, quote=True)


def escape_html_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _sanitize_csp_domains(domains: Any) -> list[str]:
    if not isinstance(domains, list):
        return []
    seen: dict[str, None] = {}
    for d in domains:
        if (
            isinstance(d, str)
            and d
            and all(0x21 <= ord(c) <= 0x7E for c in d)
            and not any(c in d for c in ";'\"")
        ):
            seen.setdefault(d)
    return list(seen)


def _directive(name: str, trusted: list[str], domains: list[str]) -> str:
    return f"{name} {' '.join(dict.fromkeys([*trusted, *domains]))}"


def _csp_content(csp: dict[str, Any] | None, sandbox: str) -> str:
    csp = csp or {}
    resource_domains = _sanitize_csp_domains(csp.get("resourceDomains"))
    connect_domains = _sanitize_csp_domains(csp.get("connectDomains"))
    frame_domains = _sanitize_csp_domains(csp.get("frameDomains"))
    base_uri_domains = _sanitize_csp_domains(csp.get("baseUriDomains"))
    return "; ".join(
        [
            "default-src 'none'",
            f"sandbox {sandbox}",
            _directive("script-src", ["'self'", "'unsafe-inline'"], resource_domains),
            _directive("style-src", ["'self'", "'unsafe-inline'"], resource_domains),
            _directive("font-src", ["'self'"], resource_domains),
            _directive("img-src", ["'self'", "data:"], resource_domains),
            _directive("media-src", ["'self'", "data:"], resource_domains),
            f"connect-src {' '.join(connect_domains)}" if connect_domains else "connect-src 'none'",
            f"frame-src {' '.join(frame_domains)}" if frame_domains else "frame-src 'none'",
            "worker-src 'none'",
            "object-src 'none'",
            f"base-uri {' '.join(base_uri_domains)}" if base_uri_domains else "base-uri 'self'",
        ]
    )


def build_csp_meta_content(csp: dict[str, Any] | None) -> str:
    """CSP for the HOST page document (the page holding the session capability)."""
    return _csp_content(csp, APP_SANDBOX)


def build_sandbox_resource_csp(csp: dict[str, Any] | None) -> str:
    """CSP for provider HTML navigated on the isolated proxy origin."""
    return _csp_content(csp, APP_INNER_SANDBOX)


def build_sandbox_proxy_html(parent_origin: str, resource_path: str, allow_attribute: str) -> str:
    """The static relay document served on the SECOND loopback origin — ported
    verbatim from buildSandboxProxyHtml (sandbox-proxy-template.ts)."""
    parent_origin_json = safe_inline_json(parent_origin)
    resource_path_json = safe_inline_json(resource_path)
    allow = escape_html_attribute(allow_attribute)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MCP App Sandbox</title>
  <style>
    html, body {{ margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; background: transparent; }}
    iframe {{ display: block; width: 100%; height: 100%; border: 0; }}
  </style>
</head>
<body>
  <iframe id="mcp-app" title="MCP App" sandbox="{APP_INNER_SANDBOX}" allow="{allow}" referrerpolicy="no-referrer"></iframe>
  <script>
    const EXPECTED_PARENT_ORIGIN = {parent_origin_json};
    const RESOURCE_PATH = {resource_path_json};
    const SANDBOX_PROXY_READY_METHOD = "ui/notifications/sandbox-proxy-ready";
    const SANDBOX_RESOURCE_READY_METHOD = "ui/notifications/sandbox-resource-ready";
    const MAX_PENDING_MESSAGES = 64;
    const innerFrame = document.getElementById("mcp-app");
    const pendingToInner = [];
    let innerReady = false;

    innerFrame.addEventListener("load", () => {{
      if (innerFrame.getAttribute("src") === RESOURCE_PATH) flushPendingMessages();
    }});

    const isObject = (value) => value !== null && typeof value === "object";
    const isMessageFromParent = (event) =>
      event.source === window.parent && event.origin === EXPECTED_PARENT_ORIGIN;
    const isMessageFromInner = (event) =>
      event.source === innerFrame.contentWindow && event.origin === window.location.origin;

    const postToParent = (data) => {{
      window.parent.postMessage(data, EXPECTED_PARENT_ORIGIN);
    }};

    const postToInner = (data) => {{
      if (!innerReady || !innerFrame.contentWindow) {{
        if (pendingToInner.length < MAX_PENDING_MESSAGES) pendingToInner.push(data);
        return;
      }}
      innerFrame.contentWindow.postMessage(data, window.location.origin);
    }};

    const flushPendingMessages = () => {{
      if (!innerFrame.contentWindow) return;
      innerReady = true;
      for (const data of pendingToInner.splice(0)) {{
        innerFrame.contentWindow.postMessage(data, window.location.origin);
      }}
    }};

    window.addEventListener("message", (event) => {{
      if (isMessageFromParent(event)) {{
        const data = event.data;
        if (!isObject(data)) return;
        if (data.method === SANDBOX_RESOURCE_READY_METHOD) {{
          innerReady = false;
          innerFrame.setAttribute("src", RESOURCE_PATH);
          return;
        }}
        if (data.method === SANDBOX_PROXY_READY_METHOD) return;
        if (typeof data.method === "string" && data.method.startsWith("ui/notifications/sandbox-")) return;
        postToInner(data);
        return;
      }}

      if (!isMessageFromInner(event)) return;
      const data = event.data;
      if (!isObject(data)) return;
      if (typeof data.method === "string" && data.method.startsWith("ui/notifications/sandbox-")) return;
      postToParent(data);
    }});

    postToParent({{
      jsonrpc: "2.0",
      method: SANDBOX_PROXY_READY_METHOD,
      params: {{}},
    }});
  </script>
</body>
</html>"""


def build_sandbox_proxy_csp() -> str:
    """CSP for the proxy document itself (relay script only)."""
    return "; ".join(
        [
            "default-src 'none'",
            "script-src 'unsafe-inline'",
            "style-src 'unsafe-inline'",
            "frame-src 'self'",
            "connect-src 'none'",
            "worker-src 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            f"sandbox {APP_PROXY_SANDBOX}",
        ]
    )


_HOST_PAGE_STYLES = """
    /* Look & feel follows svg-mcp's preview widget (dark chrome, glowing
       status dot, filled buttons) so every mcp-app surface in the ecosystem
       reads as one family. Dark-only by design, like svg-mcp. */
    :root {
      color-scheme: dark;
      --bg: #1a1b1e;
      --surface: #232428;
      --surface-raised: #2f3136;
      --surface-hover: #3a3d43;
      --text: #e6e6e6;
      --muted: #9aa0a6;
      --accent: #3b5070;
      --accent-border: #4b6a96;
      --border: #34363b;
      --button-border: #44464c;
      --good: #57ab5a;
      --warn: #f0883e;
      --bad: #f87171;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; height: 100%; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    body { display: flex; flex-direction: column; min-height: 100vh; min-height: 100dvh; font-size: 13px; line-height: 1.4; }
    header { background: var(--surface); border-bottom: 1px solid var(--border); padding: calc(8px + env(safe-area-inset-top, 0px)) calc(14px + env(safe-area-inset-right, 0px)) 8px calc(14px + env(safe-area-inset-left, 0px)); display: flex; align-items: center; justify-content: space-between; gap: 14px; flex: none; }
    .title { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
    .server { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; white-space: nowrap; }
    .tool { font-size: 14px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .badge { border: 1px solid var(--button-border); border-radius: 999px; padding: 2px 8px; font-size: 11px; color: var(--muted); white-space: nowrap; }
    .controls { display: flex; gap: 8px; align-items: center; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--warn);
           box-shadow: 0 0 6px var(--warn); transition: background .2s, box-shadow .2s; flex: none; }
    .dot.live { background: var(--good); box-shadow: 0 0 6px var(--good); }
    .dot.dead { background: var(--bad); box-shadow: 0 0 6px var(--bad); }
    .status { font-size: 12px; color: var(--muted); white-space: nowrap; }
    button { border: 1px solid var(--button-border); background: var(--surface-raised); color: var(--text); border-radius: 6px; padding: 4px 9px; cursor: pointer; font: inherit; font-size: 12px; }
    button.primary { border-color: color-mix(in srgb, var(--good) 40%, var(--button-border) 60%); color: var(--good); }
    button.danger { border-color: color-mix(in srgb, var(--bad) 40%, var(--button-border) 60%); color: var(--bad); }
    button:hover { background: var(--surface-hover); }
    button.on { background: var(--accent); border-color: var(--accent-border); }
    main { flex: 1; min-height: 0; padding: 10px; display: flex; }
    iframe { width: 100%; height: 100%; border: 1px solid var(--border); border-radius: 10px; background: white; }
    .overlay { position: fixed; inset: 0; background: color-mix(in srgb, var(--bg) 90%, black 10%); display: none; align-items: center; justify-content: center; z-index: 2; padding: 16px; }
    .overlay.visible { display: flex; }
    .panel { width: min(680px, calc(100vw - 40px)); background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px; }
    .panel h2 { margin: 0 0 8px; font-size: 16px; }
    .panel p { margin: 0; color: var(--muted); line-height: 1.4; font-size: 14px; white-space: pre-wrap; }
"""


def build_host_html(
    *,
    session_token: str,
    server_name: str,
    tool_name: str,
    tool_args: dict[str, Any] | None,
    resource_meta: dict[str, Any],
    allow_attribute: str,
    sandbox_relay_port: int,
    sandbox_proxy_path_query: str,
    host_context: dict[str, Any] | None = None,
    require_tool_consent: bool = False,
    cache_tool_consent: bool = True,
    app_bridge_module_url: str = APP_BRIDGE_MODULE_URL,
    ui_base: str = "",
) -> str:
    """The host page — ported from buildHostHtmlTemplate (host-html-template.ts).
    Stage-1 deltas, both structurally inert to the bridge contract:
    - the streaming constants carry the adapter's key but no stream session exists
      (Stage 2); /events supplies no stream events yet
    - consent defaults to the widget's own confirm() dialog (REQUIRE false)
    """
    meta = resource_meta or {}
    csp = meta.get("csp")
    permissions = meta.get("permissions")
    resource_csp = safe_inline_json(csp)
    resource_permissions = safe_inline_json(permissions)
    host_context_json = safe_inline_json(host_context or {})

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MCP UI - {escape_html(server_name)} / {escape_html(tool_name)}</title>
  <style>{_HOST_PAGE_STYLES}</style>
</head>
<body>
  <header>
    <div class="title">
      <span class="server">MCP · <span id="server-name"></span></span>
      <span class="tool" id="tool-name"></span>
      <span class="badge">Sandboxed</span>
    </div>
    <div class="controls">
      <span class="dot" id="dot" title="widget session status"></span>
      <span class="status" id="status">Loading UI...</span>
      <button class="primary" id="done-btn" title="Cmd/Ctrl+Enter">Done</button>
      <button class="danger" id="cancel-btn" title="Escape">Cancel</button>
    </div>
  </header>
  <main>
    <iframe id="mcp-app" sandbox="{APP_PROXY_SANDBOX}" referrerpolicy="no-referrer"></iframe>
  </main>
  <div class="overlay" id="error-overlay">
    <div class="panel">
      <h2>UI Error</h2>
      <p id="error-message"></p>
    </div>
  </div>
  <div class="overlay" id="completion-overlay">
    <div class="panel">
      <h2>Done</h2>
      <p>MCP UI session finished. You can close this page and return to Pi.</p>
    </div>
  </div>
  <script type="module">
    import {{ AppBridge, PostMessageTransport }} from {safe_inline_json(app_bridge_module_url)};

    const SESSION_TOKEN = {safe_inline_json(session_token)};
    const SERVER_NAME = {safe_inline_json(server_name)};
    const TOOL_NAME = {safe_inline_json(tool_name)};
    const TOOL_ARGS = {safe_inline_json(tool_args or {})};
    const HOST_CONTEXT = {host_context_json};
    const ALLOW_ATTRIBUTE = {safe_inline_json(allow_attribute)};
    const REQUIRE_TOOL_CONSENT = {safe_inline_json(require_tool_consent)};
    const CACHE_TOOL_CONSENT = {safe_inline_json(cache_tool_consent)};
    const SANDBOX_RELAY_PORT = {sandbox_relay_port};
    const SANDBOX_PROXY_PATH_QUERY = {safe_inline_json(sandbox_proxy_path_query)};
    // Client-side origin: location.hostname makes the SAME page work over
    // loopback, a LAN bind, or a tailscale address — no server-side rewrite.
    const SANDBOX_PROXY_URL = location.protocol + "//" + location.hostname + ":" + SANDBOX_RELAY_PORT + SANDBOX_PROXY_PATH_QUERY;
    const RESOURCE_CSP = {resource_csp};
    const RESOURCE_PERMISSIONS = {resource_permissions};
    const INNER_SANDBOX = {safe_inline_json(APP_INNER_SANDBOX)};
    // All fetch/EventSource paths are token-scoped under /ui/<token>/ — this page
    // is mounted BELOW the combiner root, so root-relative URLs would 404.
    const UI_BASE = {safe_inline_json(ui_base)};
    const STREAM_CONTEXT_KEY = "mcp-combiner/stream";
    const STREAM_PATCH_METHOD = "notifications/mcp-combiner/ui-result-patch";

    const iframe = document.getElementById("mcp-app");
    const statusNode = document.getElementById("status");
    const doneBtn = document.getElementById("done-btn");
    const cancelBtn = document.getElementById("cancel-btn");
    const errorOverlay = document.getElementById("error-overlay");
    const completionOverlay = document.getElementById("completion-overlay");
    const errorMessage = document.getElementById("error-message");
    const sandboxProxyOrigin = new URL(SANDBOX_PROXY_URL).origin;

    document.getElementById("server-name").textContent = SERVER_NAME;
    document.getElementById("tool-name").textContent = TOOL_NAME;

    const dotNode = document.getElementById("dot");
    const setDot = (state) => {{
      // svg-mcp's status-dot vocabulary: warn = connecting, good = live,
      // bad = errored. Purely presentational — the status text carries detail.
      dotNode.className = "dot" + (state ? " " + state : "");
    }};

    const setStatus = (text, isError = false) => {{
      statusNode.textContent = text;
      statusNode.style.color = isError ? "var(--bad)" : "var(--muted)";
      if (isError) setDot("dead");
    }};

    const showError = (message) => {{
      errorMessage.textContent = message;
      errorOverlay.classList.add("visible");
      setStatus("Error", true);
    }};

    let completionPending = false;
    const showCompletion = () => {{
      completionOverlay.classList.add("visible");
      setStatus("Complete");
    }};
    const closeOrShowDone = () => {{
      completionPending = true;
      window.close();
      setTimeout(() => {{
        if (!document.hidden) {{
          showCompletion();
        }}
      }}, 1000);
    }};
    document.addEventListener("visibilitychange", () => {{
      if (completionPending && !document.hidden) {{
        showCompletion();
      }}
    }});

    const post = async (endpoint, params) => {{
      const response = await fetch(UI_BASE + endpoint, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ token: SESSION_TOKEN, params }}),
      }});

      const body = await response.json().catch(() => ({{ ok: false, error: "Invalid JSON response" }}));
      if (!response.ok || !body.ok) {{
        const message = body.error || ("HTTP " + response.status);
        throw new Error(message);
      }}
      return body.result ?? {{}};
    }};

    let consentGranted = !REQUIRE_TOOL_CONSENT;

    const bridge = new AppBridge(
      null,
      {{ name: "mcp-combiner", version: "1.0.0" }},
      {{
        serverTools: {{}},
        openLinks: {{}},
        logging: {{}},
        updateModelContext: {{}},
        message: {{}},
        sandbox: {{
          ...(RESOURCE_CSP ? {{ csp: RESOURCE_CSP }} : {{}}),
          ...(RESOURCE_PERMISSIONS ? {{ permissions: RESOURCE_PERMISSIONS }} : {{}}),
        }},
      }},
      {{ hostContext: HOST_CONTEXT }}
    );

    let sandboxResourceSent = false;
    bridge.onsandboxready = () => {{
      if (sandboxResourceSent) return;
      sandboxResourceSent = true;
      void bridge.sendSandboxResourceReady({{
        html: "",
        sandbox: INNER_SANDBOX,
        ...(RESOURCE_CSP ? {{ csp: RESOURCE_CSP }} : {{}}),
        ...(RESOURCE_PERMISSIONS ? {{ permissions: RESOURCE_PERMISSIONS }} : {{}}),
      }}).catch((error) => {{
        showError("Failed to load MCP App resource: " + String(error));
      }});
    }};

    bridge.oncalltool = async (params) => {{
      if (!consentGranted) {{
        const accepted = window.confirm("Allow this UI to call server tools for this session?");
        if (!accepted) {{
          await post("/proxy/ui/consent", {{ approved: false }}).catch(() => {{}});
          return {{
            isError: true,
            content: [{{ type: "text", text: "Tool call denied by user." }}],
          }};
        }}
        await post("/proxy/ui/consent", {{ approved: true }});
        if (CACHE_TOOL_CONSENT) {{
          consentGranted = true;
        }}
      }}
      const result = await post("/proxy/tools/call", params);
      await post("/proxy/ui/generated-tool-call-intent", {{
        tool: params.name,
        arguments: params.arguments,
        isError: result.isError
      }}).catch(() => {{}});
      return result;
    }};

    bridge.onmessage = async (params) => post("/proxy/ui/message", params);
    bridge.onupdatemodelcontext = async (params) => post("/proxy/ui/context", params);

    window.addEventListener("message", async (event) => {{
      if (event.source !== iframe.contentWindow || event.origin !== sandboxProxyOrigin) return;
      const data = event.data;
      if (!data || typeof data !== "object") return;

      if (data.jsonrpc || (typeof data.method === "string" && (data.method.startsWith("app/") || data.method.startsWith("host/")))) return;

      const msgType = data.type;
      if (typeof msgType !== "string") return;

      if (msgType === "notify" || msgType === "prompt" || msgType === "intent" || msgType === "message") {{
        const {{ type: _, payload, ...directFields }} = data;
        await post("/proxy/ui/message", {{ type: msgType, ...directFields, ...(payload || {{}}) }}).catch(() => {{}});
      }} else if (!msgType.startsWith("ui-lifecycle-") && !msgType.startsWith("ui-message-")) {{
        const payload = data.payload || {{}};
        await post("/proxy/ui/message", {{
          type: "intent",
          intent: msgType,
          params: payload,
        }}).catch(() => {{}});
      }}
    }});
    bridge.ondownloadfile = async (params) => post("/proxy/ui/download-file", params);
    bridge.onrequestdisplaymode = async (params) => post("/proxy/ui/request-display-mode", params);
    bridge.onopenlink = async (params) => {{
      const result = await post("/proxy/ui/open-link", params);
      if (!result.isError) {{
        window.open(params.url, "_blank", "noopener,noreferrer");
        await post("/proxy/ui/message", {{
          type: "intent",
          intent: "open_link",
          params: {{ url: params.url }}
        }}).catch(() => {{}});
      }}
      return result;
    }};

    bridge.oninitialized = () => {{
      bridge.sendToolInput({{ arguments: TOOL_ARGS }});
      setStatus("Connected");
    }};

    bridge.onsizechange = ({{ width, height }}) => {{
      if (typeof width === "number" && width > 0) {{
        iframe.style.minWidth = Math.min(width, window.innerWidth - 24) + "px";
      }}
      if (typeof height === "number" && height > 0) {{
        iframe.style.height = Math.max(height, 320) + "px";
      }}
    }};

    if (ALLOW_ATTRIBUTE) {{
      iframe.setAttribute("allow", ALLOW_ATTRIBUTE);
    }}

    // Connect bridge BEFORE loading iframe to ensure we're listening when the app sends ui/initialize
    const sandboxMessageGuard = (event) => {{
      if (event.source === iframe.contentWindow && event.origin !== sandboxProxyOrigin) {{
        event.stopImmediatePropagation();
      }}
    }};
    window.addEventListener("message", sandboxMessageGuard, true);
    try {{
      const transport = new PostMessageTransport(iframe.contentWindow, iframe.contentWindow);
      await bridge.connect(transport);
    }} catch (error) {{
      console.error("[host] Bridge connection failed:", error);
      showError("Failed to initialize AppBridge: " + String(error));
    }}

    const iframeLoaded = new Promise((resolve) => {{
      iframe.onload = resolve;
    }});
    iframe.src = SANDBOX_PROXY_URL;
    await iframeLoaded;

    const eventSource = new EventSource(UI_BASE + "/events?session=" + encodeURIComponent(SESSION_TOKEN));
    eventSource.onopen = () => setDot("live");
    eventSource.onerror = () => setDot("");
    eventSource.addEventListener("tool-input", (event) => {{
      try {{
        bridge.sendToolInput(JSON.parse(event.data));
      }} catch (error) {{
        showError("Failed to forward tool input: " + String(error));
      }}
    }});
    eventSource.addEventListener("tool-result", (event) => {{
      try {{
        bridge.sendToolResult(JSON.parse(event.data));
      }} catch (error) {{
        showError("Failed to forward tool result: " + String(error));
      }}
    }});
    eventSource.addEventListener("tool-cancelled", (event) => {{
      try {{
        bridge.sendToolCancelled(JSON.parse(event.data));
      }} catch (error) {{
        showError("Failed to forward cancellation: " + String(error));
      }}
    }});
    eventSource.addEventListener("resource-updated", () => {{
      setStatus("Resource updated on the server. Reopen this UI to load the latest version.");
    }});
    eventSource.addEventListener("host-context", (event) => {{
      try {{
        bridge.setHostContext(JSON.parse(event.data));
      }} catch {{}}
    }});
    eventSource.addEventListener("session-complete", async () => {{
      await bridge.teardownResource({{}}).catch(() => {{}});
      eventSource.close();
      closeOrShowDone();
    }});
    eventSource.onerror = () => {{
      setStatus("Connection lost", true);
    }};

    const heartbeat = setInterval(() => {{
      post("/proxy/ui/heartbeat", {{}}).catch(() => {{}});
    }}, 10000);

    const complete = async (reason) => {{
      try {{
        await post("/proxy/ui/complete", {{ reason }});
      }} catch {{}}
      try {{
        await bridge.teardownResource({{}});
      }} catch {{}}
      clearInterval(heartbeat);
      eventSource.close();
      closeOrShowDone();
    }};

    doneBtn.addEventListener("click", () => complete("done"));
    cancelBtn.addEventListener("click", () => complete("cancel"));
    window.addEventListener("keydown", (event) => {{
      if (event.key === "Escape") {{
        event.preventDefault();
        complete("cancel");
      }} else if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {{
        event.preventDefault();
        complete("done");
      }}
    }});
  </script>
</body>
</html>"""
