import fnmatch
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NetworkEntry:
    seq: int
    method: str
    url: str
    status: Optional[int] = None
    request_headers: dict = field(default_factory=dict)
    response_headers: Optional[dict] = None
    request_body: Optional[str] = None
    response_body: Optional[str] = None
    resource_type: str = "other"
    timing: float = 0.0
    size: int = 0
    timestamp: float = 0.0


@dataclass
class ConsoleEntry:
    seq: int
    level: str
    text: str
    source: Optional[str] = None
    line: Optional[int] = None
    timestamp: float = 0.0


@dataclass
class ErrorEntry:
    seq: int
    message: str
    stack: Optional[str] = None
    timestamp: float = 0.0


@dataclass
class NavigationEntry:
    seq: int
    url: str
    timestamp: float = 0.0


@dataclass
class Snapshot:
    url: str
    title: str
    cursor: int
    timestamp: float
    cookies: list = field(default_factory=list)
    dom: Optional[dict] = None
    screenshot: Optional[bytes] = None
    network: list = field(default_factory=list)
    console: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    navigations: list = field(default_factory=list)
    storage: Optional[dict] = None


@dataclass
class Diff:
    cursor: int
    network_added: list = field(default_factory=list)
    console_added: list = field(default_factory=list)
    errors_added: list = field(default_factory=list)
    navigations_added: list = field(default_factory=list)
    cookies_added: list = field(default_factory=list)
    cookies_removed: list = field(default_factory=list)


_BODY_MAX = 65536


class RingBuffer:

    def __init__(self, maxlen):
        self._buf = deque(maxlen=maxlen)

    def append(self, item):
        self._buf.append(item)

    def since(self, seq):
        return [e for e in self._buf if e.seq > seq]

    def all(self):
        return list(self._buf)

    def query(self, **filters):
        results = list(self._buf)
        for key, val in filters.items():
            if val is None:
                continue
            if key == "pattern":
                results = [e for e in results if fnmatch.fnmatch(
                    getattr(e, "url", getattr(e, "text", "")), val)]
            elif key == "since":
                results = [e for e in results if e.seq > val]
            elif key == "method":
                results = [e for e in results if getattr(e, "method", "") == val.upper()]
            elif key == "level":
                results = [e for e in results if getattr(e, "level", "") == val]
            elif key == "status":
                if isinstance(val, range):
                    results = [e for e in results
                               if getattr(e, "status", None) is not None
                               and getattr(e, "status") in val]
                else:
                    results = [e for e in results if getattr(e, "status", None) == val]
        return results

    def __len__(self):
        return len(self._buf)

    def clear(self):
        self._buf.clear()


class Context:

    def __init__(self, network_cap=1000, console_cap=500):
        self.network = RingBuffer(network_cap)
        self.console = RingBuffer(console_cap)
        self.errors = RingBuffer(console_cap)
        self.navigations = RingBuffer(500)
        self._seq = 0
        self._pending = {}
        self._pages = set()
        self._last_cookies = []
        self._subscribers = []

    def _next_seq(self):
        self._seq += 1
        return self._seq

    @property
    def cursor(self):
        return self._seq

    def hook_page(self, page):
        if id(page) in self._pages:
            return
        self._pages.add(id(page))
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("console", self._on_console)
        page.on("pageerror", self._on_error)
        page.on("framenavigated", self._on_navigation)

    def _on_request(self, request):
        body = None
        try:
            body = request.post_data
            if body and len(body) > _BODY_MAX:
                body = body[:_BODY_MAX]
        except Exception:
            pass

        entry = NetworkEntry(
            seq=self._next_seq(),
            method=request.method,
            url=request.url,
            request_headers=dict(request.headers),
            request_body=body,
            resource_type=request.resource_type,
            timestamp=time.time(),
        )
        self._pending[request] = entry

    def _on_response(self, response):
        entry = self._pending.pop(response.request, None)
        if not entry:
            entry = NetworkEntry(
                seq=self._next_seq(),
                method=response.request.method,
                url=response.url,
                request_headers=dict(response.request.headers),
                resource_type=response.request.resource_type,
                timestamp=time.time(),
            )

        entry.status = response.status
        entry.response_headers = dict(response.headers)
        entry.timing = (time.time() - entry.timestamp) * 1000
        entry.size = int(response.headers.get("content-length", 0))

        self.network.append(entry)
        self._emit("network", entry)

    def _on_console(self, msg):
        location = msg.location if hasattr(msg, "location") else {}
        entry = ConsoleEntry(
            seq=self._next_seq(),
            level=msg.type,
            text=msg.text,
            source=location.get("url") if isinstance(location, dict) else None,
            line=location.get("lineNumber") if isinstance(location, dict) else None,
            timestamp=time.time(),
        )
        self.console.append(entry)
        self._emit("console", entry)

    def _on_error(self, error):
        entry = ErrorEntry(
            seq=self._next_seq(),
            message=str(error),
            stack=error.stack if hasattr(error, "stack") else None,
            timestamp=time.time(),
        )
        self.errors.append(entry)
        self._emit("error", entry)

    def _on_navigation(self, frame):
        if frame.parent_frame:
            return
        entry = NavigationEntry(
            seq=self._next_seq(),
            url=frame.url,
            timestamp=time.time(),
        )
        self.navigations.append(entry)
        self._emit("navigation", entry)

    def _emit(self, event_type, entry):
        for sub in self._subscribers:
            sub(event_type, entry)

    def subscribe(self, callback):
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback)

    def query_network(self, **filters):
        return self.network.query(**filters)

    def query_console(self, **filters):
        return self.console.query(**filters)

    def diff(self, since=0, current_cookies=None):
        cookies_added = []
        cookies_removed = []
        if current_cookies is not None:
            old_set = {(c.get("name"), c.get("domain"), c.get("path"))
                       for c in self._last_cookies}
            new_set = {(c.get("name"), c.get("domain"), c.get("path"))
                       for c in current_cookies}
            cookies_added = [c for c in current_cookies
                             if (c.get("name"), c.get("domain"), c.get("path"))
                             not in old_set]
            cookies_removed = [c for c in self._last_cookies
                               if (c.get("name"), c.get("domain"), c.get("path"))
                               not in new_set]
            self._last_cookies = list(current_cookies)

        return Diff(
            cursor=self._seq,
            network_added=self.network.since(since),
            console_added=self.console.since(since),
            errors_added=self.errors.since(since),
            navigations_added=self.navigations.since(since),
            cookies_added=cookies_added,
            cookies_removed=cookies_removed,
        )

    def clear(self):
        self.network.clear()
        self.console.clear()
        self.errors.clear()
        self.navigations.clear()
        self._pending.clear()
        self._last_cookies = []
