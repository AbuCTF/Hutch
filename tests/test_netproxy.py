"""The private-network proxy: refusals, DNS pinning, and what it adds to a session's block."""

import asyncio
import os
import shutil
import tempfile

import pytest
import pytest_asyncio

from hutch import driver as _driver
from hutch.netproxy import PrivateNetworkProxy
from hutch.pool import Pool

_TEST_BASE = os.path.join(tempfile.gettempdir(), "hutch-test-netproxy-profiles")


class _Server:
    """Records each request head it receives and answers a tiny page."""

    def __init__(self, host="127.0.0.1"):
        self.host = host
        self.heads = []
        self.port = None
        self._server = None

    @property
    def hits(self):
        return len(self.heads)

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            self.heads.append(head.decode("latin-1"))
            body = b"<html><body>internal</body></html>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nAccess-Control-Allow-Origin: *\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _through(proxy, raw):
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
    writer.write(raw.encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 5)
    writer.close()
    return data.decode("latin-1")


def _resolver(answers):
    """Fake DNS: name -> list of answers, one per lookup (the last one repeats)."""
    calls = []

    async def resolve(host, port):
        calls.append(host)
        seq = answers.get(host)
        if seq is None:
            raise OSError("no such host")
        return seq.pop(0) if len(seq) > 1 else seq[0]
    resolve.calls = calls
    return resolve


# 127.0.0.2 stands in for a public address, so these tests need no network
@pytest_asyncio.fixture
async def proxy():
    p = await PrivateNetworkProxy(resolver=_resolver({}), allow_ip=lambda ip: ip == "127.0.0.2").start()
    yield p
    await p.stop()


@pytest.mark.asyncio
async def test_tunnel_to_an_internal_address_is_refused(proxy):
    async with _Server() as server:
        reply = await _through(proxy, f"CONNECT 127.0.0.1:{server.port} HTTP/1.1\r\n\r\n")
        assert reply.startswith("HTTP/1.1 403")
        assert server.hits == 0
        assert proxy.blocked == [f"127.0.0.1:{server.port}"]


@pytest.mark.asyncio
async def test_localhost_names_are_refused_without_a_lookup(proxy):
    for target in ("localhost:80", "app.localhost:443", "[::1]:443"):
        assert (await _through(proxy, f"CONNECT {target} HTTP/1.1\r\n\r\n")).startswith("HTTP/1.1 403")
    assert proxy._resolve.calls == []


@pytest.mark.asyncio
async def test_plain_http_is_forwarded_to_the_checked_address():
    async with _Server("127.0.0.2") as server:
        p = await PrivateNetworkProxy(resolver=_resolver({"public.test": [["127.0.0.2"]]}),
                                      allow_ip=lambda ip: ip == "127.0.0.2").start()
        try:
            reply = await _through(p, f"GET http://public.test:{server.port}/x?y=1 HTTP/1.1\r\n"
                                      f"Host: public.test:{server.port}\r\nProxy-Connection: keep-alive\r\n\r\n")
        finally:
            await p.stop()
        assert reply.startswith("HTTP/1.1 200") and "internal" in reply
        head = server.heads[0]
        assert head.startswith("GET /x?y=1 HTTP/1.1\r\n")
        assert "Proxy-Connection" not in head and "Connection: close" in head


@pytest.mark.asyncio
async def test_rebinding_cannot_move_the_connection():
    # first answer passes the check; a second lookup would say loopback, but there is none
    async with _Server("127.0.0.2") as public, _Server("127.0.0.1") as internal:
        dns = _resolver({"rebind.test": [["127.0.0.2"], ["127.0.0.1"]]})
        p = await PrivateNetworkProxy(resolver=dns, allow_ip=lambda ip: ip == "127.0.0.2").start()
        try:
            reply = await _through(p, f"GET http://rebind.test:{public.port}/ HTTP/1.1\r\nHost: rebind.test\r\n\r\n")
        finally:
            await p.stop()
        assert reply.startswith("HTTP/1.1 200")
        assert public.hits == 1 and internal.hits == 0
        assert dns.calls == ["rebind.test"]


@pytest.mark.asyncio
async def test_a_name_with_any_internal_answer_is_refused():
    dns = _resolver({"mixed.test": [["127.0.0.2", "127.0.0.1"]]})
    p = await PrivateNetworkProxy(resolver=dns, allow_ip=lambda ip: ip == "127.0.0.2").start()
    try:
        assert (await _through(p, "CONNECT mixed.test:443 HTTP/1.1\r\n\r\n")).startswith("HTTP/1.1 403")
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_unresolvable_names_are_a_bad_gateway_not_a_block(proxy):
    reply = await _through(proxy, "CONNECT nowhere.test:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith("HTTP/1.1 502")
    assert proxy.blocked == []


# --- in a session -------------------------------------------------------------------------------

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
async def test_blocked_session_runs_through_the_proxy(pool):
    s = await pool.create("np-state", headless=True, block_private=True)
    state = await s.block_private_networks()
    assert state["active"] and state["proxy"]
    assert s._net_proxy is not None and s._net_proxy.port
    await s.close()
    assert s._net_proxy is None


@pytest.mark.asyncio
async def test_page_routes_can_no_longer_get_around_the_block(pool):
    # page.route() handlers run before the context's guard; the proxy still refuses the connection
    async with _Server() as server:
        s = await pool.create("np-pageroute", headless=True, block_private=True)
        page = await s.new_page()
        await page.route("**/*", lambda route: route.continue_())
        response = await page.goto(f"http://127.0.0.1:{server.port}/")
        assert response.status == 403
        assert server.hits == 0
        assert f"127.0.0.1:{server.port}" in s._net_proxy.blocked


@pytest.mark.asyncio
@pytest.mark.skipif(not _driver.available("patchright"), reason="patchright not installed")
async def test_patchright_session_blocks_fetch_and_websockets(pool):
    async with _Server() as server:
        s = await pool.create("np-patchright", headless=True, block_private=True, driver="patchright")
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
async def test_denied_hosts_are_refused_before_any_lookup():
    dns = _resolver({"public.test": [["127.0.0.2"]]})
    p = await PrivateNetworkProxy(resolver=dns, allow_ip=lambda ip: ip == "127.0.0.2").start()
    p.deny = lambda host: host == "target.test" or host.endswith(".target.test")
    try:
        reply = await _through(p, "CONNECT api.target.test:443 HTTP/1.1\r\n\r\n")
        assert reply.startswith("HTTP/1.1 403") and "denied host" in reply
        assert dns.calls == []  # not even resolved
    finally:
        await p.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(not _driver.available("patchright"), reason="patchright not installed")
async def test_deny_reaches_the_guard_and_the_proxy_of_a_live_session(pool):
    # the proxy sees every connection the browser makes (service workers and websockets included),
    # so a deny check installed there covers what page routes cannot
    async with _Server() as server:
        s = await pool.create("np-deny", headless=True, block_private=True, driver="patchright")
        state = s.deny_hosts(lambda host: host == "denied.test")
        assert state == {"deny": True, "guard": True, "proxy": True}
        assert s._net_proxy.deny is not None and s._net_guard.deny is not None
        reply = await _through(s._net_proxy, "CONNECT denied.test:443 HTTP/1.1\r\n\r\n")
        assert "denied host" in reply
        assert await s._net_guard.allowed("wss://denied.test/socket") is False
        assert server.hits == 0
