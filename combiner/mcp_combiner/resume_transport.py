"""Resumable streamable-HTTP client transport.

Attach to an EXISTING upstream MCP session instead of initializing a new one —
the client-side half of carrying isolated upstream sessions across downstream
churn and sanctioned combiner restarts. The upstream ``Mcp-Session-Id`` was
minted by the very server we reconnect to; we only replay it, which is the
spec's own session-continuation mechanism (the header rides every request).
Nothing is fabricated.

Mechanics:

- ``terminate_on_close`` — the SDK reads its knob of this name at CONNECT
  time, but parking decides at CLOSE time whether the upstream session should
  survive. So the SDK never terminates; we perform the close-time DELETE via
  the SDK transport's public ``terminate_session``, reading our own (mutable)
  ``terminate_on_close`` flag when the connection closes — flip it to
  ``False`` on a live client just before disconnecting to park.
- Seeding — the SDK transport's public ``session_id`` is what it stamps onto
  every request and what gates its standalone GET stream; it is normally
  captured from the initialize response. A resume sets it before the first
  request. The negotiated ``MCP-Protocol-Version`` (which the SDK only learns
  from the handshake) rides as a default header on the HTTP client we own.
- Skipping the handshake — ``fastmcp.Client(auto_initialize=False)``, public.
- The standalone GET stream (server-initiated notifications) starts when the
  client sends ``notifications/initialized``. The server treats a duplicate
  InitializedNotification as idempotent, so a resume simply re-sends it.

Two mirrors, both pinned by the round-trip regression tests in
tests/test_resume_transport.py:

- ``connect_session`` replicates fastmcp's
  ``StreamableHttpTransport.connect_session`` (transports/http.py) up to the
  HTTP client it builds.
- ``_sdk_streams`` replicates the body of mcp's ``streamable_http_client``
  (client/streamable_http.py). mcp 2 constructs its ``StreamableHTTPTransport``
  inside that function and no longer hands it out, so seeding it requires
  constructing it ourselves; this uses two private SDK helpers
  (``create_context_streams``, ``resync_tracer``) exactly as the original does.
"""

from __future__ import annotations

import contextlib
import logging
import ssl
from collections.abc import AsyncIterator
from typing import Any, Literal

import anyio
import httpx2
import mcp.types as mt
from fastmcp.client.dependencies import _merge_forwardable_http_headers
from fastmcp.client.transports.base import SessionKwargs, TransportOptions
from fastmcp.client.transports.http import StreamableHttpTransport
from mcp import ClientSession
from mcp.client._transport import TransportStreams
from mcp.client.streamable_http import StreamableHTTPTransport
from mcp.shared._compat import resync_tracer
from mcp.shared._context_streams import create_context_streams
from mcp.shared._httpx_utils import McpHttpClientFactory, create_mcp_http_client
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from mcp.shared.message import SessionMessage
from pydantic import AnyUrl
from typing_extensions import Unpack

from mcp_combiner.runtime import MAX_SSE_EVENT_BYTES

logger = logging.getLogger("mcp-combiner")


@contextlib.asynccontextmanager
async def _sdk_streams(
    transport: StreamableHTTPTransport, client: httpx2.AsyncClient
) -> AsyncIterator[TransportStreams]:
    """Run *transport* over *client* — mcp's ``streamable_http_client`` body,
    minus constructing the transport and minus its close-time terminate (the
    caller owns both)."""
    read_stream_writer, read_stream = create_context_streams[SessionMessage | Exception](0)
    write_stream, write_stream_reader = create_context_streams[SessionMessage](0)
    async with (
        read_stream_writer,
        read_stream,
        write_stream,
        write_stream_reader,
        anyio.create_task_group() as tg,
    ):

        def start_get_stream() -> None:
            tg.start_soon(transport.handle_get_stream, client, read_stream_writer)

        tg.start_soon(
            transport.post_writer,
            client,
            write_stream_reader,
            read_stream_writer,
            write_stream,
            start_get_stream,
            tg,
        )
        try:
            yield read_stream, write_stream
        finally:
            tg.cancel_scope.cancel()
    await resync_tracer()


class ResumableStreamableHttpTransport(StreamableHttpTransport):
    """``StreamableHttpTransport`` that can reattach to an existing session.

    With ``resume_session_id`` set, connecting seeds the SDK transport with
    the stored session identity — pair it with ``Client(auto_initialize=False)``
    so no fresh initialize is sent — and re-sends ``notifications/initialized``
    to start the GET stream. Without it, behaves exactly like the parent except
    that ``terminate_on_close`` is honoured, and read at CLOSE time (mutable
    per instance), so a live connection can be parked by flipping the flag
    before disconnecting.
    """

    def __init__(
        self,
        url: str | AnyUrl,
        headers: dict[str, str] | None = None,
        auth: httpx2.Auth | Literal["oauth"] | str | None = None,
        httpx_client_factory: McpHttpClientFactory | None = None,
        verify: ssl.SSLContext | bool | str | None = None,
        *,
        max_sse_event_size: int | None = MAX_SSE_EVENT_BYTES,
        resume_session_id: str | None = None,
        resume_protocol_version: str | None = None,
        terminate_on_close: bool = True,
    ) -> None:
        super().__init__(
            url,
            headers=headers,
            auth=auth,
            httpx_client_factory=httpx_client_factory,
            verify=verify,
            max_sse_event_size=max_sse_event_size,
        )
        self.resume_session_id = resume_session_id
        self.resume_protocol_version = resume_protocol_version
        self.terminate_on_close = terminate_on_close

    @contextlib.asynccontextmanager
    async def connect_session(
        self,
        *,
        transport_options: TransportOptions | None = None,
        **session_kwargs: Unpack[SessionKwargs],
    ) -> AsyncIterator[ClientSession]:
        options = transport_options or TransportOptions()
        # ---- mirrored from fastmcp StreamableHttpTransport.connect_session ----
        if options.forward_incoming_headers:
            headers = _merge_forwardable_http_headers(self.headers)
        else:
            headers = dict(self.headers)
        if self.resume_session_id is not None and self.resume_protocol_version is not None:
            # The SDK stamps the negotiated version only after a handshake it
            # saw; a resumed session skips it, so carry the version ourselves.
            headers[MCP_PROTOCOL_VERSION_HEADER] = self.resume_protocol_version

        timeout: httpx2.Timeout | None = None
        read_timeout_seconds = session_kwargs.get("read_timeout_seconds")
        if read_timeout_seconds is not None:
            timeout = httpx2.Timeout(30.0, read=read_timeout_seconds)

        verify_factory = self._make_verify_factory()
        if self.httpx_client_factory is not None:
            # fastmcp's factory contract is WIDER than the SDK's
            # McpHttpClientFactory Protocol: factories "must accept headers,
            # auth, follow_redirects, and optionally timeout" (see the
            # StreamableHttpTransport docstring). Pass them as a mapping so we
            # honour that documented contract without misdeclaring the SDK's.
            factory_kwargs: dict[str, Any] = {
                "headers": headers,
                "auth": self.auth,
                "follow_redirects": True,
            }
            if timeout:
                factory_kwargs["timeout"] = timeout
            http_client = self.httpx_client_factory(**factory_kwargs)
        elif verify_factory is not None:
            http_client = verify_factory(headers=headers, timeout=timeout, auth=self.auth)
        else:
            http_client = create_mcp_http_client(headers=headers, timeout=timeout, auth=self.auth)

        self._session_id = None
        http_client.event_hooks.setdefault("response", []).append(self._capture_session_id)
        # ---- end mirror; the changed part follows --------------------------

        sdk_transport = StreamableHTTPTransport(
            self.url, max_sse_event_size=self.max_sse_event_size
        )
        if self.resume_session_id is not None:
            # Seed BEFORE anything is sent.
            sdk_transport.session_id = self.resume_session_id
            self._session_id = self.resume_session_id
            logger.debug("resume: attached to upstream session %s…", self.resume_session_id[:8])

        async with http_client:
            try:
                async with (
                    _sdk_streams(sdk_transport, http_client) as (read_stream, write_stream),
                    options.session_class(read_stream, write_stream, **session_kwargs) as session,
                ):
                    if self.resume_session_id is not None:
                        # Re-announce initialized: idempotent server-side, and
                        # the SDK client starts its standalone GET stream
                        # (server-initiated notifications) on exactly this send.
                        await session.send_notification(
                            mt.InitializedNotification(method="notifications/initialized")
                        )
                    yield session
            finally:
                # terminate_on_close is OUR close-time decision (see module
                # docstring): DELETE iff the flag still says so now.
                if self.terminate_on_close:
                    await sdk_transport.terminate_session(http_client)
