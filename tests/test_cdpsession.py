"""The cdp driver: a session on hutch.cdp alone, and the navigation failures every driver shares."""

import asyncio
import os
import shutil
import socket
import tempfile

import pytest
import pytest_asyncio
from aiohttp import web

from hutch import driver as _driver
from hutch.cdpsession import CdpSession, Unsupported
from hutch.pool import Pool
from hutch.session import Session

_BASE = os.path.join(tempfile.gettempdir(), "hutch-test-cdpsession-profiles")
_DRIVERS = [d for d in _driver.CHROME_DRIVERS if _driver.available(d)]

_PAGE = b"""<title>cdp test</title><input id=name><button id=go onclick="document.title='clicked '+document.getElementById('name').value">go</button>
<label><input id=agree type=checkbox> agree</label><p id=msg>hello <b>world</b></p>
<a id=pop href="/" target="_blank">pop</a>"""


@pytest_asyncio.fixture
async def site():
    app = web.Application()
    release = asyncio.Event()

    async def page(_):
        return web.Response(body=_PAGE, content_type="text/html")

    async def stalls(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/html"})
        await resp.prepare(request)
        await resp.write(b"<html><body>started")
        await release.wait()

    app.router.add_get("/", page)
    app.router.add_get("/stalls", stalls)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    yield f"http://127.0.0.1:{tcp._server.sockets[0].getsockname()[1]}"
    release.set()
    await runner.cleanup()


@pytest_asyncio.fixture
async def pool():
    shutil.rmtree(_BASE, ignore_errors=True)
    os.makedirs(_BASE)
    p = Pool(base_dir=_BASE, max_sessions=3)
    await p.start()
    yield p
    await p.stop()
    shutil.rmtree(_BASE, ignore_errors=True)


def test_cdp_is_a_driver_that_needs_no_library():
    assert "cdp" in _driver.DRIVERS and _driver.available("cdp")
    assert _driver.check("cdp") == "cdp"
    with pytest.raises(ValueError, match="does not use a Playwright"):
        _driver.async_playwright("cdp")


def test_the_driver_sticks_to_the_profile_and_picks_the_session_class(tmp_path):
    from hutch.session import session_class

    s = session_class("cdp")("c", str(tmp_path / "c"), driver="cdp", nav_timeout=45000)
    s._save_meta()
    back = Session.from_profile_dir(str(tmp_path / "c"))
    assert isinstance(back, CdpSession) and back.driver == "cdp" and back.nav_timeout == 45000
    assert back.launcher == "native" and back.context is None
    assert type(session_class("playwright")("p", str(tmp_path / "p"))) is Session


def test_what_the_driver_cannot_do_is_refused_up_front(tmp_path):
    from hutch.session import Fingerprint, ProxyConfig

    with pytest.raises(ValueError, match="proxy authentication"):
        CdpSession("a", str(tmp_path / "a"), proxy=ProxyConfig(server="http://x:1", username="u", password="p"))
    with pytest.raises(ValueError, match="mobile"):
        CdpSession("b", str(tmp_path / "b"), fingerprint=Fingerprint(is_mobile=True))
    with pytest.raises(ValueError, match="natively"):
        CdpSession("c", str(tmp_path / "c"), launcher="playwright")


@pytest.mark.asyncio
async def test_a_cdp_session_drives_a_page(pool, site):
    s = await pool.create("c", headless=True, driver="cdp")
    assert isinstance(s, CdpSession) and s.status()["driver"] == "cdp" and s.is_alive
    state = await s.goto(site + "/")
    assert state["title"] == "cdp test" and state["url"] == site + "/"
    assert await s.evaluate("1 + 1") == 2
    assert await s.evaluate("() => document.getElementById('msg').innerText") == "hello world"
    assert await s.evaluate("async () => { await 0; return [1, {a: 2}] }") == [1, {"a": 2}]
    assert await s.evaluate("(function () { return document.title })()") == "cdp test"
    await s.fill("#name", "abu")
    await s.click("#go", wait_after=None)
    assert (await s.page_state())["title"] == "clicked abu"
    await s.check("#agree")
    assert await s.is_checked("#agree") and await s.inner_text("#msg") == "hello world"
    assert await s.get_attribute("#go", "id") == "go"
    assert await s.click("b:has-text('world')", wait_after=None)
    assert (await s.screenshot())[:4] == b"\x89PNG" and "<b>world</b>" in await s.content()
    assert {"tag": "button"}.items() <= [e for e in await s.observe() if e["id"] == "go"][0].items()
    await s.set_cookies([{"name": "k", "value": "v", "url": site + "/"}])
    assert {c["name"]: c["value"] for c in await s.cookies()}["k"] == "v"
    assert [c["name"] for c in await s.cookies([site + "/"])] == ["k"]
    await s.delete_cookies()
    assert await s.cookies() == []
    await s.new_page()
    assert len(await s.pages()) == 2
    await s.close_page()
    assert len(await s.pages()) == 1


@pytest.mark.asyncio
async def test_calls_that_need_a_library_say_so(pool, site):
    s = await pool.create("u", headless=True, driver="cdp")
    await s.goto(site + "/")
    for call in (s.intercept("**/*", lambda r: r), s.aria_snapshot(), s.set_extra_headers({"x": "y"}),
                 s.mock_response("**/*"), s.emulate_media(color_scheme="dark")):
        with pytest.raises(Unsupported, match="cdp driver"):
            await call
    assert isinstance(Unsupported("x"), AttributeError)


@pytest.mark.asyncio
async def test_the_cdp_driver_never_enables_the_runtime_domain(pool, site):
    s = await pool.create("r", headless=True, driver="cdp")
    await s.goto(site + "/")
    seen = []
    page = s._active_page()
    page.target.on("Runtime.executionContextCreated", lambda p: seen.append(p))
    await s.evaluate("1")
    await s.click("#go", wait_after=None)
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", _DRIVERS)
async def test_a_page_that_stalls_is_retried_then_times_out(pool, site, driver):
    s = await pool.create(f"stall-{driver}", headless=True, driver=driver, nav_timeout=1500)
    began = asyncio.get_running_loop().time()
    with pytest.raises(_driver.TIMEOUTS):
        await s.goto(site + "/stalls")
    took = asyncio.get_running_loop().time() - began
    assert took >= 2.8, "one retry means two full attempts"
    began = asyncio.get_running_loop().time()
    with pytest.raises(_driver.TIMEOUTS):
        await s.goto(site + "/stalls", timeout=1000, retries=0)
    assert asyncio.get_running_loop().time() - began < 2.5


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", _DRIVERS)
async def test_clear_challenge_does_not_call_a_page_that_never_loaded_clear(pool, site, driver):
    s = await pool.create(f"nolo-{driver}", headless=True, driver=driver, nav_timeout=1500)
    with pytest.raises(_driver.TIMEOUTS):
        await s.goto(site + "/stalls", retries=0)
    out = await s.clear_challenge(timeout=14)
    assert out["status"] == "load-failed" and out["clicks"] == 0
    await s.goto(site + "/")
    assert (await s.clear_challenge(timeout=14))["status"] == "clear"


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", _DRIVERS)
async def test_an_unreachable_page_is_a_failed_load_not_a_clear_one(pool, driver):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    s = await pool.create(f"dead-{driver}", headless=True, driver=driver)
    with pytest.raises(Exception, match="(?i)ERR_CONNECTION_REFUSED"):
        await s.goto(f"http://127.0.0.1:{closed}/")
    out = await s.clear_challenge(timeout=14)
    assert out["status"] == "load-failed"


@pytest.mark.asyncio
async def test_a_headless_cdp_session_looks_like_a_desktop_chrome(pool, site):
    s = await pool.create("id", headless=True, driver="cdp")
    await s.goto(site + "/")
    seen = await s.evaluate("""async () => ({
      ua: navigator.userAgent, webdriver: navigator.webdriver, screen: [screen.width, screen.height, screen.availHeight],
      pointer: matchMedia('(pointer: fine)').matches,
      hints: await navigator.userAgentData.getHighEntropyValues(['fullVersionList', 'architecture'])})""")
    assert "HeadlessChrome" not in seen["ua"] and not seen["webdriver"]
    assert seen["screen"][2] < seen["screen"][1] and seen["pointer"] and seen["hints"]["architecture"]
    assert s._browser.version in {b["version"] for b in seen["hints"]["fullVersionList"]}


@pytest.mark.asyncio
async def test_humanised_input_works_on_the_cdp_driver(pool, site):
    s = await pool.create("h", headless=True, driver="cdp", humanize=True)
    await s.goto(site + "/")
    await s.fill("#name", "ok")
    await s.click("#go", wait_after=None)
    assert (await s.page_state())["title"] == "clicked ok"


@pytest.mark.asyncio
async def test_dialogs_and_popups_reach_the_session(pool, site):
    s = await pool.create("d", headless=True, driver="cdp")
    await s.goto(site + "/")
    await s.evaluate("setTimeout(() => alert('hello'), 0); 1")
    seen = None
    for _ in range(40):
        seen = [e for e in s.events.since(0, {"dialog"})]
        if seen:
            break
        await asyncio.sleep(0.1)
    assert seen and seen[0]["message"] == "hello" and seen[0]["handled"] == "accepted"
    assert await s.evaluate("2 + 2") == 4  # the page was not left blocked
    await s.click("#pop", wait_after=None)
    for _ in range(50):
        if len(await s.pages()) == 2:
            break
        await asyncio.sleep(0.1)
    assert len(await s.pages()) == 2
    assert [e for e in s.events.since(0, {"popup"})]

