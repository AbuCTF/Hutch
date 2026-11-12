"""Human-like input: path shape and timing, typing rhythm, and trusted events in a real page."""

import math
import os
import random
import shutil
import statistics
import tempfile

import pytest
import pytest_asyncio

from hutch import human
from hutch.pool import Pool
from hutch.session import Session

_TEST_BASE = os.path.join(tempfile.gettempdir(), "hutch-test-human-profiles")


def test_path_ends_exactly_on_target_and_curves():
    rng = random.Random(7)
    pts = human.path((100, 100), (900, 500), target_width=30, rng=rng)
    assert pts[-1][:2] == (900, 500)
    straight = math.dist((100, 100), (900, 500))
    travelled = sum(math.dist(a[:2], b[:2]) for a, b in zip([(100, 100, 0)] + pts, pts))
    assert travelled > straight * 1.01  # not a straight line
    total = sum(p[2] for p in pts)
    assert 0.3 < total < 1.5  # Fitts's-law time for ~900 px onto a 30 px target
    assert len(pts) >= 20  # many small moves, not one jump


def test_path_speeds_up_then_slows_down():
    pts = human.path((0, 0), (600, 0), rng=random.Random(3))
    steps = [math.dist(a[:2], b[:2]) for a, b in zip(pts, pts[1:])]
    third = len(steps) // 3
    middle = statistics.mean(steps[third:2 * third])
    assert middle > statistics.mean(steps[:3]) and middle > statistics.mean(steps[-3:])


def test_short_moves_are_quick_and_long_ones_slower():
    rng = random.Random(1)
    short = sum(p[2] for p in human.path((0, 0), (40, 0), rng=rng))
    far = sum(p[2] for p in human.path((0, 0), (1400, 0), target_width=10, rng=rng))
    assert short < far


def test_typing_rhythm_varies_and_pauses_between_words():
    text = "the quick brown fox jumps over the lazy dog " * 4
    delays = human.key_delays(text, wpm=55, rng=random.Random(5))
    assert len(delays) == len(text)
    assert statistics.pstdev(delays) > 0.03  # not a constant rate
    after_space = [d for i, d in enumerate(delays) if i and text[i - 1] == " "]
    inside = [d for i, d in enumerate(delays) if i and text[i - 1] != " "]
    assert statistics.mean(after_space) > statistics.mean(inside)
    assert 0.12 < statistics.mean(delays) < 0.4  # around 55 words a minute


def test_click_points_stay_inside_and_off_the_edge():
    box = {"x": 10, "y": 20, "width": 100, "height": 30}
    rng = random.Random(9)
    pts = [human.target_point(box, rng=rng) for _ in range(500)]
    assert all(10 + 15 <= x <= 10 + 85 and 20 + 4.5 <= y <= 20 + 25.5 for x, y in pts)
    assert len({(round(x), round(y)) for x, y in pts}) > 50  # not the same pixel every time


def test_humanize_sticks_to_the_profile(tmp_path):
    Session("h", str(tmp_path / "h"), humanize=True)._save_meta()
    assert Session.from_profile_dir(str(tmp_path / "h")).humanize is True


@pytest_asyncio.fixture
async def pool():
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)
    os.makedirs(_TEST_BASE, exist_ok=True)
    p = Pool(base_dir=_TEST_BASE, max_sessions=2)
    await p.start()
    yield p
    await p.stop()
    if os.path.isdir(_TEST_BASE):
        shutil.rmtree(_TEST_BASE)


_PAGE = """<!doctype html><body style="margin:0;height:3000px">
<button id="b" style="position:absolute;left:700px;top:400px;width:120px;height:40px">Go</button>
<input id="t" style="position:absolute;left:100px;top:600px">
<script>
window.log = {moves: 0, untrusted: 0, down: 0, up: 0, hold: 0, clicks: 0, keys: [], wheels: 0};
let downAt = 0;
addEventListener('mousemove', e => { log.moves++; if (!e.isTrusted) log.untrusted++ });
addEventListener('mousedown', e => { log.down++; downAt = performance.now() });
addEventListener('mouseup', e => { log.up++; log.hold = performance.now() - downAt });
document.getElementById('b').addEventListener('click', e => { if (e.isTrusted) log.clicks++ });
addEventListener('keydown', e => log.keys.push(performance.now()));
addEventListener('wheel', e => log.wheels++);
</script></body>"""


@pytest.mark.asyncio
async def test_humanized_session_moves_clicks_types_and_scrolls_like_a_hand(pool):
    s = await pool.create("human", headless=True, humanize=True)
    page = await s.new_page()
    await page.route("https://human.test/", lambda r: r.fulfill(body=_PAGE, content_type="text/html"))
    await page.goto("https://human.test/")
    await s.click("#b", wait_after=None)
    log = await s.evaluate("window.log")
    assert log["clicks"] == 1 and log["down"] == 1 and log["up"] == 1
    assert log["moves"] >= 15 and log["untrusted"] == 0
    assert 40 <= log["hold"] <= 250
    await s.type_text("#t", "hello world")  # clicks into the field first, as a person would
    await s.scroll(direction="down", amount=600)
    log = await s.evaluate("window.log")
    gaps = [b - a for a, b in zip(log["keys"], log["keys"][1:])]
    assert len(log["keys"]) == len("hello world") and statistics.pstdev(gaps) > 10
    assert log["wheels"] >= 5
    assert await page.input_value("#t") == "hello world"
