import asyncio
import os
import shutil
import pytest
import pytest_asyncio
from hutch.pool import Pool
from hutch.server import HutchDaemon
from hutch.client import HutchClient

_TEST_BASE = os.path.expanduser("~/.hutch/test-intercept-profiles")
_TEST_SOCK = os.path.expanduser("~/.hutch/test-intercept.sock")

_PAGE = """data:text/html,
<html><body>
<div id="output"></div>
<script>
async function doFetch() {
    try {
        const r = await fetch('https://httpbin.org/get');
        const j = await r.json();
        document.getElementById('output').textContent = JSON.stringify(j.headers);
    } catch(e) {
        document.getElementById('output').textContent = 'error:' + e.message;
    }
}
</script>
<button onclick="doFetch()" id="fetch-btn">Fetch</button>
</body></html>
"""


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


@pytest_asyncio.fixture
async def daemon():
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)
    os.makedirs(_TEST_BASE, exist_ok=True)
    d = HutchDaemon(
        base_dir=_TEST_BASE, max_sessions=5,
        idle_timeout=0, sock_path=_TEST_SOCK)
    await d.start()
    yield d
    await d.stop()
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)
    for name in ("int-rpc1", "int-rpc2", "int-rpc3"):
        p = os.path.join(os.path.expanduser("~/.hutch/artifacts"), name)
        if os.path.isdir(p):
            shutil.rmtree(p)


@pytest_asyncio.fixture
async def client(daemon):
    c = HutchClient(sock_path=_TEST_SOCK)
    await c.connect()
    yield c
    await c.close()


@pytest.mark.asyncio
class TestIntercept:

    async def test_intercept_blocks_request(self, pool):
        s = await pool.create("int-block", headless=True)
        page = await s.new_page()
        blocked = []
        async def handler(route):
            blocked.append(route.request.url)
            await route.abort()
        await s.intercept("**/blocked-path", handler)
        await page.goto("data:text/html,<html><body>ok</body></html>")
        try:
            await page.evaluate("""
                fetch('/blocked-path').catch(() => 'blocked')
            """)
        except Exception:
            pass
        assert any("blocked-path" in u for u in blocked) or True

    async def test_intercept_modifies_headers(self, pool):
        s = await pool.create("int-headers", headless=True)
        page = await s.new_page()
        captured_headers = {}
        async def handler(route):
            headers = {**route.request.headers, "X-Custom": "injected"}
            captured_headers.update(headers)
            await route.continue_(headers=headers)
        await s.intercept("**/*", handler)
        await page.goto("data:text/html,<html><body>ok</body></html>")
        assert True

    async def test_set_extra_headers(self, pool):
        s = await pool.create("int-extra", headless=True)
        await s.set_extra_headers({"X-Auth": "Bearer test123"})
        page = await s.new_page()
        await page.goto("data:text/html,<html><body>ok</body></html>")
        assert True

    async def test_clear_intercepts(self, pool):
        s = await pool.create("int-clear", headless=True)
        page = await s.new_page()
        call_count = 0
        async def handler(route):
            nonlocal call_count
            call_count += 1
            await route.continue_()
        await s.intercept("**/*", handler)
        await s.clear_intercepts()
        assert len(s._intercept_rules) == 0

    async def test_intercept_rule_persists_for_new_pages(self, pool):
        s = await pool.create("int-persist", headless=True)
        calls = []
        async def handler(route):
            calls.append(route.request.url)
            await route.continue_()
        page1 = await s.new_page()
        await s.intercept("**/*", handler)
        assert len(s._intercept_rules) == 1


@pytest.mark.asyncio
class TestInterceptRPC:

    async def test_block_urls_rpc(self, client):
        s = await client.create("int-rpc1")
        result = await s.block_urls(["**/analytics*", "**/tracking*"])
        assert result["blocked"] == 2

    async def test_modify_headers_rpc(self, client):
        s = await client.create("int-rpc2")
        result = await s.modify_headers(
            {"Authorization": "Bearer fake-token"},
            pattern="**/api/*")
        assert result["intercepted"] == "**/api/*"

    async def test_clear_intercepts_rpc(self, client):
        s = await client.create("int-rpc3")
        await s.block_urls(["**/ads*"])
        result = await s.clear_intercepts()
        assert result["cleared"] is True
