import asyncio
import os
import shutil
import pytest
import pytest_asyncio
from hutch.server import HutchDaemon
from hutch.client import HutchClient, HutchError, connect

_TEST_BASE = os.path.expanduser("~/.hutch/test-daemon-profiles")
_TEST_SOCK = os.path.expanduser("~/.hutch/test-daemon.sock")


@pytest_asyncio.fixture
async def daemon():
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE, ignore_errors=True)
    os.makedirs(_TEST_BASE, exist_ok=True)

    d = HutchDaemon(
        base_dir=_TEST_BASE,
        max_sessions=5,
        idle_timeout=0,
        sock_path=_TEST_SOCK,
    )
    await d.start()
    yield d
    await d.stop()

    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE, ignore_errors=True)
    artifacts_dir = os.path.expanduser("~/.hutch/artifacts")
    for name in ("d-ping", "d-create", "d-snap", "d-nav", "d-net",
                 "d-console", "d-eval", "d-ss", "d-har", "d-notes",
                 "d-health", "d-destroy", "d-multi-a", "d-multi-b"):
        p = os.path.join(artifacts_dir, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)


@pytest_asyncio.fixture
async def client(daemon):
    c = HutchClient(sock_path=_TEST_SOCK)
    await c.connect()
    yield c
    await c.close()


@pytest.mark.asyncio
class TestDaemon:

    async def test_ping(self, client):
        result = await client.ping()
        assert result["pong"] is True

    async def test_create_and_list(self, client):
        s = await client.create("d-create", program="test-prog")
        assert s.name == "d-create"

        sessions = await client.list()
        assert any(s["name"] == "d-create" for s in sessions)

    async def test_snapshot(self, client):
        s = await client.create("d-snap")
        await s.goto("https://example.com")
        await asyncio.sleep(0.5)

        snap = await s.snapshot()
        assert "example.com" in snap["url"]
        assert snap["title"] != ""
        assert len(snap["network"]) > 0
        assert snap["cursor"] > 0

        snap2 = await s.snapshot()
        assert len(snap2["network"]) == 0

    async def test_navigate_and_diff(self, client):
        s = await client.create("d-nav")
        await s.goto("https://example.com")
        await asyncio.sleep(0.5)

        snap1 = await s.snapshot()
        c1 = snap1["cursor"]

        await s.evaluate("console.log('diff-test')")
        await asyncio.sleep(0.3)

        diff = await s.diff(since=c1)
        assert any("diff-test" in e["text"] for e in diff["console_added"])

    async def test_network_query(self, client):
        s = await client.create("d-net")
        await s.goto("https://example.com")
        await asyncio.sleep(0.5)

        reqs = await s.network(pattern="*example*")
        assert len(reqs) > 0
        assert all("example" in r["url"] for r in reqs)

    async def test_console_query(self, client):
        s = await client.create("d-console")
        await s.goto("data:text/html,<h1>hi</h1>")
        await s.evaluate("console.error('bad-thing')")
        await s.evaluate("console.log('good-thing')")
        await asyncio.sleep(0.3)

        errors = await s.console(level="error")
        assert any("bad-thing" in e["text"] for e in errors)
        assert not any("good-thing" in e["text"] for e in errors)

    async def test_evaluate(self, client):
        s = await client.create("d-eval")
        await s.goto("data:text/html,<title>eval-test</title>")
        title = await s.evaluate("document.title")
        assert title == "eval-test"

    async def test_screenshot(self, client):
        s = await client.create("d-ss")
        await s.goto("https://example.com")
        result = await s.screenshot(label="test")
        assert result["size"] > 0
        assert os.path.exists(result["path"])

    async def test_export_har(self, client):
        s = await client.create("d-har")
        await s.goto("https://example.com")
        await asyncio.sleep(0.5)
        result = await s.export_har(label="test")
        assert result["entries"] > 0
        assert os.path.exists(result["path"])

    async def test_notes(self, client):
        s = await client.create("d-notes")
        await s.note("auth", {"type": "bearer", "header": "Authorization"})
        notes = await s.notes()
        assert notes["auth"]["type"] == "bearer"

    async def test_alerts(self, client):
        s = await client.create("d-health")
        await s.goto("data:text/html,<h1>ok</h1>")
        alerts = await client.alerts(session="d-health")
        assert alerts == []

    async def test_destroy(self, client):
        s = await client.create("d-destroy")
        await s.destroy()
        sessions = await client.list()
        assert not any(s["name"] == "d-destroy" for s in sessions)

    async def test_destroy_nonexistent(self, client):
        with pytest.raises(HutchError):
            await client.destroy("nonexistent-session")

    async def test_multiple_sessions(self, client):
        a = await client.create("d-multi-a", program="alpha")
        b = await client.create("d-multi-b", program="beta")
        await a.goto("data:text/html,<title>A</title>")
        await b.goto("data:text/html,<title>B</title>")

        ta = await a.evaluate("document.title")
        tb = await b.evaluate("document.title")
        assert ta == "A"
        assert tb == "B"

    async def test_status(self, client):
        st = await client.status()
        assert "alive" in st
        assert "total" in st
        assert "max_sessions" in st


@pytest.mark.asyncio
class TestClientErrors:

    async def test_connect_no_daemon(self):
        c = HutchClient(sock_path="/tmp/hutch-nonexistent.sock")
        with pytest.raises(HutchError, match="daemon not running"):
            await c.connect()

    async def test_connect_function(self, daemon):
        c = await connect(sock_path=_TEST_SOCK)
        try:
            result = await c.ping()
            assert result["pong"] is True
        finally:
            await c.close()
