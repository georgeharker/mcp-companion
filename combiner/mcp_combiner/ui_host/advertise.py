"""The advertised UI origin — what widget URLs NAME, as distinct from what the
combiner BINDS (docs/designs/advertised-ui-origin.md).

The remote-client widget leg (un-bien's paired app) cannot reach loopback URLs,
so URL *construction* must name an origin the renderer can route to — a
tailnet address being the designed case. Binding is untouched (`--host`): the
advertise knob changes only the origin prefix of `/ui/<token>/…` URLs.

Modes (MCP_COMBINER_UI_ADVERTISE, default ``auto``):

- ``auto`` — this machine's tailnet name, by :func:`tailnet_host`: (1) its
  MagicDNS name, else (2) its tailnet IP — both from one ``tailscale status
  --json``, looked up once per process (:func:`tailnet_info`) — then
  ``tailscale ip -4``, then (3) an interface address in
  Tailscale's reserved CGNAT range (100.64.0.0/10 — some ISPs assign CGNAT
  space too, which is why the daemon outranks it); none → the bind host
  (loopback).
- ``loopback`` — force local-only URLs (the pre-feature behavior).
- ``http://host[:port]`` — explicit override; ``{auto}`` as the host stands for
  the tailnet name (``https://{auto}`` for the tailscale-serve variant), so the
  setting stays portable. Braces can't occur in a real host name.

v1 resolves once at boot (tailnet IP changes are rare; a restart heals). The
design doc notes the refinement: a TTL cache behind the URL constructors so
IP changes self-heal without a restart.
"""

from __future__ import annotations

from mcp_combiner.tailnet import (  # re-exported: callers and tests import them here
    _TAILSCALE_V4_NET as _TAILSCALE_V4_NET,
)
from mcp_combiner.tailnet import (
    AUTO as AUTO,
)
from mcp_combiner.tailnet import (
    AUTO_HOST as AUTO_HOST,
)
from mcp_combiner.tailnet import (
    prefetch_tailnet_info as prefetch_tailnet_info,
)
from mcp_combiner.tailnet import (
    reset_tailnet_info as reset_tailnet_info,
)
from mcp_combiner.tailnet import (
    tailnet_host as tailnet_host,
)
from mcp_combiner.tailnet import (
    tailnet_info as tailnet_info,
)
from mcp_combiner.tailnet import (
    tailnet_ip as tailnet_ip,
)

_ENV = "MCP_COMBINER_UI_ADVERTISE"


def resolve_advertised_host(bind_host: str, mode: str | None = None) -> str:
    """The host widget URLs should name. URL construction only — never binding.

    ``mode`` wins over the env (callers resolving explicit config); both
    default to ``$MCP_COMBINER_UI_ADVERTISE`` (``auto``).
    """
    import os

    advertise = (mode or os.environ.get(_ENV) or AUTO).strip().lower()
    # Bind-all is a bind, not a name — never construct URLs with it.
    local = "127.0.0.1" if bind_host in ("0.0.0.0", "::", "") else bind_host
    if advertise in ("", AUTO):
        return tailnet_host() or local
    if advertise in ("loopback", "local", "none"):
        return bind_host
    # Explicit origin: accept scheme://host[:port] and a bare host alike.
    if "://" in advertise:
        advertise = advertise.split("://", 1)[1]
    advertise = advertise.rstrip("/")
    if AUTO_HOST in advertise:
        name = tailnet_host()
        if name is None:
            return local
        advertise = advertise.replace(AUTO_HOST, name)
    return advertise or bind_host


def advertised_origin(bind_host: str, port: int, mode: str | None = None) -> str:
    """The full advertised origin for the UI host page (``http://<host>:<port>``)."""
    host = resolve_advertised_host(bind_host, mode)
    if _has_port(host):
        return f"http://{host}"  # an explicit origin's own port wins
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # a bare IPv6 address
    return f"http://{host}:{port}"


def _has_port(host: str) -> bool:
    """Whether *host* already carries ``:port`` (``name:80``, ``[::1]:80``;
    a bare IPv6 address does not)."""
    head, sep, tail = host.rpartition(":")
    if not sep or not tail.isdigit():
        return False
    return head.endswith("]") or ":" not in head
