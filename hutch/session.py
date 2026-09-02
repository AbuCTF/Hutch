"""hutch/session.py — a single isolated browser session.

A Session wraps a Playwright persistent browser context with:
- Its own profile directory (cookies, storage, cache — survives restarts)
- Optional proxy routing (e.g., Caido at localhost:8081)
- Fingerprint configuration (viewport, UA, timezone, locale)
- Auth state persistence (login once, reuse forever)

The key concept: Playwright's `launch_persistent_context(user_data_dir)` creates
a browser that saves ALL state to that directory. When you close and reopen with
the same dir, cookies/localStorage/everything is restored. Each Session gets its
own directory, so sessions are fully isolated from each other.

This is how antidetect browsers work internally — separate profile directories
per "browser profile." We're doing the same thing but open-source and designed
for security testing.
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Fingerprint:
    """A browser fingerprint profile.

    For security testing, the main goal isn't stealth — it's ISOLATION.
    Each session should have a consistent fingerprint that doesn't leak
    into other sessions. Optional randomization helps when you need the
    target to not correlate two sessions (e.g., cross-account IDOR testing).

    Consistency matters: viewport must match screen, timezone must match
    locale and proxy geography, UA must match platform. Mismatches are
    the #1 detection signal. Use Fingerprint.generate() to get a coherent
    profile, or set fields manually if you know what you're doing.
    """
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
    """Per-session proxy configuration.

    For Caido integration: set server="http://127.0.0.1:8081"
    For SOCKS proxy: server="socks5://host:port"
    For direct (no proxy): leave as None in Session
    """
    server: str = ""
    username: Optional[str] = None
    password: Optional[str] = None
    bypass: Optional[str] = None


_META_FILE = "hutch_meta.json"


class Session:
    """One isolated browser session.

    Lifecycle:
        session = Session(name="target-a", profile_dir="/path/to/profile")
        await session.launch(playwright)    # opens browser
        page = await session.new_page()     # creates a tab
        await page.goto("https://target.com")
        await session.save_state()          # persist auth state
        await session.close()               # close browser, profile survives
        # later:
        await session.launch(playwright)    # everything restored
    """

    def __init__(self, name, profile_dir, *,
                 proxy=None, fingerprint=None, headless=True,
                 ignore_https_errors=False, stealth=True, tags=None):
        self.name = name
        self.profile_dir = profile_dir
        self.proxy = proxy
        self.fingerprint = fingerprint or Fingerprint()
        self.headless = headless
        self.ignore_https_errors = ignore_https_errors
        self.stealth = stealth
        self.tags = tags or {}

        self._context = None
        self._browser = None
        self._pages = []
        self._launched_at = None
        self._created_at = time.time()

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
        """Reconstruct a Session from its saved metadata on disk."""
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
        """Build the kwargs dict for playwright.chromium.launch_persistent_context()."""
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
        """Open the browser with this session's profile.

        Uses launch_persistent_context — this is THE key Playwright feature
        that makes isolated sessions work. It creates a Chromium instance
        with ALL state stored in the profile directory. When you close and
        reopen with the same dir, cookies, localStorage, IndexedDB, cache,
        and service workers are all restored.

        If stealth=True (default), applies playwright-stealth patches to
        defeat navigator.webdriver, WebGL/canvas fingerprinting, and other
        common bot detection. Requires `pip install playwright-stealth`.
        """
        if self._context:
            return self._context
        kwargs = self._launch_args()
        self._context = await playwright.chromium.launch_persistent_context(**kwargs)
        self._launched_at = time.time()
        if self.stealth:
            from .stealth import apply_stealth
            await apply_stealth(self._context)
        self._pages = self._context.pages[:]
        return self._context

    async def new_page(self):
        """Create a new tab in this session's browser."""
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched — call launch() first")
        page = await self._context.new_page()
        self._pages.append(page)
        return page

    async def save_state(self):
        """Explicitly persist the current storage state (cookies + localStorage).

        Playwright persistent contexts auto-save on close, but this lets you
        checkpoint mid-session — useful if the browser might crash or if you
        want to snapshot auth state immediately after login.
        """
        if not self._context:
            return
        path = os.path.join(self.profile_dir, "storage_state.json")
        await self._context.storage_state(path=path)

    async def close(self):
        """Close the browser. Profile directory survives — reopen with launch()."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._pages = []
            self._launched_at = None

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
        }

    def __repr__(self):
        state = "alive" if self.is_alive else "closed"
        proxy = self.proxy.server if self.proxy else "direct"
        return f"<Session '{self.name}' {state} proxy={proxy}>"
