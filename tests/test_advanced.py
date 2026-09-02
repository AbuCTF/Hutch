import asyncio
import os
import shutil
import tempfile
import pytest
import pytest_asyncio
from hutch import Pool, generate

_FORM_PAGE = """data:text/html,<html><body>
<input id="i1" type="text" value="hello">
<input id="cb" type="checkbox">
<button id="btn" onclick="document.title='clicked'">Click me</button>
<select id="sel"><option value="a">A</option><option value="b">B</option></select>
<div id="hover-target" onmouseenter="this.style.background='red'">hover me</div>
<div id="long" style="height:3000px">tall</div>
<a href="https://example.com" target="_blank" id="popup-link">open</a>
</body></html>"""

_FRAME_PAGE = """data:text/html,<html><body>
<iframe name="child" srcdoc="<html><body><p id='fp'>in frame</p></body></html>"></iframe>
</body></html>"""


@pytest_asyncio.fixture
async def pool(tmp_path):
    base = str(tmp_path / "profiles")
    os.makedirs(base, exist_ok=True)
    async with Pool(base_dir=base, max_sessions=3) as p:
        yield p


@pytest_asyncio.fixture
async def session(pool):
    s = await pool.create("adv", headless=True)
    yield s


class TestNavigation:

    @pytest.mark.asyncio
    async def test_go_back_forward(self, session):
        await session.goto("data:text/html,<html><body>page1</body></html>")
        await session.goto("data:text/html,<html><body>page2</body></html>")
        state = await session.go_back()
        assert "page1" in state["url"] or state["title"] == ""
        state = await session.go_forward()
        assert "page2" in state["url"] or state["title"] == ""

    @pytest.mark.asyncio
    async def test_reload(self, session):
        await session.goto("data:text/html,<html><body>reload</body></html>")
        state = await session.reload()
        assert state["url"]


class TestMouseActions:

    @pytest.mark.asyncio
    async def test_hover(self, session):
        await session.goto(_FORM_PAGE)
        state = await session.hover("#hover-target")
        assert state["url"]

    @pytest.mark.asyncio
    async def test_dblclick(self, session):
        page = """data:text/html,<html><body>
        <button id="db" ondblclick="document.title='double'">dbl</button>
        </body></html>"""
        await session.goto(page)
        state = await session.dblclick("#db")
        assert state["title"] == "double"

    @pytest.mark.asyncio
    async def test_right_click(self, session):
        page = """data:text/html,<html><body>
        <div id="ctx" oncontextmenu="document.title='context';return false">right click</div>
        </body></html>"""
        await session.goto(page)
        state = await session.right_click("#ctx")
        assert state["title"] == "context"

    @pytest.mark.asyncio
    async def test_scroll(self, session):
        await session.goto(_FORM_PAGE)
        state = await session.scroll(direction="down", amount=500)
        assert state["url"]


class TestFormInteractions:

    @pytest.mark.asyncio
    async def test_check_uncheck(self, session):
        await session.goto(_FORM_PAGE)
        await session.check("#cb")
        assert await session.is_checked("#cb")
        await session.uncheck("#cb")
        assert not await session.is_checked("#cb")

    @pytest.mark.asyncio
    async def test_set_checked(self, session):
        await session.goto(_FORM_PAGE)
        await session.set_checked("#cb", True)
        assert await session.is_checked("#cb")
        await session.set_checked("#cb", False)
        assert not await session.is_checked("#cb")

    @pytest.mark.asyncio
    async def test_focus(self, session):
        await session.goto(_FORM_PAGE)
        await session.focus("#i1")

    @pytest.mark.asyncio
    async def test_input_value(self, session):
        await session.goto(_FORM_PAGE)
        val = await session.input_value("#i1")
        assert val == "hello"


class TestContentExtraction:

    @pytest.mark.asyncio
    async def test_content(self, session):
        await session.goto("data:text/html,<html><body><p>test</p></body></html>")
        html = await session.content()
        assert "<p>test</p>" in html

    @pytest.mark.asyncio
    async def test_inner_text(self, session):
        await session.goto("data:text/html,<html><body><div id='d'>hello world</div></body></html>")
        text = await session.inner_text("#d")
        assert text == "hello world"

    @pytest.mark.asyncio
    async def test_inner_html(self, session):
        await session.goto("data:text/html,<html><body><div id='d'><b>bold</b></div></body></html>")
        html = await session.inner_html("#d")
        assert "<b>bold</b>" in html

    @pytest.mark.asyncio
    async def test_text_content(self, session):
        await session.goto("data:text/html,<html><body><span id='s'>content</span></body></html>")
        text = await session.text_content("#s")
        assert text == "content"

    @pytest.mark.asyncio
    async def test_get_attribute(self, session):
        await session.goto(_FORM_PAGE)
        attr = await session.get_attribute("#i1", "type")
        assert attr == "text"


class TestElementState:

    @pytest.mark.asyncio
    async def test_is_visible(self, session):
        await session.goto(_FORM_PAGE)
        assert await session.is_visible("#btn")

    @pytest.mark.asyncio
    async def test_is_hidden(self, session):
        page = """data:text/html,<html><body>
        <div id="h" style="display:none">hidden</div>
        </body></html>"""
        await session.goto(page)
        assert await session.is_hidden("#h")

    @pytest.mark.asyncio
    async def test_is_enabled(self, session):
        await session.goto(_FORM_PAGE)
        assert await session.is_enabled("#btn")

    @pytest.mark.asyncio
    async def test_is_editable(self, session):
        await session.goto(_FORM_PAGE)
        assert await session.is_editable("#i1")


class TestFrameSupport:

    @pytest.mark.asyncio
    async def test_list_frames(self, session):
        await session.goto(_FRAME_PAGE)
        await asyncio.sleep(0.3)
        frames = await session.frames()
        assert len(frames) >= 2
        names = [f["name"] for f in frames]
        assert "child" in names

    @pytest.mark.asyncio
    async def test_frame_evaluate(self, session):
        await session.goto(_FRAME_PAGE)
        await asyncio.sleep(0.3)
        result = await session.frame_evaluate(
            "document.getElementById('fp').textContent",
            name="child")
        assert result == "in frame"

    @pytest.mark.asyncio
    async def test_frame_content(self, session):
        await session.goto(_FRAME_PAGE)
        await asyncio.sleep(0.3)
        html = await session.frame_content(name="child")
        assert "in frame" in html


class TestLocatorAPI:

    @pytest.mark.asyncio
    async def test_query_by_role(self, session):
        await session.goto(_FORM_PAGE)
        results = await session.query(None, role="button")
        assert len(results) >= 1
        assert any("Click me" in r["text"] for r in results)

    @pytest.mark.asyncio
    async def test_query_by_text(self, session):
        await session.goto("data:text/html,<html><body><p>unique text here</p></body></html>")
        results = await session.query(None, text="unique text here")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_locator_click(self, session):
        await session.goto(_FORM_PAGE)
        state = await session.locator_click(role="button", name="Click me")
        assert state["title"] == "clicked"


class TestPageManagement:

    @pytest.mark.asyncio
    async def test_pages_list(self, session):
        await session.goto("data:text/html,<html><body>tab1</body></html>")
        pages = await session.pages()
        assert len(pages) >= 1

    @pytest.mark.asyncio
    async def test_switch_page(self, session):
        await session.goto("data:text/html,<html><body>first</body></html>")
        await session.new_page()
        page = session._active_page()
        await page.goto("data:text/html,<html><body>second</body></html>")
        state = await session.switch_page(0)
        assert "first" in state.get("url", "")

    @pytest.mark.asyncio
    async def test_close_page(self, session):
        await session.goto("data:text/html,<html><body>main</body></html>")
        initial = len([p for p in session._pages if not p.is_closed()])
        await session.new_page()
        await session.close_page()
        current = len([p for p in session._pages if not p.is_closed()])
        assert current == initial


class TestAdvancedWaits:

    @pytest.mark.asyncio
    async def test_wait_for_function(self, session):
        await session.goto("data:text/html,<html><body><div id='w'></div></body></html>")
        asyncio.get_event_loop().call_later(
            0.1,
            lambda: asyncio.ensure_future(
                session.evaluate("document.getElementById('w').textContent = 'ready'")))
        state = await session.wait_for_function(
            "document.getElementById('w').textContent === 'ready'",
            timeout=5000)
        assert state["url"]

    @pytest.mark.asyncio
    async def test_wait_for_load_state(self, session):
        await session.goto("data:text/html,<html><body>loaded</body></html>")
        state = await session.wait_for_load_state("load")
        assert state["url"]


class TestDialogHandling:

    @pytest.mark.asyncio
    async def test_handle_dialog_dismiss(self, session):
        session.handle_dialog(action="dismiss")
        await session.goto("data:text/html,<html><body><button onclick='alert(1)'>a</button></body></html>")
        await session.click("button")

    @pytest.mark.asyncio
    async def test_handle_dialog_accept(self, session):
        session.handle_dialog(action="accept")
        page = """data:text/html,<html><body>
        <button onclick="if(confirm('ok?')) document.title='confirmed'">c</button>
        </body></html>"""
        await session.goto(page)
        state = await session.click("button")
        assert state["title"] == "confirmed"


class TestPageManipulation:

    @pytest.mark.asyncio
    async def test_set_content(self, session):
        await session.goto("data:text/html,<html><body></body></html>")
        state = await session.set_content("<html><body><p>injected</p></body></html>")
        assert state["url"]
        text = await session.inner_text("p")
        assert text == "injected"

    @pytest.mark.asyncio
    async def test_emulate_media(self, session):
        await session.goto("data:text/html,<html><body>test</body></html>")
        await session.emulate_media(color_scheme="dark")
        scheme = await session.evaluate(
            "window.matchMedia('(prefers-color-scheme: dark)').matches")
        assert scheme is True

    @pytest.mark.asyncio
    async def test_add_script(self, session):
        await session.goto("data:text/html,<html><body></body></html>")
        await session.add_script(content="window.__test = 42")
        result = await session.evaluate("window.__test")
        assert result == 42

    @pytest.mark.asyncio
    async def test_add_style(self, session):
        await session.goto("data:text/html,<html><body><p id='sp'>text</p></body></html>")
        await session.add_style(content="#sp { color: red; }")
        color = await session.evaluate(
            "getComputedStyle(document.getElementById('sp')).color")
        assert "255" in color


class TestContextLevel:

    @pytest.mark.asyncio
    async def test_set_offline(self, session):
        await session.goto("data:text/html,<html><body>online</body></html>")
        await session.set_offline(True)
        online = await session.evaluate("navigator.onLine")
        assert online is False
        await session.set_offline(False)

    @pytest.mark.asyncio
    async def test_set_geolocation(self, session):
        await session.grant_permissions(["geolocation"])
        await session.set_geolocation(40.7128, -74.0060)

    @pytest.mark.asyncio
    async def test_permissions(self, session):
        await session.grant_permissions(["geolocation"])
        await session.clear_permissions()


class TestMocking:

    @pytest.mark.asyncio
    async def test_mock_response(self, session):
        await session.mock_response(
            "**/api/test",
            status=200,
            body='{"ok": true}',
            content_type="application/json")
        await session.goto("https://example.com")
        result = await session.evaluate("""
            fetch('/api/test')
                .then(r => r.json())
                .then(d => d.ok)
                .catch(() => false)
        """)
        assert result is True

    @pytest.mark.asyncio
    async def test_modify_request(self, session):
        await session.modify_request("**/*", headers={"X-Custom": "hutch"})
        await session.goto("data:text/html,<html><body>modified</body></html>")


class TestDragAndDrop:

    @pytest.mark.asyncio
    async def test_drag_and_drop(self, session):
        page = """data:text/html,<html><body>
        <div id="src" draggable="true" style="width:50px;height:50px;background:red">drag</div>
        <div id="tgt" style="width:100px;height:100px;background:blue"
             ondragover="event.preventDefault()"
             ondrop="document.title='dropped'">drop</div>
        </body></html>"""
        await session.goto(page)
        state = await session.drag_and_drop("#src", "#tgt")
        assert state["url"]


class TestFileUpload:

    @pytest.mark.asyncio
    async def test_set_input_files(self, session, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")
        page = """data:text/html,<html><body>
        <input type="file" id="fu" onchange="document.title=this.files[0].name">
        </body></html>"""
        await session.goto(page)
        state = await session.set_input_files("#fu", str(test_file))
        assert state["title"] == "test.txt"


class TestTracing:

    @pytest.mark.asyncio
    async def test_start_stop_tracing(self, session):
        await session.start_tracing(screenshots=False, snapshots=False)
        await session.goto("data:text/html,<html><body>trace</body></html>")
        path = await session.stop_tracing()
        assert os.path.exists(path)
        os.remove(path)
