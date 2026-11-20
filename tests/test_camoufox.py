"""The experimental camoufox driver. Skipped unless camoufox and its browser are installed."""

import os
import shutil
import tempfile

import pytest
import pytest_asyncio

from hutch import driver as _driver
from hutch.pool import Pool
from hutch.session import session_class

_BASE = os.path.join(tempfile.gettempdir(), "hutch-test-camoufox-profiles")


def test_camoufox_is_a_known_driver_with_a_session_of_its_own(tmp_path):
    assert "camoufox" in _driver.DRIVERS and "camoufox" in _driver.OWN_DRIVERS
    cls = session_class("camoufox")
    s = cls("f", str(tmp_path / "f"))
    s._save_meta()
    from hutch.session import Session
    assert type(Session.from_profile_dir(str(tmp_path / "f"))) is cls
    with pytest.raises(ValueError, match="personas"):
        from hutch.session import Fingerprint
        cls("g", str(tmp_path / "g"), fingerprint=Fingerprint())


@pytest_asyncio.fixture
async def pool():
    shutil.rmtree(_BASE, ignore_errors=True)
    os.makedirs(_BASE)
    p = Pool(base_dir=_BASE)
    await p.start()
    yield p
    await p.stop()
    shutil.rmtree(_BASE, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.skipif(not _driver.available("camoufox"), reason="camoufox not installed")
async def test_camoufox_drives_a_page(pool):
    s = await pool.create("f", headless=True, driver="camoufox")
    state = await s.goto("data:text/html,<title>fx</title><input id=a>")
    assert state["title"] == "fx" and "Firefox" in await s.evaluate("navigator.userAgent")
    await s.fill("#a", "x")
    assert await s.evaluate("document.getElementById('a').value") == "x"
    assert (await s.clear_challenge(timeout=12))["status"] == "clear"
