"""Paginated list responses (mcp_combiner.listpaging)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ProcFactory, http_mock_entry, write_servers_config
from fastmcp import Client
from fastmcp.resources import Resource

from mcp_combiner import listpaging
from mcp_combiner.listpaging import PAGE_SIZE_ENV, ResourceListSnapshots, list_page_size
from mcp_combiner.runtime import CLIENT_CONNECT_MODE


class TestPageSize:
    def test_default_and_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PAGE_SIZE_ENV, raising=False)
        assert list_page_size() == listpaging.DEFAULT_PAGE_SIZE
        monkeypatch.setenv(PAGE_SIZE_ENV, "50")
        assert list_page_size() == 50
        monkeypatch.setenv(PAGE_SIZE_ENV, "0")
        assert list_page_size() is None
        monkeypatch.setenv(PAGE_SIZE_ENV, "lots")
        assert list_page_size() == listpaging.DEFAULT_PAGE_SIZE


class TestSnapshots:
    def _resources(self, n: int) -> list[Resource]:
        return [
            Resource.from_function(lambda: "x", uri=f"mem://r/{i}", name=f"r{i}") for i in range(n)
        ]

    def test_keeps_per_walk_and_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snaps = ResourceListSnapshots()
        clock = [1000.0]
        monkeypatch.setattr("mcp_combiner.listpaging.time.monotonic", lambda: clock[0])
        a, b = self._resources(2), self._resources(3)
        snaps.keep("token:a", a)
        snaps.keep("token:b", b)
        assert snaps.get("token:a") is a and snaps.get("token:b") is b
        clock[0] += listpaging.SNAPSHOT_TTL_S
        assert snaps.get("token:a") is None

    def test_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snaps = ResourceListSnapshots()
        clock = [0.0]
        monkeypatch.setattr("mcp_combiner.listpaging.time.monotonic", lambda: clock[0])
        for i in range(listpaging._MAX_SNAPSHOTS + 5):
            clock[0] += 0.01
            snaps.keep(f"token:{i}", [])
        assert snaps.get("token:0") is None
        assert snaps.get(f"token:{listpaging._MAX_SNAPSHOTS + 4}") == []


@pytest.mark.e2e
async def test_lists_page_and_clients_get_everything(procs: ProcFactory, tmp_path: Path) -> None:
    mock = await procs.start_http_mock("mockup")
    config = write_servers_config(tmp_path / "servers.json", {"mockup": http_mock_entry(mock.port)})
    combiner = await procs.start_combiner(config, env={PAGE_SIZE_ENV: "1"})
    await combiner.wait_server_state("mockup", ("ready",))
    url = f"{combiner.mcp_url}/paging-0001"
    async with Client(url, mode=CLIENT_CONNECT_MODE) as c:
        everything = {str(r.uri) for r in await c.list_resources()}
        assert len(everything) >= 2, everything

        # Walk it by hand: one resource per page, every one once, then no cursor.
        seen: list[str] = []
        page = await c.list_resources_mcp()
        while True:
            assert len(page.resources) == 1
            seen.extend(str(r.uri) for r in page.resources)
            if page.next_cursor is None:
                break
            page = await c.list_resources_mcp(cursor=page.next_cursor)
        assert sorted(seen) == sorted(everything)

        # Tools page too, and the auto-paginating client still sees them all.
        first = await c.list_tools_mcp()
        assert len(first.tools) == 1 and first.next_cursor is not None
        assert len(await c.list_tools()) > 1


@pytest.mark.e2e
async def test_resource_filter_narrows_a_servers_listing(
    procs: ProcFactory, tmp_path: Path
) -> None:
    mock = await procs.start_http_mock("mockup")
    unfiltered = write_servers_config(
        tmp_path / "plain.json", {"mockup": http_mock_entry(mock.port)}
    )
    plain = await procs.start_combiner(unfiltered)
    await plain.wait_server_state("mockup", ("ready",))
    async with Client(f"{plain.mcp_url}/rfilter-0000", mode=CLIENT_CONNECT_MODE) as c:
        assert "data://mockup/mock/readme" in [str(r.uri) for r in await c.list_resources()]

    entry = {**http_mock_entry(mock.port), "resourceFilter": ["ui://*"]}
    config = write_servers_config(tmp_path / "servers.json", {"mockup": entry})
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("mockup", ("ready",))
    async with Client(f"{combiner.mcp_url}/rfilter-0001", mode=CLIENT_CONNECT_MODE) as c:
        uris = [str(r.uri) for r in await c.list_resources()]
        assert uris and all(u.startswith("ui://") for u in uris), uris
        hidden = "data://mockup/mock/readme"
        # Listing only: a filtered-out resource can still be read by URI.
        assert hidden not in uris
        assert (await c.read_resource(hidden))[0] is not None
