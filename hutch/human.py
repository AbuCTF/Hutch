"""Human-like input: mouse paths, clicks, typing and scrolling with a person's timing.

Plain automation teleports the pointer onto the centre of an element, holds the button for zero
milliseconds, types at a constant rate and scrolls in one jump. Behavioural checks look for exactly
that. This module moves the real (trusted, CDP-dispatched) pointer the way a hand does:

- paths are curved (a cubic Bezier with random control points), take the time Fitts's law predicts
  for the distance and target size, speed up and slow down (minimum-jerk profile), carry a little
  tremor, and on long moves overshoot slightly and correct;
- clicks land near, not on, the centre (Gaussian), after a short settle, with a realistic hold;
- typing has per-key gaps drawn from a log-normal distribution, with longer pauses after spaces
  and punctuation and the odd hesitation;
- scrolling is a series of wheel notches with decaying gaps.

It hedges against simple behavioural rules; it is not cover against vendors that model behaviour
across a whole session. Where a site insists on proof that a person is present, hand the session
to one (Hutch's handoff).
"""

import asyncio
import math
import random

POLL_HZ = 90  # pointer events per second, about what a desktop mouse reports
_POS = "_hutch_pointer"


def _fitts(distance, width):
    """Seconds to move ``distance`` px onto a target ``width`` px wide (Shannon form)."""
    return 0.12 + 0.14 * math.log2(distance / max(width, 4.0) + 1)


def _ease(t):
    """Minimum-jerk position profile: slow start, fast middle, slow end."""
    return t * t * t * (10 - 15 * t + 6 * t * t)


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def path(start, end, *, target_width=40.0, rng=None):
    """Points ``[(x, y, seconds_since_previous), ...]`` from ``start`` to exactly ``end``."""
    rng = rng or random.Random()
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)
    if distance < 1:
        return [(x1, y1, 0.0)]
    # control points pushed sideways from the straight line, more for longer moves
    normal = (-dy / distance, dx / distance)
    bend = min(distance * 0.3, 120) * rng.uniform(-1, 1)
    c1 = (x0 + dx * rng.uniform(0.2, 0.4) + normal[0] * bend, y0 + dy * rng.uniform(0.2, 0.4) + normal[1] * bend)
    c2 = (x0 + dx * rng.uniform(0.6, 0.8) + normal[0] * bend * rng.uniform(0.3, 0.8),
          y0 + dy * rng.uniform(0.6, 0.8) + normal[1] * bend * rng.uniform(0.3, 0.8))
    overshoot = distance > 400 and rng.random() < 0.6
    aim = (x1 + dx / distance * rng.uniform(4, 12), y1 + dy / distance * rng.uniform(4, 12)) if overshoot else end
    duration = _fitts(distance, target_width) * rng.uniform(0.85, 1.2)
    steps = max(3, int(duration * POLL_HZ))
    points = []
    for i in range(1, steps + 1):
        t = _ease(i / steps)
        x, y = _bezier(start, c1, c2, aim, t)
        tremor = 0.6 if i < steps else 0.0
        points.append((x + rng.gauss(0, tremor), y + rng.gauss(0, tremor), duration / steps))
    if overshoot:  # a short, slower correction back onto the target
        back = max(3, int(0.12 * POLL_HZ))
        for i in range(1, back + 1):
            t = _ease(i / back)
            points.append((aim[0] + (x1 - aim[0]) * t, aim[1] + (y1 - aim[1]) * t, rng.uniform(0.009, 0.016)))
    points[-1] = (x1, y1, points[-1][2])
    return points


def key_delays(text, *, wpm=55, rng=None):
    """Seconds to wait before each character of ``text``."""
    rng = rng or random.Random()
    mean = 60.0 / (wpm * 5)  # five characters to a word
    sigma = 0.45
    mu = math.log(mean) - sigma * sigma / 2
    delays = []
    for i, ch in enumerate(text):
        d = rng.lognormvariate(mu, sigma)
        if i and text[i - 1] in " .,;:!?\n":
            d += rng.uniform(0.03, 0.18)  # a beat between words
        if rng.random() < 0.03:
            d += rng.uniform(0.25, 0.7)  # the odd hesitation
        delays.append(min(d, 1.5))
    return delays


def target_point(box, *, rng=None):
    """Somewhere a person would click inside ``box`` (x, y, width, height): near the centre, never the edge."""
    rng = rng or random.Random()
    x, y, w, h = box["x"], box["y"], box["width"], box["height"]
    cx = x + w / 2 + rng.gauss(0, w / 7)
    cy = y + h / 2 + rng.gauss(0, h / 7)
    return (min(max(cx, x + w * 0.15), x + w * 0.85), min(max(cy, y + h * 0.15), y + h * 0.85))


def _start(page):
    pos = getattr(page, _POS, None)
    if pos is None:
        size = page.viewport_size or {"width": 1280, "height": 720}
        pos = (size["width"] * random.uniform(0.3, 0.7), size["height"] * random.uniform(0.3, 0.7))
    return pos


async def move(page, x, y, *, target_width=40.0, rng=None):
    for px, py, dt in path(_start(page), (x, y), target_width=target_width, rng=rng):
        await page.mouse.move(px, py)
        await asyncio.sleep(dt)
    setattr(page, _POS, (x, y))


async def click(page, locator, *, button="left", click_count=1, rng=None):
    """Move to ``locator`` like a hand, settle, and click it."""
    rng = rng or random.Random()
    await locator.scroll_into_view_if_needed()
    box = await locator.bounding_box()
    if not box:
        raise RuntimeError("element has no box to click (hidden or detached)")
    x, y = target_point(box, rng=rng)
    await move(page, x, y, target_width=min(box["width"], box["height"]), rng=rng)
    await asyncio.sleep(rng.uniform(0.04, 0.15))
    for i in range(click_count):
        await page.mouse.down(button=button, click_count=i + 1)
        await asyncio.sleep(rng.uniform(0.055, 0.13))
        await page.mouse.up(button=button, click_count=i + 1)
        if i + 1 < click_count:
            await asyncio.sleep(rng.uniform(0.08, 0.16))


async def type_text(page, text, *, wpm=55, rng=None):
    for ch, delay in zip(text, key_delays(text, wpm=wpm, rng=rng)):
        await asyncio.sleep(delay)
        await page.keyboard.type(ch)


async def scroll(page, dy, *, rng=None):
    """Scroll by about ``dy`` pixels in wheel notches of 100 px."""
    rng = rng or random.Random()
    notches = max(1, round(abs(dy) / 100))
    step = math.copysign(100, dy)
    for i in range(notches):
        await page.mouse.wheel(0, step)
        await asyncio.sleep(rng.uniform(0.03, 0.09) + 0.02 * i / notches)
