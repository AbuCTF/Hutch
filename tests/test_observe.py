import asyncio
import os
import shutil
import pytest
import pytest_asyncio
from hutch.pool import Pool
from hutch.server import HutchDaemon
from hutch.client import HutchClient

_TEST_BASE = os.path.expanduser("~/.hutch/test-observe-profiles")
_TEST_SOCK = os.path.expanduser("~/.hutch/test-observe.sock")

_FORM_PAGE = """data:text/html,
<html><body>
<h1>Test App</h1>
<form action="/api/login" method="POST">
  <input type="text" name="username" placeholder="Username" id="user-input">
  <input type="password" name="password" placeholder="Password">
  <button type="submit" id="login-btn">Log In</button>
</form>
<a href="/admin">Admin Panel</a>
<a href="/profile">My Profile</a>
<input type="file" name="avatar" id="file-upload">
<select name="role" id="role-select">
  <option>User</option>
  <option>Admin</option>
  <option>Moderator</option>
</select>
<button onclick="alert('hi')">Click Me</button>
<textarea name="bio" placeholder="Tell us about yourself"></textarea>
<div role="button" tabindex="0">Custom Button</div>
</body></html>
"""


@pytest_asyncio.fixture
async def pool():
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)
    os.makedirs(_TEST_BASE, exist_ok=True)
    p = Pool(base_dir=_TEST_BASE, max_sessions=3)
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
    for name in ("obs-test", "obs-action", "obs-empty"):
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
class TestObserve:

    async def test_observe_finds_interactive_elements(self, pool):
        s = await pool.create("obs-local", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.3)

        elements = await s.observe()
        assert len(elements) > 0

        tags = [e["tag"] for e in elements]
        assert "input" in tags
        assert "button" in tags
        assert "a" in tags
        assert "select" in tags
        assert "textarea" in tags

    async def test_observe_has_indices(self, pool):
        s = await pool.create("obs-idx", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.3)

        elements = await s.observe()
        indices = [e["idx"] for e in elements]
        assert indices == list(range(len(elements)))

    async def test_observe_captures_form_context(self, pool):
        s = await pool.create("obs-form", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.3)

        elements = await s.observe()
        form_elements = [e for e in elements if e.get("formMethod")]
        assert len(form_elements) > 0
        methods = {e["formMethod"] for e in form_elements}
        assert "post" in methods or "POST" in methods or "get" in methods

    async def test_observe_captures_links(self, pool):
        s = await pool.create("obs-links", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.3)

        elements = await s.observe()
        links = [e for e in elements if e["tag"] == "a"]
        assert len(links) >= 2
        texts = [l["text"] for l in links]
        assert "Admin Panel" in texts
        assert "My Profile" in texts

    async def test_observe_captures_file_upload(self, pool):
        s = await pool.create("obs-file", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.3)

        elements = await s.observe()
        file_inputs = [e for e in elements
                       if e["tag"] == "input" and e.get("inputType") == "file"]
        assert len(file_inputs) >= 1

    async def test_observe_captures_select_options(self, pool):
        s = await pool.create("obs-select", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.3)

        elements = await s.observe()
        selects = [e for e in elements if e["tag"] == "select"]
        assert len(selects) >= 1
        assert "Admin" in selects[0]["options"]

    async def test_observe_captures_custom_roles(self, pool):
        s = await pool.create("obs-role", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.3)

        elements = await s.observe()
        role_buttons = [e for e in elements if e.get("role") == "button"]
        assert len(role_buttons) >= 1

    async def test_observe_selectors_present(self, pool):
        s = await pool.create("obs-sel", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.3)

        elements = await s.observe()
        for e in elements:
            assert "selector" in e
            assert e["selector"] != ""

    async def test_observe_empty_page(self, pool):
        s = await pool.create("obs-empty-local", headless=True)
        page = await s.new_page()
        await page.goto("data:text/html,<html><body></body></html>")
        await asyncio.sleep(0.2)

        elements = await s.observe()
        assert elements == []


@pytest.mark.asyncio
class TestActionPageState:

    async def test_goto_returns_page_state(self, client):
        s = await client.create("obs-action")
        result = await s.goto("https://example.com")
        assert "url" in result
        assert "title" in result
        assert "dom" in result
        assert "example.com" in result["url"]

    async def test_observe_via_daemon(self, client):
        s = await client.create("obs-test")
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.3)

        elements = await s.observe()
        assert len(elements) > 0
        tags = {e["tag"] for e in elements}
        assert "input" in tags
        assert "button" in tags

    async def test_observe_empty_via_daemon(self, client):
        s = await client.create("obs-empty")
        await s.goto("data:text/html,<html><body></body></html>")
        await asyncio.sleep(0.2)

        elements = await s.observe()
        assert elements == []
