"""The ``cdp`` driver: a Session on hutch's own DevTools client, with no automation library attached.

Playwright and patchright sit between Hutch and Chrome. Hutch launches Chrome natively either way,
so what the page sees comes from the launch flags, the real display and the way clicks arrive, and
this driver keeps exactly that and drops the library. ``hutch.cdp`` sends only the commands a call
needs: no ``Runtime.enable`` (so a page cannot catch its console being serialised), no init scripts,
no routes and no emulation overrides. Challenge clicks still go through the X server.

``CdpPage`` is the slice of Playwright's page API that ``Session``, the daemon and the MCP server
read, built from DevTools commands, so everything written against that slice runs unchanged:
navigation with load waits, evaluate, content, screenshots, CSS-selector click/fill/type/press, mouse
and keyboard, cookies, tabs, dialogs, popups and ``clear_challenge``. What needs a library to
intercept or watch traffic (routing, mocking, request capture, locators by role or text, aria
snapshots, tracing, emulation) raises ``Unsupported`` naming the call.
"""

import asyncio
import base64
import contextlib
import json
import re
import time
from urllib.parse import urlsplit

from . import browser as _browser
from . import challenge as _challenge
from .cdp import Browser as _RawBrowser
from .session import _IDENTITY_JS, Session, SessionState, _log


class Unsupported(AttributeError):
    """A Playwright-shaped call the cdp driver has no DevTools-only equivalent for."""


def _unsupported(what):
    return Unsupported(f"{what} is not available on the cdp driver (it has no automation library "
                       "behind it); use driver playwright or patchright for it")


# a selector is CSS, optionally ending in :has-text("...") as `observe` writes them
_PICK = """(sel => {
  const m = /^([\\s\\S]*?)(?::has-text\\((["'])([\\s\\S]*)\\2\\))?$/.exec(sel);
  let all;
  try { all = Array.from(document.querySelectorAll(m[1] || '*')); }
  catch (e) { throw new Error('unsupported selector: ' + sel); }
  if (m[3] !== undefined) all = all.filter(e => (e.innerText || e.textContent || '').includes(m[3]));
  return all[0] || null;
})"""

_RECT = "(sel => { const el = " + _PICK + "(sel); if (!el) return null; " \
    "el.scrollIntoView({block: 'center', inline: 'center'}); const r = el.getBoundingClientRect(); " \
    "const s = getComputedStyle(el); return {x: r.x, y: r.y, width: r.width, height: r.height, " \
    "visible: r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'}; })"
_FOCUS = "(sel => { const el = " + _PICK + "(sel); if (!el) return false; el.focus(); " \
    "if (el.select) el.select(); else document.execCommand('selectAll'); return true; })"
_FUNCTION = re.compile(r"^\s*(async\s+)?(function\b|\((?:[^()]|\([^()]*\))*\)\s*=>|[\w$]+\s*=>)")
_READY = {"domcontentloaded": ("interactive", "complete"), "load": ("complete",), "networkidle": ("complete",)}
_LOAD_EVENT = {"load": "Page.loadEventFired", "networkidle": "Page.loadEventFired",
               "domcontentloaded": "Page.domContentEventFired"}
_BUTTONS = {"left": 1, "right": 2, "middle": 4}
_MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
_NAMED = {"Enter": (13, "\r"), "Tab": (9, None), "Escape": (27, None), "Backspace": (8, None),
          "Delete": (46, None), "ArrowLeft": (37, None), "ArrowUp": (38, None), "ArrowRight": (39, None),
          "ArrowDown": (40, None), "Home": (36, None), "End": (35, None), "PageUp": (33, None),
          "PageDown": (34, None), "Space": (32, " ")}


def _key_event(key):
    """(modifiers, params) for a key name like ``Enter``, ``a`` or ``Control+a``."""
    *mods, name = key.split("+") if len(key) > 1 else [key]
    mask = sum(_MODIFIERS.get(m, 0) for m in mods)
    if name == " ":
        name = "Space"
    if name in _NAMED:
        vk, text = _NAMED[name]
        return mask, {"key": " " if name == "Space" else name, "code": name, "windowsVirtualKeyCode": vk,
                      "text": text if not mask & 6 else None}
    code = f"Key{name.upper()}" if name.isalpha() else f"Digit{name}" if name.isdigit() else ""
    return mask, {"key": name, "code": code, "windowsVirtualKeyCode": ord(name.upper()),
                  "text": name if not mask & 6 else None}


class _Mouse:
    def __init__(self, page):
        self._page, self.x, self.y, self._down = page, 0.0, 0.0, 0

    async def _event(self, kind, x, y, **kw):
        await self._page.target.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, **kw})

    async def move(self, x, y, steps=1):
        x0, y0 = self.x, self.y
        for i in range(1, max(1, steps) + 1):
            self.x, self.y = x0 + (x - x0) * i / steps, y0 + (y - y0) * i / steps
            await self._event("mouseMoved", self.x, self.y, buttons=self._down)

    async def down(self, button="left", click_count=1):
        self._down |= _BUTTONS[button]
        await self._event("mousePressed", self.x, self.y, button=button, buttons=self._down, clickCount=click_count)

    async def up(self, button="left", click_count=1):
        self._down &= ~_BUTTONS[button]
        await self._event("mouseReleased", self.x, self.y, button=button, buttons=self._down, clickCount=click_count)

    async def click(self, x, y, button="left", click_count=1, delay=0):
        await self.move(x, y)
        for i in range(click_count):
            await self.down(button, i + 1)
            if delay:
                await asyncio.sleep(delay / 1000)
            await self.up(button, i + 1)

    async def dblclick(self, x, y, button="left", delay=0):
        await self.click(x, y, button, 2, delay)

    async def wheel(self, delta_x, delta_y):
        await self._event("mouseWheel", self.x, self.y, deltaX=delta_x, deltaY=delta_y)


class _Keyboard:
    def __init__(self, page):
        self._page, self._mods = page, 0

    async def _send(self, kind, params, mods):
        params = {k: v for k, v in params.items() if v is not None}
        await self._page.target.send("Input.dispatchKeyEvent", {"type": kind, "modifiers": mods, **params})

    async def down(self, key):
        mask, params = _key_event(key)
        self._mods |= mask
        await self._send("keyDown" if params["text"] else "rawKeyDown", params, self._mods)

    async def up(self, key):
        mask, params = _key_event(key)
        await self._send("keyUp", {**params, "text": None}, self._mods)
        self._mods &= ~mask

    async def press(self, key, delay=0):
        await self.down(key)
        if delay:
            await asyncio.sleep(delay / 1000)
        await self.up(key)

    async def insert_text(self, text):
        await self._page.target.send("Input.insertText", {"text": text})

    async def type(self, text, delay=0):
        for ch in text:
            if ch == "\n":
                await self.press("Enter")
            elif ch.isascii() and ch.isprintable():
                await self.press(ch)
            else:
                await self.insert_text(ch)
            if delay:
                await asyncio.sleep(delay / 1000)


class _Dialog:
    def __init__(self, page, params):
        self._page = page
        self.type, self.message = params.get("type"), params.get("message", "")
        self.default_value = params.get("defaultPrompt", "")

    async def accept(self, prompt_text=None):
        await self._page.target.send("Page.handleJavaScriptDialog", {"accept": True, "promptText": prompt_text or ""})

    async def dismiss(self):
        await self._page.target.send("Page.handleJavaScriptDialog", {"accept": False})


class _Locator:
    """Just enough of a locator for the humanised click: the first element a CSS selector finds."""

    def __init__(self, page, selector):
        self._page, self._selector = page, selector

    @property
    def first(self):
        return self

    async def scroll_into_view_if_needed(self):
        await self._page._rect(self._selector, 0)

    async def bounding_box(self):
        box = await self._page._rect(self._selector, 5000)
        return {k: box[k] for k in ("x", "y", "width", "height")}

    async def fill(self, value):
        await self._page.fill(self._selector, value)


class CdpPage:
    """One tab, spoken to through a ``hutch.cdp.Target``."""

    def __init__(self, session, target, url="about:blank"):
        self.session, self.target, self.url = session, target, url
        self.mouse, self.keyboard = _Mouse(self), _Keyboard(self)
        self.viewport_size = None
        self.frames = []
        self._closed = False
        self._handlers = {}

    async def start(self):
        self.target.on("Page.javascriptDialogOpening", lambda p: self._emit("dialog", _Dialog(self, p)))
        await self.target.send("Page.enable")
        info = (await self.session._browser.raw.conn.send("Target.getTargetInfo", {"targetId": self.target.id}))
        self.url = info["targetInfo"]["url"]

    def on(self, event, handler):
        if event in ("dialog", "close"):
            self._handlers.setdefault(event, []).append(handler)

    def _emit(self, event, *args):
        for handler in list(self._handlers.get(event, ())):
            try:
                handler(*args)
            except Exception:
                _log.debug("handler for %s failed", event, exc_info=True)

    def _gone(self):
        if not self._closed:
            self._closed = True
            self._emit("close", self)

    def is_closed(self):
        return self._closed

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise _unsupported(f"page.{name}")

    async def title(self):
        got = await self.session._browser.raw.conn.send("Target.getTargetInfo", {"targetId": self.target.id})
        return got["targetInfo"].get("title", "")

    async def evaluate(self, expression, arg=None, **_):
        send = self.target.send
        if _FUNCTION.match(expression):
            held = await send("Runtime.evaluate", {"expression": f"({expression})", "objectGroup": "hutch"})
            self._check(held)
            call = {"objectId": held["result"]["objectId"], "functionDeclaration": "function(...a){return this(...a)}",
                    "returnByValue": True, "awaitPromise": True}
            if arg is not None:
                call["arguments"] = [{"value": arg}]
            try:
                out = await send("Runtime.callFunctionOn", call)
            finally:
                await send("Runtime.releaseObjectGroup", {"objectGroup": "hutch"})
        else:
            out = await send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                  "awaitPromise": True})
        self._check(out)
        return out.get("result", {}).get("value")

    @staticmethod
    def _check(out):
        if "exceptionDetails" in out:
            details = out["exceptionDetails"]
            raise RuntimeError((details.get("exception") or {}).get("description") or details.get("text", "evaluate failed"))

    async def content(self):
        return await self.evaluate("document.documentElement.outerHTML")

    async def goto(self, url, *, wait_until="load", timeout=30000, **_):
        """Navigate and wait for ``wait_until`` (commit, domcontentloaded, load, networkidle).

        A page that never gets there raises ``TimeoutError``; one the browser could not reach raises
        ``RuntimeError`` with Chrome's error text.
        """
        event = _LOAD_EVENT.get(wait_until)
        waiter = asyncio.ensure_future(self.target.wait_event(event, timeout / 1000)) if event else None
        try:
            got = await self.target.send("Page.navigate", {"url": url}, timeout=timeout / 1000)
            if got.get("errorText"):
                raise RuntimeError(f"navigating to {url}: {got['errorText']}")
            if waiter and "loaderId" in got:  # a same-document change fires no load event
                await waiter
        except asyncio.TimeoutError:
            raise TimeoutError(f"navigating to {url}: {wait_until} not reached within {timeout / 1000:.0f}s") from None
        finally:
            if waiter and not waiter.done():
                waiter.cancel()
            await asyncio.gather(*([waiter] if waiter else []), return_exceptions=True)
        if wait_until == "networkidle":
            await asyncio.sleep(0.5)

    async def _history(self, step, wait_until, timeout):
        hist = await self.target.send("Page.getNavigationHistory")
        index = hist["currentIndex"] + step
        if not 0 <= index < len(hist["entries"]):
            return None
        waiter = asyncio.ensure_future(self.target.wait_event(_LOAD_EVENT.get(wait_until, "Page.loadEventFired"), timeout / 1000))
        await self.target.send("Page.navigateToHistoryEntry", {"entryId": hist["entries"][index]["id"]})
        try:
            await waiter
        except asyncio.TimeoutError:
            raise TimeoutError(f"history navigation: {wait_until} not reached within {timeout / 1000:.0f}s") from None

    async def go_back(self, *, wait_until="load", timeout=30000, **_):
        return await self._history(-1, wait_until, timeout)

    async def go_forward(self, *, wait_until="load", timeout=30000, **_):
        return await self._history(1, wait_until, timeout)

    async def reload(self, *, wait_until="load", timeout=30000, **_):
        waiter = asyncio.ensure_future(self.target.wait_event(_LOAD_EVENT.get(wait_until, "Page.loadEventFired"), timeout / 1000))
        await self.target.send("Page.reload")
        try:
            await waiter
        except asyncio.TimeoutError:
            raise TimeoutError(f"reload: {wait_until} not reached within {timeout / 1000:.0f}s") from None

    async def wait_for_load_state(self, state="load", *, timeout=30000):
        deadline = time.monotonic() + timeout / 1000
        while await self.evaluate("document.readyState") not in _READY.get(state, ("complete",)):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{state} not reached within {timeout / 1000:.0f}s")
            await asyncio.sleep(0.1)
        if state == "networkidle":
            await asyncio.sleep(0.5)

    async def screenshot(self, *, full_page=False, path=None, **_):
        params = {"format": "png"}
        if full_page:
            size = (await self.target.send("Page.getLayoutMetrics"))["cssContentSize"]
            params.update(captureBeyondViewport=True, clip={"x": 0, "y": 0, "width": size["width"],
                                                            "height": size["height"], "scale": 1})
        png = base64.b64decode((await self.target.send("Page.captureScreenshot", params))["data"])
        if path:
            with open(path, "wb") as f:
                f.write(png)
        return png

    async def bring_to_front(self):
        await self.target.send("Page.bringToFront")

    async def close(self):
        await self.session._browser.raw.conn.send("Target.closeTarget", {"targetId": self.target.id})
        self._gone()

    def locator(self, selector):
        return _Locator(self, selector)

    def _need_css(self, selector):
        if re.match(r"^(aria-ref=|e\d+$|f\d+e\d+$|text=|role=|xpath=|//)", selector):
            raise _unsupported(f"selector '{selector}'")

    async def _rect(self, selector, timeout, state="visible"):
        self._need_css(selector)
        deadline = time.monotonic() + timeout / 1000
        while True:
            box = await self.evaluate(f"{_RECT}({json.dumps(selector)})")
            if box and (box["visible"] or state == "attached"):
                return box
            if time.monotonic() >= deadline:
                raise TimeoutError(f"'{selector}' not {state} within {timeout / 1000:.0f}s")
            await asyncio.sleep(0.1)

    async def wait_for_selector(self, selector, *, state="visible", timeout=30000):
        if state in ("hidden", "detached"):
            deadline = time.monotonic() + timeout / 1000
            while True:
                try:
                    await self._rect(selector, 0, "attached" if state == "hidden" else state)
                except TimeoutError:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"'{selector}' still {'attached' if state == 'detached' else 'visible'}")
                await asyncio.sleep(0.1)
        await self._rect(selector, timeout, state)

    async def _prop(self, selector, expression):
        self._need_css(selector)
        return await self.evaluate(f"(sel => {{ const el = {_PICK}(sel); return el ? ({expression}) : null; }})"
                                   f"({json.dumps(selector)})")

    async def inner_text(self, selector, **_):
        return await self._prop(selector, "el.innerText")

    async def inner_html(self, selector, **_):
        return await self._prop(selector, "el.innerHTML")

    async def text_content(self, selector, **_):
        return await self._prop(selector, "el.textContent")

    async def get_attribute(self, selector, name, **_):
        return await self._prop(selector, f"el.getAttribute({json.dumps(name)})")

    async def input_value(self, selector, **_):
        return await self._prop(selector, "el.value")

    async def is_visible(self, selector, **_):
        return bool(await self._prop(selector, "el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden'"))

    async def is_hidden(self, selector, **_):
        return not await self.is_visible(selector)

    async def is_checked(self, selector, **_):
        return bool(await self._prop(selector, "el.checked"))

    async def is_enabled(self, selector, **_):
        return bool(await self._prop(selector, "!el.disabled"))

    async def is_editable(self, selector, **_):
        return bool(await self._prop(selector, "!el.disabled && !el.readOnly"))

    async def focus(self, selector, **_):
        await self._focus(selector, 30000)

    async def set_checked(self, selector, checked, **_):
        if await self.is_checked(selector) != checked:
            await self.click(selector)

    async def check(self, selector, **_):
        await self.set_checked(selector, True)

    async def uncheck(self, selector, **_):
        await self.set_checked(selector, False)

    async def dblclick(self, selector, *, timeout=30000, **_):
        await self.click(selector, timeout=timeout, click_count=2)

    async def click(self, selector, *, timeout=30000, button="left", click_count=1, **_):
        box = await self._rect(selector, timeout)
        await self.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, button, click_count)

    async def hover(self, selector, *, timeout=30000, **_):
        box = await self._rect(selector, timeout)
        await self.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    async def _focus(self, selector, timeout):
        await self._rect(selector, timeout)
        if not await self.evaluate(f"{_FOCUS}({json.dumps(selector)})"):
            raise TimeoutError(f"'{selector}' not found")

    async def fill(self, selector, value, *, timeout=30000, **_):
        await self._focus(selector, timeout)
        if value:
            await self.keyboard.insert_text(value)
        else:
            await self.keyboard.press("Backspace")

    async def type(self, selector, text, *, delay=0, timeout=30000, **_):
        await self._focus(selector, timeout)
        await self.keyboard.type(text, delay)

    async def press(self, selector, key, *, timeout=30000, **_):
        await self._focus(selector, timeout)
        await self.keyboard.press(key)


class CdpBrowser:
    """What ``Session`` asks of a browser object, over a ``hutch.cdp.Browser``."""

    def __init__(self, raw, version=""):
        self.raw, self.version = raw, version

    async def new_browser_cdp_session(self):
        return _Raw(self.raw.conn)

    def on(self, event, handler):
        if event == "disconnected":
            self.raw.conn.closed.add_done_callback(lambda _: handler(self))

    async def close(self):
        await self.raw.close()


class _Raw:
    def __init__(self, conn):
        self._conn = conn

    async def send(self, method, params=None):
        return await self._conn.send(method, params)

    async def detach(self):
        return None


def _sent_to(cookie, url):
    """Would the browser send this cookie to ``url``?"""
    host, domain = url.hostname or "", cookie["domain"]
    return ((host == domain.lstrip(".") or (domain.startswith(".") and host.endswith(domain)))
            and (url.path or "/").startswith(cookie["path"]) and (url.scheme == "https" or not cookie["secure"]))


def _cdp_cookie(c):
    out = {k: c[k] for k in ("name", "value", "domain", "path", "httpOnly", "secure", "sameSite") if k in c}
    out["expires"] = -1 if c.get("session") else c.get("expires", -1)
    return out


class CdpContext:
    """The browser's default context: cookies and tabs."""

    def __init__(self, session):
        self._session = session

    @property
    def pages(self):
        return list(self._session._pages)

    async def cookies(self, urls=None):
        got = (await self._session._browser.raw.conn.send("Storage.getCookies"))["cookies"]
        if urls:
            wanted = [urlsplit(u) for u in ([urls] if isinstance(urls, str) else urls)]
            got = [c for c in got if any(_sent_to(c, u) for u in wanted)]
        return [_cdp_cookie(c) for c in got]

    async def add_cookies(self, cookies):
        batch = []
        for c in cookies:
            one = {k: v for k, v in c.items() if k in ("name", "value", "url", "domain", "path", "httpOnly", "secure", "sameSite")}
            if c.get("expires", -1) not in (-1, None):
                one["expires"] = c["expires"]
            batch.append(one)
        await self._session._browser.raw.conn.send("Storage.setCookies", {"cookies": batch})

    async def clear_cookies(self):
        await self._session._browser.raw.conn.send("Storage.clearCookies", {})

    def on(self, *_):
        return None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise _unsupported(f"context.{name}")


class CdpSession(Session):
    """A Session whose browser is Chrome launched by Hutch and driven by ``hutch.cdp`` alone."""

    def __init__(self, name, profile_dir, **kw):
        kw["driver"] = "cdp"
        if kw.get("launcher") not in (None, "native", "external") or kw.get("record_video"):
            raise ValueError("the cdp driver launches Chrome natively; use driver playwright for that")
        if kw.get("proxy") and getattr(kw["proxy"], "username", None):
            raise ValueError("the cdp driver cannot answer proxy authentication; use driver playwright")
        if kw.get("fingerprint") is not None and (kw["fingerprint"].is_mobile or kw["fingerprint"].has_touch):
            raise ValueError("the cdp driver has no touch or mobile emulation; use driver playwright")
        kw["capture"] = False
        kw["launcher"] = "external" if kw.get("endpoint") else "native"
        super().__init__(name, profile_dir, **kw)

    async def launch(self, playwright=None):
        if self._context:
            return self._context
        if self.require_proxy and not self.proxy and not self.endpoint:
            raise RuntimeError(f"session '{self.name}' requires a proxy and has none")
        running = None if self.endpoint else _browser.status(self.profile_dir)
        if running and self.proxy and running.get("proxy") != self.proxy.server:
            raise RuntimeError(f"session '{self.name}' is open without its proxy {self.proxy.server} "
                               f"(running with {running.get('proxy') or 'none'}); stop it first")
        if self.block_private and not self.endpoint and not self.proxy:
            await self._start_net_proxy(running)
        owned = False
        if self.endpoint:
            info = {"ws": await asyncio.to_thread(_browser.resolve_endpoint, self.endpoint)}
        elif running and running["ws"]:
            info = running
            if running.get("headless") is not None:
                self.headless = running["headless"]
        elif running:
            raise RuntimeError(f"session '{self.name}' is open in login mode, close that window first")
        else:
            kw = await asyncio.to_thread(self.spawn_options)
            info = await asyncio.to_thread(_browser.spawn, self.profile_dir, tie_to_parent=self.headless, **kw)
            owned = True
        await self._connect(info, owned=owned)
        self._real_ua = None
        if self.headless and not self.persona:
            self._real_ua = await self._probe_real_ua()
        await self._adopt_existing()
        self._launched_at = self._last_activity = time.time()
        self.state = SessionState.ACTIVE
        return self._context

    async def _connect(self, info, *, owned):
        raw = await _RawBrowser.connect(info["ws"])
        try:
            version = (await raw.conn.send("Browser.getVersion")).get("product", "").rpartition("/")[2]
            self._browser = CdpBrowser(raw, version)
            self._context = CdpContext(self)
            self._owned = owned
            self._pid = info.get("pid")
            self._pages, self._adopting, self._ready = [], {}, False
            self._browser.on("disconnected", lambda _: self._on_browser_gone())
            raw.conn.on("Target.targetInfoChanged", self._info_changed)
            raw.conn.on("Target.targetDestroyed", self._target_destroyed)
            raw.conn.on("Target.targetCreated", self._target_created)
            await raw.conn.send("Target.setDiscoverTargets", {"discover": True})
        except BaseException:
            self._context = self._browser = None
            await raw.close()
            raise

    async def _adopt_existing(self):
        self._ready = True
        await asyncio.gather(*(self._adopt(t["targetId"], t["url"]) for t in await self._browser.raw.pages()),
                             return_exceptions=True)  # a tab closing as we list it is not a failure

    async def _read_identity(self):
        try:
            target = await self._browser.raw.new_page("about:blank")
        except Exception:
            return None
        try:  # a browser page is a secure context with no network: the hints exist there
            await target.send("Page.enable")
            loaded = asyncio.ensure_future(target.wait_event("Page.loadEventFired", 10))
            await target.send("Page.navigate", {"url": "chrome://version"})
            await loaded
            got = await target.send("Runtime.evaluate", {"expression": _IDENTITY_JS, "returnByValue": True})
            return got.get("result", {}).get("value")
        except Exception as exc:
            _log.debug("session '%s': client-hint probe failed: %s", self.name, exc)
            return None
        finally:
            with contextlib.suppress(Exception):
                await self._browser.raw.conn.send("Target.closeTarget", {"targetId": target.id})

    def _adopt(self, target_id, url="about:blank"):
        """The page for a target, attached once however many events and callers ask for it."""
        if target_id not in self._adopting:
            async def attach():
                page = CdpPage(self, await self._browser.raw.attach(target_id), url)
                await page.start()
                if self._ua_params():  # headless: give back the client hints --user-agent blanks
                    await page.target.send("Network.setUserAgentOverride", self._ua_params())
                page.on("dialog", lambda d: asyncio.ensure_future(self._on_dialog(page, d)))
                page.on("close", lambda p: self._on_page_closed(p))
                self._pages.append(page)
                if self.headless:
                    self._claim(page)
                await self._remember_target(page)
                return page

            task = self._adopting[target_id] = asyncio.ensure_future(attach())
            task.add_done_callback(lambda t: (t.cancelled() or t.exception()) and self._adopting.pop(target_id, None))
        return self._adopting[target_id]

    def _page_of(self, target_id):
        return next((p for p in self._pages if p.target.id == target_id), None)

    def _info_changed(self, params):
        page = self._page_of(params["targetInfo"]["targetId"])
        if page:
            page.url = params["targetInfo"]["url"]

    def _target_destroyed(self, params):
        page = self._page_of(params["targetId"])
        if page:
            page._gone()
        self._adopting.pop(params["targetId"], None)

    def _target_created(self, params):
        info = params["targetInfo"]
        if (info["type"] != "page" or info["url"].startswith("devtools://") or not self._browser
                or not self._ready or self._closing):
            return
        fresh = info["targetId"] not in self._adopting
        task = self._adopt(info["targetId"], info["url"])

        def opened(done):
            opener = self._page_of(info.get("openerId") or "")
            if fresh and opener and not done.cancelled() and not done.exception():
                self._follow_popup(opener, done.result())

        task.add_done_callback(opened)

    async def new_page(self):
        self._require_active()
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        made = await self._browser.raw.conn.send("Target.createTarget", {"url": "about:blank"})
        page = await self._adopt(made["targetId"])
        self._focus = page
        self._claim(page)
        self._last_activity = time.time()
        return page

    async def target_id(self, page=None):
        page = page or self._active_page()
        return page.target.id if page else None

    def _challenge_surface(self, page, xdisplay):
        return _challenge.RawSurface(self._browser.raw, page.target, xdisplay=xdisplay)

    async def cdp_send(self, method, params=None):
        return await self._require_page().target.send(method, params)

    async def cdp_close(self):
        return None

    async def _recover_page(self):
        return False

    def _start_watchdog(self):
        return None
