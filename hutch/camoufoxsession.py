"""EXPERIMENTAL: the ``camoufox`` driver, a Session on Camoufox (a patched Firefox) instead of Chrome.

Camoufox spoofs its fingerprint inside the browser and scored best on CreepJS's like-headless meter
in Hutch's benchmark (6%, against 31% for Chrome), and it passes the managed and full-page
Cloudflare checks there. It failed the interstitial on the one page where Hutch's Chrome path passes
every time, and nobody has yet told whether that is its fingerprint or its click recipe, so treat it
as a second opinion, not a replacement.

What this driver is: the same Playwright page API Hutch already speaks, over ``camoufox``'s persistent
profile. Navigation, evaluate, content, screenshots, clicks, cookies and tabs work as on the other
drivers. What it is not: Firefox has no DevTools protocol, so everything Hutch does over one
(``cdp_send``, the X-server click path, tab leases, user-agent client hints, websocket tracking) is
absent, and ``clear_challenge`` uses the community recipe (find the widget's frame, move to its box,
click) instead of Hutch's own solver.

Install it apart from Hutch: ``pip install camoufox && python -m camoufox fetch``.
"""

import asyncio
import time

from . import challenge as _challenge
from .session import Session, SessionState, _log

_INSTALL = "pip install camoufox && python -m camoufox fetch"


async def _widget(page):
    """(kind, box) of the first Turnstile or reCAPTCHA frame on the page with a visible box, else None."""
    for frame in page.frames:
        kind = ("turnstile" if _challenge.TURNSTILE.search(frame.url) or "challenges.cloudflare.com" in frame.url
                else "recaptcha" if _challenge.RECAPTCHA.search(frame.url) else None)
        if not kind or "size=invisible" in frame.url:
            continue
        try:
            box = await (await frame.frame_element()).bounding_box()
        except Exception:
            continue
        if box and box["width"] > 100 and box["height"] > 30:
            return kind, box
    return None


async def clear(page, *, timeout=60.0, forbids_bypass=False, settle=4.0):
    """``challenge.clear``'s contract on a Firefox page: status clear, passed, hitl, timeout or load-failed."""
    started = time.monotonic()
    clicks, kind, clicked_at, last_seen, loaded = 0, None, None, 0.0, False

    def done(status, **extra):
        return {"status": status, "kind": kind, "seconds": round(time.monotonic() - started, 1),
                "clicks": clicks, **extra}

    while time.monotonic() - started < timeout:
        title = await page.title()
        interstitial = bool(_challenge._INTERSTITIAL_TITLE.search(title))
        info = {}
        if not interstitial:  # a Cloudflare interstitial is looked at, never scripted
            try:
                info = await page.evaluate(_challenge._TOKEN_JS) or {}
            except Exception:
                info = {}
        loaded = loaded or interstitial or info.get("ready") in _challenge._LOADED
        if info.get("errorPage") or page.url.startswith(("chrome-error:", "about:neterror")):
            return done("load-failed", reason="the browser could not load the page")
        found = await _widget(page)
        if found:
            kind, last_seen = found[0], time.monotonic()
            if forbids_bypass:
                return done("hitl", reason="forbids_bypass")
        if any(info.get(k, 0) > 20 for k in ("turnstile", "recaptcha")):
            return done("passed", clicked=clicks > 0)
        if not found:
            if interstitial:
                await asyncio.sleep(1.0)
            elif last_seen and time.monotonic() - last_seen > settle:
                return done("passed", clicked=clicks > 0)
            elif not last_seen and loaded and time.monotonic() - started > settle * 2:
                return done("clear")
            else:
                await asyncio.sleep(0.5)
            continue
        if clicks < 3 and (clicked_at is None or time.monotonic() - clicked_at > 12):
            box = found[1]
            x, y = box["x"] + 30, box["y"] + box["height"] / 2
            try:
                await page.mouse.move(x - 120, y - 40)
                await page.mouse.move(x, y, steps=12)
                await asyncio.sleep(0.3)
                await page.mouse.click(x, y)
                clicks += 1
            except Exception as exc:
                _log.debug("camoufox click failed: %s", exc)
            clicked_at = time.monotonic()
        await asyncio.sleep(0.7)
    return done("timeout" if kind or loaded else "load-failed")


class CamoufoxSession(Session):
    """EXPERIMENTAL. A Session whose browser is Camoufox, launched in a persistent profile."""

    def __init__(self, name, profile_dir, **kw):
        kw["driver"] = "camoufox"
        kw["launcher"] = "camoufox"
        kw["capture"] = False
        for refused, why in (("endpoint", "attach to a running browser"), ("record_video", "video recording")):
            if kw.get(refused):
                raise ValueError(f"the camoufox driver cannot {why}")
        if kw.get("fingerprint") is not None:
            raise ValueError("camoufox generates its own fingerprint; personas are not supported")
        super().__init__(name, profile_dir, **kw)
        self._camoufox = None

    async def launch(self, playwright=None):
        if self._context:
            return self._context
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as exc:
            raise RuntimeError(f"driver 'camoufox' is not installed: {_INSTALL}") from exc
        options = {"headless": True if self.headless else ("virtual" if self._screen_kind() == "virtual" else False),
                   "humanize": True, "os": "linux", "persistent_context": True, "user_data_dir": self.profile_dir}
        if self.proxy:
            options["proxy"] = {k: v for k, v in (("server", self.proxy.server), ("bypass", self.proxy.bypass),
                                                  ("username", self.proxy.username),
                                                  ("password", self.proxy.password)) if v}
        self._camoufox = AsyncCamoufox(**options)
        self._context = await self._camoufox.__aenter__()
        self._owned = True
        self._pages = list(self._context.pages)
        for page in self._pages:
            self._wire(page)
        self._context.on("page", lambda page: (self._pages.append(page), self._wire(page)))
        self._context.on("close", lambda _: self._on_browser_gone())
        self._launched_at = self._last_activity = time.time()
        self.state = SessionState.ACTIVE
        return self._context

    def _wire(self, page):
        page.on("dialog", lambda d, p=page: asyncio.ensure_future(self._on_dialog(p, d)))
        page.on("popup", lambda popup, opener=page: self._follow_popup(opener, popup))
        page.on("close", lambda p: self._on_page_closed(p))
        if self.headless:
            self._claim(page)

    async def target_id(self, page=None):
        return None

    async def clear_challenge(self, *, timeout=60.0, forbids_bypass=None):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        if forbids_bypass is None:
            forbids_bypass = str(self.tags.get("forbids_bypass", "")).lower() in ("1", "true", "yes")
        result = await clear(page, timeout=timeout, forbids_bypass=forbids_bypass)
        self.events.emit("challenge", **{k: v for k, v in result.items() if k in ("status", "kind")})
        self.challenge = {**result, "at": round(time.time())}
        return result

    async def _teardown(self, *, keep_browser=False, clear_capture=True):
        await super()._teardown(keep_browser=keep_browser, clear_capture=clear_capture)
        runner, self._camoufox = self._camoufox, None
        if runner:
            try:
                await asyncio.wait_for(runner.__aexit__(None, None, None), 15)
            except Exception:
                pass

    async def _recover_page(self):
        return False

    def _start_watchdog(self):
        return None
