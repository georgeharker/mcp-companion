"""Sanctioned-restart handover: one-shot state transfer between combiner processes.

``mcp-combiner restart`` (the ONLY sanctioned restart) carries a small
payload from the dying combiner to its successor so infrastructure churn is
invisible to live chats. This is NOT persistence — the no-persist rule
stands (runtime control state is a cache of someone else's live intent):
the snapshot is a one-shot transfer artifact inside a single sanctioned
lifecycle operation, explicitly REQUESTED by ctl (``POST /handover/prepare``
sets flag+path; the lifespan finally writes it only when flagged — no crash
path can produce the file), consumed-and-deleted by the successor's
``--restore <path>``, refused on version/staleness mismatch (boot fresh +
log). Crash, ``sharedserver admin kill``, grace-period shutdown: no file,
fresh boot = config + supervisor re-assertion.

Payload (see the design decision and its Widening):

- ``token_filters`` — the canonical token-keyed filter store. A reconnecting
  chat presenting its token finds its filter already in place, closing the
  re-assert unfiltered-window residual.
- ``token_instances`` + ``instances`` — nvim chat↔editor binds and the
  instance registry snapshot. Cache-warming only: nvim's boot_id protocol
  re-asserts within seconds and overwrites; a dead instance's entry fails
  its first probe/route and is evicted by the channel's normal error path.
- ``parked`` — per-token upstream-session ids for isolate:true servers
  (two strings per server per token). The dying combiner PARKS its live
  isolated sessions (disconnect WITHOUT terminate) so the ids stay alive in
  the still-running backing servers; the successor loads them as parked
  entries and the first tokened use lazily resumes (probe, fresh-init
  fallback). Nothing is dialed at boot.

NOT carried, by decision: downstream sessions (clients re-init on 404),
negotiated state, tool caches, SSE event stores, session-scoped state of
tokenless sessions. File mode 600 (carries tokens).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from mcp_combiner.isolated import REGISTRY, ParkedSession
from mcp_combiner.runtime import RUNTIME

logger = logging.getLogger("mcp-combiner")

HANDOVER_VERSION = 1

# A handover file older than this is refused: the restart it belonged to has
# plainly not proceeded directly to this boot.
MAX_AGE_SECONDS = 120.0


async def write_handover(path: str) -> None:
    """Park all isolated sessions and write the handover payload (mode 600).

    Called from the lifespan shutdown ONLY when ctl flagged the handover.
    Parking (not terminating) is what keeps the upstream session ids alive
    for the successor to resume.
    """
    from mcp_combiner import nvim_proxy

    await REGISTRY.close_all(terminate=False)

    token_instances, instances = nvim_proxy.snapshot_routing()
    payload: dict[str, Any] = {
        "version": HANDOVER_VERSION,
        "created_at": time.time(),
        "boot_id": RUNTIME.boot_id,
        "token_filters": {
            token: sorted(servers)
            for token, servers in RUNTIME.sessions.pending_token_filters.items()
        },
        "token_instances": token_instances,
        "instances": instances,
        "parked": [
            {
                "server": server,
                "token": token,
                "session_id": parked.session_id,
                "protocol_version": parked.protocol_version,
                "url": parked.url,
                "headers": parked.headers,
            }
            for (server, token), parked in REGISTRY.parked.items()
        ],
    }

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    logger.info(
        "handover: wrote %s (%d filter(s), %d bind(s), %d instance(s), %d parked session(s))",
        path,
        len(payload["token_filters"]),
        len(payload["token_instances"]),
        len(payload["instances"]),
        len(payload["parked"]),
    )


def load_handover(path: str) -> bool:
    """Consume a handover file into the fresh combiner's stores.

    The file is DELETED regardless of outcome (one-shot: even a refused
    snapshot must not survive to a later boot). Version or staleness
    mismatch refuses the restore — the combiner boots fresh and logs why.
    Returns True when state was restored.
    """
    from mcp_combiner import nvim_proxy

    try:
        with open(path) as f:
            payload = json.load(f)
    except FileNotFoundError:
        logger.warning("handover: restore file %s not found — booting fresh", path)
        return False
    except Exception:
        logger.exception("handover: unreadable restore file %s — booting fresh", path)
        with _suppress_oserror():
            os.unlink(path)
        return False
    with _suppress_oserror():
        os.unlink(path)

    if payload.get("version") != HANDOVER_VERSION:
        logger.warning(
            "handover: version mismatch (%r != %d) — booting fresh",
            payload.get("version"),
            HANDOVER_VERSION,
        )
        return False
    age = time.time() - float(payload.get("created_at", 0))
    if not 0 <= age <= MAX_AGE_SECONDS:
        logger.warning("handover: snapshot is %.0fs old — booting fresh", age)
        return False

    for token, servers in dict(payload.get("token_filters", {})).items():
        RUNTIME.sessions.set_pending(str(token), {str(s) for s in servers})

    nvim_proxy.restore_routing(
        dict(payload.get("token_instances", {})),
        dict(payload.get("instances", {})),
    )

    parked_entries = list(payload.get("parked", []))
    for entry in parked_entries:
        REGISTRY.restore_parked(
            str(entry["server"]),
            str(entry["token"]),
            # parked_at stamps fresh: a restored session gets a full TTL
            # window from this boot, not the previous process's clock.
            ParkedSession(
                session_id=str(entry["session_id"]),
                protocol_version=(
                    str(entry["protocol_version"])
                    if entry.get("protocol_version") is not None
                    else None
                ),
                url=str(entry["url"]),
                headers=dict(entry.get("headers", {})),
            ),
        )

    logger.info(
        "handover: restored %d filter(s), %d bind(s), %d instance(s), %d parked session(s) "
        "from predecessor boot %s",
        len(payload.get("token_filters", {})),
        len(payload.get("token_instances", {})),
        len(payload.get("instances", {})),
        len(parked_entries),
        payload.get("boot_id", "?")[:8],
    )
    return True


def _suppress_oserror() -> Any:
    import contextlib

    return contextlib.suppress(OSError)
