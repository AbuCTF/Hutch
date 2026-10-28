import http.server
import os
import sys
import threading

import pytest
import pytest_asyncio

from hutch import browser, snapshot
from hutch.pool import Pool

_SNAP = '''- generic [active] [ref=e1]:
  - heading "Shop" [level=1] [ref=e2]
  - navigation [ref=e3]:
    - link "Home" [ref=e4] [cursor=pointer]:
      - /url: /a
  - paragraph [ref=e12]: idle
  - generic [ref=e20] [cursor=pointer]: Fake
  - iframe [ref=e14] [box=8,174,304,154]:
    - button "inner" [ref=f1e2] [box=8,8,46,21]
  - button "Save" [ref=e11] [box=359,85,46,21]'''

_PAGE = b"""<title>shop</title><h1>Shop</h1>
<nav><a href="/next">Next page</a></nav>
<label>Email <input id=email></label>
<button id=save onclick="msg.textContent='Saved!'">Save</button>
<p id=msg>idle</p>
<p>static text one</p><p>static text two</p>
<div style="height:1500px"></div>
<button id=far>Far below</button>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<title>next</title><h1>Next</h1>" if self.path == "/next" else _PAGE)


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


def test_interactive_keeps_actionable_elements_and_headings():
    out = snapshot.interactive(_SNAP).splitlines()
    assert out[0] == '- heading "Shop" [level=1] [ref=e2]'
    assert '- link "Home" [ref=e4] -> /a' in out
    assert "- generic [ref=e20] [cursor=pointer]: Fake" in out
    assert '- button "inner" [ref=f1e2]' in out
    assert not any("paragraph" in l or "navigation" in l for l in out)


def test_diff_shows_changes_with_parent_and_ignores_focus():
    new = _SNAP.replace("idle", "Saved!").replace("[active] ", "").replace(
        '    - link "Home"', '    - link "New" [ref=e15] [cursor=pointer]:\n      - /url: /new\n    - link "Home"')
    out = snapshot.diff(_SNAP, new).splitlines()
    assert out == ["  navigation [ref=e3]:", '+ link "New" [ref=e15] [cursor=pointer]:', "+ /url: /new",
                   "  generic [ref=e1]:", "- paragraph [ref=e12]: idle", "+ paragraph [ref=e12]: Saved!"]
    assert snapshot.diff(_SNAP, _SNAP.replace("[active] ", "")) == ""


def test_boxes_offset_iframe_children():
    got = {b["ref"]: (b["x"], b["y"], b["w"], b["h"]) for b in snapshot.boxes(_SNAP)}
    assert got == {"f1e2": (16, 182, 46, 21), "e11": (359, 85, 46, 21)}


@pytest.mark.asyncio
async def test_actions_return_only_changes(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("diff", headless=True)
        st = await s.goto(site)
        assert st["changes"] is None and "static text one" in st["dom"]
        st = await s.click("#save")
        assert "+ paragraph" in st["changes"] and "Saved!" in st["changes"]
        assert "static text" not in st["changes"]
        assert await s.aria_snapshot(diff=True) == "no changes"
        assert "a@b.c" in (await s.fill("#email", "a@b.c"))["changes"]
        await s.evaluate("msg.textContent = 'changed behind the agent'")
        assert "changed behind the agent" in await s.aria_snapshot(diff=True)
        st = await s.click("text=Next page")
        assert st["changes"] is None and "Next" in st["dom"]

        await s.goto(site)
        inter = await s.aria_snapshot(interactive=True)
        assert 'link "Next page"' in inter and "textbox" in inter and "static text" not in inter
        assert len(inter) < len(await s.aria_snapshot()) / 2


@pytest.mark.asyncio
async def test_annotated_screenshot_labels_visible_elements(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("shot", headless=True)
        await s.goto(site)
        page = s._active_page()
        await page.evaluate("scrollTo(0, 40)")
        png, legend = await s.annotated_screenshot()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert 'button "Save"' in legend and "Far below" not in legend
        assert await page.evaluate("document.getElementById('__hutch_annotations')") is None

        ref = next(l.split()[0] for l in legend.splitlines() if '"Save"' in l)
        boxes = {b["ref"]: b for b in snapshot.boxes(await page.aria_snapshot(mode="ai", boxes=True))}
        real = await page.locator(f"aria-ref={ref}").bounding_box()
        assert abs(boxes[ref]["x"] - real["x"]) <= 1 and abs(boxes[ref]["y"] - real["y"]) <= 1


@pytest.mark.asyncio
async def test_mcp_actions_return_changes_and_annotations(base, site):
    pytest.importorskip("mcp")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "hutch.mcp"],
                                   env={**os.environ, "HUTCH_BASE_DIR": base})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as client:
        await client.initialize()
        await client.call_tool("hutch_sessions", {"action": "create", "name": "m", "headless": True})
        res = await client.call_tool("hutch_navigate", {"session": "m", "url": site})
        assert "static text one" in res.content[0].text
        res = await client.call_tool("hutch_interact", {"session": "m", "action": "click", "selector": "#save"})
        text = res.content[0].text
        assert text.splitlines()[1] == "changes:" and "Saved!" in text and "static text" not in text
        res = await client.call_tool("hutch_observe", {"session": "m", "filter": "interactive"})
        assert "textbox" in res.content[0].text and "static text" not in res.content[0].text
        res = await client.call_tool("hutch_screenshot", {"session": "m", "annotate": True})
        assert res.content[0].type == "image" and '"Save"' in res.content[1].text
        await client.call_tool("hutch_sessions", {"action": "close", "name": "m"})
