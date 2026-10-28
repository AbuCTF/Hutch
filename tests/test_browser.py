import http.server
import os
import re
import threading

import pytest
import pytest_asyncio

from hutch import browser
from hutch.fingerprint import generate
from hutch.pool import Pool
from hutch.session import ProxyConfig, Session, _match_engine, _ref


def test_ref_resolution():
    assert _ref("e12") == "aria-ref=e12"
    assert _ref("f1e3") == "aria-ref=f1e3"
    assert _ref("#e12") == "#e12"
    assert _ref("button") == "button"


def test_match_engine_pins_persona_version():
    fp = generate(preset="win-desktop-1080p")
    _match_engine(fp, "156.0.8078.4")
    assert "Chrome/156.0.0.0" in fp.user_agent
    assert '"Google Chrome";v="156"' in fp.sec_ch_ua


def test_launcher_selection(tmp_path):
    assert Session("a", str(tmp_path / "a")).launcher == "native"
    assert Session("b", str(tmp_path / "b"), record_video=str(tmp_path)).launcher == "playwright"
    authed = ProxyConfig(server="http://p:1", username="u", password="p")
    assert Session("c", str(tmp_path / "c"), proxy=authed).launcher == "playwright"
    assert Session("d", str(tmp_path / "d"), endpoint="9222").launcher == "external"


def test_meta_round_trip(tmp_path):
    fp = generate(preset="mac-desktop-retina")
    Session("p", str(tmp_path / "p"), fingerprint=fp, browser="chrome", headless=False)
    s = Session.from_profile_dir(str(tmp_path / "p"))
    assert s.persona and s.fingerprint.sec_ch_ua == fp.sec_ch_ua
    assert (s.browser, s.launcher, s.headless) == ("chrome", "native", False)

    Session("r", str(tmp_path / "r"))
    assert not Session.from_profile_dir(str(tmp_path / "r")).persona


def test_legacy_meta_stays_on_playwright(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    (d / "hutch_meta.json").write_text('{"name": "old", "fingerprint": {"viewport": "1366x768"}}')
    s = Session.from_profile_dir(str(d))
    assert s.launcher == "playwright" and s.persona


def test_build_args():
    args = browser.build_args("/p", headless=True, proxy="http://127.0.0.1:8080",
                              ignore_https_errors=True)
    assert "--remote-debugging-port=0" in args
    assert "--disable-blink-features=AutomationControlled" in args
    assert "--headless=new" in args and "--ignore-certificate-errors" in args
    assert not any("site-per-process" in a for a in args)
    plain = browser.build_args("/p", debug=False)
    assert not any(a.startswith("--remote-debugging") for a in plain)


def test_clone_skips_caches_and_locks(tmp_path):
    src = tmp_path / "src"
    (src / "Default" / "Cache").mkdir(parents=True)
    (src / "Default" / "Cookies").write_text("c")
    (src / "Default" / "Cache" / "blob").write_text("x")
    (src / "DevToolsActivePort").write_text("1\n/x")
    browser.clone_profile(str(src), str(tmp_path / "dst"))
    assert (tmp_path / "dst" / "Default" / "Cookies").exists()
    assert not (tmp_path / "dst" / "Default" / "Cache").exists()
    assert not (tmp_path / "dst" / "DevToolsActivePort").exists()


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("www")
    (root / "index.html").write_text("<title>t</title><button>go</button>")
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **kw)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest_asyncio.fixture
async def base(tmp_path):
    yield str(tmp_path / "profiles")
    for name in os.listdir(tmp_path / "profiles") if (tmp_path / "profiles").exists() else []:
        await browser.stop(str(tmp_path / "profiles" / name))


@pytest.mark.asyncio
async def test_native_session_is_real_browser(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("real", headless=True)
        assert s.status()["launcher"] == "native" and s._owned
        state = await s.goto(site)
        assert "[ref=" in state["dom"]
        assert await s.evaluate("navigator.webdriver") is False
        assert "Headless" not in await s.evaluate("navigator.userAgent")
        ref = re.search(r'button "go" \[ref=(\w+)\]', state["dom"]).group(1)
        await s.click(ref)
    assert browser.status(os.path.join(base, "real")) is None


@pytest.mark.asyncio
async def test_second_client_attaches_without_owning(base, site):
    async with Pool(base_dir=base) as owner:
        s = await owner.create("shared", headless=True)
        await s.goto(site + "/?owner")
        async with Pool(base_dir=base) as agent:
            other = await agent.get("shared", launch=True)
            assert not other._owned
            assert any("owner" in p["url"] for p in await other.pages())
        assert browser.status(os.path.join(base, "shared"))
        assert s.is_alive


@pytest.mark.asyncio
async def test_relaunch_restores_tabs_and_session_state(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("restore", headless=True)
        await s.goto(site + "/?a")
        await s.evaluate("sessionStorage.setItem('k', 'v'); document.cookie = 'sid=1; path=/'")
        await s.close()
        s = await pool.get("restore", launch=True)
        page = next(p for p in s._pages if p.url.endswith("?a"))
        assert await page.evaluate("sessionStorage.getItem('k')") == "v"
        assert "sid=1" in await page.evaluate("document.cookie")


@pytest.mark.asyncio
async def test_browser_exit_hibernates_session(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("gone", headless=True)
        await s.goto(site)
        gone = []
        s.on_close(gone.append)
        await browser.stop(s.profile_dir)
        for _ in range(50):
            if gone:
                break
            await __import__("asyncio").sleep(0.05)
        assert gone == ["gone"] and not s.is_alive
        s = await pool.get("gone", launch=True)
        assert s.is_alive


@pytest.mark.asyncio
async def test_joining_client_acts_on_the_tab_in_front(base, site):
    async with Pool(base_dir=base) as owner:
        s = await owner.create("front", headless=True)
        await s.goto(site + "/?a")
        st = browser.status(s.profile_dir)
        for q in ("b", "c"):
            await browser.cdp_call(st["ws"], "Target.createTarget", {"url": f"{site}/?{q}"})
        targets = (await browser.cdp_call(st["ws"], "Target.getTargets"))["targetInfos"]
        b = next(t["targetId"] for t in targets if t["url"].endswith("?b"))
        await browser.cdp_call(st["ws"], "Target.activateTarget", {"targetId": b})
        async with Pool(base_dir=base) as agent:
            other = await agent.get("front", launch=True)
            assert other._active_page().url.endswith("?b")
