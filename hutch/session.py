import asyncio
import functools
import json
import logging
import os
import re
import sys
import shutil
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TargetClosedError

from . import browser as _browser
from .context import Context, Snapshot
from .events import EventLog
from . import recorder as _recorder
from . import secrets as _secrets
from . import snapshot as _snapshot

_log = logging.getLogger(__name__)


_ANNOTATE_JS = """items => {
  const root = document.createElement("div");
  root.id = "__hutch_annotations";
  root.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
  const colors = ["#e5484d", "#3e63dd", "#30a46c", "#f76b15", "#8e4ec6", "#0090ff", "#d6409f"];
  items.forEach((b, i) => {
    const c = colors[i % colors.length];
    const box = document.createElement("div");
    box.style.cssText = `position:fixed;left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px;` +
      `outline:2px solid ${c};outline-offset:-1px`;
    const tag = document.createElement("div");
    tag.textContent = b.ref;
    tag.style.cssText = `position:fixed;left:${b.x}px;top:${Math.max(0, b.y - 15)}px;background:${c};` +
      "color:#fff;font:bold 11px/14px monospace;padding:0 3px;border-radius:2px";
    root.append(box, tag);
  });
  document.documentElement.append(root);
}"""


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
    sec_ch_ua: Optional[str] = None     # client-hint brands, MUST match user_agent (else HeadlessChrome leaks)


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
_WS_CONN_CAP = 100    # connections kept; reconnecting SPAs open many


_REF_RE = re.compile(r"^(?:f\d+)?e\d+$")


def _ref(selector):
    """Accept aria snapshot refs (e12, f1e3) anywhere a selector is expected."""
    if isinstance(selector, str) and _REF_RE.match(selector):
        return f"aria-ref={selector}"
    return selector


def _match_engine(fp, version):
    """Pin a persona's UA / client-hint major to the real engine so features match the claim."""
    if not fp or not fp.user_agent or not version:
        return
    major = version.split(".")[0]
    fp.user_agent = re.sub(r"Chrome/\d+[\d.]*", f"Chrome/{major}.0.0.0", fp.user_agent)
    if fp.sec_ch_ua:
        fp.sec_ch_ua = re.sub(r'(Chromium|Google Chrome|Microsoft Edge|Brave)";v="\d+"',
                              rf'\1";v="{major}"', fp.sec_ch_ua)


def _is_disconnect(exc):
    """Return True if exc signals a dead page/context/browser."""
    if isinstance(exc, TargetClosedError):
        return True
    if isinstance(exc, PlaywrightError):
        msg = str(exc).lower()
        return any(p in msg for p in _DISCONNECT_PATTERNS)
    return False


def _ua_override_params(fp):
    """Build CDP Network.setUserAgentOverride params so the UA header, the sec-ch-ua
    client-hint HEADERS, navigator.userAgentData and Accept-Language ALL agree with the
    fingerprint. Headless Chrome otherwise emits sec-ch-ua: "HeadlessChrome" and its real
    engine version (a version/UA mismatch) — a dead giveaway that a JS-only override can't
    fix (the headers are set by the network stack, not JS)."""
    import re
    ua = fp.user_agent or ""
    m = re.search(r"Chrome/(\d+)(?:\.[\d.]+)?", ua)
    major = m.group(1) if m else "156"
    full = m.group(0).split("/", 1)[1] if m else "156.0.0.0"
    brands = [{"brand": b, "version": v} for b, v in re.findall(r'"([^"]+)";v="([^"]+)"', fp.sec_ch_ua or "")]
    if not brands:
        brands = [{"brand": "Chromium", "version": major},
                  {"brand": "Google Chrome", "version": major},
                  {"brand": "Not=A?Brand", "version": "24"}]
    full_list = [{"brand": b["brand"],
                  "version": full if b["version"] == major else b["version"] + ".0.0.0"} for b in brands]
    plat = fp.platform or "Win32"
    if "Mac" in plat:
        ua_plat, plat_ver, arch = "macOS", "10.15.7", "x86"
    elif "Linux" in plat:
        ua_plat, plat_ver, arch = "Linux", "6.5.0", "x86"
    else:
        ua_plat, plat_ver, arch = "Windows", "10.0.0", "x86"
    return {
        "userAgent": ua,
        "acceptLanguage": fp.locale or "en-US",
        "platform": plat,
        "userAgentMetadata": {
            "brands": brands,
            "fullVersionList": full_list,
            "fullVersion": full,
            "platform": ua_plat,
            "platformVersion": plat_ver,
            "architecture": arch,
            "model": "",
            "mobile": bool(getattr(fp, "is_mobile", False)),
            "bitness": "64",
            "wow64": False,
        },
    }


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
                 capture=True, record_video=None, launcher=None,
                 browser="chromium", endpoint=None):
        self.name = name
        self.profile_dir = profile_dir
        self.proxy = proxy
        # persona = explicit fingerprint; otherwise the real browser identity is used untouched
        self.persona = fingerprint is not None
        self.fingerprint = fingerprint or Fingerprint()
        self.headless = headless
        self.browser = browser or "chromium"
        self.endpoint = endpoint
        if endpoint:
            launcher = "external"
        if launcher is None:
            needs_pw = bool(record_video or (proxy and proxy.username)
                            or self.fingerprint.is_mobile or self.fingerprint.has_touch)
            launcher = "playwright" if needs_pw else "native"
        self.launcher = launcher
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
        self.events = EventLog()
        self._dialog_policy = None
        self._dialog_plans = deque()
        self._agent_pages = weakref.WeakSet()
        self._target_ids = weakref.WeakKeyDictionary()
        self._seen = weakref.WeakKeyDictionary()
        self._recording = None
        self._recorder_installed = False
        self._front = None
        self._file_chooser = None
        self._downloads = {}
        self._browser_cdp = None
        self._popup_pages = []
        self._cdp_session = None
        self._watchdog_task = None
        self._last_url = None
        self._websockets = deque(maxlen=_WS_CONN_CAP)
        self._owned = False
        self._closing = False
        self._pid = None
        self._playwright = None
        self._focus = None
        self._close_callbacks = []

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
                "sec_ch_ua": self.fingerprint.sec_ch_ua,
            },
            "persona": self.persona,
            "launcher": self.launcher,
            "browser": self.browser,
            "endpoint": self.endpoint,
            "stealth": self.stealth,
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
            sec_ch_ua=fp_meta.get("sec_ch_ua"),
        )
        # profiles from before 0.12 were always launched by playwright with a fingerprint
        legacy = "launcher" not in meta
        s = cls(
            name=meta["name"],
            profile_dir=profile_dir,
            proxy=proxy,
            fingerprint=fp if meta.get("persona", legacy) else None,
            headless=meta.get("headless", True),
            ignore_https_errors=meta.get("ignore_https_errors", False),
            stealth=meta.get("stealth", True),
            tags=meta.get("tags", {}),
            launcher=meta.get("launcher", "playwright" if legacy else None),
            browser=meta.get("browser", "chromium"),
            endpoint=meta.get("endpoint"),
        )
        s._created_at = meta.get("created_at", time.time())
        s.state = SessionState.HIBERNATED
        return s

    def _window_class(self):
        suffix = re.sub(r"[^a-z0-9-]+", "-", self.name.lower()).strip("-")
        return f"hutch-browser-{suffix[:48]}" if suffix else "hutch-browser"

    def _launch_args(self):
        fp = self.fingerprint
        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-port=0",
        ]
        if not self.headless:
            chrome_args.append(f"--class={self._window_class()}")
            # Hyprland owns tiled window geometry. Asking Chromium to maximize
            # is commonly suppressed by compositor policy and can leave its
            # render surface at the default size until DevTools forces a reset.
            if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
                chrome_args.append("--start-maximized")
        if fp.disable_webrtc:
            chrome_args.extend([
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-webrtc-hw-encoding",
            ])
        args = {
            "user_data_dir": self.profile_dir,
            "headless": self.headless,
            # full binary in both modes: same profile lock, codecs and version as headed
            "channel": "chromium",
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

    async def _hyprctl_json(self, resource):
        process = await asyncio.create_subprocess_exec(
            "hyprctl", "-j", resource,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2)
        if process.returncode:
            raise RuntimeError(f"hyprctl {resource} failed")
        return json.loads(stdout)

    async def _sync_hyprland_viewport(self, page):
        """Match a headed page viewport to its fractionally-scaled tile."""
        # native windows are never viewport-emulated, so there is nothing to resync
        if self.headless or self.launcher == "native" or not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return

        try:
            # Hyprland can expose a newly mapped client before its tiling
            # animation reaches the final workspace geometry.
            await asyncio.sleep(0.35)
            client = None
            window_class = self._window_class()
            last_geometry = None
            stable_samples = 0
            for _ in range(30):
                clients = await self._hyprctl_json("clients")
                current = next(
                    (c for c in clients if c.get("class") == window_class),
                    None,
                )
                if current:
                    geometry = (
                        tuple(current.get("at", ())),
                        tuple(current.get("size", ())),
                        current.get("monitor"),
                    )
                    if geometry == last_geometry and current.get("mapped", True):
                        stable_samples += 1
                    else:
                        stable_samples = 0
                    client = current
                    last_geometry = geometry
                if client and stable_samples >= 2:
                    break
                await asyncio.sleep(0.05)
            if not client or stable_samples < 2:
                return

            center_x = client["at"][0] + client["size"][0] / 2
            center_y = client["at"][1] + client["size"][1] / 2
            monitor = None
            for candidate in await self._hyprctl_json("monitors"):
                scale = float(candidate.get("scale", 1.0)) or 1.0
                width = candidate["width"] / scale
                height = candidate["height"] / scale
                if candidate.get("transform", 0) % 2:
                    width, height = height, width
                if (candidate["x"] <= center_x < candidate["x"] + width and
                        candidate["y"] <= center_y < candidate["y"] + height):
                    monitor = candidate
                    break
            if not monitor:
                return

            metrics = await page.evaluate("""({
                dpr: window.devicePixelRatio || 1,
                chromeHeight: Math.max(0, window.outerHeight - window.innerHeight)
            })""")
            dpr = max(float(metrics.get("dpr", 1.0)), 0.1)
            scale = float(monitor.get("scale", 1.0)) or 1.0
            margin = 24
            width = max(320, int(
                client["size"][0] * scale / dpr - margin / dpr
            ))
            height = max(320, int(
                client["size"][1] * scale / dpr - margin / dpr -
                float(metrics.get("chromeHeight", 0))
            ))
            await page.set_viewport_size({"width": width, "height": height})
            _log.debug(
                "session '%s': synced Hyprland viewport to %dx%d",
                self.name, width, height,
            )
        except Exception as exc:
            _log.debug(
                "session '%s': Hyprland viewport sync skipped: %s",
                self.name, exc,
            )

    @property
    def _spoof(self):
        return self.persona and self.stealth

    async def launch(self, playwright):
        if self._context:
            return self._context
        self._playwright = playwright
        running = None if self.endpoint else _browser.status(self.profile_dir)
        if self.endpoint:
            ws = await asyncio.to_thread(_browser.resolve_endpoint, self.endpoint)
            await self._connect(playwright, {"ws": ws}, owned=False)
        elif running and running["ws"]:
            if running.get("headless") is not None:
                self.headless = running["headless"]
            await self._connect(playwright, running, owned=False)
        elif running:
            raise RuntimeError(
                f"session '{self.name}' is open in login mode, close that window first")
        elif self.launcher == "native":
            await self._spawn(playwright)
        else:
            await self._launch_playwright(playwright)
        self._launched_at = time.time()
        self._last_activity = time.time()
        self.state = SessionState.ACTIVE
        await self._apply_persona()
        self._pages = self._context.pages[:]
        for page in self._pages:
            self._setup_page(page)
        # apply the UA/client-hint override to initial pages NOW (awaited) so the first
        # request carries the right sec-ch-ua, not racing the ensure_future in _setup_page.
        if self._spoof and self.fingerprint.user_agent:
            for page in self._pages:
                await self._apply_ua_override(page)
        def _on_new_page(page):
            self._pages.append(page)
            self._setup_page(page)
        self._context.on("page", _on_new_page)
        if self._recording is not None:
            await self._install_recorder()
        await self._find_front()
        self._start_watchdog()
        return self._context

    async def _launch_playwright(self, playwright):
        kwargs = self._launch_args()
        if self.persona:
            _match_engine(self.fingerprint, await asyncio.to_thread(
                _browser.browser_version, playwright.chromium.executable_path))
            if self.fingerprint.user_agent:
                kwargs["user_agent"] = self.fingerprint.user_agent
        elif self.headless:
            kwargs["user_agent"] = _browser.real_user_agent(await asyncio.to_thread(
                _browser.browser_version, playwright.chromium.executable_path))
        self._context = await playwright.chromium.launch_persistent_context(**kwargs)
        self._owned = True
        self._context.on("close", lambda _: self._on_browser_gone())

    def spawn_options(self, *, restore=True):
        """Keyword arguments for browser.spawn() that reproduce this session's setup."""
        fp = self.fingerprint
        kw = {
            "browser": self.browser,
            "headless": self.headless,
            "restore": restore,
            "ignore_https_errors": self.ignore_https_errors,
        }
        if not self.headless:
            kw["window_class"] = self._window_class()
            kw["maximize"] = not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if self.proxy:
            kw["proxy"] = self.proxy.server
            kw["proxy_bypass"] = self.proxy.bypass
        extra, env = [], {}
        if fp.disable_webrtc:
            extra.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
        if self.persona:
            if fp.user_agent:
                _match_engine(fp, _browser.browser_version(_browser.find_executable(self.browser)))
                kw["user_agent"] = fp.user_agent
            kw["lang"] = fp.locale
            if self.headless:
                kw["window_size"] = (fp.viewport_width, fp.viewport_height)
            if fp.device_scale_factor and fp.device_scale_factor != 1.0:
                extra.append(f"--force-device-scale-factor={fp.device_scale_factor}")
            if fp.color_scheme == "dark":
                extra.append("--force-dark-mode")
            if fp.timezone and sys.platform != "win32":
                env["TZ"] = fp.timezone
        elif self.headless:
            kw["window_size"] = (fp.viewport_width, fp.viewport_height)
        kw["extra"] = extra
        kw["env"] = env
        return kw

    async def _spawn(self, playwright, *, restore=True):
        kw = await asyncio.to_thread(self.spawn_options, restore=restore)
        # headed browsers outlive their launcher; headless ones die with it
        info = await asyncio.to_thread(_browser.spawn, self.profile_dir,
                                       tie_to_parent=self.headless, **kw)
        await self._connect(playwright, info, owned=True)

    async def _connect(self, playwright, info, *, owned):
        self._browser = await playwright.chromium.connect_over_cdp(info["ws"], no_defaults=True)
        self._context = self._browser.contexts[0]
        self._owned = owned
        self._pid = info.get("pid")
        if info.get("headless") is not None:
            self.headless = info["headless"]
        self._browser.on("disconnected", lambda _: self._on_browser_gone())
        await self._watch_downloads()

    @property
    def download_dir(self):
        return os.path.join(self.profile_dir, "downloads")

    async def _watch_downloads(self):
        """Report downloads; a headless browser saves into the profile, a visible one where the human expects."""
        params = {"behavior": "default", "eventsEnabled": True}
        if self.headless:
            os.makedirs(self.download_dir, exist_ok=True)
            params = {"behavior": "allow", "downloadPath": self.download_dir, "eventsEnabled": True}
        try:
            cdp = await self._browser.new_browser_cdp_session()
            cdp.on("Browser.downloadWillBegin", self._on_download_begin)
            cdp.on("Browser.downloadProgress", self._on_download_progress)
            await cdp.send("Browser.setDownloadBehavior", params)
            self._browser_cdp = cdp
        except Exception as exc:
            _log.warning("session '%s': download events unavailable: %s", self.name, exc)

    def _page_for_target(self, target_id):
        for page, tid in list(self._target_ids.items()):
            if tid == target_id and not page.is_closed():
                return page
        return None

    def _on_download_begin(self, p):
        page = self._page_for_target(p.get("frameId"))
        info = {"url": p["url"], "filename": p["suggestedFilename"],
                "tab": self._tab(page) if page else None}
        self._downloads[p["guid"]] = info
        self.events.emit("download", state="started", **info)

    def _on_download_progress(self, p):
        if p["state"] == "inProgress":
            return
        info = self._downloads.pop(p["guid"], {})
        extra = {"path": p.get("filePath"), "bytes": p.get("receivedBytes")}
        if not extra["path"] and p["state"] == "completed" and self.headless and info.get("filename"):
            extra["path"] = os.path.join(self.download_dir, info["filename"])
        self.events.emit("download", state=p["state"], **info, **extra)

    async def _on_pw_download(self, page, download):
        info = {"url": download.url, "filename": download.suggested_filename, "tab": self._tab(page)}
        self.events.emit("download", state="started", **info)
        os.makedirs(self.download_dir, exist_ok=True)
        dest = os.path.join(self.download_dir, download.suggested_filename)
        try:
            await download.save_as(dest)
            self.events.emit("download", state="completed", path=dest,
                             bytes=os.path.getsize(dest), **info)
        except Exception as exc:
            self.events.emit("download", state="canceled", error=str(exc), **info)

    async def _apply_persona(self):
        if not self.persona:
            return
        fp = self.fingerprint
        if self._spoof:
            from .stealth import apply_stealth
            seed = int.from_bytes(self.name.encode()[:4].ljust(4, b'\0'), 'big')
            await apply_stealth(self._context, fingerprint=fp, seed=seed, headless=self.headless)
        elif fp.platform:
            await self._context.add_init_script(
                f"Object.defineProperty(navigator, 'platform', {{configurable: true, get: () => '{fp.platform}'}})"
            )
        if self.launcher == "native":
            if fp.extra_headers:
                await self._context.set_extra_http_headers(fp.extra_headers)
            if fp.geolocation:
                await self._context.grant_permissions(["geolocation"])
                await self._context.set_geolocation(fp.geolocation)

    def _on_browser_gone(self):
        """Window closed / browser exited outside our control: drop refs, keep the profile."""
        if self._closing:
            return
        _log.info("session '%s': browser gone, hibernating", self.name)
        self._stop_watchdog()
        self._context = None
        self._browser = None
        self._focus = None
        self._pages = []
        self._cdp_session = None
        self._launched_at = None
        if self.state != SessionState.PAUSED:
            self.state = SessionState.HIBERNATED
        for cb in list(self._close_callbacks):
            try:
                cb(self.name)
            except Exception:
                pass

    def on_close(self, callback):
        self._close_callbacks.append(callback)
        return lambda: self._close_callbacks.remove(callback)

    def _setup_page(self, page):
        if self.context:
            self.context.hook_page(page)
        # a dialog listener on every tab stops playwright auto-dismissing the human's dialogs
        page.on("dialog", lambda d, p=page: asyncio.ensure_future(self._on_dialog(p, d)))
        page.on("download", lambda d, p=page: asyncio.ensure_future(self._on_pw_download(p, d)))
        page.on("popup", lambda popup, opener=page: self._follow_popup(opener, popup))
        page.on("crash", lambda p: self.events.emit("crash", tab=self._tab(p), url=p.url))
        page.on("framenavigated", self._on_navigated)
        page.on("close", lambda _: self._on_page_closed())
        if self.headless:
            self._claim(page)
        asyncio.ensure_future(self._remember_target(page))
        # propagate intercept rules to new pages (popups, target=_blank, etc.)
        # page.route() is async; batch into one coroutine with error handling
        if self._intercept_rules:
            asyncio.ensure_future(self._apply_intercept_rules(page))
        # match sec-ch-ua client-hints to the fingerprint (no HeadlessChrome leak)
        if self._spoof and self.fingerprint.user_agent:
            asyncio.ensure_future(self._apply_ua_override(page))
        self._track_websockets(page)
        self._watch_hyprland_viewport(page)

    async def _apply_ua_override(self, page):
        """Pin the UA + sec-ch-ua client-hints + Accept-Language at the network layer via
        CDP so they agree with the fingerprint. Idempotent per page."""
        if getattr(page, "_hutch_ua_overridden", False):
            return
        try:
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Network.setUserAgentOverride", _ua_override_params(self.fingerprint))
            # keep the CDP session ATTACHED — detaching reverts the override, re-leaking
            # HeadlessChrome. Store it on the page so it lives as long as the page does.
            page._hutch_ua_cdp = cdp
            page._hutch_ua_overridden = True
        except Exception as exc:
            if _is_disconnect(exc):
                return
            _log.warning("session '%s': UA client-hint override failed: %s", self.name, exc)

    def _watch_hyprland_viewport(self, page):
        if getattr(page, "_hutch_hyprland_viewport_watched", False):
            return

        def resync_after_load():
            asyncio.create_task(self._sync_hyprland_viewport(page))

        page.on("load", resync_after_load)
        page._hutch_hyprland_viewport_watched = True

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
        self._focus = page
        self._claim(page)
        self._last_activity = time.time()
        return page

    def _on_page_closed(self):
        # playwright-launched windows linger with zero tabs; native chromium exits by itself
        if (self.launcher == "playwright" and not self.headless and not self._closing
                and self._context and not any(not p.is_closed() for p in self._pages)):
            asyncio.ensure_future(self.close())

    def _follow_popup(self, opener, popup):
        if self._focus is None or self._focus is opener:
            self._focus = popup
        if self._is_agent_page(opener):
            self._claim(popup)
        asyncio.ensure_future(self._report_popup(opener, popup))

    async def _report_popup(self, opener, popup):
        try:
            await popup.wait_for_load_state("commit", timeout=5000)
        except Exception:
            pass
        self.events.emit("popup", tab=self._tab(popup), opener=self._tab(opener),
                         url=popup.url, agent=self._is_agent_page(popup))

    def _claim(self, page):
        """Mark a tab as the agent's: its dialogs are answered and file choosers intercepted."""
        if page in self._agent_pages:
            return
        self._agent_pages.add(page)
        page.on("filechooser", lambda fc, p=page: self._on_file_chooser(p, fc))

    def _is_agent_page(self, page):
        return self.headless or page in self._agent_pages

    def _tab(self, page):
        pages = [p for p in self._pages if not p.is_closed()]
        return pages.index(page) if page in pages else None

    async def _remember_target(self, page):
        try:
            self._target_ids[page] = await self.target_id(page)
        except Exception:
            pass

    async def _on_dialog(self, page, dialog):
        info = {"kind": dialog.type, "message": dialog.message, "url": page.url, "tab": self._tab(page)}
        if dialog.type == "prompt":
            info["default"] = dialog.default_value
        if not self._is_agent_page(page):
            self.events.emit("dialog", handled="human", **info)
            return
        if self._dialog_plans:
            accept, text = self._dialog_plans.popleft()
        elif self._dialog_policy:
            accept, text = self._dialog_policy
        else:
            accept, text = dialog.type in ("alert", "beforeunload"), None
        handled = "accepted" if accept else "dismissed"
        try:
            if accept:
                await (dialog.accept(text) if text is not None and dialog.type == "prompt"
                       else dialog.accept())
            else:
                await dialog.dismiss()
        except Exception:
            handled = "answered elsewhere"
        if accept and text is not None and dialog.type == "prompt":
            info["text"] = text
        self.events.emit("dialog", handled=handled, **info)

    def _on_file_chooser(self, page, chooser):
        self._file_chooser = chooser
        self.events.emit("filechooser", tab=self._tab(page), url=page.url,
                         multiple=chooser.is_multiple())

    async def set_files(self, files):
        """Answer the file chooser an agent tab opened (see the 'filechooser' event)."""
        chooser, self._file_chooser = self._file_chooser, None
        if chooser is None:
            raise RuntimeError("no file chooser is open")
        await chooser.set_files(files)
        return {"files": files if isinstance(files, list) else [files]}

    @staticmethod
    def _secret(value, page):
        return _secrets.resolve(value, page.url)

    async def start_recording(self):
        """Record clicks, fills, keys and navigations in every tab, human or agent, until stop_recording."""
        if not self._context:
            raise RuntimeError(f"session '{self.name}' not launched")
        page = self._active_page()
        self._recording = {"version": 1, "start": page.url if page else None, "steps": []}
        self._recording_scrub = {}
        await self._install_recorder()
        return {"recording": True, "start": self._recording["start"]}

    async def _install_recorder(self):
        if not self._recorder_installed:
            await self._context.expose_binding("__hutch_record", self._on_record)
            await self._context.add_init_script(_recorder.SCRIPT)
            self._recorder_installed = True
        for p in self._pages:
            if not p.is_closed():
                try:
                    await p.evaluate(_recorder.SCRIPT)
                except Exception:
                    pass

    def stop_recording(self):
        recording, self._recording = self._recording, None
        if recording is None:
            raise RuntimeError("not recording")
        for step in recording["steps"]:
            step.pop("t", None)
        return json.loads(_secrets.scrub(json.dumps(recording), self._recording_scrub))

    def _on_record(self, source, step):
        if self._recording is None:
            return
        url = source["frame"].url if source.get("frame") else ""
        if step.get("action") == "fill":
            raw = step.get("value", "")
            secret = _secrets.placeholder_for(raw) or ("{{password}}" if step.get("password") else None)
            if secret:
                step["value"] = secret
                self._recording_scrub[raw] = secret
        step.pop("password", None)
        step["url"] = url
        step["t"] = time.time()
        _recorder.add_step(self._recording, step)

    def _on_navigated(self, frame):
        rec = self._recording
        if rec is None or frame.parent_frame is not None or frame.url.startswith("about:"):
            return
        last = rec["steps"][-1] if rec["steps"] else None
        if last and last.get("url") == frame.url:
            return
        caused = last and last["action"] in ("click", "press") and time.time() - last.get("t", 0) < 3
        rec["steps"].append({"action": "wait_url" if caused else "goto", "url": frame.url})

    async def replay(self, recording, *, timeout=15000):
        """Run a recording (dict or path) on the active tab; {{name}} values come from the vault."""
        rec = _recorder.load(recording)
        page = self._require_page()
        done = 0
        try:
            if rec.get("start") and page.url != rec["start"]:
                await page.goto(rec["start"])
            for step in rec["steps"]:
                page = self._require_page()
                await self._replay_step(page, step, timeout)
                done += 1
        except Exception as exc:
            message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            return {"ok": False, "done": done, "failed": rec["steps"][done] if done < len(rec["steps"]) else None,
                    "error": message}
        return {"ok": True, "done": done, "url": self._require_page().url}

    async def _replay_step(self, page, step, timeout):
        a, sel = step["action"], step.get("selector")
        if a == "goto":
            await page.goto(step["url"])
        elif a == "wait_url":
            want = step["url"].split("?")[0].split("#")[0]
            await page.wait_for_url(lambda u: u.split("?")[0].split("#")[0] == want, timeout=timeout)
        elif a == "click":
            await page.click(sel, timeout=timeout)
        elif a == "fill":
            await page.fill(sel, self._secret(step["value"], page), timeout=timeout)
        elif a == "press":
            await page.press(sel, step["key"], timeout=timeout)
        elif a == "select":
            await page.select_option(sel, step["value"], timeout=timeout)
        elif a in ("check", "uncheck"):
            await getattr(page, a)(sel, timeout=timeout)
        elif a == "upload":
            raise RuntimeError(f"upload step needs files: {step.get('files')}")

    def arm_dialog(self, accept=True, text=None):
        """Answer the next dialog on an agent tab this way (once), instead of the policy."""
        self._dialog_plans.append((bool(accept), text))
        return {"queued": len(self._dialog_plans)}

    def _active_page(self):
        """The tab this session drives: its focus, else the tab in front when it joined, else the newest.

        On a browser the human is using, tabs they open never steal the agent's focus.
        """
        if not self._context:
            return None
        if self._focus is not None and not self._focus.is_closed():
            return self._focus
        if self._front is not None and not self._front.is_closed():
            return self._front
        pages = [p for p in self._pages if not p.is_closed()]
        return pages[-1] if pages else None

    async def _find_front(self):
        """Remember the tab in front (restored sessions do not list it last)."""
        async def visible(page):
            try:
                return await asyncio.wait_for(
                    page.evaluate("[document.visibilityState === 'visible', document.hasFocus()]"), 2)
            except Exception:
                return [False, False]
        pages = [p for p in self._pages if not p.is_closed()]
        if len(pages) < 2:
            return
        states = await asyncio.gather(*(visible(p) for p in pages))
        ranked = sorted(zip(states, range(len(pages))), key=lambda x: (x[0][1], x[0][0], x[1]))
        best = ranked[-1]
        self._front = pages[best[1]] if best[0][0] else None

    def cdp_endpoint(self):
        """The browser-level CDP websocket URL, or None when the browser is not running."""
        if self.endpoint:
            return _browser.resolve_endpoint(self.endpoint)
        ep = _browser.read_endpoint(self.profile_dir)
        return ep[1] if ep else None

    async def target_id(self, page=None):
        page = page or self._active_page()
        if page is None:
            return None
        cdp = await self._context.new_cdp_session(page)
        try:
            return (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
        finally:
            await cdp.detach()

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
                dom = await page.aria_snapshot(mode="ai")
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
        # attached to someone's visible browser: work in our own tab, never theirs
        shared = self._browser is not None and not self._owned and not self.headless and self._focus is None
        page = None if shared else self._active_page()
        if not page:
            page = await self.new_page()
        self._last_activity = time.time()
        self._last_url = url
        await page.goto(url, wait_until=wait_until)
        await self._sync_hyprland_viewport(page)
        return await self.page_state()

    @_retry_on_disconnect
    async def click(self, selector, *, wait_after="networkidle", timeout=5000):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.click(_ref(selector))
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
        await page.fill(_ref(selector), self._secret(value, page))
        if press_enter:
            await page.press(_ref(selector), "Enter")
        return await self.page_state()

    @_retry_on_disconnect
    async def type_text(self, selector, text, *, delay=50):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.type(_ref(selector), self._secret(text, page), delay=delay)
        return await self.page_state()

    @_retry_on_disconnect
    async def press(self, selector, key):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.press(_ref(selector), key)
        return await self.page_state()

    @_retry_on_disconnect
    async def select_option(self, selector, value):
        self._require_active()
        page = self._active_page()
        if not page:
            raise RuntimeError(f"session '{self.name}' has no open page")
        self._last_activity = time.time()
        await page.select_option(_ref(selector), value)
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
            await page.wait_for_selector(_ref(selector), state=state, timeout=timeout)
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
            dom = await page.aria_snapshot(mode="ai")
        except Exception:
            pass
        url = page.url
        self._last_url = url
        return {
            "url": url,
            "title": await page.title(),
            "dom": dom,
            "changes": self._delta(page, url, dom) if dom is not None else None,
        }

    def _delta(self, page, url, snap):
        """What changed since the agent last saw this tab; None after a navigation or first look."""
        prev = self._seen.get(page)
        self._seen[page] = (url, snap)
        if not prev or prev[0] != url:
            return None
        return _snapshot.diff(prev[1], snap)

    @_retry_on_disconnect
    async def aria_snapshot(self, *, selector=None, interactive=False, diff=False):
        """Accessibility tree with refs (e12) usable as selectors in every action.

        interactive: only elements an agent can act on (plus headings).
        diff: only what changed since this tab was last snapshotted; the full tree after a navigation.
        """
        page = self._require_page()
        if selector:
            snap = await page.locator(_ref(selector)).aria_snapshot(mode="ai")
        else:
            snap = await page.aria_snapshot(mode="ai")
            delta = self._delta(page, page.url, snap)
            if diff and delta is not None:
                return delta or "no changes"
        return _snapshot.interactive(snap) if interactive else snap

    async def annotated_screenshot(self, *, path=None):
        """Viewport screenshot with every actionable element boxed and labelled by its ref."""
        page = self._require_page()
        snap = await page.aria_snapshot(mode="ai", boxes=True)
        size = await page.evaluate("[innerWidth, innerHeight]")
        items = [b for b in _snapshot.boxes(snap)
                 if b["x"] < size[0] and b["y"] < size[1] and b["x"] + b["w"] > 0 and b["y"] + b["h"] > 0]
        await page.evaluate(_ANNOTATE_JS, items)
        try:
            image = await page.screenshot(path=path)
        finally:
            await page.evaluate("document.getElementById('__hutch_annotations')?.remove()")
        legend = "\n".join(f'{b["ref"]} {b["role"]} "{b["name"]}"'.replace(' ""', "") for b in items)
        return image, legend

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
        await page.hover(_ref(selector))
        return await self.page_state()

    @_retry_on_disconnect
    async def dblclick(self, selector, *, wait_after="networkidle", timeout=5000):
        page = self._require_page()
        await page.dblclick(_ref(selector))
        if wait_after:
            try:
                await page.wait_for_load_state(wait_after, timeout=timeout)
            except Exception:
                pass
        return await self.page_state()

    @_retry_on_disconnect
    async def right_click(self, selector):
        page = self._require_page()
        await page.click(_ref(selector), button="right")
        return await self.page_state()

    @_retry_on_disconnect
    async def scroll(self, *, direction="down", amount=500, selector=None):
        page = self._require_page()
        if selector:
            loc = page.locator(_ref(selector))
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
        await page.focus(_ref(selector))

    @_retry_on_disconnect
    async def check(self, selector):
        page = self._require_page()
        await page.check(_ref(selector))
        return await self.page_state()

    @_retry_on_disconnect
    async def uncheck(self, selector):
        page = self._require_page()
        await page.uncheck(_ref(selector))
        return await self.page_state()

    @_retry_on_disconnect
    async def set_checked(self, selector, checked):
        page = self._require_page()
        await page.set_checked(_ref(selector), checked)
        return await self.page_state()

    @_retry_on_disconnect
    async def set_input_files(self, selector, files):
        page = self._require_page()
        await page.set_input_files(_ref(selector), files)
        return await self.page_state()

    @_retry_on_disconnect
    async def drag_and_drop(self, source, target):
        page = self._require_page()
        await page.drag_and_drop(_ref(source), _ref(target))
        return await self.page_state()

    @_retry_on_disconnect
    async def tap(self, selector):
        page = self._require_page()
        await page.tap(_ref(selector))
        return await self.page_state()

    @_retry_on_disconnect
    async def dispatch_event(self, selector, event_type, event_init=None):
        page = self._require_page()
        await page.dispatch_event(_ref(selector), event_type, event_init)

    # --- content extraction ---

    @_retry_on_disconnect
    async def content(self):
        page = self._require_page()
        return await page.content()

    @_retry_on_disconnect
    async def inner_text(self, selector):
        page = self._require_page()
        return await page.inner_text(_ref(selector))

    @_retry_on_disconnect
    async def inner_html(self, selector):
        page = self._require_page()
        return await page.inner_html(_ref(selector))

    @_retry_on_disconnect
    async def text_content(self, selector):
        page = self._require_page()
        return await page.text_content(_ref(selector))

    @_retry_on_disconnect
    async def get_attribute(self, selector, name):
        page = self._require_page()
        return await page.get_attribute(_ref(selector), name)

    @_retry_on_disconnect
    async def input_value(self, selector):
        page = self._require_page()
        return await page.input_value(_ref(selector))

    # --- element state ---

    @_retry_on_disconnect
    async def is_visible(self, selector):
        page = self._require_page()
        return await page.is_visible(_ref(selector))

    @_retry_on_disconnect
    async def is_checked(self, selector):
        page = self._require_page()
        return await page.is_checked(_ref(selector))

    @_retry_on_disconnect
    async def is_enabled(self, selector):
        page = self._require_page()
        return await page.is_enabled(_ref(selector))

    @_retry_on_disconnect
    async def is_hidden(self, selector):
        page = self._require_page()
        return await page.is_hidden(_ref(selector))

    @_retry_on_disconnect
    async def is_editable(self, selector):
        page = self._require_page()
        return await page.is_editable(_ref(selector))

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
        await frame.click(_ref(selector))
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
        await frame.fill(_ref(selector), self._secret(value, frame))
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
            loc = page.locator(_ref(selector))
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
        await loc.nth(nth).fill(self._secret(value, page))
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
                await page.click(_ref(action_selector))
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
        self._focus = target
        self._claim(target)
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

    async def expect_download(self, action_selector, *, save_path=None, timeout=60.0):
        page = self._require_page()
        seq = self.events.last
        await page.click(_ref(action_selector))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            found = await self.events.wait(seq, {"download"}, max(0.0, deadline - loop.time()))
            if not found:
                raise TimeoutError(f"no download finished within {timeout:g}s")
            seq = found[-1]["seq"]
            done = [e for e in found if e["state"] != "started"]
            if done:
                break
        event = done[0]
        if event["state"] != "completed":
            raise RuntimeError(f"download {event['state']}: {event.get('filename')}")
        result = {"suggested_filename": event.get("filename"), "url": event.get("url"),
                  "path": event.get("path")}
        if save_path and event.get("path"):
            shutil.copyfile(event["path"], save_path)
            result["saved_to"] = save_path
        return result

    # --- dialog handling ---

    def handle_dialog(self, action="dismiss", prompt_text=None):
        """Answer every agent-tab dialog this way; action 'auto' restores the default
        (accept alerts and beforeunload, dismiss confirm and prompt)."""
        self._dialog_policy = None if action == "auto" else (action == "accept", prompt_text)

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
        await page.keyboard.type(self._secret(text, page), delay=delay)
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
        await page.keyboard.insert_text(self._secret(text, page))

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

    async def _shutdown_browser(self):
        """Graceful Browser.close so chromium writes its session for --restore-last-session."""
        try:
            cdp = await self._browser.new_browser_cdp_session()
            await cdp.send("Browser.close")
        except Exception:
            pass
        await _browser.wait_closed(self.profile_dir, self._pid)

    async def _teardown(self, *, keep_browser=False, clear_capture=True):
        self._closing = True
        try:
            self._stop_watchdog()
            if self._cdp_session:
                try:
                    await self._cdp_session.detach()
                except Exception:
                    pass
                self._cdp_session = None
            if self._browser:
                if not keep_browser:
                    await self._shutdown_browser()
                try:
                    await self._browser.close()
                except Exception:
                    pass
            elif self._context and not keep_browser:
                try:
                    await self._context.close()
                except Exception:
                    pass
            self._context = None
            self._browser = None
            self._focus = None
            self._pages = []
            self._popup_pages = []
            self._downloads = {}
            self._websockets = deque(maxlen=_WS_CONN_CAP)
            self._file_chooser = None
            self._browser_cdp = None
            self._agent_pages = weakref.WeakSet()
            self._target_ids = weakref.WeakKeyDictionary()
            self._seen = weakref.WeakKeyDictionary()
            self._recorder_installed = False
            self._front = None
            self._launched_at = None
            if clear_capture and self.context:
                self.context.clear()
            if self.state != SessionState.PAUSED:
                self.state = SessionState.HIBERNATED
        finally:
            self._closing = False

    async def close(self):
        """Close the browser if this session owns it; just disconnect from one it attached to."""
        if not self._context:
            return
        await self._teardown(keep_browser=not self._owned)

    async def detach(self):
        """Disconnect but leave the browser running (e.g. a human's headed window)."""
        if not self._context:
            return
        await self._teardown(keep_browser=True)

    async def set_headless(self, headless, playwright=None):
        """Relaunch the same profile headed or headless; tabs and session state carry over."""
        playwright = playwright or self._playwright
        if self.is_alive and self.headless == headless:
            return self.status()
        if self.launcher == "external":
            raise RuntimeError("an attached external browser cannot be relaunched by hutch")
        carry = None
        if self.is_alive and self.launcher != "native":
            carry = await self._carry_state()
        if self.is_alive:
            await self._teardown(keep_browser=False, clear_capture=False)
        elif _browser.status(self.profile_dir):
            await _browser.stop(self.profile_dir)
        self.headless = headless
        self._save_meta()
        await self.launch(playwright)
        if carry:
            await self._restore_state(carry)
        return self.status()

    async def _carry_state(self):
        cookies = [c for c in await self._context.cookies() if c.get("expires", -1) == -1]
        tabs = []
        for p in self._pages:
            if p.is_closed() or not p.url.startswith("http"):
                continue
            try:
                store = await p.evaluate(
                    "() => Object.fromEntries(Object.keys(sessionStorage).map(k => [k, sessionStorage.getItem(k)]))")
            except Exception:
                store = {}
            tabs.append({"url": p.url, "session_storage": store})
        return {"cookies": cookies, "tabs": tabs}

    async def _restore_state(self, carry):
        if carry["cookies"]:
            await self._context.add_cookies(carry["cookies"])
        blank = [p for p in self._pages if not p.is_closed() and p.url in ("about:blank", "")]
        for tab in carry["tabs"]:
            page = blank.pop(0) if blank else await self._context.new_page()
            try:
                await page.goto(tab["url"], wait_until="domcontentloaded")
                if tab["session_storage"]:
                    await page.evaluate(
                        "s => { for (const [k, v] of Object.entries(s)) sessionStorage.setItem(k, v) }",
                        tab["session_storage"])
                    await page.reload(wait_until="domcontentloaded")
            except Exception as exc:
                _log.warning("session '%s': could not restore %s: %s", self.name, tab["url"], exc)

    @property
    def is_alive(self):
        return self._context is not None

    @property
    def running(self):
        """A browser holds this profile, connected to this process or not."""
        if self.is_alive:
            return True
        return False if self.endpoint else bool(_browser.status(self.profile_dir))

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
            "launcher": self.launcher,
            "browser": self.browser,
            "persona": self.persona,
            "owned": self._owned if self.is_alive else None,
            "running": self.running,
            "pid": self._pid,
        }

    def __repr__(self):
        state = "alive" if self.is_alive else "closed"
        proxy = self.proxy.server if self.proxy else "direct"
        return f"<Session '{self.name}' {state} proxy={proxy}>"
