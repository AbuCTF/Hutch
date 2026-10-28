import asyncio
import http.server
import json
import os
import threading

import aiohttp
import pytest
import pytest_asyncio

from hutch import browser
from hutch.pool import Pool


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("www")
    (root / "index.html").write_text(
        "<title>form</title><input id=q style='position:absolute;left:40px;top:40px;width:300px'>"
        "<button id=b style='position:absolute;left:40px;top:100px' "
        "onclick=\"document.title='clicked'\">go</button>")
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


async def _until(ws, pred, timeout=10):
    async def loop():
        while True:
            m = await ws.receive()
            if m.type == aiohttp.WSMsgType.BINARY:
                if pred(("frame", m.data)):
                    return m.data
            elif m.type == aiohttp.WSMsgType.TEXT:
                d = json.loads(m.data)
                if pred((d["t"], d)):
                    return d
            else:
                raise AssertionError(f"socket closed: {m.type}")
    return await asyncio.wait_for(loop(), timeout)


async def _eventually(fn, expect, timeout=5):
    for _ in range(int(timeout / 0.1)):
        if await fn() == expect:
            return
        await asyncio.sleep(0.1)
    assert await fn() == expect


@pytest.mark.asyncio
async def test_live_view_streams_and_drives_the_agent_tab(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("live", headless=True)
        await s.goto(site)
        url = await pool.view("live")
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as r:
                assert r.status == 200 and "<canvas" in await r.text()
            async with http.get(url.replace("t=", "t=x")) as r:
                assert r.status == 403
            async with http.ws_connect(url.replace("/v/", "/ws/")) as ws:
                tabs = await _until(ws, lambda m: m[0] == "tabs" and m[1]["current"])
                assert any(t["url"].startswith(site) for t in tabs["tabs"]
                           if t["id"] == tabs["current"])
                frame = await _until(ws, lambda m: m[0] == "frame")
                assert frame[:2] == b"\xff\xd8"

                box = await s.evaluate("(() => { const r = q.getBoundingClientRect();"
                                       " return [r.x + r.width / 2, r.y + r.height / 2]; })()")
                for kind in ("mousePressed", "mouseReleased"):
                    await ws.send_json({"t": "mouse", "type": kind, "x": box[0], "y": box[1],
                                        "button": "left", "buttons": 1, "clickCount": 1})
                for ch in "hi":
                    await ws.send_json({"t": "key", "type": "keyDown", "key": ch, "text": ch,
                                        "code": f"Key{ch.upper()}", "keyCode": ord(ch.upper())})
                    await ws.send_json({"t": "key", "type": "keyUp", "key": ch,
                                        "code": f"Key{ch.upper()}", "keyCode": ord(ch.upper())})
                await ws.send_json({"t": "text", "text": " there"})
                await _eventually(lambda: s.evaluate("q.value"), "hi there")

                await s.evaluate("q.select()")
                await ws.send_json({"t": "copy"})
                assert (await _until(ws, lambda m: m[0] == "clip"))["text"] == "hi there"

                await ws.send_json({"t": "mouse", "type": "mousePressed", "x": 50, "y": 110,
                                    "button": "left", "buttons": 1, "clickCount": 1})
                await ws.send_json({"t": "mouse", "type": "mouseReleased", "x": 50, "y": 110,
                                    "button": "left", "buttons": 0, "clickCount": 1})
                await _eventually(lambda: s.evaluate("document.title"), "clicked")

                await ws.send_json({"t": "nav", "url": site + "/?moved"})
                await _eventually(lambda: asyncio.sleep(0, s._active_page().url.endswith("?moved")),
                                  True)

                first = tabs["current"]
                await ws.send_json({"t": "newtab"})
                tabs = await _until(ws, lambda m: m[0] == "tabs" and m[1]["current"] != first)
                assert len(tabs["tabs"]) == 2


@pytest.mark.asyncio
async def test_live_view_reports_a_stopped_browser(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("gone", headless=True)
        await s.goto(site)
        url = await pool.view("gone")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url.replace("/v/", "/ws/")) as ws:
                await _until(ws, lambda m: m[0] == "frame")
                await browser.stop(s.profile_dir)
                await _until(ws, lambda m: m[0] == "gone")
            async with http.ws_connect(url.replace("/v/", "/ws/")) as ws:
                await _until(ws, lambda m: m[0] == "gone")


@pytest.mark.asyncio
async def test_agent_launch_of_stopped_profile_is_headless(base):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("shown", headless=False, launch=False)
        s = await pool.get("shown", launch=True)
        assert s.headless and browser.status(s.profile_dir)["headless"]
    assert browser.status(os.path.join(base, "shown")) is None
