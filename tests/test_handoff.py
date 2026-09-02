import asyncio
import os
import shutil
import pytest
import pytest_asyncio
from hutch.pool import Pool
from hutch.session import SessionState
from hutch.health import HealthMonitor, wire_context_to_health
from hutch.server import HutchDaemon
from hutch.client import HutchClient, HutchError

_TEST_BASE = os.path.expanduser("~/.hutch/test-handoff-profiles")
_TEST_SOCK = os.path.expanduser("~/.hutch/test-handoff.sock")


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
    for name in ("ho-rpc", "ho-auto", "ho-resume", "ho-state"):
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
class TestSessionPauseResume:

    async def test_session_starts_active(self, pool):
        s = await pool.create("ho-active", headless=True)
        assert s.state == SessionState.ACTIVE
        assert s.pause_reason is None

    async def test_pause_sets_state(self, pool):
        s = await pool.create("ho-pause", headless=True)
        s.pause("captcha")
        assert s.state == SessionState.PAUSED
        assert s.pause_reason == "captcha"

    async def test_pause_blocks_observe(self, pool):
        s = await pool.create("ho-block", headless=True)
        page = await s.new_page()
        await page.goto("data:text/html,<html><body><button>x</button></body></html>")
        await asyncio.sleep(0.2)

        s.pause("captcha")
        with pytest.raises(RuntimeError, match="paused"):
            await s.observe()

    async def test_pause_blocks_new_page(self, pool):
        s = await pool.create("ho-np", headless=True)
        s.pause("auth_expired")
        with pytest.raises(RuntimeError, match="paused"):
            await s.new_page()

    async def test_pause_allows_snapshot(self, pool):
        s = await pool.create("ho-snap", headless=True)
        page = await s.new_page()
        await page.goto("data:text/html,<html><body>test</body></html>")
        await asyncio.sleep(0.2)

        s.pause("captcha")
        snap = await s.snapshot()
        assert snap.url != ""

    async def test_resume_restores_active(self, pool):
        s = await pool.create("ho-res", headless=True)
        s.pause("captcha")
        assert s.state == SessionState.PAUSED
        s.resume()
        assert s.state == SessionState.ACTIVE
        assert s.pause_reason is None

    async def test_resume_allows_observe(self, pool):
        s = await pool.create("ho-resobs", headless=True)
        page = await s.new_page()
        await page.goto("data:text/html,<html><body><button>x</button></body></html>")
        await asyncio.sleep(0.2)

        s.pause("captcha")
        s.resume()
        elements = await s.observe()
        assert len(elements) > 0

    async def test_pause_callback_fires(self, pool):
        s = await pool.create("ho-cb", headless=True)
        events = []
        s.on_pause(lambda name, reason: events.append((name, reason)))
        s.pause("mfa")
        assert len(events) == 1
        assert events[0] == ("ho-cb", "mfa")

    async def test_double_pause_noop(self, pool):
        s = await pool.create("ho-dbl", headless=True)
        events = []
        s.on_pause(lambda name, reason: events.append(reason))
        s.pause("captcha")
        s.pause("auth_expired")
        assert len(events) == 1
        assert s.pause_reason == "captcha"

    async def test_status_includes_state(self, pool):
        s = await pool.create("ho-stat", headless=True)
        st = s.status()
        assert st["state"] == "active"
        s.pause("captcha")
        st = s.status()
        assert st["state"] == "paused"
        assert st["pause_reason"] == "captcha"


@pytest.mark.asyncio
class TestAutoPause:

    async def test_captcha_auto_pauses(self, pool):
        s = await pool.create("ho-cap", headless=True)
        monitor = HealthMonitor(s.name)
        wire_context_to_health(s.context, monitor)
        monitor.on_alert(
            lambda alert, sess=s: sess.pause(alert.type)
            if alert.type in ("captcha", "auth_expired") and sess.auto_pause
            else None)
        monitor.check_page_content('<div class="g-recaptcha"></div>')
        assert s.state == SessionState.PAUSED
        assert s.pause_reason == "captcha"

    async def test_auth_expired_auto_pauses(self, pool):
        s = await pool.create("ho-auth", headless=True)
        monitor = HealthMonitor(s.name, auth_fail_threshold=2)
        monitor.on_alert(
            lambda alert, sess=s: sess.pause(alert.type)
            if alert.type in ("captcha", "auth_expired") and sess.auto_pause
            else None)
        monitor.check_response(401, "/api/me")
        monitor.check_response(401, "/api/me")
        assert s.state == SessionState.PAUSED
        assert s.pause_reason == "auth_expired"

    async def test_auto_pause_disabled(self, pool):
        s = await pool.create("ho-noap", headless=True)
        s.auto_pause = False
        monitor = HealthMonitor(s.name)
        monitor.on_alert(
            lambda alert, sess=s: sess.pause(alert.type)
            if alert.type in ("captcha", "auth_expired") and sess.auto_pause
            else None)
        monitor.check_page_content('<div class="g-recaptcha"></div>')
        assert s.state == SessionState.ACTIVE


@pytest.mark.asyncio
class TestHandoffRPC:

    async def test_pause_via_rpc(self, client):
        s = await client.create("ho-rpc")
        result = await s.pause(reason="captcha")
        assert result["paused"] == "ho-rpc"
        assert result["reason"] == "captcha"

    async def test_resume_via_rpc(self, client):
        s = await client.create("ho-resume")
        await s.pause(reason="captcha")
        result = await s.resume()
        assert result["resumed"] == "ho-resume"

    async def test_handoff_returns_state(self, client):
        s = await client.create("ho-state")
        await s.pause(reason="mfa")
        info = await s.handoff()
        assert info["state"] == "paused"
        assert info["pause_reason"] == "mfa"
        assert info["alive"] is True

    async def test_paused_session_in_list(self, client):
        s = await client.create("ho-auto")
        await s.pause(reason="captcha")
        sessions = await client.list()
        match = [x for x in sessions if x["name"] == "ho-auto"]
        assert len(match) == 1
        assert match[0]["state"] == "paused"
        assert match[0]["pause_reason"] == "captcha"

    async def test_goto_fails_when_paused(self, client, daemon):
        s = await client.create("ho-rpc")
        await s.goto("data:text/html,<html><body>ok</body></html>")
        sess = await daemon.pool.get("ho-rpc")
        sess.pause("captcha")
        with pytest.raises(HutchError, match="paused"):
            await s.goto("data:text/html,<html><body>nope</body></html>")
