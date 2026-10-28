import json
import os

import pytest
import pytest_asyncio

from hutch import browser
from hutch.pool import Pool
from hutch.session import Session


@pytest_asyncio.fixture
async def base(tmp_path):
    yield str(tmp_path / "profiles")
    for name in os.listdir(tmp_path / "profiles") if (tmp_path / "profiles").exists() else []:
        await browser.stop(str(tmp_path / "profiles" / name))


_PAGE = "data:text/html,<title>{}</title><input id=q>"


@pytest.mark.asyncio
async def test_two_agents_never_drive_the_same_tab(base):
    async with Pool(base_dir=base) as one:
        a = await one.create("shared", headless=True)
        await a.goto(_PAGE.format("a"))
        mine = a._active_page()
        leases = os.listdir(os.path.join(a.profile_dir, "hutch_leases"))
        assert len(leases) == 1
        async with Pool(base_dir=base) as two:
            b = await two.get("shared", launch=True)
            await b.goto(_PAGE.format("b"))
            assert await mine.title() == "a"
            assert await b.evaluate("document.title") == "b"
            tabs = await b.pages()
            held = {t["title"]: t["held"] for t in tabs}
            assert held["b"] == "me" and held["a"].startswith("agent shared (pid")
            other = next(t["index"] for t in tabs if t["title"] == "a")
            with pytest.raises(RuntimeError, match="held by agent"):
                await b.switch_page(other)

            assert a.release_tab()["released"]
            await b.switch_page(other)
            assert await b.evaluate("document.title") == "a"
        assert len(os.listdir(os.path.join(a.profile_dir, "hutch_leases"))) == 0


@pytest.mark.asyncio
async def test_dead_owner_lease_is_ignored(base):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("stale", headless=True)
        await s.goto(_PAGE.format("x"))
        page = s._active_page()
        tid = s._target_ids[page]
        s._release(tid)
        with open(s._lease_file(tid), "w") as f:
            json.dump({"owner": "gone", "pid": 2 ** 22 + 12345, "session": "old"}, f)
        assert s._active_page() is page and s._holder(tid) == "me"


@pytest.mark.asyncio
async def test_badge_marks_agent_tabs_in_a_visible_browser(base):
    profile = os.path.join(base, "seen")
    owner = Session("seen", profile, headless=True)
    st = browser.spawn(profile, **owner.spawn_options())
    state = os.path.join(profile, "hutch_browser.json")
    with open(state) as f:
        meta = json.load(f)
    with open(state, "w") as f:
        json.dump({**meta, "headless": False}, f)
    async with Pool(base_dir=base) as pool:
        s = await pool.get("seen", launch=True)
        assert not s.headless
        await s.goto(_PAGE.format("agent tab"))
        page = s._active_page()
        badge = "document.getElementById('__hutch_agent') !== null"
        for _ in range(20):
            if await page.evaluate(badge):
                break
            await page.wait_for_timeout(50)
        assert await page.evaluate(badge)
        assert "agent" not in (await s.aria_snapshot()).replace("agent tab", "")
        await s.screenshot()
        assert await page.evaluate(badge)
        await page.reload()
        await page.wait_for_function(badge)
        s.release_tab()
        await page.wait_for_function("document.getElementById('__hutch_agent') === null")
    await browser.stop(profile)
