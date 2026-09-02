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
            await apply_stealth(self._context)
        self._pages = self._context.pages[:]
        for page in self._pages:
            self._setup_page(page)
        self._context.on("page", lambda page: self._setup_page(page))
        return self._context

    def _setup_page(self, page):
        if self.context:
            self.context.hook_page(page)
        if self._dialog_handler:
            page.on("dialog", self._dialog_handler)
        page.on("download", lambda dl: self._downloads.append(dl))

    async def new_page(self):
        self._require_active()
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        page = await self._context.new_page()
        self._pages.append(page)
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

    async def goto(self, url, *, wait_until="load"):
        self._require_active()
        page = self._active_page()
        if not page:
            page = await self.new_page()
        self._last_activity = time.time()
        await page.goto(url, wait_until=wait_until)
        return await self.page_state()

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

    async def type_text(self, selector, text, *, delay=50):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.type(selector, text, delay=delay)
        return await self.page_state()

    async def press(self, selector, key):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.press(selector, key)
        return await self.page_state()

    async def select_option(self, selector, value):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.select_option(selector, value)
        return await self.page_state()

    async def evaluate(self, expression):
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        return await page.evaluate(expression)

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
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        kwargs = {"full_page": full_page}
        if path:
            kwargs["path"] = path
        return await page.screenshot(**kwargs)

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

    # --- navigation ---

    async def go_back(self, *, wait_until="load"):
        page = self._require_page()
        resp = await page.go_back(wait_until=wait_until)
        return await self.page_state()

    async def go_forward(self, *, wait_until="load"):
        page = self._require_page()
        resp = await page.go_forward(wait_until=wait_until)
        return await self.page_state()

    async def reload(self, *, wait_until="load"):
        page = self._require_page()
        await page.reload(wait_until=wait_until)
        return await self.page_state()

    # --- interaction ---

    async def hover(self, selector):
        page = self._require_page()
        await page.hover(selector)
        return await self.page_state()

    async def dblclick(self, selector, *, wait_after="networkidle", timeout=5000):
        page = self._require_page()
        await page.dblclick(selector)
        if wait_after:
            try:
                await page.wait_for_load_state(wait_after, timeout=timeout)
            except Exception:
                pass
        return await self.page_state()

    async def right_click(self, selector):
        page = self._require_page()
        await page.click(selector, button="right")
        return await self.page_state()

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

    async def focus(self, selector):
        page = self._require_page()
        await page.focus(selector)

    async def check(self, selector):
        page = self._require_page()
        await page.check(selector)
        return await self.page_state()

    async def uncheck(self, selector):
        page = self._require_page()
        await page.uncheck(selector)
        return await self.page_state()

    async def set_checked(self, selector, checked):
        page = self._require_page()
        await page.set_checked(selector, checked)
        return await self.page_state()

    async def set_input_files(self, selector, files):
        page = self._require_page()
        await page.set_input_files(selector, files)
        return await self.page_state()

    async def drag_and_drop(self, source, target):
        page = self._require_page()
        await page.drag_and_drop(source, target)
        return await self.page_state()

    async def tap(self, selector):
        page = self._require_page()
        await page.tap(selector)
        return await self.page_state()

    async def dispatch_event(self, selector, event_type, event_init=None):
        page = self._require_page()
        await page.dispatch_event(selector, event_type, event_init)

    # --- content extraction ---

    async def content(self):
        page = self._require_page()
        return await page.content()

    async def inner_text(self, selector):
        page = self._require_page()
        return await page.inner_text(selector)

    async def inner_html(self, selector):
        page = self._require_page()
        return await page.inner_html(selector)

    async def text_content(self, selector):
        page = self._require_page()
        return await page.text_content(selector)

    async def get_attribute(self, selector, name):
        page = self._require_page()
        return await page.get_attribute(selector, name)

    async def input_value(self, selector):
        page = self._require_page()
        return await page.input_value(selector)

    # --- element state ---

    async def is_visible(self, selector):
        page = self._require_page()
        return await page.is_visible(selector)

    async def is_checked(self, selector):
        page = self._require_page()
        return await page.is_checked(selector)

    async def is_enabled(self, selector):
        page = self._require_page()
        return await page.is_enabled(selector)

    async def is_hidden(self, selector):
        page = self._require_page()
        return await page.is_hidden(selector)

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

    async def wait_for_function(self, expression, *, timeout=30000):
        page = self._require_page()
        await page.wait_for_function(expression, timeout=timeout)
        return await self.page_state()

    async def wait_for_load_state(self, state="load", *, timeout=30000):
        page = self._require_page()
        await page.wait_for_load_state(state, timeout=timeout)
        return await self.page_state()

    async def wait_for_response(self, url_glob, *, timeout=30000):
        page = self._require_page()
        resp = await page.wait_for_response(url_glob, timeout=timeout)
        return {
            "url": resp.url,
            "status": resp.status,
            "headers": dict(resp.headers),
        }

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
        self._pages.append(popup)
        self._popup_pages.append(popup)
        if self.context:
            self.context.hook_page(popup)
        return {
            "url": popup.url,
            "title": await popup.title(),
            "page_index": len(self._pages) - 1,
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
        for i, p in enumerate(self._pages):
            if p.is_closed():
                continue
            result.append({
                "index": i,
                "url": p.url,
                "title": await p.title(),
            })
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

    async def mouse_click(self, x, y, *, button="left", click_count=1, delay=0):
        page = self._require_page()
        await page.mouse.click(x, y, button=button, click_count=click_count,
                               delay=delay)
        return await self.page_state()

    async def mouse_dblclick(self, x, y, *, button="left", delay=0):
        page = self._require_page()
        await page.mouse.dblclick(x, y, button=button, delay=delay)
        return await self.page_state()

    async def mouse_move(self, x, y, *, steps=1):
        page = self._require_page()
        await page.mouse.move(x, y, steps=steps)

    async def mouse_down(self, *, button="left"):
        page = self._require_page()
        await page.mouse.down(button=button)

    async def mouse_up(self, *, button="left"):
        page = self._require_page()
        await page.mouse.up(button=button)

    async def mouse_wheel(self, delta_x, delta_y):
        page = self._require_page()
        await page.mouse.wheel(delta_x, delta_y)

    async def keyboard_press(self, key):
        page = self._require_page()
        await page.keyboard.press(key)
        return await self.page_state()

    async def keyboard_type(self, text, *, delay=0):
        page = self._require_page()
        await page.keyboard.type(text, delay=delay)
        return await self.page_state()

    async def keyboard_down(self, key):
        page = self._require_page()
        await page.keyboard.down(key)

    async def keyboard_up(self, key):
        page = self._require_page()
        await page.keyboard.up(key)

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
            self._dialog_handler = None
            self._pending_dialog = None
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
