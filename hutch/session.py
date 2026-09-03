import asyncio
import functools
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TargetClosedError

from .context import Context, Snapshot

_log = logging.getLogger(__name__)


class SessionState(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    HIBERNATED = "hibernated"


@dataclass
class Fingerprint:
    viewport_width: int = 1920
    viewport_height: int = 1080
    screen_width: int = 1920
    screen_height: int = 1080
    user_agent: Optional[str] = None
    platform: Optional[str] = None
    locale: str = "en-US"
    timezone: str = "America/New_York"
    color_scheme: str = "light"
    device_scale_factor: float = 1.0
    has_touch: bool = False
    is_mobile: bool = False
    geolocation: Optional[dict] = None
    disable_webrtc: bool = True
    extra_headers: dict = field(default_factory=dict)


@dataclass
class ProxyConfig:
    server: str = ""
    username: Optional[str] = None
    password: Optional[str] = None
    bypass: Optional[str] = None


_META_FILE = "hutch_meta.json"

_DISCONNECT_PATTERNS = (
    "browser has been closed",
    "browser closed",
    "connection closed",
    "target closed",
    "page crashed",
    "execution context was destroyed",
    "frame was detached",
    "navigation failed because page crashed",
)

_SENTINEL = object()

_WS_FRAME_CAP = 200    # max frames stored per WebSocket connection
_WS_PAYLOAD_MAX = 4096  # truncation limit for individual frame payloads


def _is_disconnect(exc):
    """Return True if exc signals a dead page/context/browser."""
    if isinstance(exc, TargetClosedError):
        return True
    if isinstance(exc, PlaywrightError):
        msg = str(exc).lower()
        return any(p in msg for p in _DISCONNECT_PATTERNS)
    return False


def _retry_on_disconnect(method=None, *, fallback=_SENTINEL):
    """Decorator: catch disconnect errors, recover page, retry once.

    If *fallback* is given and recovery + retry still fails, return
    *fallback* (called if callable) instead of raising.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            try:
                return await fn(self, *args, **kwargs)
            except Exception as exc:
                if not _is_disconnect(exc):
                    raise
                _log.warning(
                    "session '%s': %s in %s, attempting recovery",
                    self.name, type(exc).__name__, fn.__name__,
                )
                if await self._recover_page():
                    try:
                        return await fn(self, *args, **kwargs)
                    except Exception:
                        if fallback is not _SENTINEL:
                            return fallback() if callable(fallback) else fallback
                        raise
                if fallback is not _SENTINEL:
                    return fallback() if callable(fallback) else fallback
                raise
        return wrapper

    if method is not None:
        # bare @_retry_on_disconnect without parens
        return decorator(method)
    return decorator


class Session:

    def __init__(self, name, profile_dir, *,
                 proxy=None, fingerprint=None, headless=True,
                 ignore_https_errors=False, stealth=True, tags=None,
                 capture=True, record_video=None):
        self.name = name
        self.profile_dir = profile_dir
        self.proxy = proxy
        self.fingerprint = fingerprint or Fingerprint()
        self.headless = headless
        self.ignore_https_errors = ignore_https_errors
        self.stealth = stealth
        self.tags = tags or {}
        self.capture = capture
        self.record_video = record_video
        self.context = Context() if capture else None
        self.state = SessionState.ACTIVE
        self.pause_reason = None
        self.auto_pause = True

        self._context = None
        self._browser = None
        self._pages = []
        self._launched_at = None
        self._created_at = time.time()
        self._last_activity = time.time()
        self._snapshot_cursor = 0
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._pause_callbacks = []
        self._intercept_rules = []
        self._dialog_handler = None
        self._pending_dialog = None
        self._popup_pages = []
        self._downloads = []
        self._cdp_session = None
        self._watchdog_task = None
        self._last_url = None
        self._websockets = []

        os.makedirs(profile_dir, exist_ok=True)
        self._save_meta()

    def _save_meta(self):
        meta = {
            "name": self.name,
            "created_at": self._created_at,
            "proxy": {"server": self.proxy.server, "bypass": self.proxy.bypass}
                  if self.proxy else None,
            "fingerprint": {
                "viewport": f"{self.fingerprint.viewport_width}x{self.fingerprint.viewport_height}",
                "user_agent": self.fingerprint.user_agent,
                "locale": self.fingerprint.locale,
                "timezone": self.fingerprint.timezone,
                "screen_width": self.fingerprint.screen_width,
                "screen_height": self.fingerprint.screen_height,
                "platform": self.fingerprint.platform,
                "color_scheme": self.fingerprint.color_scheme,
                "device_scale_factor": self.fingerprint.device_scale_factor,
                "has_touch": self.fingerprint.has_touch,
                "is_mobile": self.fingerprint.is_mobile,
                "geolocation": self.fingerprint.geolocation,
                "disable_webrtc": self.fingerprint.disable_webrtc,
                "extra_headers": self.fingerprint.extra_headers,
            },
            "headless": self.headless,
            "ignore_https_errors": self.ignore_https_errors,
            "tags": self.tags,
        }
        with open(os.path.join(self.profile_dir, _META_FILE), "w") as f:
            json.dump(meta, f, indent=2)

    @classmethod
    def from_profile_dir(cls, profile_dir):
        meta_path = os.path.join(profile_dir, _META_FILE)
        if not os.path.exists(meta_path):
            return None
        with open(meta_path) as f:
            meta = json.load(f)
        proxy = None
        if meta.get("proxy"):
            proxy = ProxyConfig(
                server=meta["proxy"]["server"],
                bypass=meta["proxy"].get("bypass"),
            )
        fp_meta = meta.get("fingerprint", {})
        vp = fp_meta.get("viewport", "1920x1080").split("x")
        fp = Fingerprint(
            viewport_width=int(vp[0]),
            viewport_height=int(vp[1]),
            user_agent=fp_meta.get("user_agent"),
            locale=fp_meta.get("locale", "en-US"),
            timezone=fp_meta.get("timezone", "America/New_York"),
            screen_width=fp_meta.get("screen_width", 1920),
            screen_height=fp_meta.get("screen_height", 1080),
            platform=fp_meta.get("platform"),
            color_scheme=fp_meta.get("color_scheme", "light"),
            device_scale_factor=fp_meta.get("device_scale_factor", 1.0),
            has_touch=fp_meta.get("has_touch", False),
            is_mobile=fp_meta.get("is_mobile", False),
            geolocation=fp_meta.get("geolocation"),
            disable_webrtc=fp_meta.get("disable_webrtc", True),
            extra_headers=fp_meta.get("extra_headers", {}),
        )
        s = cls(
            name=meta["name"],
            profile_dir=profile_dir,
            proxy=proxy,
            fingerprint=fp,
            headless=meta.get("headless", True),
            ignore_https_errors=meta.get("ignore_https_errors", False),
            tags=meta.get("tags", {}),
        )
        s._created_at = meta.get("created_at", time.time())
        s.state = SessionState.HIBERNATED
        return s

    def _launch_args(self):
        fp = self.fingerprint
        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if not self.headless:
            chrome_args.append("--start-maximized")
        if fp.disable_webrtc:
            chrome_args.extend([
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-webrtc-hw-encoding",
            ])
        args = {
            "user_data_dir": self.profile_dir,
            "headless": self.headless,
            "viewport": None if not self.headless else {"width": fp.viewport_width, "height": fp.viewport_height},
            "screen": {"width": fp.screen_width, "height": fp.screen_height},
            "locale": fp.locale,
            "timezone_id": fp.timezone,
            "color_scheme": fp.color_scheme,
            "device_scale_factor": fp.device_scale_factor,
            "has_touch": fp.has_touch,
            "is_mobile": fp.is_mobile,
            "ignore_https_errors": self.ignore_https_errors,
            "accept_downloads": True,
            "permissions": [],
            "args": chrome_args,
        }
        if self.record_video:
            args["record_video_dir"] = self.record_video
        if fp.user_agent:
            args["user_agent"] = fp.user_agent
        if fp.geolocation:
            args["geolocation"] = fp.geolocation
            args["permissions"] = ["geolocation"]
        if fp.extra_headers:
            args["extra_http_headers"] = fp.extra_headers
        if self.proxy:
            args["proxy"] = {"server": self.proxy.server}
            if self.proxy.username:
                args["proxy"]["username"] = self.proxy.username
            if self.proxy.password:
                args["proxy"]["password"] = self.proxy.password
            if self.proxy.bypass:
                args["proxy"]["bypass"] = self.proxy.bypass
        return args

    async def launch(self, playwright):
        if self._context:
            return self._context
        kwargs = self._launch_args()
        self._context = await playwright.chromium.launch_persistent_context(**kwargs)
        self._launched_at = time.time()
        self._last_activity = time.time()
        self.state = SessionState.ACTIVE
        if self.fingerprint.platform:
            await self._context.add_init_script(
                f"Object.defineProperty(navigator, 'platform', {{get: () => '{self.fingerprint.platform}'}})"
            )
        if self.stealth:
            from .stealth import apply_stealth
            await apply_stealth(self._context, fingerprint=self._fingerprint)
        self._pages = self._context.pages[:]
        for page in self._pages:
            self._setup_page(page)
        def _on_new_page(page):
            self._pages.append(page)
            self._setup_page(page)
        self._context.on("page", _on_new_page)
        self._start_watchdog()
        return self._context

    def _setup_page(self, page):
        if self.context:
            self.context.hook_page(page)
        if self._dialog_handler:
            page.on("dialog", self._dialog_handler)
        page.on("download", lambda dl: self._downloads.append(dl))
        # propagate intercept rules to new pages (popups, target=_blank, etc.)
        # page.route() is async; batch into one coroutine with error handling
        if self._intercept_rules:
            asyncio.ensure_future(self._apply_intercept_rules(page))
        self._track_websockets(page)

    async def _apply_intercept_rules(self, page):
        """Apply all stored intercept rules to a page.

        Called via ensure_future from the sync _setup_page callback.
        Playwright page.route() survives navigations within the same page,
        so rules set here persist across in-page navigations automatically.
        """
        for pattern, handler in self._intercept_rules:
            try:
                await page.route(pattern, handler)
            except Exception as exc:
                if _is_disconnect(exc):
                    return  # page gone, stop applying
                _log.warning(
                    "session '%s': failed to apply intercept %s: %s",
                    self.name, pattern, exc,
                )

    def _track_websockets(self, page):
        """Hook WebSocket events on a page for protocol analysis.

        Captures connection URLs and frame messages -- critical for mobile
        apps and SPAs that communicate via WebSocket APIs. Each connection
        stores up to _WS_FRAME_CAP frames with payloads truncated at
        _WS_PAYLOAD_MAX bytes.
        """
        def _on_ws_created(ws):
            frames = deque(maxlen=_WS_FRAME_CAP)
            entry = {
                "url": ws.url,
                "page_url": page.url,
                "opened_at": time.time(),
                "closed_at": None,
                "frames": frames,
            }
            self._websockets.append(entry)
            _log.debug("session '%s': ws opened %s", self.name, ws.url)

            def _on_sent(data):
                payload = data.get("payload", "") if isinstance(data, dict) else str(data)
                if isinstance(payload, bytes):
                    payload = payload.hex()
                if len(payload) > _WS_PAYLOAD_MAX:
                    payload = payload[:_WS_PAYLOAD_MAX]
                frames.append({"dir": "out", "data": payload, "ts": time.time()})
                if self.context:
                    self.context._emit("websocket_frame", {
                        "url": ws.url, "dir": "out", "data": payload,
                    })

            def _on_received(data):
                payload = data.get("payload", "") if isinstance(data, dict) else str(data)
                if isinstance(payload, bytes):
                    payload = payload.hex()
                if len(payload) > _WS_PAYLOAD_MAX:
                    payload = payload[:_WS_PAYLOAD_MAX]
                frames.append({"dir": "in", "data": payload, "ts": time.time()})
                if self.context:
                    self.context._emit("websocket_frame", {
                        "url": ws.url, "dir": "in", "data": payload,
                    })

            def _on_ws_close(_):
                entry["closed_at"] = time.time()
                _log.debug("session '%s': ws closed %s", self.name, ws.url)

            ws.on("framesent", _on_sent)
            ws.on("framereceived", _on_received)
            ws.on("close", _on_ws_close)

        page.on("websocket", _on_ws_created)

    def websocket_log(self):
        """Return collected WebSocket connection data.

        Each entry: url, page_url, opened_at, closed_at,
        frame_count, and frames (list of {dir, data, ts}).
        """
        result = []
        for entry in self._websockets:
            result.append({
                "url": entry["url"],
                "page_url": entry["page_url"],
                "opened_at": entry["opened_at"],
                "closed_at": entry["closed_at"],
                "frame_count": len(entry["frames"]),
                "frames": list(entry["frames"]),
            })
        return result

    # --- crash recovery ---

    async def _recover_page(self):
        """Try to restore a live page after crash or disconnect.

        Checks context, then browser.  Returns True on success.
        """
        # Determine last known URL
        last_url = self._last_url
        if not last_url and self.context:
            try:
                entries = self.context.navigations.all()
                if entries:
                    last_url = entries[-1].url
            except Exception:
                pass

        # If context ref is gone, nothing to recover into
        if not self._context:
            _log.error("session '%s': no context, cannot recover", self.name)
            self.state = SessionState.HIBERNATED
            return False

        # Probe whether the context is still alive
        try:
            await self._context.cookies()
        except Exception as exc:
            if _is_disconnect(exc):
                _log.error(
                    "session '%s': context dead, marking hibernated", self.name,
                )
                self._context = None
                self._pages = []
                if self._cdp_session:
                    self._cdp_session = None
                self.state = SessionState.HIBERNATED
                self._stop_watchdog()
                return False
            raise

        # Context alive -- open a fresh page
        try:
            page = await self._context.new_page()
        except Exception:
            _log.error("session '%s': cannot open page, context broken", self.name)
            self._context = None
            self._pages = []
            self.state = SessionState.HIBERNATED
            self._stop_watchdog()
            return False

        # _on_new_page handler already appended page and called _setup_page

        # Navigate back to the last URL if we have one
        if last_url and last_url not in ("", "about:blank"):
            try:
                await page.goto(last_url, wait_until="load", timeout=15000)
            except Exception:
                _log.debug(
                    "session '%s': recovered page but navigation to %s failed",
                    self.name, last_url,
                )

        # Invalidate stale CDP session
        if self._cdp_session:
            self._cdp_session = None

        _log.info("session '%s': page recovered (url=%s)", self.name, last_url or "blank")
        return True

    # --- connection watchdog ---

    def _start_watchdog(self):
        """Launch background browser-connectivity check (every 30s)."""
        if self._watchdog_task and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.ensure_future(self._watchdog_loop())

    def _stop_watchdog(self):
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    async def _watchdog_loop(self):
        try:
            while True:
                await asyncio.sleep(30)
                if self.state != SessionState.ACTIVE or not self._context:
                    break
                try:
                    await self._context.cookies()
                except Exception as exc:
                    if _is_disconnect(exc):
                        _log.warning(
                            "session '%s': watchdog detected disconnect",
                            self.name,
                        )
                        recovered = await self._recover_page()
                        if not recovered:
                            break
                    else:
                        break
        except asyncio.CancelledError:
            pass

    async def new_page(self):
        self._require_active()
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        # _on_new_page handler (registered in launch()) appends the page
        # to self._pages and calls _setup_page -- no explicit append here
        page = await self._context.new_page()
        self._last_activity = time.time()
        return page

    def _active_page(self):
        if not self._context:
            return None
        pages = [p for p in self._pages if not p.is_closed()]
        return pages[-1] if pages else None

    def _require_page(self):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        return page

    @_retry_on_disconnect
    async def snapshot(self, *, screenshot=False, storage=False, full=False):
        if not self.context:
            raise RuntimeError("context capture not enabled")
        self._last_activity = time.time()
        page = self._active_page()
        url = ""
        title = ""
        cookies = []
        dom = None
        shot = None
        store = None

        if page:
            url = page.url
            title = await page.title()
            try:
                dom = await page.accessibility.snapshot()
            except Exception:
                pass
            if screenshot:
                shot = await page.screenshot()
            if storage:
                store = await page.evaluate("""() => {
                    const ls = {}; const ss = {};
                    try { for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i); ls[k] = localStorage.getItem(k);
                    }} catch(e) {}
                    try { for (let i = 0; i < sessionStorage.length; i++) {
                        const k = sessionStorage.key(i); ss[k] = sessionStorage.getItem(k);
                    }} catch(e) {}
                    return {localStorage: ls, sessionStorage: ss};
                }""")

        if self._context:
            cookies = await self._context.cookies()

        since = 0 if full else self._snapshot_cursor
        snap = Snapshot(
            url=url,
            title=title,
            cursor=self.context.cursor,
            timestamp=time.time(),
            cookies=cookies,
            dom=dom,
            screenshot=shot,
            network=self.context.network.since(since),
            console=self.context.console.since(since),
            errors=self.context.errors.since(since),
            navigations=self.context.navigations.since(since),
            storage=store,
        )
        self.context._last_cookies = list(cookies)
        self._snapshot_cursor = self.context.cursor
        return snap

    async def diff(self, since=None):
        if not self.context:
            raise RuntimeError("context capture not enabled")
        self._last_activity = time.time()
        cursor = since if since is not None else self._snapshot_cursor
        cookies = []
        if self._context:
            cookies = await self._context.cookies()
        d = self.context.diff(since=cursor, current_cookies=cookies)
        self._snapshot_cursor = self.context.cursor
        return d

    @_retry_on_disconnect
    async def goto(self, url, *, wait_until="load"):
        self._require_active()
        page = self._active_page()
        if not page:
            page = await self.new_page()
        self._last_activity = time.time()
        self._last_url = url
        await page.goto(url, wait_until=wait_until)
        return await self.page_state()

    @_retry_on_disconnect
    async def click(self, selector, *, wait_after="networkidle", timeout=5000):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.click(selector)
        if wait_after:
            try:
                await page.wait_for_load_state(wait_after, timeout=timeout)
            except Exception:
                pass
        return await self.page_state()

    @_retry_on_disconnect
    async def fill(self, selector, value, *, press_enter=False):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.fill(selector, value)
        if press_enter:
            await page.press(selector, "Enter")
        return await self.page_state()

    @_retry_on_disconnect
    async def type_text(self, selector, text, *, delay=50):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.type(selector, text, delay=delay)
        return await self.page_state()

    @_retry_on_disconnect
    async def press(self, selector, key):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.press(selector, key)
        return await self.page_state()

    @_retry_on_disconnect
    async def select_option(self, selector, value):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.select_option(selector, value)
        return await self.page_state()

    @_retry_on_disconnect
    async def evaluate(self, expression):
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        return await page.evaluate(expression)

    @_retry_on_disconnect
    async def wait_for(self, selector=None, *, state="visible",
                       url=None, timeout=30000):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        if url:
            await page.wait_for_url(url, timeout=timeout)
        elif selector:
            await page.wait_for_selector(selector, state=state, timeout=timeout)
        return await self.page_state()

    async def cookies(self, urls=None):
        if not self._context:
            return []
        if urls:
            return await self._context.cookies(urls)
        return await self._context.cookies()

    async def set_cookie(self, **cookie):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.add_cookies([cookie])

    async def set_cookies(self, cookies):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.add_cookies(cookies)

    async def delete_cookies(self):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.clear_cookies()

    async def storage(self):
        page = self._active_page()
        if not page:
            return {"localStorage": {}, "sessionStorage": {}}
        return await page.evaluate("""() => {
            const ls = {}; const ss = {};
            try { for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i); ls[k] = localStorage.getItem(k);
            }} catch(e) {}
            try { for (let i = 0; i < sessionStorage.length; i++) {
                const k = sessionStorage.key(i); ss[k] = sessionStorage.getItem(k);
            }} catch(e) {}
            return {localStorage: ls, sessionStorage: ss};
        }""")

    async def set_storage(self, key, value, *, session_storage=False):
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        store = "sessionStorage" if session_storage else "localStorage"
        await page.evaluate(
            f"{store}.setItem({json.dumps(key)}, {json.dumps(value)})")

    async def screenshot(self, *, full_page=False, path=None):
        kwargs = {"full_page": full_page}
        if path:
            kwargs["path"] = path

        page = self._active_page()
        if page:
            try:
                return await page.screenshot(**kwargs)
            except Exception as exc:
                if not _is_disconnect(exc):
                    raise
                _log.warning(
                    "session '%s': disconnect in screenshot, recovering",
                    self.name,
                )
                if await self._recover_page():
                    page = self._active_page()
                    if page:
                        return await page.screenshot(**kwargs)

        # Fallback: try any remaining live page
        if self._context:
            for p in reversed(self._pages):
                if not p.is_closed():
                    try:
                        return await p.screenshot(**kwargs)
                    except Exception:
                        continue

        raise RuntimeError(f"session '{self.name}' has no open page")

    @_retry_on_disconnect(fallback=lambda: {"url": "", "title": "", "dom": None})
    async def page_state(self):
        page = self._active_page()
        if not page:
            return {"url": "", "title": "", "dom": None}
        self._last_activity = time.time()
        dom = None
        try:
            dom = await page.accessibility.snapshot()
        except Exception:
            pass
        url = page.url
        self._last_url = url
        return {
            "url": url,
            "title": await page.title(),
            "dom": dom,
        }

    @_retry_on_disconnect(fallback=[])
    async def observe(self):
        self._require_active()
        page = self._active_page()
        if not page:
            return []
        self._last_activity = time.time()
        return await page.evaluate("""() => {
            const results = [];
            let idx = 0;
            const walk = (el) => {
                const tag = el.tagName?.toLowerCase();
                if (!tag) return;
                const interactive = (
                    tag === 'a' || tag === 'button' || tag === 'input' ||
                    tag === 'select' || tag === 'textarea' ||
                    el.getAttribute('role') === 'button' ||
                    el.getAttribute('role') === 'link' ||
                    el.getAttribute('role') === 'tab' ||
                    el.getAttribute('role') === 'menuitem' ||
                    el.getAttribute('contenteditable') === 'true' ||
                    el.onclick !== null ||
                    el.hasAttribute('tabindex')
                );
                if (interactive && el.offsetParent !== null) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        const entry = {
                            idx: idx++,
                            tag: tag,
                            type: el.type || null,
                            role: el.getAttribute('role') || null,
                            text: (el.innerText || el.value || el.placeholder || el.title || el.alt || '').slice(0, 100).trim(),
                            name: el.name || null,
                            id: el.id || null,
                            href: el.href || null,
                            selector: _selector(el),
                        };
                        if (tag === 'input') entry.inputType = el.type;
                        if (tag === 'select') {
                            entry.options = Array.from(el.options).slice(0, 20).map(o => o.text.trim());
                        }
                        if (tag === 'form' || el.closest('form')) {
                            const form = tag === 'form' ? el : el.closest('form');
                            entry.formAction = form?.getAttribute('action') || null;
                            entry.formMethod = form?.getAttribute('method') || null;
                        }
                        results.push(entry);
                    }
                }
                for (const child of el.children) walk(child);
            };
            const _selector = (el) => {
                if (el.id) return '#' + el.id;
                const tag = el.tagName.toLowerCase();
                const cls = el.className?.split?.(' ')?.filter(c => c && c.length < 30)?.slice(0, 2)?.join('.') || '';
                const base = cls ? tag + '.' + cls : tag;
                const text = (el.innerText || '').slice(0, 30).trim();
                if (text) return base + ':has-text("' + text.replace(/"/g, '\\\\"') + '")';
                return base;
            };
            walk(document.body);
            return results;
        }""")

    # --- navigation ---

    @_retry_on_disconnect
    async def go_back(self, *, wait_until="load"):
        page = self._require_page()
        resp = await page.go_back(wait_until=wait_until)
        return await self.page_state()

    @_retry_on_disconnect
    async def go_forward(self, *, wait_until="load"):
        page = self._require_page()
        resp = await page.go_forward(wait_until=wait_until)
        return await self.page_state()

    @_retry_on_disconnect
    async def reload(self, *, wait_until="load"):
        page = self._require_page()
        await page.reload(wait_until=wait_until)
        return await self.page_state()

    # --- interaction ---

    @_retry_on_disconnect
    async def hover(self, selector):
        page = self._require_page()
        await page.hover(selector)
        return await self.page_state()

    @_retry_on_disconnect
    async def dblclick(self, selector, *, wait_after="networkidle", timeout=5000):
        page = self._require_page()
        await page.dblclick(selector)
        if wait_after:
            try:
                await page.wait_for_load_state(wait_after, timeout=timeout)
            except Exception:
                pass
        return await self.page_state()

    @_retry_on_disconnect
    async def right_click(self, selector):
        page = self._require_page()
        await page.click(selector, button="right")
        return await self.page_state()

    @_retry_on_disconnect
    async def scroll(self, *, direction="down", amount=500, selector=None):
        page = self._require_page()
        if selector:
            loc = page.locator(selector)
            await loc.scroll_into_view_if_needed()
        else:
            delta_x, delta_y = 0, 0
            if direction == "down":
                delta_y = amount
            elif direction == "up":
                delta_y = -amount
            elif direction == "right":
                delta_x = amount
            elif direction == "left":
                delta_x = -amount
            await page.mouse.wheel(delta_x, delta_y)
        return await self.page_state()

    @_retry_on_disconnect
    async def focus(self, selector):
        page = self._require_page()
        await page.focus(selector)

    @_retry_on_disconnect
    async def check(self, selector):
        page = self._require_page()
        await page.check(selector)
        return await self.page_state()

    @_retry_on_disconnect
    async def uncheck(self, selector):
        page = self._require_page()
        await page.uncheck(selector)
        return await self.page_state()

    @_retry_on_disconnect
    async def set_checked(self, selector, checked):
        page = self._require_page()
        await page.set_checked(selector, checked)
        return await self.page_state()

    @_retry_on_disconnect
    async def set_input_files(self, selector, files):
        page = self._require_page()
        await page.set_input_files(selector, files)
        return await self.page_state()

    @_retry_on_disconnect
    async def drag_and_drop(self, source, target):
        page = self._require_page()
        await page.drag_and_drop(source, target)
        return await self.page_state()

    @_retry_on_disconnect
    async def tap(self, selector):
        page = self._require_page()
        await page.tap(selector)
        return await self.page_state()

    @_retry_on_disconnect
    async def dispatch_event(self, selector, event_type, event_init=None):
        page = self._require_page()
        await page.dispatch_event(selector, event_type, event_init)

    # --- content extraction ---

    @_retry_on_disconnect
    async def content(self):
        page = self._require_page()
        return await page.content()

    @_retry_on_disconnect
    async def inner_text(self, selector):
        page = self._require_page()
        return await page.inner_text(selector)

    @_retry_on_disconnect
    async def inner_html(self, selector):
        page = self._require_page()
        return await page.inner_html(selector)

    @_retry_on_disconnect
    async def text_content(self, selector):
        page = self._require_page()
        return await page.text_content(selector)

    @_retry_on_disconnect
    async def get_attribute(self, selector, name):
        page = self._require_page()
        return await page.get_attribute(selector, name)

    @_retry_on_disconnect
    async def input_value(self, selector):
        page = self._require_page()
        return await page.input_value(selector)

    # --- element state ---

    @_retry_on_disconnect
    async def is_visible(self, selector):
        page = self._require_page()
        return await page.is_visible(selector)

    @_retry_on_disconnect
    async def is_checked(self, selector):
        page = self._require_page()
        return await page.is_checked(selector)

    @_retry_on_disconnect
    async def is_enabled(self, selector):
        page = self._require_page()
        return await page.is_enabled(selector)

    @_retry_on_disconnect
    async def is_hidden(self, selector):
        page = self._require_page()
        return await page.is_hidden(selector)

    @_retry_on_disconnect
    async def is_editable(self, selector):
        page = self._require_page()
        return await page.is_editable(selector)

    # --- frame support ---

    async def frames(self):
        page = self._require_page()
        result = []
        for f in page.frames:
            result.append({
                "name": f.name,
                "url": f.url,
                "is_detached": f.is_detached(),
            })
        return result

    @_retry_on_disconnect
    async def frame_evaluate(self, expression, *, name=None, url=None):
        page = self._require_page()
        frame = None
        if name:
            frame = page.frame(name=name)
        elif url:
            frame = page.frame(url=url)
        if not frame:
            raise RuntimeError("frame not found")
        return await frame.evaluate(expression)

    @_retry_on_disconnect
    async def frame_click(self, selector, *, name=None, url=None):
        page = self._require_page()
        if name:
            frame = page.frame(name=name)
        elif url:
            frame = page.frame(url=url)
        else:
            raise RuntimeError("specify frame name or url")
        if not frame:
            raise RuntimeError("frame not found")
        await frame.click(selector)
        return await self.page_state()

    @_retry_on_disconnect
    async def frame_fill(self, selector, value, *, name=None, url=None):
        page = self._require_page()
        if name:
            frame = page.frame(name=name)
        elif url:
            frame = page.frame(url=url)
        else:
            raise RuntimeError("specify frame name or url")
        if not frame:
            raise RuntimeError("frame not found")
        await frame.fill(selector, value)
        return await self.page_state()

    @_retry_on_disconnect
    async def frame_content(self, *, name=None, url=None):
        page = self._require_page()
        if name:
            frame = page.frame(name=name)
        elif url:
            frame = page.frame(url=url)
        else:
            raise RuntimeError("specify frame name or url")
        if not frame:
            raise RuntimeError("frame not found")
        return await frame.content()

    # --- locator helpers ---

    @_retry_on_disconnect
    async def query(self, selector, *, text=None, role=None, label=None,
                    placeholder=None, alt_text=None, title=None, test_id=None):
        page = self._require_page()
        if role:
            loc = page.get_by_role(role, name=text)
        elif label:
            loc = page.get_by_label(label)
        elif placeholder:
            loc = page.get_by_placeholder(placeholder)
        elif alt_text:
            loc = page.get_by_alt_text(alt_text)
        elif title:
            loc = page.get_by_title(title)
        elif test_id:
            loc = page.get_by_test_id(test_id)
        elif text:
            loc = page.get_by_text(text)
        elif selector:
            loc = page.locator(selector)
        else:
            raise RuntimeError("specify at least one locator parameter")
        count = await loc.count()
        results = []
        for i in range(min(count, 50)):
            el = loc.nth(i)
            results.append({
                "index": i,
                "visible": await el.is_visible(),
                "text": (await el.text_content() or "")[:200],
                "tag": await el.evaluate("el => el.tagName.toLowerCase()"),
            })
        return results

    @_retry_on_disconnect
    async def locator_click(self, *, text=None, role=None, name=None,
                            label=None, nth=0):
        page = self._require_page()
        if role:
            loc = page.get_by_role(role, name=name)
        elif label:
            loc = page.get_by_label(label)
        elif text:
            loc = page.get_by_text(text)
        else:
            raise RuntimeError("specify text, role, or label")
        await loc.nth(nth).click()
        return await self.page_state()

    @_retry_on_disconnect
    async def locator_fill(self, value, *, label=None, placeholder=None,
                           role=None, name=None, nth=0):
        page = self._require_page()
        if label:
            loc = page.get_by_label(label)
        elif placeholder:
            loc = page.get_by_placeholder(placeholder)
        elif role:
            loc = page.get_by_role(role, name=name)
        else:
            raise RuntimeError("specify label, placeholder, or role")
        await loc.nth(nth).fill(value)
        return await self.page_state()

    # --- advanced waits ---

    @_retry_on_disconnect
    async def wait_for_function(self, expression, *, timeout=30000):
        page = self._require_page()
        await page.wait_for_function(expression, timeout=timeout)
        return await self.page_state()

    @_retry_on_disconnect
    async def wait_for_load_state(self, state="load", *, timeout=30000):
        page = self._require_page()
        await page.wait_for_load_state(state, timeout=timeout)
        return await self.page_state()

    @_retry_on_disconnect
    async def wait_for_response(self, url_glob, *, timeout=30000):
        page = self._require_page()
        resp = await page.wait_for_response(url_glob, timeout=timeout)
        return {
            "url": resp.url,
            "status": resp.status,
            "headers": dict(resp.headers),
        }

    @_retry_on_disconnect
    async def wait_for_request(self, url_glob, *, timeout=30000):
        page = self._require_page()
        req = await page.wait_for_request(url_glob, timeout=timeout)
        return {
            "url": req.url,
            "method": req.method,
            "headers": dict(req.headers),
            "post_data": req.post_data,
        }

    # --- popup / new tab handling ---

    async def expect_popup(self, action_selector, *, click_action=True):
        page = self._require_page()
        async with page.expect_popup() as popup_info:
            if click_action:
                await page.click(action_selector)
        popup = popup_info.value
        # _on_new_page handler already appended popup to self._pages
        # and called _setup_page (which hooks context + intercepts + ws)
        self._popup_pages.append(popup)
        pages = [p for p in self._pages if not p.is_closed()]
        return {
            "url": popup.url,
            "title": await popup.title(),
            "page_index": len(pages) - 1,
        }

    async def switch_page(self, index):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        pages = [p for p in self._pages if not p.is_closed()]
        if index < 0 or index >= len(pages):
            raise RuntimeError(f"page index {index} out of range (0-{len(pages)-1})")
        target = pages[index]
        self._pages.remove(target)
        self._pages.append(target)
        await target.bring_to_front()
        return await self.page_state()

    async def pages(self):
        if not self._context:
            return []
        result = []
        filtered_idx = 0
        for p in self._pages:
            if p.is_closed():
                continue
            result.append({
                "index": filtered_idx,
                "url": p.url,
                "title": await p.title(),
            })
            filtered_idx += 1
        return result

    async def close_page(self, index=None):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        pages = [p for p in self._pages if not p.is_closed()]
        if not pages:
            return
        if index is None:
            index = len(pages) - 1
        if index < 0 or index >= len(pages):
            raise RuntimeError(f"page index {index} out of range")
        target = pages[index]
        await target.close()
        try:
            self._pages.remove(target)
        except ValueError:
            pass
        return await self.page_state()

    # --- download handling ---

    async def expect_download(self, action_selector, *, save_path=None):
        page = self._require_page()
        async with page.expect_download() as dl_info:
            await page.click(action_selector)
        download = dl_info.value
        result = {
            "suggested_filename": download.suggested_filename,
            "url": download.url,
        }
        if save_path:
            await download.save_as(save_path)
            result["saved_to"] = save_path
        else:
            path = await download.path()
            result["temp_path"] = str(path) if path else None
        return result

    # --- dialog handling ---

    def handle_dialog(self, action="dismiss", prompt_text=None):
        async def handler(dialog):
            if action == "accept":
                if prompt_text is not None:
                    await dialog.accept(prompt_text)
                else:
                    await dialog.accept()
            else:
                await dialog.dismiss()

        if self._dialog_handler:
            for page in self._pages:
                if not page.is_closed():
                    page.remove_listener("dialog", self._dialog_handler)

        self._dialog_handler = handler
        for page in self._pages:
            if not page.is_closed():
                page.on("dialog", handler)

    # --- page manipulation ---

    async def set_content(self, html, *, wait_until="load"):
        page = self._require_page()
        await page.set_content(html, wait_until=wait_until)
        return await self.page_state()

    async def set_viewport_size(self, width, height):
        page = self._require_page()
        await page.set_viewport_size({"width": width, "height": height})
        return {"width": width, "height": height}

    async def emulate_media(self, *, media=None, color_scheme=None,
                            reduced_motion=None):
        page = self._require_page()
        kwargs = {}
        if media is not None:
            kwargs["media"] = media
        if color_scheme is not None:
            kwargs["color_scheme"] = color_scheme
        if reduced_motion is not None:
            kwargs["reduced_motion"] = reduced_motion
        await page.emulate_media(**kwargs)

    async def bring_to_front(self):
        page = self._require_page()
        await page.bring_to_front()

    async def add_script(self, *, url=None, content=None):
        page = self._require_page()
        kwargs = {}
        if url:
            kwargs["url"] = url
        if content:
            kwargs["content"] = content
        await page.add_script_tag(**kwargs)

    async def add_style(self, *, url=None, content=None):
        page = self._require_page()
        kwargs = {}
        if url:
            kwargs["url"] = url
        if content:
            kwargs["content"] = content
        await page.add_style_tag(**kwargs)

    async def add_init_script(self, script):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.add_init_script(script)

    async def expose_function(self, name, callback):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.expose_function(name, callback)

    # --- context-level ---

    async def set_offline(self, offline):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.set_offline(offline)

    async def set_geolocation(self, latitude, longitude, *, accuracy=None):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        geo = {"latitude": latitude, "longitude": longitude}
        if accuracy is not None:
            geo["accuracy"] = accuracy
        await self._context.set_geolocation(geo)

    async def grant_permissions(self, permissions):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.grant_permissions(permissions)

    async def clear_permissions(self):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.clear_permissions()

    # --- tracing ---

    async def start_tracing(self, *, screenshots=True, snapshots=True,
                            sources=False):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.tracing.start(
            screenshots=screenshots, snapshots=snapshots, sources=sources)

    async def stop_tracing(self, *, path=None):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        trace_path = path or os.path.join(self.profile_dir, "trace.zip")
        await self._context.tracing.stop(path=trace_path)
        return trace_path

    # --- pdf ---

    async def pdf(self, *, path=None, format=None, landscape=False,
                  print_background=True):
        page = self._require_page()
        kwargs = {"print_background": print_background}
        if path:
            kwargs["path"] = path
        if format:
            kwargs["format"] = format
        if landscape:
            kwargs["landscape"] = landscape
        return await page.pdf(**kwargs)

    # --- response mocking ---

    async def mock_response(self, pattern, *, status=200, headers=None,
                            body="", content_type="text/plain"):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        mock_headers = {"content-type": content_type}
        if headers:
            mock_headers.update(headers)

        async def handler(route):
            await route.fulfill(
                status=status,
                headers=mock_headers,
                body=body,
            )

        await self.intercept(pattern, handler)

    async def route_from_har(self, har_path, *, url=None, update=False,
                             not_found="abort"):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        kwargs = {"not_found": not_found}
        if url:
            kwargs["url"] = url
        if update:
            kwargs["update"] = update
        await self._context.route_from_har(har_path, **kwargs)

    # --- request body modification ---

    async def modify_request(self, pattern, *, headers=None, post_data=None,
                             method=None):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")

        async def handler(route):
            kwargs = {}
            if headers:
                kwargs["headers"] = {**route.request.headers, **headers}
            if post_data is not None:
                kwargs["post_data"] = post_data
            if method:
                kwargs["method"] = method
            await route.continue_(**kwargs)

        await self.intercept(pattern, handler)

    # --- mouse / keyboard ---

    @_retry_on_disconnect
    async def mouse_click(self, x, y, *, button="left", click_count=1, delay=0):
        page = self._require_page()
        await page.mouse.click(x, y, button=button, click_count=click_count,
                               delay=delay)
        return await self.page_state()

    @_retry_on_disconnect
    async def mouse_dblclick(self, x, y, *, button="left", delay=0):
        page = self._require_page()
        await page.mouse.dblclick(x, y, button=button, delay=delay)
        return await self.page_state()

    @_retry_on_disconnect
    async def mouse_move(self, x, y, *, steps=1):
        page = self._require_page()
        await page.mouse.move(x, y, steps=steps)

    @_retry_on_disconnect
    async def mouse_down(self, *, button="left"):
        page = self._require_page()
        await page.mouse.down(button=button)

    @_retry_on_disconnect
    async def mouse_up(self, *, button="left"):
        page = self._require_page()
        await page.mouse.up(button=button)

    @_retry_on_disconnect
    async def mouse_wheel(self, delta_x, delta_y):
        page = self._require_page()
        await page.mouse.wheel(delta_x, delta_y)

    @_retry_on_disconnect
    async def keyboard_press(self, key):
        page = self._require_page()
        await page.keyboard.press(key)
        return await self.page_state()

    @_retry_on_disconnect
    async def keyboard_type(self, text, *, delay=0):
        page = self._require_page()
        await page.keyboard.type(text, delay=delay)
        return await self.page_state()

    @_retry_on_disconnect
    async def keyboard_down(self, key):
        page = self._require_page()
        await page.keyboard.down(key)

    @_retry_on_disconnect
    async def keyboard_up(self, key):
        page = self._require_page()
        await page.keyboard.up(key)

    @_retry_on_disconnect
    async def keyboard_insert_text(self, text):
        page = self._require_page()
        await page.keyboard.insert_text(text)

    # --- cdp ---

    async def cdp_send(self, method, params=None):
        if not self._cdp_session:
            page = self._require_page()
            self._cdp_session = await self._context.new_cdp_session(page)
        return await self._cdp_session.send(method, params or {})

    async def cdp_close(self):
        if self._cdp_session:
            await self._cdp_session.detach()
            self._cdp_session = None

    def pause(self, reason="manual"):
        if self.state == SessionState.PAUSED:
            return
        self.state = SessionState.PAUSED
        self.pause_reason = reason
        self._resume_event.clear()
        for cb in self._pause_callbacks:
            try:
                cb(self.name, reason)
            except Exception:
                pass

    def resume(self):
        if self.state != SessionState.PAUSED:
            return
        self.state = SessionState.ACTIVE
        self.pause_reason = None
        self._resume_event.set()
        self._last_activity = time.time()

    def on_pause(self, callback):
        self._pause_callbacks.append(callback)
        return lambda: self._pause_callbacks.remove(callback)

    def _require_active(self):
        if self.state == SessionState.PAUSED:
            raise RuntimeError(
                f"session '{self.name}' is paused ({self.pause_reason}) "
                "— resolve the challenge and call resume()")
        if self.state == SessionState.HIBERNATED:
            raise RuntimeError(f"session '{self.name}' is hibernated — relaunch first")

    async def intercept(self, pattern, handler):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        for page in self._pages:
            if not page.is_closed():
                await page.route(pattern, handler)
        self._intercept_rules.append((pattern, handler))

    async def set_extra_headers(self, headers):
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        await self._context.set_extra_http_headers(headers)

    async def clear_intercepts(self):
        if not self._context:
            return
        for pattern, handler in self._intercept_rules:
            for page in self._pages:
                if not page.is_closed():
                    try:
                        await page.unroute(pattern, handler)
                    except Exception:
                        pass
        self._intercept_rules.clear()

    async def save_state(self):
        if not self._context:
            return
        path = os.path.join(self.profile_dir, "storage_state.json")
        await self._context.storage_state(path=path)

    async def close(self):
        self._stop_watchdog()
        if self._cdp_session:
            try:
                await self._cdp_session.detach()
            except Exception:
                pass
            self._cdp_session = None
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._pages = []
            self._popup_pages = []
            self._downloads = []
            self._websockets = []
            self._dialog_handler = None
            self._pending_dialog = None
            self._launched_at = None
            if self.context:
                self.context.clear()
            if self.state != SessionState.PAUSED:
                self.state = SessionState.HIBERNATED

    @property
    def is_alive(self):
        return self._context is not None

    @property
    def page_count(self):
        return len([p for p in self._pages if not p.is_closed()]) if self._context else 0

    @property
    def uptime(self):
        if self._launched_at:
            return time.time() - self._launched_at
        return 0

    def status(self):
        return {
            "name": self.name,
            "alive": self.is_alive,
            "pages": self.page_count,
            "uptime_s": round(self.uptime),
            "proxy": self.proxy.server if self.proxy else "direct",
            "headless": self.headless,
            "fingerprint": f"{self.fingerprint.viewport_width}x{self.fingerprint.viewport_height}",
            "profile_dir": self.profile_dir,
            "tags": self.tags,
            "state": self.state.value,
            "pause_reason": self.pause_reason,
        }

    def __repr__(self):
        state = "alive" if self.is_alive else "closed"
        proxy = self.proxy.server if self.proxy else "direct"
        return f"<Session '{self.name}' {state} proxy={proxy}>"
