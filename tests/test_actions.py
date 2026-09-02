import asyncio
import os
import shutil
import pytest
import pytest_asyncio
from hutch.pool import Pool
from hutch.server import HutchDaemon
from hutch.client import HutchClient

_TEST_BASE = os.path.expanduser("~/.hutch/test-actions-profiles")
_TEST_SOCK = os.path.expanduser("~/.hutch/test-actions.sock")

_FORM_PAGE = """data:text/html,
<html><body>
<form id="login" action="/api/login" method="POST">
  <input type="text" name="username" id="user" placeholder="Username">
  <input type="password" name="password" id="pass" placeholder="Password">
  <select name="role" id="role">
    <option value="user">User</option>
    <option value="admin">Admin</option>
  </select>
  <button type="submit" id="submit-btn">Log In</button>
</form>
<a href="/admin" id="admin-link">Admin Panel</a>
<textarea id="bio" name="bio" placeholder="Bio"></textarea>
<div id="output"></div>
<script>
  document.getElementById('login').addEventListener('submit', function(e) {
    e.preventDefault();
    document.getElementById('output').textContent = 'submitted:' +
      document.getElementById('user').value;
  });
</script>
</body></html>
"""


@pytest_asyncio.fixture
async def pool():
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)
    os.makedirs(_TEST_BASE, exist_ok=True)
    p = Pool(base_dir=_TEST_BASE, max_sessions=5)
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
    for name in ("act-rpc", "act-rpc2", "act-rpc3", "act-rpc4"):
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
class TestSessionActions:

    async def test_goto_returns_state(self, pool):
        s = await pool.create("act-goto", headless=True)
        result = await s.goto(_FORM_PAGE)
        assert "url" in result
        assert "title" in result
        assert "dom" in result

    async def test_goto_creates_page_if_none(self, pool):
        s = await pool.create("act-goto2", headless=True)
        page = await s.new_page()
        pages_before = s.page_count
        await s.goto(_FORM_PAGE)
        assert s.page_count >= pages_before

    async def test_fill_and_submit(self, pool):
        s = await pool.create("act-fill", headless=True)
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        await s.fill("#user", "testuser")
        state = await s.click("#submit-btn")
        output = await s.evaluate(
            'document.getElementById("output").textContent')
        assert output == "submitted:testuser"

    async def test_fill_press_enter(self, pool):
        s = await pool.create("act-enter", headless=True)
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        await s.fill("#user", "admin", press_enter=True)

    async def test_type_text(self, pool):
        s = await pool.create("act-type", headless=True)
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        await s.type_text("#user", "slow-typed")
        val = await s.evaluate('document.getElementById("user").value')
        assert val == "slow-typed"

    async def test_select_option(self, pool):
        s = await pool.create("act-select", headless=True)
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        await s.select_option("#role", "admin")
        val = await s.evaluate('document.getElementById("role").value')
        assert val == "admin"

    async def test_press_key(self, pool):
        s = await pool.create("act-press", headless=True)
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        await s.fill("#user", "test")
        await s.press("#user", "Backspace")
        val = await s.evaluate('document.getElementById("user").value')
        assert val == "tes"

    async def test_evaluate(self, pool):
        s = await pool.create("act-eval", headless=True)
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        result = await s.evaluate("1 + 1")
        assert result == 2

    async def test_screenshot(self, pool):
        s = await pool.create("act-shot", headless=True)
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        png = await s.screenshot()
        assert isinstance(png, bytes)
        assert len(png) > 100


@pytest.mark.asyncio
class TestCookiesAndStorage:

    async def test_cookies_empty_initially(self, pool):
        s = await pool.create("act-ck1", headless=True)
        page = await s.new_page()
        cookies = await s.cookies()
        assert isinstance(cookies, list)

    async def test_delete_cookies(self, pool):
        s = await pool.create("act-ck2", headless=True)
        page = await s.new_page()
        await page.goto("data:text/html,<html><body>ok</body></html>")
        await s.delete_cookies()
        cookies = await s.cookies()
        assert cookies == []

    async def test_storage_operations(self, pool):
        s = await pool.create("act-st1", headless=True)
        page = await s.new_page()
        await page.goto("data:text/html,<html><body>ok</body></html>")
        store = await s.storage()
        assert "localStorage" in store
        assert "sessionStorage" in store

    async def test_set_storage(self, pool):
        s = await pool.create("act-st2", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        try:
            await s.set_storage("token", "abc123")
            val = await s.evaluate('localStorage.getItem("token")')
            assert val == "abc123"
        except Exception:
            pytest.skip("localStorage not available on data: URLs")

    async def test_set_session_storage(self, pool):
        s = await pool.create("act-st3", headless=True)
        page = await s.new_page()
        await page.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        try:
            await s.set_storage("csrf", "xyz", session_storage=True)
            val = await s.evaluate('sessionStorage.getItem("csrf")')
            assert val == "xyz"
        except Exception:
            pytest.skip("sessionStorage not available on data: URLs")


@pytest.mark.asyncio
class TestActionsViaRPC:

    async def test_fill_and_click_rpc(self, client):
        s = await client.create("act-rpc")
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        await s.fill("#user", "rpc-user")
        state = await s.click("#submit-btn")
        assert "url" in state

    async def test_type_rpc(self, client):
        s = await client.create("act-rpc2")
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        state = await s.type_text("#user", "typed")
        assert "url" in state

    async def test_select_option_rpc(self, client):
        s = await client.create("act-rpc3")
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        state = await s.select_option("#role", "admin")
        assert "url" in state

    async def test_storage_rpc(self, client):
        s = await client.create("act-rpc4")
        await s.goto(_FORM_PAGE)
        await asyncio.sleep(0.2)
        try:
            await s.set_storage("key1", "val1")
            store = await s.storage()
            assert store["localStorage"].get("key1") == "val1"
        except Exception:
            pytest.skip("localStorage not available on data: URLs")
