# Advertised UI origin — widgets over remote clients (un-bien)

Status: **sketch (2026-10-04, post-discussion)** — the un-bien widget leg's
blocker. Companion doc:
[`interactive-resource-host.md`](./interactive-resource-host.md) (the host
design this extends).

## The problem

The UI host constructs loopback URLs — `http://127.0.0.1:9741/ui/<token>/…` —
which are correct for the desktop pi session (the browser auto-open path) and
dead for anything remote. The un-bien paired app receives widget URLs it
cannot reach: **a remote client can render the widget only if the origin is
routable from wherever the app runs.** The natural transport is Tailscale
(the user's devices share a tailnet), but any bind/advertise split works the
same way.

## What must become reachable (the URL inventory)

The host serves several path families, all currently under the combiner's
port (plus one second origin):

| surface | purpose | who must reach it |
|---|---|---|
| `/ui/<token>/?resource=<namespaced-uri>` | the host page (session-scoped sandbox page) | the renderer (browser **or** remote app webview) |
| `/events` (SSE) | the widget's live data stream, session-queued | the host page (from wherever it was loaded) |
| `/proxy/*` | the app-bridge action path (widget → host) | the host page |
| provider HTML on the **second loopback origin** | the widget content origin (isolated from the host page's origin) | the host page's iframe |
| `/ui/*` static assets (host page JS/CSS bundle) | the host page itself | the renderer |

Two consequences fall out of the inventory:

1. **Bind and advertise are different knobs.** The combiner already has
   `--host` (default `127.0.0.1`) — serving on a tailnet interface means
   binding `0.0.0.0` or the tailnet IP, which that flag already does. What's
   missing is the **advertised origin** used in URL *construction* — the
   token-bearing URLs embedded in tool results and hold announcements must
   name a host the renderer can reach, while local legs (browser auto-open)
   may prefer loopback.
2. **The second origin must be advertised too.** The provider-content origin
   (a second port today) is part of the URL construction; if it stays
   loopback while the host page loads from the tailnet origin, the iframe is
   cross-origin to an unreachable origin and the widget dies. Both origins
   ride the same advertise knob, derived: `advertised_origin` and
   `advertised_provider_origin`.

## The config shape

Machine-specific hostnames in shared config are an anti-pattern (the config is
synced across machines; a tailnet IP strands the remote leg when it changes).
So the default is **discovery**, and the config holds a *preference*, not an
address:

```jsonc
// servers.json
{
  "ui_host": {
    // URL construction only — NEVER affects binding.
    "advertise": "auto"
    // "auto" (default), two-tier at URL-construction time (re-resolved, cheap,
    //   so tailnet IP changes self-heal):
    //   1. probe tailscale's own daemon (tailscale ip -4 / the local socket API)
    //      — the authoritative answer when tailscaled runs;
    //   2. fall back to interface enumeration preferring 100.64.0.0/10 —
    //      Tailscale's reserved CGNAT range, with the caveat that some ISPs
    //      assign CGNAT addresses too (tethered/cellular), so this tier can
    //      false-positive; the daemon probe (tier 1) exists to avoid trusting it.
    //   Neither finds a tailnet → the bind host (loopback today).
    // "loopback": force local-only URLs (today's behavior).
    // "http://combiner.tailnet.ts.net:9741": explicit override — the only
    //   machine-specific form, for the tailscale-serve (TLS/MagicDNS) case.
    // Both origins (host + provider) derive from the SAME interface/resolution,
    // so the pair cannot drift.
    // Binding stays --host/--port (the ctl flags); serving a tailnet = bind 0.0.0.0.
  }
}
```

Rules:

- **One resolution helper, used by every constructor.** The URL builders
  (`meta_tools._url_for`, the middleware's hold announce, the extension's
  `withUiResource`/`read_*` URL-append) all flow through a single
  `advertised_origin` resolver so no path drifts. Local-only legs (browser
  auto-open on the desktop) can keep a loopback preference via the same
  resolver when the session is local — the knob is per-*construction-site*,
  not global.
- **The token stays in the URL path.** It is the UI host's scoping +
  auth-for-widget-sessions today; the advertise knob changes only the
  origin prefix.
- **Scheme follows the value** — `https://` when tailscale serve (or any
  terminator) fronts the host; the host page and SSE already speak relative
  URLs, so TLS termination at the tailnet edge needs no combiner changes.

## The security posture

- **Tailnet-only by intent.** The design assumption is a private overlay
  network (tailnet) where the network layer already authenticates devices;
  the token-in-path remains the session gate, as on loopback. Advertising a
  public origin is explicitly out of scope — the control routes
  (`/sessions/*`, `/handover`) keep their bearer gate, but the UI host's
  widget sessions were never designed for hostile networks.
- `/proxy/*` actions ride the widget's session scope exactly as on loopback;
  no auth model changes here, only reachability.

## Open questions

- **The un-bien app's renderer**: webview vs external browser — determines
  whether the app needs anything besides a reachable URL (CORS for the SSE
  stream is already permissive for same-origin host pages; a webview
  behaves like a browser and needs nothing new).
- **tailscale serve vs raw tailnet IP**: `advertise: "auto"` covers the raw-IP
  case with zero config; the tailscale-serve variant (TLS + MagicDNS names)
  is what the explicit override exists for. Whether `auto` should also probe
  `tailscale status` for a MagicDNS name (nicer URLs, soft dependency on the
  tailscale binary) is open.
- **Provider-origin discovery**: today the host page learns the provider
  origin from the resource read; confirm that flow carries the *advertised*
  provider origin (it must — this is the second knob's whole job).
- Whether the extension should offer a per-session origin preference (e.g.
  un-bien-presented sessions advertise remote, local sessions loopback) or
  whether one global advertised origin is honest enough to start.

## Non-goals

- No port *forwarding* machinery in the combiner — Tailscale already owns
  that layer; the combiner only needs to *name* the right origin.
- No change to the hold/fold flow, the token model, or the permission
  pipeline — this is a URL-construction feature.