"""Resumable streamable-HTTP client transport.

Attach to an EXISTING upstream MCP session instead of initializing a new one —
the client-side half of carrying isolated upstream sessions across downstream
churn and sanctioned combiner restarts. The upstream ``Mcp-Session-Id`` was
minted by the very server we reconnect to; we only replay it, which is the
spec's own session-continuation mechanism (the header rides every request).
Nothing is fabricated.

Mechanics (all but one piece public API):

- ``terminate_on_close`` — the SDK reads its knob of this name at CONNECT
  time, but parking decides at CLOSE time whether the upstream session should
  survive. So we always pass the SDK ``False`` and perform the close-time
  DELETE ourselves via the public ``terminate_session`` method, reading our
  own (mutable) ``terminate_on_close`` flag when the connection closes —
  flip it to ``False`` on a live client just before disconnecting to park.
- Seeding — the SDK transport keeps session identity as two plain attributes
  (``session_id``, ``protocol_version``) captured from the initialize
  exchange and stamped onto every request's headers. A resume sets them
  before the first request. The transport object is reached via the
  ``get_session_id`` bound method the SDK yields (``__self__``).
- Skipping the handshake — ``fastmcp.Client(auto_initialize=False)``, public.
- The standalone GET stream (server-initiated notifications) normally starts
  when the client sends ``notifications/initialized``. The server treats a
  duplicate InitializedNotification as idempotent (it re-sets an already-set
  state), so a resume simply re-sends it: this kicks the GET stream via the
  SDK's own trigger, no internals reached.

The one genuine mirror: ``connect_session`` replicates the body of fastmcp's
``StreamableHttpTransport.connect_session`` (transports/http.py) to change
one call — passing ``terminate_on_close`` and seeding after connect. Drift
risk on fastmcp upgrades is confined to this file and pinned by the
round-trip regression test in tests/test_resume_transport.py.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
from collections.abc import AsyncIterator
from typing import cast

import httpx
import mcp.types as mt
from fastmcp.client.dependencies import get_http_headers
from fastmcp.client.transports.base import SessionKwargs
from fastmcp.client.transports.http import StreamableHttpTransport
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from typing_extensions import Unpack

logger = logging.getLogger("mcp-combiner")


class ResumableStreamableHttpTransport(StreamableHttpTransport):
    """``StreamableHttpTransport`` that can reattach to an existing session.

    With ``resume_session_id`` set, connecting seeds the SDK transport with
    the stored session identity, skips nothing else — pair it with
    ``Client(auto_initialize=False)`` so no fresh initialize is sent — and
    re-sends ``notifications/initialized`` to start the GET stream. Without
    it, behaves exactly like the parent except that ``terminate_on_close``
    is honoured, and read at CLOSE time (mutable per instance), so a live
    connection can be parked by flipping the flag before disconnecting.
    """

    def __init__(
        self,
        *args: object,
        resume_session_id: str | None = None,
        resume_protocol_version: str | None = None,
        terminate_on_close: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.resume_session_id = resume_session_id
        self.resume_protocol_version = resume_protocol_version
        self.terminate_on_close = terminate_on_close

    @contextlib.asynccontextmanager
    async def connect_session(
        self, **session_kwargs: Unpack[SessionKwargs]
    ) -> AsyncIterator[ClientSession]:
        # ---- mirrored from fastmcp StreamableHttpTransport.connect_session ----
        if self.forward_incoming_headers:
            headers = get_http_headers(include={"authorization"}) | self.headers
        else:
            headers = dict(self.headers)

        timeout: httpx.Timeout | None = None
        if session_kwargs.get("read_timeout_seconds") is not None:
            read_timeout_seconds = cast(
                datetime.timedelta, session_kwargs.get("read_timeout_seconds")
            )
            timeout = httpx.Timeout(30.0, read=read_timeout_seconds.total_seconds())

        verify_factory = self._make_verify_factory()
        if self.httpx_client_factory is not None:
            http_client = self.httpx_client_factory(
                headers=headers,
                auth=self.auth,
                follow_redirects=True,  # type: ignore[call-arg]
                **({"timeout": timeout} if timeout else {}),
            )
        elif verify_factory is not None:
            http_client = verify_factory(headers=headers, timeout=timeout, auth=self.auth)
        else:
            http_client = create_mcp_http_client(headers=headers, timeout=timeout, auth=self.auth)
        # ---- end mirror; the changed part follows --------------------------

        async with (
            http_client,
            # terminate_on_close is OUR close-time decision (see module
            # docstring): the SDK never terminates; we DELETE in the finally
            # below iff the flag still says so when the connection closes.
            streamable_http_client(
                self.url,
                http_client=http_client,
                terminate_on_close=False,
            ) as transport,
        ):
            read_stream, write_stream, get_session_id = transport
            self._get_session_id_cb = get_session_id
            # The SDK transport is the bound method's receiver; session
            # identity lives in two plain attributes it stamps onto every
            # request's headers.
            sdk_transport = get_session_id.__self__  # type: ignore[attr-defined]

            if self.resume_session_id is not None:
                # Seed BEFORE anything is sent.
                sdk_transport.session_id = self.resume_session_id
                if self.resume_protocol_version is not None:
                    sdk_transport.protocol_version = self.resume_protocol_version
                logger.debug(
                    "resume: attached to upstream session %s…",
                    self.resume_session_id[:8],
                )

            try:
                async with ClientSession(read_stream, write_stream, **session_kwargs) as session:
                    if self.resume_session_id is not None:
                        # Re-announce initialized: idempotent server-side, and
                        # the SDK client starts its standalone GET stream
                        # (server-initiated notifications) on exactly this send.
                        await session.send_notification(
                            mt.ClientNotification(
                                mt.InitializedNotification(method="notifications/initialized")
                            )
                        )
                    yield session
            finally:
                if self.terminate_on_close:
                    await sdk_transport.terminate_session(http_client)
