"""Clearing "verify you are human" checkboxes, driver-agnostic.

For the test accounts a program's own rules allow us to create: when a sign-up or login page shows a
Cloudflare Turnstile checkbox or a reCAPTCHA v2 "I'm not a robot" box, click it the way a person does
and wait for the answer. Image grids are not attempted; the session is marked ``grid-required`` and
left for a solver or a person. A program that forbids bypassing its checks is never touched
(``forbids_bypass=True`` returns ``hitl`` and does nothing).

How a click is made to pass, from what the widgets are documented and observed to look at:

- the pointer arrives from somewhere else, over a curved path that slows into the target, so the
  widget sees mouse movement before the press instead of a press out of nowhere;
- it dwells on the box a moment (hover), then presses for a human-length hold and releases inside it;
- the page is scrolled so the widget is fully in view, and the window is focused;
- nothing is clicked while the widget is still "Verifying": managed Turnstile decides on its own
  whether to show a box at all, and often passes with no click. The checkbox is clicked only once it
  is on screen, and only once;
- after the click it waits for the widget to settle (a token appears, a grid opens or it resets).

It works over plain DevTools commands, so any driver can supply them: ``hutch.challenge.surface()``
adapts a Playwright or patchright page, ``RawSurface`` a ``hutch.cdp`` target.
"""

import asyncio
import random
import re
import time

from . import human as _human
from . import xinput as _xinput

TURNSTILE = re.compile(r"https://challenges\.cloudflare\.com/cdn-cgi/challenge-platform/.*/turnstile/")
RECAPTCHA = re.compile(r"https://www\.(google|recaptcha)\.(com|net)/recaptcha/(api2|enterprise)/anchor")
RECAPTCHA_GRID = re.compile(r"https://www\.(google|recaptcha)\.(com|net)/recaptcha/(api2|enterprise)/bframe")

# the answer a widget leaves in the page once it is passed
_TOKEN_JS = """(() => {
  const best = (sel) => Array.from(document.querySelectorAll(sel)).map(e => (e.value || '').length)
    .reduce((a, b) => Math.max(a, b), 0);
  return {turnstile: best('[name="cf-turnstile-response"]'), recaptcha: best('[name="g-recaptcha-response"]'),
          interstitial: !!document.querySelector('#challenge-form, #challenge-stage, #cf-challenge-running'),
          title: document.title || ''};
})()"""
_INTERSTITIAL_TITLE = re.compile(r"just a moment|attention required|checking your browser|verify you are", re.I)


class Surface:
    """What the solver needs from a driver: DevTools commands for the page, and for its child frames.

    ``xdisplay`` names the X display the browser window is on, when there is one this process can
    drive: the pointer then moves and clicks through the X server instead of through DevTools.
    """

    xdisplay = None

    async def send(self, method, params=None):
        raise NotImplementedError

    async def child_frames(self):
        """The page's child frames that run in their own process: ``[{"id": frameId, "url": url}]``."""
        raise NotImplementedError

    async def frame_send(self, frame):
        """An async callable ``send(method, params)`` into one of ``child_frames()``."""
        raise NotImplementedError

    async def info(self):
        """{"title", "url"} of the page without running a script in it, else {}."""
        return {}

    async def local_frames(self):
        """Child frames that share the page's process: ``[{"id", "url", "local": True}]``. They have no
        session of their own; their documents are reached through the page's DOM."""
        try:
            tree = (await self.send("Page.getFrameTree"))["frameTree"]
        except Exception:
            return []
        out = []

        def walk(node):
            for child in node.get("childFrames", []):
                out.append({"id": child["frame"]["id"], "url": child["frame"].get("url", ""), "local": True})
                walk(child)

        walk(tree)
        return out

    async def close(self):
        """Release what this surface opened."""

    async def evaluate(self, expression):
        out = await self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                   "awaitPromise": True})
        if "exceptionDetails" in out:
            raise RuntimeError(out["exceptionDetails"].get("text", "evaluate failed"))
        return out.get("result", {}).get("value")


class RawSurface(Surface):
    """A ``hutch.cdp.Target`` (the page) on its ``Browser``."""

    def __init__(self, browser, target, xdisplay=None):
        self.browser, self.target, self._frames = browser, target, {}
        self.xdisplay = xdisplay

    async def send(self, method, params=None):
        return await self.target.send(method, params)

    async def info(self):
        got = await self.browser.conn.send("Target.getTargetInfo", {"targetId": self.target.id})
        return {"title": got["targetInfo"].get("title", ""), "url": got["targetInfo"].get("url", "")}

    async def child_frames(self):
        info = await self.browser.conn.send("Target.getTargets")
        return [{"id": t["targetId"], "url": t["url"]} for t in info["targetInfos"]
                if t["type"] == "iframe" and t.get("parentFrameId") == self.target.id]

    async def frame_send(self, frame):
        fid = frame["id"]
        if fid not in self._frames:
            self._frames[fid] = await self.browser.attach(fid)
        return self._frames[fid].send


class PlaywrightSurface(Surface):
    """A Playwright or patchright page, through CDP sessions it opens for the page and its frames."""

    def __init__(self, page, xdisplay=None):
        self.page, self._cdp, self._frames = page, None, {}
        self.xdisplay = xdisplay

    async def send(self, method, params=None):
        if self._cdp is None:
            self._cdp = await self.page.context.new_cdp_session(self.page)
        return await self._cdp.send(method, params or {})

    async def info(self):
        got = await self.send("Target.getTargetInfo")
        return {"title": got["targetInfo"].get("title", ""), "url": got["targetInfo"].get("url", "")}

    async def child_frames(self):
        out = []
        for f in self.page.frames:
            if f is self.page.main_frame:
                continue
            if id(f) not in self._frames:
                try:
                    session = await self.page.context.new_cdp_session(f)
                    own = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
                except Exception:
                    continue  # same process as the page: no session of its own
                self._frames[id(f)] = (session, own)
            out.append({"id": self._frames[id(f)][1], "url": f.url, "key": id(f)})
        return out

    async def frame_send(self, frame):
        return self._frames[frame["key"]][0].send

    async def close(self):
        sessions = [s for s, _ in self._frames.values()] + ([self._cdp] if self._cdp else [])
        self._frames, self._cdp = {}, None
        for session in sessions:
            try:
                await session.detach()
            except Exception:
                pass


def surface(page, xdisplay=None):
    """A Surface for a Playwright or patchright page; ``xdisplay`` as for ``Surface``."""
    return PlaywrightSurface(page, xdisplay)


def _find_all(node, pred, out=None):
    out = [] if out is None else out
    if pred(node):
        out.append(node)
    for key in ("children", "shadowRoots"):
        for child in node.get(key) or ():
            _find_all(child, pred, out)
    if node.get("contentDocument"):
        _find_all(node["contentDocument"], pred, out)
    return out


def _find(node, pred):
    """Depth-first over a DOM.getDocument(pierce) tree, including shadow roots and iframe documents."""
    if pred(node):
        return node
    for key in ("children", "shadowRoots"):
        for child in node.get(key) or ():
            hit = _find(child, pred)
            if hit:
                return hit
    if node.get("contentDocument"):
        return _find(node["contentDocument"], pred)
    return None


def _attrs(node):
    a = node.get("attributes") or []
    return dict(zip(a[::2], a[1::2]))


def _is_checkbox(node):
    if node.get("nodeName", "").upper() != "INPUT":
        return False
    return _attrs(node).get("type") == "checkbox"


async def _quad_box(send, **target):
    got = await send("DOM.getBoxModel", target)
    q = got["model"]["content"]
    xs, ys = q[0::2], q[1::2]
    return {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)}


async def widgets(surf):
    """Challenge widgets in the page right now: [{kind, frame, box, checkbox, interactive}].

    ``box`` is the widget's iframe in page coordinates; ``checkbox`` the checkbox's own box when the
    widget is showing one, else None (still verifying, already passed, or a grid).
    """
    try:
        await surf.send("DOM.enable")
    except Exception:
        return []
    found = []
    for frame in [*await surf.child_frames(), *await surf.local_frames()]:
        url = frame.get("url", "")
        kind = "turnstile" if TURNSTILE.search(url) else "recaptcha" if RECAPTCHA.search(url) else None
        if not kind or "size=invisible" in url:
            continue
        try:
            owner = await surf.send("DOM.getFrameOwner", {"frameId": frame["id"]})
            box = await _quad_box(surf.send, backendNodeId=owner["backendNodeId"])
        except Exception:
            continue
        if box["width"] < 20 or box["height"] < 20:
            continue
        found.append({"kind": kind, "frame": frame, "box": box, "checkbox": await _checkbox(surf, frame, box, kind)})
    return found


async def _checkbox(surf, frame, outer, kind):
    """The checkbox's box in page coordinates, or None while the widget shows none."""
    try:
        if frame.get("local"):
            send = surf.send
            root = (await send("DOM.getDocument", {"depth": -1, "pierce": True}))["root"]
            holder = _find(root, lambda n: n.get("nodeName") == "IFRAME" and n.get("frameId") == frame["id"])
            doc = (holder or {}).get("contentDocument")
            if not doc:
                return None
        else:
            send = await surf.frame_send(frame)
            if send is None:
                return None
            await send("DOM.enable")
            doc = (await send("DOM.getDocument", {"depth": -1, "pierce": True}))["root"]
        nodes = _find_all(doc, lambda n: n.get("nodeType") == 1 and (
            _is_checkbox(n) or "checkbox" in (_attrs(n).get("class", "") + " " + _attrs(n).get("role", ""))))
        boxes = []
        for node in nodes[:8]:
            try:
                box = await _quad_box(send, nodeId=node["nodeId"])
            except Exception:
                continue
            if box["width"] >= 12 and box["height"] >= 12:
                boxes.append(box)
        if not boxes:
            return None
        # the tightest match is the box itself; wrappers around it and its label are larger
        inner = min(boxes, key=lambda b: b["width"] * b["height"])
    except Exception:
        return None
    if inner["width"] < 6 or inner["height"] < 6:
        return None
    if inner["width"] > inner["height"] * 1.6:
        # a label spanning box and text: the box is the square at its left end
        inner["width"] = inner["height"]
    if frame.get("local"):
        return inner
    # a frame's own coordinates start at its top-left corner
    return {"x": outer["x"] + inner["x"], "y": outer["y"] + inner["y"],
            "width": inner["width"], "height": inner["height"]}


async def tokens(surf):
    try:
        return await surf.evaluate(_TOKEN_JS) or {}
    except Exception:
        return {}


async def grid_open(surf):
    """True when a reCAPTCHA image challenge is on screen."""
    try:
        for frame in [*await surf.child_frames(), *await surf.local_frames()]:
            if not RECAPTCHA_GRID.search(frame.get("url", "")):
                continue
            owner = await surf.send("DOM.getFrameOwner", {"frameId": frame["id"]})
            box = await _quad_box(surf.send, backendNodeId=owner["backendNodeId"])
            shown = await surf.evaluate(
                "Array.from(document.querySelectorAll('iframe[src*=\"recaptcha\"][src*=\"bframe\"]'))"
                ".some(f => { let e = f; while (e) { if (getComputedStyle(e).visibility === 'hidden' "
                "|| getComputedStyle(e).display === 'none') return false; e = e.parentElement; } "
                "return f.getBoundingClientRect().height > 100; })")
            if shown and box["height"] > 100:
                return True
    except Exception:
        pass
    return False


class Pointer:
    """The pointer's last position, so successive moves start where the previous one ended."""

    def __init__(self, surf, rng=None):
        self.surf, self.rng, self.pos = surf, rng or random.Random(), None

    async def _event(self, kind, x, y, **kw):
        await self.surf.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, **kw})

    async def start(self):
        if self.pos is None:
            size = await self.surf.evaluate("[innerWidth, innerHeight]") or [1280, 720]
            self.pos = (size[0] * self.rng.uniform(0.25, 0.7), size[1] * self.rng.uniform(0.2, 0.6))
        return self.pos

    async def move(self, x, y, width=40.0):
        for px, py, dt in _human.path(await self.start(), (x, y), target_width=width, rng=self.rng):
            await self._event("mouseMoved", px, py)
            await asyncio.sleep(dt)
        self.pos = (x, y)

    async def click(self, x, y, width=24.0):
        rng = self.rng
        await self.move(x, y, width)
        await asyncio.sleep(rng.uniform(0.25, 0.7))  # hover on the box before pressing
        await self._event("mousePressed", x, y, button="left", buttons=1, clickCount=1)
        await asyncio.sleep(rng.uniform(0.06, 0.14))
        await self._event("mouseReleased", x, y, button="left", buttons=0, clickCount=1)


class XPointer(Pointer):
    """The same movements, delivered by the X server as a physical mouse would be."""

    def __init__(self, surf, rng=None):
        super().__init__(surf, rng)
        self.x = _xinput.XInput(surf.xdisplay)

    async def start(self):
        self.x.origin = await _xinput.viewport_origin(self.surf.send)
        if self.pos is None:
            size = await self.surf.evaluate("[innerWidth, innerHeight]") or [1280, 720]
            self.pos = (size[0] * self.rng.uniform(0.25, 0.7), size[1] * self.rng.uniform(0.2, 0.6))
            self.x.move_page(*self.pos)
        return self.pos

    async def _event(self, kind, x, y, **kw):
        if kind == "mouseMoved":
            self.x.move_page(x, y)
        else:
            self.x.button(kind == "mousePressed")

    def close(self):
        self.x.close()


def pointer_for(surf, rng=None):
    if surf.xdisplay:
        try:
            return XPointer(surf, rng)
        except _xinput.XInputError:
            pass
    return Pointer(surf, rng)


async def click_checkbox(surf, widget, pointer):
    """Scroll the widget into view and click its checkbox like a person. Returns the point clicked."""
    box = widget["box"]
    view = await surf.evaluate("[innerWidth, innerHeight]") or [1280, 720]
    if box["y"] < 0 or box["y"] + box["height"] > view[1] or box["x"] < 0 or box["x"] + box["width"] > view[0]:
        await surf.send("Runtime.evaluate", {"expression":
                        f"window.scrollBy(0, {int(box['y'] + box['height'] / 2 - view[1] / 2)})"})
        await asyncio.sleep(0.5)
        fresh = [w for w in await widgets(surf) if w["kind"] == widget["kind"]]
        if fresh:
            widget = fresh[0]
    target = widget["checkbox"]
    if not target:
        raise RuntimeError("no checkbox on screen")
    x, y = _human.target_point(target, rng=pointer.rng)
    await surf.send("Page.bringToFront")
    await pointer.click(x, y, min(target["width"], target["height"]))
    return (x, y)


async def clear(surf, *, timeout=60.0, forbids_bypass=False, settle=4.0, rng=None, on_event=None):
    """Clear the checkbox challenge on the page, if there is one.

    Returns ``{"status": ..., "kind": ..., "seconds": ..., "clicks": ...}``, where status is one of:

    ``clear``       no challenge was ever shown
    ``passed``      a token or a normal page was left behind (``clicked`` says whether a click did it)
    ``grid-required``  an image challenge opened; nothing was guessed
    ``hitl``        the program forbids bypassing checks; the page was not touched
    ``timeout``     the widget never settled
    """
    started = time.monotonic()
    pointer = pointer_for(surf, rng)
    try:
        return await _clear(surf, pointer, started, timeout, forbids_bypass, settle, on_event)
    finally:
        if hasattr(pointer, "close"):
            pointer.close()


async def _clear(surf, pointer, started, timeout, forbids_bypass, settle, on_event):
    emit = on_event or (lambda *a, **k: None)
    clicks, kind, last_seen, clicked_at = 0, None, 0.0, None

    def done(status, **extra):
        return {"status": status, "kind": kind, "seconds": round(time.monotonic() - started, 1),
                "clicks": clicks, **extra}

    while time.monotonic() - started < timeout:
        # A Cloudflare interstitial watches its own page: running a script in it every poll
        # (reading the token field) is what makes it distrust the visit, in measurements, while the
        # DevTools DOM reads below do not. So on an interstitial the page is only looked at, never
        # scripted, until it has been left.
        try:
            meta = await surf.info()
        except Exception:
            meta = {}
        on_interstitial = bool(_INTERSTITIAL_TITLE.search(meta.get("title", "")) or "__cf_chl" in meta.get("url", ""))
        info = {"interstitial": True, "title": meta.get("title", "")} if on_interstitial else await tokens(surf)
        shown = await widgets(surf)
        if shown:
            kind, last_seen = shown[0]["kind"], time.monotonic()
        if shown and forbids_bypass:
            emit("challenge", status="hitl", kind=kind)
            return done("hitl", reason="forbids_bypass")
        if any(info.get(k, 0) > 20 for k in ("turnstile", "recaptcha")):
            return done("passed", clicked=clicks > 0)
        if await grid_open(surf):
            emit("challenge", status="grid-required", kind="recaptcha")
            return done("grid-required", kind="recaptcha")
        if not shown:
            if info.get("interstitial") or _INTERSTITIAL_TITLE.search(info.get("title", "")):
                await asyncio.sleep(1.0)  # a full-page check that has not drawn its widget yet
                continue
            if last_seen and time.monotonic() - last_seen > settle:
                return done("passed", clicked=clicks > 0)
            if not last_seen and time.monotonic() - started > settle * 2:
                return done("clear")
            await asyncio.sleep(0.5)
            continue
        ready = [w for w in shown if w["checkbox"]]
        # one click per appearance of the box; a widget that resets itself gets another try later
        if ready and (clicked_at is None or time.monotonic() - clicked_at > 12) and clicks < 3:
            clicked_at = time.monotonic()
            try:
                at = await click_checkbox(surf, ready[0], pointer)
                clicks += 1
                emit("challenge", status="clicked", kind=ready[0]["kind"], at=[round(at[0]), round(at[1])],
                     box=ready[0]["checkbox"])
            except Exception as exc:
                emit("challenge", status="click-failed", error=str(exc)[:120])
            await asyncio.sleep(1.5)
            continue
        await asyncio.sleep(0.7)
    return done("timeout")
