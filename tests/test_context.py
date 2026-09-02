import asyncio
import os
import shutil
import uuid
import pytest
import pytest_asyncio
from hutch.pool import Pool
from hutch.context import Context, NetworkEntry, ConsoleEntry, RingBuffer

_TEST_BASE = os.path.expanduser("~/.hutch/test-profiles")


@pytest.fixture
def ctx():
    return Context(network_cap=10, console_cap=10)


@pytest_asyncio.fixture
async def pool():
    os.makedirs(_TEST_BASE, exist_ok=True)
    p = Pool(base_dir=_TEST_BASE, max_sessions=3)
    await p.start()
    yield p
    await p.stop()
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)


def _name():
    return f"t-{uuid.uuid4().hex[:8]}"


class TestRingBuffer:

    def test_append_and_all(self):
        rb = RingBuffer(5)
        for i in range(3):
            rb.append(NetworkEntry(seq=i, method="GET", url=f"/{i}"))
        assert len(rb) == 3
        assert len(rb.all()) == 3

    def test_eviction(self):
        rb = RingBuffer(3)
        for i in range(5):
            rb.append(NetworkEntry(seq=i, method="GET", url=f"/{i}"))
        assert len(rb) == 3
        urls = [e.url for e in rb.all()]
        assert urls == ["/2", "/3", "/4"]

    def test_since(self):
        rb = RingBuffer(10)
        for i in range(1, 6):
            rb.append(NetworkEntry(seq=i, method="GET", url=f"/{i}"))
        after = rb.since(3)
        assert len(after) == 2
        assert [e.seq for e in after] == [4, 5]

    def test_query_pattern(self):
        rb = RingBuffer(10)
        rb.append(NetworkEntry(seq=1, method="GET", url="/api/users"))
        rb.append(NetworkEntry(seq=2, method="GET", url="/static/main.js"))
        rb.append(NetworkEntry(seq=3, method="POST", url="/api/login"))
        results = rb.query(pattern="*/api/*")
        assert len(results) == 2

    def test_query_method(self):
        rb = RingBuffer(10)
        rb.append(NetworkEntry(seq=1, method="GET", url="/a"))
        rb.append(NetworkEntry(seq=2, method="POST", url="/b"))
        results = rb.query(method="post")
        assert len(results) == 1
        assert results[0].url == "/b"

    def test_query_status_range(self):
        rb = RingBuffer(10)
        rb.append(NetworkEntry(seq=1, method="GET", url="/a", status=200))
        rb.append(NetworkEntry(seq=2, method="GET", url="/b", status=404))
        rb.append(NetworkEntry(seq=3, method="GET", url="/c", status=500))
        results = rb.query(status=range(400, 500))
        assert len(results) == 1
        assert results[0].status == 404


class TestContext:

    def test_cursor_increments(self, ctx):
        assert ctx.cursor == 0
        ctx._next_seq()
        assert ctx.cursor == 1
        ctx._next_seq()
        assert ctx.cursor == 2

    def test_subscribe(self, ctx):
        events = []
        ctx.subscribe(lambda t, e: events.append((t, e)))
        entry = ConsoleEntry(seq=ctx._next_seq(), level="error", text="fail")
        ctx.console.append(entry)
        ctx._emit("console", entry)
        assert len(events) == 1
        assert events[0][0] == "console"

    def test_diff_empty(self, ctx):
        d = ctx.diff()
        assert d.cursor == 0
        assert d.network_added == []
        assert d.console_added == []

    def test_diff_with_cookies(self, ctx):
        old = [{"name": "a", "domain": ".x.com", "path": "/", "value": "1"}]
        new = [
            {"name": "a", "domain": ".x.com", "path": "/", "value": "1"},
            {"name": "b", "domain": ".x.com", "path": "/", "value": "2"},
        ]
        ctx._last_cookies = old
        d = ctx.diff(current_cookies=new)
        assert len(d.cookies_added) == 1
        assert d.cookies_added[0]["name"] == "b"
        assert d.cookies_removed == []

    def test_diff_cookie_removed(self, ctx):
        old = [{"name": "a", "domain": ".x.com", "path": "/", "value": "1"}]
        ctx._last_cookies = old
        d = ctx.diff(current_cookies=[])
        assert len(d.cookies_removed) == 1
        assert d.cookies_removed[0]["name"] == "a"


@pytest.mark.asyncio
class TestContextIntegration:

    async def test_network_capture(self, pool):
        name = _name()
        s = await pool.create(name, headless=True)
        assert s.context is not None
        page = await s.new_page()
        await page.goto("https://example.com")
        await asyncio.sleep(0.5)

        reqs = s.context.network.all()
        assert len(reqs) > 0
        doc = [r for r in reqs if r.resource_type == "document"]
        assert len(doc) >= 1
        assert doc[0].status == 200
        assert "example.com" in doc[0].url

    async def test_console_capture(self, pool):
        name = _name()
        s = await pool.create(name, headless=True)
        page = await s.new_page()
        await page.goto("data:text/html,<h1>test</h1>")
        await page.evaluate("console.log('hutch-marker')")
        await page.evaluate("console.error('hutch-error')")
        await asyncio.sleep(0.3)

        logs = s.context.console.all()
        texts = [e.text for e in logs]
        assert "hutch-marker" in texts
        assert "hutch-error" in texts

        errors_only = s.context.query_console(level="error")
        assert any("hutch-error" in e.text for e in errors_only)

    async def test_snapshot(self, pool):
        name = _name()
        s = await pool.create(name, headless=True)
        page = await s.new_page()
        await page.goto("https://example.com")
        await asyncio.sleep(0.5)

        snap = await s.snapshot()
        assert "example.com" in snap.url
        assert snap.title != ""
        assert len(snap.network) > 0
        assert snap.cursor > 0

        snap2 = await s.snapshot()
        assert len(snap2.network) == 0

    async def test_snapshot_full(self, pool):
        name = _name()
        s = await pool.create(name, headless=True)
        page = await s.new_page()
        await page.goto("https://example.com")
        await asyncio.sleep(0.5)

        await s.snapshot()
        snap = await s.snapshot(full=True)
        assert len(snap.network) > 0

    async def test_snapshot_with_storage(self, pool):
        name = _name()
        s = await pool.create(name, headless=True)
        page = await s.new_page()
        await page.goto("https://example.com")
        await page.evaluate("localStorage.setItem('hutch_key', 'hutch_val')")

        snap = await s.snapshot(storage=True)
        assert snap.storage is not None
        assert snap.storage["localStorage"]["hutch_key"] == "hutch_val"

    async def test_diff(self, pool):
        name = _name()
        s = await pool.create(name, headless=True)
        page = await s.new_page()
        await page.goto("https://example.com")
        await asyncio.sleep(0.5)

        snap1 = await s.snapshot()
        c1 = snap1.cursor
        assert len(snap1.network) > 0

        await page.evaluate("console.log('after-snap')")
        await asyncio.sleep(0.3)

        d = await s.diff(since=c1)
        assert any("after-snap" in e.text for e in d.console_added)
        assert len(d.network_added) == 0

    async def test_navigation_capture(self, pool):
        name = _name()
        s = await pool.create(name, headless=True)
        page = await s.new_page()
        await page.goto("https://example.com")
        await asyncio.sleep(0.3)

        navs = s.context.navigations.all()
        assert len(navs) >= 1
        assert any("example.com" in n.url for n in navs)

    async def test_network_query_pattern(self, pool):
        name = _name()
        s = await pool.create(name, headless=True)
        page = await s.new_page()
        await page.goto("https://example.com")
        await asyncio.sleep(0.5)

        all_reqs = s.context.query_network()
        example_reqs = s.context.query_network(pattern="*example.com*")
        assert len(example_reqs) > 0
        assert len(example_reqs) <= len(all_reqs)

    async def test_error_capture(self, pool):
        name = _name()
        s = await pool.create(name, headless=True)
        page = await s.new_page()
        await page.goto("data:text/html,<h1>test</h1>")
        await page.evaluate("setTimeout(() => { throw new Error('hutch-uncaught') }, 0)")
        await asyncio.sleep(0.5)

        errs = s.context.errors.all()
        assert any("hutch-uncaught" in e.message for e in errs)
