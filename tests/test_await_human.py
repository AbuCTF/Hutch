"""await_human: pause for a person, notice when they are done, never answer anything itself."""

import asyncio
import io
import os
import shutil
import tarfile

import pytest
import pytest_asyncio

from hutch import browser, handoff
from hutch.artifacts import ArtifactStore
from hutch.client import HutchClient
from hutch.pool import Pool
from hutch.server import HutchDaemon
from hutch.session import SessionState

_TEST_BASE = os.path.expanduser("~/.hutch/test-await-profiles")
_TEST_ART = os.path.expanduser("~/.hutch/test-await-artifacts")
_TEST_SOCK = os.path.expanduser("~/.hutch/test-await.sock")

_CHALLENGE = "data:text/html,<html><head><title>Just a moment...</title></head><body>wait</body></html>"


def _clean():
    for d in (_TEST_BASE, _TEST_ART):
        if os.path.isdir(d):
            shutil.rmtree(d)


@pytest_asyncio.fixture
async def pool():
    _clean()
    os.makedirs(_TEST_BASE, exist_ok=True)
    p = Pool(base_dir=_TEST_BASE, max_sessions=3)
    await p.start()
    yield p
    await p.stop()
    _clean()


@pytest.fixture
def store():
    return ArtifactStore(base_dir=_TEST_ART)


async def _ready(pool, name, html="<html><body><p>hi</p></body></html>"):
    s = await pool.create(name, headless=True)
    page = await s.new_page()
    await page.goto(html if html.startswith("data:") else f"data:text/html,{html}")
    return s, page


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
class TestAwaitHuman:

    async def test_resume_returns_and_reactivates(self, pool, store):
        s, _ = await _ready(pool, "aw-resume")
        task = asyncio.create_task(s.await_human("captcha", timeout=10, poll=0.1, artifacts=store))
        await _until(lambda: s.state == SessionState.PAUSED)
        assert s.pause_reason == "captcha"
        assert s.handoff_request["reason"] == "captcha"
        assert handoff.read_request(s.profile_dir)["reason"] == "captcha"
        s.resume()
        result = await task
        assert result["outcome"] == "resumed"
        assert result["reason"] == "captcha"
        assert os.path.isfile(result["screenshot"])
        assert s.state == SessionState.ACTIVE
        assert s.handoff_request is None
        assert handoff.read_request(s.profile_dir) is None
        kinds = [e["type"] for e in s.events.since(0)]
        assert "handoff" in kinds and "handoff_end" in kinds
        start = next(e for e in s.events.since(0) if e["type"] == "handoff")
        assert start["screenshot"] == result["screenshot"]
        assert "hutch view aw-resume" in start["how"]

    async def test_pause_callbacks_get_the_reason(self, pool, store):
        s, _ = await _ready(pool, "aw-callback")
        seen = []
        s.on_pause(lambda name, reason: seen.append((name, reason, s.handoff_request["screenshot"])))
        task = asyncio.create_task(s.await_human("email code", timeout=10, poll=0.1, artifacts=store))
        await _until(lambda: seen)
        assert seen[0][:2] == ("aw-callback", "email code")
        assert seen[0][2] and os.path.isfile(seen[0][2])
        s.resume()
        await task

    async def test_resume_from_another_process(self, pool, store):
        s, _ = await _ready(pool, "aw-file")
        task = asyncio.create_task(s.await_human(timeout=10, poll=0.1, artifacts=store))
        await _until(lambda: s.state == SessionState.PAUSED)
        handoff.request_resume(s.profile_dir)  # what `hutch resume aw-file` does
        result = await task
        assert result["outcome"] == "resumed"
        assert not os.path.exists(os.path.join(s.profile_dir, handoff.RESUME_FILE))

    async def test_until_callable(self, pool, store):
        s, _ = await _ready(pool, "aw-callable")
        done = {"v": False}
        task = asyncio.create_task(s.await_human(
            until=lambda _s: done["v"], timeout=10, poll=0.1, artifacts=store))
        await asyncio.sleep(0.3)
        assert not task.done()
        done["v"] = True
        assert (await task)["outcome"] == "cleared"

    async def test_until_selector_gone(self, pool, store):
        s, page = await _ready(pool, "aw-gone", "<html><body><div id=cap>check</div></body></html>")
        task = asyncio.create_task(s.await_human(
            until={"gone": "#cap"}, timeout=10, poll=0.1, artifacts=store))
        await asyncio.sleep(0.3)
        assert not task.done()
        await page.evaluate("document.getElementById('cap').remove()")
        assert (await task)["outcome"] == "cleared"

    async def test_until_challenge_interstitial(self, pool, store):
        s, page = await _ready(pool, "aw-inter", _CHALLENGE)
        assert (await handoff.challenge_state(page))["kind"] == "interstitial"
        task = asyncio.create_task(s.await_human(until="challenge", timeout=10, poll=0.1, artifacts=store))
        await asyncio.sleep(0.4)
        assert not task.done()
        await page.evaluate("document.title = 'Welcome'")
        result = await task
        assert result["outcome"] == "cleared"

    async def test_challenge_widget_needs_its_answer(self, pool):
        s, page = await _ready(pool, "aw-widget")

        async def fake_widget(route):
            await route.fulfill(status=200, content_type="text/html", body="<p>widget</p>")

        await s.intercept("https://challenges.cloudflare.com/**", fake_widget)
        await page.set_content(
            '<form><iframe src="https://challenges.cloudflare.com/turnstile/if/x"></iframe>'
            '<input type=hidden name="cf-turnstile-response" value=""></form>')
        await page.wait_for_function("document.querySelector('iframe').contentDocument !== undefined")
        await _until(lambda: any("challenges.cloudflare.com" in f.url for f in page.frames))
        state = await handoff.challenge_state(page)
        assert state == {"present": True, "kind": "turnstile", "detail": "cf-turnstile-response is empty"}
        await page.evaluate(
            "document.querySelector('[name=cf-turnstile-response]').value = 'x'.repeat(40)")
        assert not (await handoff.challenge_state(page))["present"]

    async def test_timeout_still_reactivates(self, pool, store):
        s, _ = await _ready(pool, "aw-timeout")
        result = await s.await_human(timeout=0.3, poll=0.1, artifacts=store)
        assert result["outcome"] == "timeout"
        assert s.state == SessionState.ACTIVE
        assert s.pause_reason is None

    async def test_closed_while_waiting(self, pool, store):
        s, _ = await _ready(pool, "aw-closed")
        task = asyncio.create_task(s.await_human(timeout=20, poll=0.1, artifacts=store))
        await _until(lambda: s.state == SessionState.PAUSED)
        await s.close()
        result = await task
        assert result["outcome"] == "closed"
        assert s.state == SessionState.HIBERNATED

    async def test_bad_until_raises_before_pausing(self, pool, store):
        s, _ = await _ready(pool, "aw-bad")
        with pytest.raises(ValueError, match="until"):
            await s.await_human(until={"nope": "x"}, artifacts=store)
        assert s.state == SessionState.ACTIVE
        assert not s.events.since(0, {"handoff"})

    async def test_already_paused_by_health(self, pool, store):
        s, _ = await _ready(pool, "aw-prepaused")
        s.pause("captcha")
        task = asyncio.create_task(s.await_human("solve it", timeout=10, poll=0.1, artifacts=store))
        await _until(lambda: s.handoff_request is not None)
        assert s.pause_reason == "solve it"
        s.resume()
        assert (await task)["outcome"] == "resumed"


@pytest_asyncio.fixture
async def daemon():
    _clean()
    os.makedirs(_TEST_BASE, exist_ok=True)
    d = HutchDaemon(base_dir=_TEST_BASE, max_sessions=3, idle_timeout=0, sock_path=_TEST_SOCK)
    d.artifacts = ArtifactStore(base_dir=_TEST_ART)
    await d.start()
    yield d
    await d.stop()
    _clean()


@pytest.mark.asyncio
async def test_await_human_over_the_daemon(daemon):
    waiter, other = HutchClient(sock_path=_TEST_SOCK), HutchClient(sock_path=_TEST_SOCK)
    await waiter.connect()
    await other.connect()
    try:
        s = await waiter.create("aw-rpc")
        await s.goto(_CHALLENGE)
        task = asyncio.create_task(s.await_human("captcha", until="challenge", timeout=20, poll=0.2))
        sess = await daemon.pool.get("aw-rpc")
        await _until(lambda: sess.state == SessionState.PAUSED)
        handle = await other.session("aw-rpc")
        info = await handle.handoff()
        assert info["waiting"]["reason"] == "captcha"
        await handle.resume()
        result = await task
        assert result["outcome"] == "resumed"
        assert result["screenshot"].startswith(_TEST_ART)
    finally:
        await waiter.close()
        await other.close()


@pytest.mark.asyncio
class TestProfileArchive:

    async def test_export_import_round_trip(self, pool, tmp_path):
        s, page = await _ready(pool, "aw-export")
        await page.goto("data:text/html,<p>x</p>")
        await s.close()
        archive = str(tmp_path / "acct.tar.gz")
        browser.export_profile(s.profile_dir, archive)
        assert oct(os.stat(archive).st_mode & 0o777) == "0o600"
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        assert "hutch-profile/hutch_meta.json" in names
        assert not any("/Cache" in n or "Singleton" in n for n in names)
        dst = os.path.join(str(tmp_path), "restored")
        browser.import_profile(archive, dst)
        assert os.path.isfile(os.path.join(dst, "hutch_meta.json"))
        with pytest.raises(browser.BrowserError, match="exists"):
            browser.export_profile(s.profile_dir, archive)

    async def test_import_rejects_escapes(self, tmp_path):
        for name, kind in (("hutch-profile/../evil", tarfile.REGTYPE),
                           ("/etc/evil", tarfile.REGTYPE),
                           ("hutch-profile/link", tarfile.SYMTYPE),
                           ("other/hutch_meta.json", tarfile.REGTYPE)):
            archive = tmp_path / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.SYMTYPE:
                    info.linkname = "/etc/passwd"
                    tar.addfile(info)
                else:
                    info.size = 2
                    tar.addfile(info, io.BytesIO(b"{}"))
            with pytest.raises(browser.BrowserError, match="unexpected entry"):
                browser.import_profile(str(archive), str(tmp_path / "dst"))
            assert not (tmp_path / "dst").exists()
            assert not (tmp_path / "evil").exists()
            archive.unlink()


@pytest.mark.asyncio
async def test_mcp_handoff(tmp_path):
    pytest.importorskip("mcp")
    import json
    import sys
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "hutch.mcp"],
                                   env={**os.environ, "HUTCH_BASE_DIR": str(tmp_path)})
    try:
        async with stdio_client(params) as (r, w), ClientSession(r, w) as client:
            await client.initialize()
            tools = {t.name for t in (await client.list_tools()).tools}
            assert "hutch_handoff" in tools
            await client.call_tool("hutch_sessions", {"action": "create", "name": "aw-mcp", "headless": True})
            await client.call_tool("hutch_navigate", {"session": "aw-mcp", "url": "data:text/html,<p>ok</p>"})
            res = await client.call_tool("hutch_handoff", {"session": "aw-mcp", "until": "challenge",
                                                           "timeout": 30})
            result = json.loads(res.content[0].text)
            assert result["outcome"] == "cleared"
            assert any(c.type == "image" for c in res.content)
            text = "\n".join(c.text for c in res.content if c.type == "text")
            assert "waiting for a person (challenge)" in text and "handoff over: cleared" in text
            res = await client.call_tool("hutch_handoff", {"session": "aw-mcp", "action": "status"})
            status = json.loads(res.content[0].text)
            assert status["state"] == "active" and status["waiting"] is None
            await client.call_tool("hutch_sessions", {"action": "close", "name": "aw-mcp"})
    finally:
        shutil.rmtree(os.path.expanduser("~/.hutch/artifacts/aw-mcp"), ignore_errors=True)


@pytest.mark.asyncio
class TestSeenFromElsewhere:
    """The CLI, the daemon and MCP see a handoff another process is waiting on."""

    async def test_status_reports_another_process_waiting(self, pool):
        s = await pool.create("aw-elsewhere", headless=True)
        handoff.write_request(s.profile_dir, {"reason": "turnstile", "pid": os.getpid(),
                                              "how": "the browser window"})
        try:
            other = type(s).from_profile_dir(s.profile_dir)  # what `hutch status` loads
            info = other.status()
            assert info["state"] == "paused"
            assert info["pause_reason"] == "turnstile"
            assert info["waiting"]["reason"] == "turnstile"
        finally:
            handoff.clear_request(s.profile_dir)
        assert s.status()["waiting"] is None

    async def test_dead_waiter_is_not_reported(self, pool):
        import subprocess
        import sys
        s = await pool.create("aw-dead", headless=True)
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        handoff.write_request(s.profile_dir, {"reason": "captcha", "pid": gone.pid})
        info = type(s).from_profile_dir(s.profile_dir).status()
        assert info["state"] != "paused" and info["waiting"] is None
        assert handoff.read_request(s.profile_dir) is None  # cleaned up
