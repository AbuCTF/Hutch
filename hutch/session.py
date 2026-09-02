import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .context import Context, Snapshot


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


class Session:

    def __init__(self, name, profile_dir, *,
                 proxy=None, fingerprint=None, headless=True,
                 ignore_https_errors=False, stealth=True, tags=None,
                 capture=True):
        self.name = name
        self.profile_dir = profile_dir
        self.proxy = proxy
        self.fingerprint = fingerprint or Fingerprint()
        self.headless = headless
        self.ignore_https_errors = ignore_https_errors
        self.stealth = stealth
        self.tags = tags or {}
        self.capture = capture
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
        vp = meta.get("fingerprint", {}).get("viewport", "1920x1080").split("x")
        fp = Fingerprint(
            viewport_width=int(vp[0]),
            viewport_height=int(vp[1]),
            user_agent=meta.get("fingerprint", {}).get("user_agent"),
            locale=meta.get("fingerprint", {}).get("locale", "en-US"),
            timezone=meta.get("fingerprint", {}).get("timezone", "America/New_York"),
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
        return s

    def _launch_args(self):
        fp = self.fingerprint
        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if fp.disable_webrtc:
            chrome_args.extend([
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-webrtc-hw-encoding",
            ])
        args = {
            "user_data_dir": self.profile_dir,
            "headless": self.headless,
            "viewport": {"width": fp.viewport_width, "height": fp.viewport_height},
            "screen": {"width": fp.screen_width, "height": fp.screen_height},
            "locale": fp.locale,
            "timezone_id": fp.timezone,
            "color_scheme": fp.color_scheme,
            "device_scale_factor": fp.device_scale_factor,
            "has_touch": fp.has_touch,
            "is_mobile": fp.is_mobile,
            "ignore_https_errors": self.ignore_https_errors,
            "permissions": [],
            "args": chrome_args,
        }
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
        if self.fingerprint.platform:
            await self._context.add_init_script(
                f"Object.defineProperty(navigator, 'platform', {{get: () => '{self.fingerprint.platform}'}})"
            )
        if self.stealth:
            from .stealth import apply_stealth
            await apply_stealth(self._context)
        self._pages = self._context.pages[:]
        if self.context:
            for page in self._pages:
                self.context.hook_page(page)
        return self._context

    async def new_page(self):
        self._require_active()
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        page = await self._context.new_page()
        self._pages.append(page)
        self._last_activity = time.time()
        if self.context:
            self.context.hook_page(page)
        return page

    def _active_page(self):
        if not self._context:
            return None
        pages = [p for p in self._pages if not p.is_closed()]
        return pages[-1] if pages else None

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
        return {
            "url": page.url,
            "title": await page.title(),
            "dom": dom,
        }

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

    async def save_state(self):
        if not self._context:
            return
        path = os.path.join(self.profile_dir, "storage_state.json")
        await self._context.storage_state(path=path)

    async def close(self):
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._pages = []
            self._launched_at = None
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
