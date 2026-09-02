import asyncio
import os
import shutil
import pytest
import pytest_asyncio
from hutch.pool import Pool
from hutch.server import HutchDaemon
from hutch.client import HutchClient

_TEST_BASE = os.path.expanduser("~/.hutch/test-parallel-profiles")
_TEST_SOCK = os.path.expanduser("~/.hutch/test-parallel.sock")

_PAGE_A = "data:text/html,<html><body><h1>Page A</h1></body></html>"
_PAGE_B = "data:text/html,<html><body><h1>Page B</h1></body></html>"


@pytest_asyncio.fixture
async def pool():
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)
    os.makedirs(_TEST_BASE, exist_ok=True)
    p = Pool(base_dir=_TEST_BASE, max_sessions=5)
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
    for name in ("par-rpc-a", "par-rpc-b"):
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
class TestParallelPool:

    async def test_parallel_goto(self, pool):
        await pool.create("par-a", headless=True)
        await pool.create("par-b", headless=True)
        results = await pool.parallel_goto(
            ["par-a", "par-b"], _PAGE_A)
        assert "par-a" in results
        assert "par-b" in results
        assert "url" in results["par-a"]
        assert "url" in results["par-b"]

    async def test_parallel_custom_action(self, pool):
        await pool.create("par-c", headless=True)
        await pool.create("par-d", headless=True)
        for name in ("par-c", "par-d"):
            s = await pool.get(name)
            await s.goto(_PAGE_A)
            await asyncio.sleep(0.1)

        async def get_title(s):
            return await s.evaluate("document.title")

        results = await pool.parallel(["par-c", "par-d"], get_title)
        assert "par-c" in results
        assert "par-d" in results

    async def test_parallel_handles_errors(self, pool):
        await pool.create("par-e", headless=True)
        await pool.create("par-f", headless=True)
        se = await pool.get("par-e")
        se.pause("test")

        results = await pool.parallel_goto(
            ["par-e", "par-f"], _PAGE_A)
        assert isinstance(results["par-e"], Exception)
        assert "url" in results["par-f"]

    async def test_compare(self, pool):
        await pool.create("par-g", headless=True)
        await pool.create("par-h", headless=True)
        results = await pool.compare(
            ["par-g", "par-h"], _PAGE_A)
        assert "par-g" in results
        assert "par-h" in results
        for name in ("par-g", "par-h"):
            assert "url" in results[name]
            assert "title" in results[name]


@pytest.mark.asyncio
class TestParallelRPC:

    async def test_parallel_goto_rpc(self, client):
        await client.create("par-rpc-a")
        await client.create("par-rpc-b")
        results = await client.parallel_goto(
            ["par-rpc-a", "par-rpc-b"], _PAGE_A)
        assert "par-rpc-a" in results
        assert "par-rpc-b" in results

    async def test_compare_rpc(self, client):
        await client.create("par-rpc-a")
        await client.create("par-rpc-b")
        results = await client.compare(
            ["par-rpc-a", "par-rpc-b"], _PAGE_A)
        assert "par-rpc-a" in results
        assert "par-rpc-b" in results
