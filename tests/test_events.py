import asyncio
import http.server
import json
import os
import sys
import threading

import aiohttp
import pytest
import pytest_asyncio

from hutch import browser
from hutch.events import EventLog
from hutch.pool import Pool
from hutch.session import Session

_PAGE = b"""<title>events</title>
<button id=alert onclick="alert('xss')">alert</button>
<button id=confirm onclick="document.title = String(confirm('sure?'))">confirm</button>
<button id=prompt onclick="document.title = String(prompt('name?', 'def'))">prompt</button>
<a id=dl href="/file">download</a>
<input id=file type=file>
<button id=pop onclick="window.open('/popped', '_blank')">popup</button>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/file"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Disposition", "attachment; filename=report.txt")
            self.end_headers()
            self.wfile.write(b"secret report")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<title>popped</title>" if self.path.startswith("/popped") else _PAGE)


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest_asyncio.fixture
async def base(tmp_path):
    yield str(tmp_path / "profiles")
    for name in os.listdir(tmp_path / "profiles") if (tmp_path / "profiles").exists() else []:
        await browser.stop(str(tmp_path / "profiles" / name))


@pytest.mark.asyncio
async def test_event_log_wait_and_filter():
    log = EventLog(maxlen=3)
    assert await log.wait(0, timeout=0.05) == []
    asyncio.get_running_loop().call_later(0.05, lambda: log.emit("popup", url="x"))
    got = await log.wait(0, {"popup"}, timeout=2)
    assert [e["type"] for e in got] == ["popup"]
    for i in range(4):
        log.emit("dialog", n=i)
    assert [e["n"] for e in log.since(0, {"dialog"})] == [1, 2, 3]
    assert log.last == 5


@pytest.mark.asyncio
async def test_agent_dialogs_answered_by_default_plan_and_policy(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("dlg", headless=True)
        await s.goto(site)
        page = s._active_page()

        await asyncio.wait_for(page.click("#alert"), 5)
        await asyncio.wait_for(page.click("#confirm"), 5)
        assert await page.title() == "false"
        s.arm_dialog(accept=True)
        await page.click("#confirm")
        assert await page.title() == "true"
        s.arm_dialog(accept=True, text="abu")
        await page.click("#prompt")
        assert await page.title() == "abu"
        s.handle_dialog("accept")
        await page.click("#confirm")
        assert await page.title() == "true"

        dialogs = [(e["kind"], e["handled"]) for e in s.events.since(0, {"dialog"})]
        assert dialogs == [("alert", "accepted"), ("confirm", "dismissed"), ("confirm", "accepted"),
                           ("prompt", "accepted"), ("confirm", "accepted")]
        alert = s.events.since(0, {"dialog"})[0]
        assert alert["message"] == "xss" and alert["tab"] == 0 and alert["url"].startswith(site)


@pytest.mark.asyncio
async def test_downloads_land_in_the_profile_and_are_reported(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("dl", headless=True)
        await s.goto(site)
        result = await s.expect_download("#dl", save_path=os.path.join(base, "copy.txt"))
        assert result["path"] == os.path.join(s.download_dir, "report.txt")
        assert open(result["path"]).read() == "secret report"
        assert open(result["saved_to"]).read() == "secret report"
        states = [(e["state"], e["filename"], e["tab"]) for e in s.events.since(0, {"download"})]
        assert states == [("started", "report.txt", 0), ("completed", "report.txt", 0)]
    assert not os.path.exists(os.path.expanduser("~/Downloads/report.txt")) or \
        open(os.path.expanduser("~/Downloads/report.txt")).read() != "secret report"


@pytest.mark.asyncio
async def test_file_chooser_and_popup_events(base, site, tmp_path):
    upload = tmp_path / "poc.svg"
    upload.write_text("<svg/>")
    async with Pool(base_dir=base) as pool:
        s = await pool.create("fc", headless=True)
        await s.goto(site)
        page = s._active_page()
        seq = s.events.last
        await page.click("#file")
        got = await s.events.wait(seq, {"filechooser"}, timeout=5)
        assert got and got[0]["multiple"] is False
        await s.set_files([str(upload)])
        assert await page.evaluate("file.files[0].name") == "poc.svg"
        with pytest.raises(RuntimeError):
            await s.set_files([str(upload)])

        seq = s.events.last
        await page.click("#pop")
        got = await s.events.wait(seq, {"popup"}, timeout=5)
        assert got[0]["url"].endswith("/popped") and got[0]["opener"] == 0 and got[0]["agent"]
        assert s._active_page().url.endswith("/popped")


@pytest.mark.asyncio
async def test_dialogs_on_human_tabs_are_left_open(base, site):
    profile = os.path.join(base, "shared")
    owner = Session("shared", profile, headless=True)
    st = browser.spawn(profile, **owner.spawn_options())
    human = (await browser.cdp_call(st["ws"], "Target.createTarget", {"url": site}))["targetId"]
    state = os.path.join(base, "shared", "hutch_browser.json")
    with open(state) as f:
        meta = json.load(f)
    with open(state, "w") as f:
        json.dump({**meta, "headless": False}, f)
    async with Pool(base_dir=base) as agent:
        s = await agent.get("shared", launch=True)
        assert not s.headless and not s._owned
        page = next(p for p in s._pages if p.url.startswith(site))
        assert not s._is_agent_page(page)
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(st["ws"], max_msg_size=0) as ws:
                await ws.send_json({"id": 1, "method": "Target.attachToTarget",
                                    "params": {"targetId": human, "flatten": True}})
                sid = None
                while sid is None:
                    m = json.loads((await ws.receive()).data)
                    sid = m.get("result", {}).get("sessionId")
                await ws.send_json({"id": 4, "sessionId": sid, "method": "Page.enable"})
                await ws.send_json({"id": 2, "sessionId": sid, "method": "Runtime.evaluate",
                                    "params": {"expression": "setTimeout(() => alert('hi'))"}})
                got = await s.events.wait(0, {"dialog"}, timeout=5)
                assert got[0]["handled"] == "human"
                await asyncio.sleep(0.5)
                await ws.send_json({"id": 3, "sessionId": sid, "method": "Page.handleJavaScriptDialog",
                                    "params": {"accept": True}})
                while True:
                    m = json.loads((await ws.receive()).data)
                    if m.get("id") == 3:
                        break
                assert "error" not in m


@pytest.mark.asyncio
async def test_live_view_shows_dialogs(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("lv", headless=True)
        await s.goto(site)
        url = await pool.view("lv")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url.replace("/v/", "/ws/")) as ws:
                kinds = []

                async def read():
                    while True:
                        m = await ws.receive()
                        if m.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(m.data)
                            kinds.append(d["t"])
                            if d["t"] == "dialog_closed":
                                return
                reader = asyncio.ensure_future(read())
                await asyncio.sleep(0.5)
                await s._active_page().click("#alert")
                await asyncio.wait_for(reader, 5)
                assert "dialog" in kinds


@pytest.mark.asyncio
async def test_mcp_appends_events_to_tool_results(base, site):
    mcp = pytest.importorskip("mcp")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "hutch.mcp"],
                                   env={**os.environ, "HUTCH_BASE_DIR": base})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as client:
        await client.initialize()
        tools = {t.name for t in (await client.list_tools()).tools}
        assert "hutch_events" in tools
        await client.call_tool("hutch_sessions", {"action": "create", "name": "m", "headless": True})
        await client.call_tool("hutch_navigate", {"session": "m", "url": site})
        res = await client.call_tool("hutch_interact", {"session": "m", "action": "click", "selector": "#alert"})
        text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
        assert "events:" in text and "alert" in text and "'xss'" in text
        res = await client.call_tool("hutch_events", {"session": "m", "action": "list", "types": ["dialog"]})
        assert json.loads(res.content[0].text)[0]["message"] == "xss"
        await client.call_tool("hutch_events", {"session": "m", "action": "dialog", "accept": True})
        await client.call_tool("hutch_interact", {"session": "m", "action": "click", "selector": "#confirm"})
        res = await client.call_tool("hutch_observe", {"session": "m"})
        assert "true" in res.content[0].text.splitlines()[0]
        await client.call_tool("hutch_sessions", {"action": "close", "name": "m"})
