"""Which drivers switch on Runtime.enable where a page can see it. Playwright must, the others must not."""

import os
import shutil
import tempfile

import pytest
import pytest_asyncio

from hutch import driver as _driver
from hutch import leak
from hutch.pool import Pool

_BASE = os.path.join(tempfile.gettempdir(), "hutch-test-leak-profiles")
_DRIVERS = [d for d in _driver.CHROME_DRIVERS if _driver.available(d)]
_CLEAN = [d for d in _DRIVERS if d != "playwright"]


@pytest_asyncio.fixture
async def pool():
    shutil.rmtree(_BASE, ignore_errors=True)
    os.makedirs(_BASE)
    p = Pool(base_dir=_BASE, max_sessions=3)
    await p.start()
    yield p
    await p.stop()
    shutil.rmtree(_BASE, ignore_errors=True)


def test_the_verdict_reads_both_traps():
    assert leak.verdict({"stack": 0, "name": 1})["leaks"] is False
    assert leak.verdict({"stack": 0, "name": 2})["leaks"] is True  # current chrome: name read twice
    assert leak.verdict({"stack": 1, "name": 1})["leaks"] is True  # older chrome: stack read at all


async def _probe(pool, driver):
    s = await pool.create(f"leak-{driver}", headless=True, driver=driver)
    await s.goto("about:blank")
    return s, await leak.check(s)


@pytest.mark.asyncio
async def test_the_probe_sees_runtime_enable_when_it_is_switched_on(pool):
    """The control: the cdp driver is clean until Runtime.enable is sent by hand."""
    s, quiet = await _probe(pool, "cdp")
    assert quiet["leaks"] is False, quiet
    await s.cdp_send("Runtime.enable")
    loud = await leak.check(s)
    assert loud["leaks"] is True, loud
    await s.cdp_send("Runtime.disable")
    assert (await leak.check(s))["leaks"] is False


@pytest.mark.asyncio
@pytest.mark.skipif("playwright" not in _DRIVERS, reason="playwright not installed")
async def test_plain_playwright_leaks(pool):
    _, got = await _probe(pool, "playwright")
    assert got["leaks"] is True, got


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", _CLEAN)
async def test_patchright_and_cdp_do_not_leak(pool, driver):
    _, got = await _probe(pool, driver)
    assert got["leaks"] is False, got
