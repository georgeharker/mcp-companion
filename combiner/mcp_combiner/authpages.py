"""The pages a person sees during an out-of-band OAuth authorization.

Opened on whatever device the authorization link reached — often a phone, via
a client's card — so they are self-contained (inline CSS and SVG, no external
fetches: the page may load inside a tailnet with no internet) and sized for a
mobile viewport. Three of them:

- the authorize page at ``/oauth/authorize/<id>`` (the short link): which
  server, what happens next, an Authorize button that continues to the
  provider, and a countdown to the link's expiry;
- the gone page: the link expired, was used, or never existed;
- the result page after the provider redirects back to the combiner
  (``/oauth/callback/<server>``): authorized, denied, or failed.
"""

from __future__ import annotations

import html
import math

from starlette.responses import HTMLResponse

#: The combiner mark: several inputs converging on one hub, one output — what
#: the combiner does with MCP servers. Inline, coloured by the page.
LOGO = """<svg class="mark" viewBox="0 0 64 64" role="img" aria-label="mcp-combiner">
  <rect x="2" y="2" width="60" height="60" rx="16" fill="var(--accent)"/>
  <g stroke="var(--on-accent)" stroke-width="3.5" stroke-linecap="round" fill="none">
    <path d="M15 18 C24 18 25 32 32 32"/>
    <path d="M15 32 H32"/>
    <path d="M15 46 C24 46 25 32 32 32"/>
    <path d="M32 32 H49"/>
  </g>
  <g fill="var(--on-accent)">
    <circle cx="15" cy="18" r="4"/><circle cx="15" cy="32" r="4"/>
    <circle cx="15" cy="46" r="4"/><circle cx="49" cy="32" r="4.5"/>
  </g>
  <circle cx="32" cy="32" r="7" fill="var(--on-accent)"/>
  <circle cx="32" cy="32" r="3.2" fill="var(--accent)"/>
</svg>"""

_STYLE = """
:root {
  --bg: #f4f5f7; --card: #ffffff; --text: #1d2330; --muted: #5d6676;
  --accent: #3b5bdb; --on-accent: #ffffff; --line: #e3e6ec;
  --ok: #2b8a3e; --warn: #c2410c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #11141a; --card: #1b2029; --text: #e8ebf1; --muted: #9aa3b2;
    --accent: #6b8cff; --on-accent: #0f1320; --line: #2b313c;
    --ok: #51cf66; --warn: #ff922b;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 24px 16px; background: var(--bg); color: var(--text);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
    sans-serif;
}
.card {
  width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--line);
  border-radius: 20px; padding: 28px 24px; box-shadow: 0 8px 30px rgba(0,0,0,.08);
  overflow-wrap: anywhere;
}
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }
.mark { width: 44px; height: 44px; flex: none; }
.wordmark { font-weight: 650; letter-spacing: -.01em; font-size: 17px; }
.wordmark span { color: var(--muted); font-weight: 500; }
h1 { font-size: 22px; line-height: 1.25; margin: 0 0 10px; letter-spacing: -.01em; }
p { margin: 0 0 14px; color: var(--muted); }
code {
  font: 600 .95em ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--text);
  background: var(--bg); padding: 1px 6px; border-radius: 6px;
}
.button {
  display: block; width: 100%; margin: 22px 0 10px; padding: 14px 18px; border-radius: 12px;
  background: var(--accent); color: var(--on-accent); text-align: center;
  text-decoration: none; font-weight: 650; font-size: 17px;
}
.button[aria-disabled="true"] { opacity: .45; pointer-events: none; }
.meta { font-size: 14px; color: var(--muted); text-align: center; margin: 0; }
.status {
  display: inline-flex; align-items: center; gap: 8px; font-weight: 600; margin-bottom: 10px;
}
.status.ok { color: var(--ok); }
.status.warn { color: var(--warn); }
.dot { width: 10px; height: 10px; border-radius: 50%; background: currentColor; }
"""


def _page(title: str, body: str, status: int = 200, script: str = "") -> HTMLResponse:
    document = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=robots content="noindex,nofollow">'
        f"<title>{html.escape(title)} · mcp-combiner</title><style>{_STYLE}</style></head>"
        '<body><main class="card">'
        f'<div class="brand">{LOGO}<div class="wordmark">mcp<span>-combiner</span></div></div>'
        f"{body}</main>{script}</body></html>"
    )
    # Pages carrying an authorization URL must not be cached or framed.
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src data:; frame-ancestors 'none'"
        ),
    }
    return HTMLResponse(document, status_code=status, headers=headers)


def authorize_page(server: str, provider_url: str, seconds_left: float) -> HTMLResponse:
    """The short link's page: who is asking, and the way on to the provider."""
    name = html.escape(server)
    seconds = max(0, math.floor(seconds_left))
    body = (
        f"<h1>Authorize <code>{name}</code></h1>"
        f"<p>Your MCP combiner needs your permission to connect to <strong>{name}</strong>. "
        "You'll sign in with the service and approve access; then you're sent back here.</p>"
        f'<a class="button" id="go" href="{html.escape(provider_url, quote=True)}" '
        f'rel="noreferrer">Authorize {name}</a>'
        f'<p class="meta" id="left" data-seconds="{seconds}">'
        f"This link expires in {_duration(seconds)}.</p>"
    )
    script = """<script>
(function () {
  var left = document.getElementById("left"), go = document.getElementById("go");
  var end = Date.now() + 1000 * Number(left.dataset.seconds);
  function fmt(s) { var m = Math.floor(s / 60), r = s % 60;
    return m ? m + " min " + (r < 10 ? "0" : "") + r + " s" : r + " s"; }
  function tick() {
    var s = Math.max(0, Math.round((end - Date.now()) / 1000));
    if (s === 0) {
      left.textContent = "This link has expired. Ask for a new one from your app.";
      go.setAttribute("aria-disabled", "true"); go.removeAttribute("href");
      return;
    }
    left.textContent = "This link expires in " + fmt(s) + ".";
    setTimeout(tick, 1000);
  }
  tick();
})();
</script>"""
    return _page(f"Authorize {server}", body, script=script)


def gone_page() -> HTMLResponse:
    """A short link that expired, was already used, or never existed."""
    body = (
        '<div class="status warn"><span class="dot"></span>Link no longer valid</div>'
        "<h1>This authorization link has expired or was already used</h1>"
        "<p>Links are good for 15 minutes and work once. Go back to your app and try "
        "again: the next request that needs this server issues a fresh link (an "
        "assistant can also call <code>combiner__authorize_server</code>).</p>"
    )
    return _page("Link expired", body, status=404)


def authorized_page(server: str) -> HTMLResponse:
    name = html.escape(server)
    body = (
        '<div class="status ok"><span class="dot"></span>Authorized</div>'
        f"<h1><code>{name}</code> is connected</h1>"
        "<p>You can close this page and return to your app. If a request was waiting "
        "on this, retry it.</p>"
    )
    return _page(f"{server} authorized", body)


def failed_page(server: str, reason: str, status: int) -> HTMLResponse:
    name = html.escape(server)
    body = (
        '<div class="status warn"><span class="dot"></span>Not authorized</div>'
        f"<h1>Authorizing <code>{name}</code> didn't complete</h1>"
        f"<p>{html.escape(reason)}</p>"
        "<p>Return to your app and try again for a fresh link.</p>"
    )
    return _page(f"{server} not authorized", body, status=status)


def _duration(seconds: int) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min {rest:02d} s" if minutes else f"{rest} s"
