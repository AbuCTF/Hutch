"""Headless sessions look like a desktop Chrome: real screen, a mouse, full client hints, either driver."""

import os
import shutil
import tempfile

import pytest
import pytest_asyncio

from hutch import browser as _browser
from hutch import driver as _driver
from hutch.pool import Pool
from hutch.session import Session

_TEST_BASE = os.path.join(tempfile.gettempdir(), "hutch-test-identity-profiles")
_WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
_DRIVERS = [d for d in _driver.DRIVERS if _driver.available(d)]

_PROBE = """async () => ({
  ua: navigator.userAgent,
  screen: [screen.width, screen.height, screen.availWidth, screen.availHeight],
  outer: [outerWidth, outerHeight],
  pointer: matchMedia('(any-pointer: fine)').matches && matchMedia('(pointer: fine)').matches,
  hover: matchMedia('(hover: hover)').matches,
  hints: await navigator.userAgentData.getHighEntropyValues(['fullVersionList', 'architecture', 'bitness']),
})"""


def test_driver_sticks_to_the_profile(tmp_path):
    Session("d", str(tmp_path / "d"), driver="patchright")._save_meta()
    assert Session.from_profile_dir(str(tmp_path / "d")).driver == "patchright"
    with pytest.raises(ValueError, match="unknown driver"):
        Session("x", str(tmp_path / "x"), driver="selenium")


def test_main_world_kwargs_follow_the_driver():
    class FakePatchright:
        pass
    FakePatchright.__module__ = "patchright._impl._page"
    assert _driver.main_world(FakePatchright()) == {"isolated_context": False}
    assert _driver.main_world(object()) == {}


def test_headless_display_flags():
    args = _browser.build_args("/p", headless=True, screen=(1920, 1080), window_size=(1920, 1080),
                               user_agent=_WIN_UA)
    assert "--screen-info={0,0 1920x1080 workAreaBottom=48}" in args
    assert "--window-size=1920,1032" in args  # a maximised window fills the work area, not the screen
    assert any(a.startswith("--blink-settings=primaryPointerType=4") for a in args)
    touch = _browser.build_args("/p", headless=True, screen=(412, 915), touch=True, user_agent=_WIN_UA)
    assert any(a.startswith("--blink-settings=primaryPointerType=2") for a in touch)
    headed = _browser.build_args("/p", headless=False, screen=(1920, 1080))
    assert not any(a.startswith(("--screen-info", "--blink-settings")) for a in headed)


@pytest_asyncio.fixture
async def pool():
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)
    os.makedirs(_TEST_BASE, exist_ok=True)
    p = Pool(base_dir=_TEST_BASE, max_sessions=2)
    await p.start()
    yield p
    await p.stop()
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", _DRIVERS)
async def test_headless_page_looks_like_a_desktop(pool, driver):
    s = await pool.create(f"id-{driver}", headless=True, driver=driver)
    page = await s.new_page()
    await page.route("https://identity.test/", lambda r: r.fulfill(body="<p>x</p>", content_type="text/html"))
    await page.goto("https://identity.test/")
    seen = await page.evaluate(_PROBE)
    width, height, avail_w, avail_h = seen["screen"]
    assert (width, height) != (800, 600)
    assert avail_h < height and seen["outer"][1] <= avail_h  # a taskbar, and a window that fits
    assert seen["pointer"] and seen["hover"]
    assert "HeadlessChrome" not in seen["ua"]
    hints = seen["hints"]
    assert hints["architecture"] and hints["bitness"]
    full = {b["brand"]: b["version"] for b in hints["fullVersionList"]}
    assert s._browser.version in full.values()  # the real full version, as a headed Chrome reports it


@pytest.mark.asyncio
@pytest.mark.skipif("patchright" not in _DRIVERS, reason="patchright not installed")
async def test_patchright_evaluate_still_reaches_page_globals(pool):
    s = await pool.create("id-globals", headless=True, driver="patchright")
    page = await s.new_page()
    await page.route("https://globals.test/", lambda r: r.fulfill(
        body="<script>window.appState = {ready: true}</script>", content_type="text/html"))
    await page.goto("https://globals.test/")
    assert await s.evaluate("window.appState && window.appState.ready") is True


def test_wsl_on_utc_reports_the_windows_zone(tmp_path, monkeypatch):
    from hutch import tz

    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(tz._browser, "is_wsl", lambda: True)
    monkeypatch.setattr(tz, "_windows_zone_id", lambda: "India Standard Time")
    monkeypatch.setattr(tz.time, "localtime", lambda *a: type("T", (), {"tm_gmtoff": 0})())
    assert tz.host_timezone() == "Asia/Kolkata"
    kw = Session("tz", str(tmp_path / "tz"), headless=True).spawn_options()
    assert kw["env"]["TZ"] == "Asia/Kolkata" and kw["screen"] == (1920, 1080)
    monkeypatch.setenv("TZ", "Europe/Paris")  # an explicit TZ always wins
    assert tz.host_timezone() is None
    monkeypatch.delenv("TZ")
    monkeypatch.setattr(tz._browser, "is_wsl", lambda: False)  # elsewhere the system zone is inherited
    assert tz.host_timezone() is None


def test_status_shows_the_profile_settings(tmp_path):
    s = Session("st", str(tmp_path / "st"), driver="patchright", block_private=True, humanize=True)
    status = s.status()
    assert (status["driver"], status["block_private"], status["humanize"]) == ("patchright", True, True)
    plain = Session("plain", str(tmp_path / "plain")).status()
    assert (plain["driver"], plain["block_private"], plain["humanize"]) == ("playwright", False, False)
