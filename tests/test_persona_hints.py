"""A persona's client hints must hold from a tab's first request, popups included."""

import asyncio
import os
import shutil

import pytest
import pytest_asyncio

from hutch.fingerprint import generate
from hutch.pool import Pool

_TEST_BASE = os.path.expanduser("~/.hutch/test-persona-hints-profiles")


class _HeaderServer:
    def __init__(self):
        self.requests = []  # (path, {header: [values]})

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer):
        lines = []
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n":
                break
            lines.append(line.decode().rstrip("\r\n"))
        if lines:
            headers = {}
            for raw in lines[1:]:
                name, _, value = raw.partition(":")
                headers.setdefault(name.strip().lower(), []).append(value.strip())
            self.requests.append((lines[0].split()[1], headers))
        body = b"<p>ok</p>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()

    def first(self, path):
        return next(h for p, h in self.requests if p == path)


@pytest_asyncio.fixture
async def pool():
    shutil.rmtree(_TEST_BASE, ignore_errors=True)
    os.makedirs(_TEST_BASE, exist_ok=True)
    p = Pool(base_dir=_TEST_BASE, max_sessions=2)
    await p.start()
    yield p
    await p.stop()
    shutil.rmtree(_TEST_BASE, ignore_errors=True)


@pytest.mark.asyncio
async def test_windows_persona_hints_from_the_first_request(pool):
    async with _HeaderServer() as server:
        s = await pool.create("hints", headless=True, fingerprint=generate(preset="win-desktop-1080p"))
        assert s.launcher == "native" and s._spoof
        page = await s.new_page()
        await page.goto(f"http://127.0.0.1:{server.port}/tab")
        for i in range(3):
            async with page.expect_popup() as info:
                await page.evaluate(f"window.open('http://127.0.0.1:{server.port}/popup{i}')")
            popup = await info.value
            await popup.wait_for_load_state()
            await popup.close()

        for path in ["/tab", "/popup0", "/popup1", "/popup2"]:
            headers = server.first(path)
            assert headers["sec-ch-ua-platform"] == ['"Windows"'], path
            assert headers["sec-ch-ua-mobile"] == ["?0"], path
            assert len(headers["sec-ch-ua"]) == 1 and "HeadlessChrome" not in headers["sec-ch-ua"][0], path
            assert "Windows NT" in headers["user-agent"][0], path
