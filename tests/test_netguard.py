import asyncio
import os
import shutil

import pytest
import pytest_asyncio

from hutch.netguard import PrivateNetworkGuard, is_public_ip
from hutch.pool import Pool
from hutch.session import Session

_TEST_BASE = os.path.expanduser("~/.hutch/test-netguard-profiles")


# --- address rules (no browser) ---

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
    "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fc00::1",
    "::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe", "2002:7f00:1::",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",
])
def test_internal_addresses_are_refused(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8",
                                "64:ff9b::808:808"])
def test_public_addresses_pass(ip):
    assert is_public_ip(ip)


def _resolver(table):
    async def resolve(host, port):
        if host not in table:
            raise OSError("no such host")
        return table[host]
    return resolve


@pytest.mark.asyncio
async def test_guard_decisions():
    guard = PrivateNetworkGuard(_resolver({
        "public.test": ["93.184.215.14"],
        "intranet.test": ["10.0.0.5"],
        "mixed.test": ["93.184.215.14", "127.0.0.1"],
        "empty.test": [],
    }))
    assert await guard.allowed("https://public.test/a")
    assert await guard.allowed("wss://public.test/socket")
    assert await guard.allowed("data:text/html,hi")
    assert await guard.allowed("blob:https://public.test/1234")
    assert await guard.allowed("about:blank")
    assert await guard.allowed("http://8.8.8.8/")
    assert not await guard.allowed("http://intranet.test/")
    assert not await guard.allowed("http://mixed.test/")
    assert not await guard.allowed("http://empty.test/")
    assert not await guard.allowed("http://unknown.test/")
    assert not await guard.allowed("http://localhost:8080/")
    assert not await guard.allowed("http://app.localhost/")
    assert not await guard.allowed("http://127.0.0.1/")
    assert not await guard.allowed("http://[::1]/")
    assert not await guard.allowed("http://169.254.169.254/latest/meta-data/")
    assert not await guard.allowed("ws://[::ffff:7f00:1]/")
    assert not await guard.allowed("file:///etc/passwd")
    assert not await guard.allowed("ftp://public.test/")


@pytest.mark.asyncio
async def test_guard_caches_lookups():
    calls = []

    async def resolve(host, port):
        calls.append(host)
        return ["93.184.215.14"]

    guard = PrivateNetworkGuard(resolve)
    await guard.allowed("https://public.test/a")
    await guard.allowed("https://public.test/b")
    assert calls == ["public.test"]


def test_flag_sticks_to_the_profile(tmp_path):
    s = Session("guarded", str(tmp_path / "guarded"), block_private=True)
    os.makedirs(s.profile_dir, exist_ok=True)
    s._save_meta()
    assert Session.from_profile_dir(s.profile_dir).block_private
    plain = Session("plain", str(tmp_path / "plain"))
    os.makedirs(plain.profile_dir, exist_ok=True)
    plain._save_meta()
    assert not Session.from_profile_dir(plain.profile_dir).block_private


# --- in a real browser ---

class _LocalServer:
    """Counts every request that reaches it; answers a tiny page."""

    def __init__(self):
        self.hits = 0
        self._server = None
        self.port = None

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if line:
                self.hits += 1
                body = b"<html><body>internal</body></html>"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                             b"Access-Control-Allow-Origin: *\r\n"
                             b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                             b"Connection: close\r\n\r\n" + body)
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest_asyncio.fixture
async def pool():
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)
    os.makedirs(_TEST_BASE, exist_ok=True)
    p = Pool(base_dir=_TEST_BASE, max_sessions=3)
    await p.start()
    yield p
    await p.stop()
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)


@pytest.mark.asyncio
async def test_unguarded_session_reaches_loopback(pool):
    # control: proves the blocked case below is the guard, not the test setup
    async with _LocalServer() as server:
        s = await pool.create("ng-open", headless=True)
        page = await s.new_page()
        await page.goto(f"http://127.0.0.1:{server.port}/")
        assert "internal" in await page.content()
        assert server.hits >= 1


@pytest.mark.asyncio
async def test_guarded_session_refuses_navigation(pool):
    async with _LocalServer() as server:
        s = await pool.create("ng-nav", headless=True, block_private=True)
        page = await s.new_page()
        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
            await page.goto(f"http://127.0.0.1:{server.port}/")
        assert server.hits == 0
        assert any(str(server.port) in u for u in s._net_guard.blocked)


@pytest.mark.asyncio
async def test_guarded_session_refuses_page_requests(pool):
    async with _LocalServer() as server:
        s = await pool.create("ng-sub", headless=True, block_private=True)
        page = await s.new_page()
        await page.goto("data:text/html,<p>ok</p>")
        base = f"127.0.0.1:{server.port}"
        result = await page.evaluate(f"""async () => {{
            const out = {{}};
            try {{ await fetch('http://{base}/x'); out.fetch = 'ok' }} catch (e) {{ out.fetch = 'blocked' }}
            out.ws = await new Promise(done => {{
                const ws = new WebSocket('ws://{base}/');
                ws.onopen = () => done('open');
                ws.onerror = () => done('blocked');
                ws.onclose = () => done('blocked');
                setTimeout(() => done('timeout'), 5000);
            }});
            return out;
        }}""")
        assert result == {"fetch": "blocked", "ws": "blocked"}
        assert server.hits == 0


@pytest.mark.asyncio
async def test_guard_comes_back_after_relaunch(pool):
    async with _LocalServer() as server:
        s = await pool.create("ng-relaunch", headless=True, block_private=True)
        await s.close()
        s = await pool.get("ng-relaunch", launch=True)
        page = await s.new_page()
        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
            await page.goto(f"http://127.0.0.1:{server.port}/")
        assert server.hits == 0


@pytest.mark.asyncio
async def test_guard_covers_tabs_opened_later(pool):
    async with _LocalServer() as server:
        s = await pool.create("ng-tabs", headless=True, block_private=True)
        await s.new_page()
        second = await s.new_page()
        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
            await second.goto(f"http://127.0.0.1:{server.port}/")
        assert server.hits == 0


@pytest.mark.asyncio
async def test_guard_covers_popups_from_their_first_request(pool):
    async with _LocalServer() as server:
        s = await pool.create("ng-popup", headless=True, block_private=True)
        page = await s.new_page()
        await page.goto("data:text/html,<p>opener</p>")
        async with page.expect_popup() as info:
            await page.evaluate(f"window.open('http://127.0.0.1:{server.port}/popup')")
        await info.value
        await asyncio.sleep(0.5)
        assert server.hits == 0


@pytest.mark.asyncio
async def test_guard_runs_before_intercept_rules(pool):
    async with _LocalServer() as server:
        s = await pool.create("ng-rules", headless=True, block_private=True)

        async def let_through(route):  # a rule that would happily continue anything
            await route.continue_(headers={**route.request.headers, "x-test": "1"})

        await s.intercept("**/*", let_through)
        page = await s.new_page()
        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
            await page.goto(f"http://127.0.0.1:{server.port}/")
        assert server.hits == 0


@pytest.mark.asyncio
async def test_allowed_requests_still_reach_intercept_rules(pool):
    s = await pool.create("ng-fallback", headless=True, block_private=True)

    async def answer(route):
        await route.fulfill(status=200, content_type="text/html", body="<p>from the rule</p>")

    await s.intercept("**/hutch-test", answer)
    page = await s.new_page()
    await page.goto("http://8.8.8.8/hutch-test")  # public literal: no lookup, never sent
    assert "from the rule" in await page.content()


@pytest.mark.asyncio
async def test_a_hung_dns_lookup_times_out_and_is_refused(monkeypatch):
    import threading
    import time

    from hutch import netguard

    release = threading.Event()
    monkeypatch.setattr(netguard.socket, "getaddrinfo", lambda *a, **k: (release.wait(), [])[1])
    started = time.monotonic()
    with pytest.raises(OSError, match="timed out"):
        await netguard._system_resolve("hang.test", 443, timeout=0.2)
    assert time.monotonic() - started < 2
    guard = netguard.PrivateNetworkGuard(resolver=lambda h, p: netguard._system_resolve(h, p, timeout=0.2))
    assert await guard.allowed("https://hang.test/") is False  # unresolvable means refused
    release.set()  # let the daemon threads finish
