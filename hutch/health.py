import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Alert:
    type: str
    session: str
    timestamp: float
    detail: str = ""
    acknowledged: bool = False


_CAPTCHA_PATTERNS = [
    re.compile(r"recaptcha", re.I),
    re.compile(r"hcaptcha", re.I),
    re.compile(r"cf-challenge", re.I),
    re.compile(r"challenge-platform", re.I),
    re.compile(r"turnstile", re.I),
    re.compile(r"g-recaptcha", re.I),
    re.compile(r"captcha[_-]?container", re.I),
]

_CAPTCHA_URL_PATTERNS = [
    "challenges.cloudflare.com",
    "recaptcha/api",
    "hcaptcha.com/1/api",
    "challenges.cloudflare.com/turnstile",
]


class HealthMonitor:

    def __init__(self, session_name,
                 auth_fail_threshold=3,
                 rate_limit_threshold=3,
                 alert_cap=100):
        self.session_name = session_name
        self._auth_fail_threshold = auth_fail_threshold
        self._rate_limit_threshold = rate_limit_threshold
        self._alerts = deque(maxlen=alert_cap)
        self._recent_statuses = deque(maxlen=20)
        self._consecutive_auth_fails = 0
        self._consecutive_rate_limits = 0
        self._callbacks = []

    def on_alert(self, callback):
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    def _fire(self, alert_type, detail=""):
        alert = Alert(
            type=alert_type,
            session=self.session_name,
            timestamp=time.time(),
            detail=detail,
        )
        self._alerts.append(alert)
        for cb in self._callbacks:
            try:
                cb(alert)
            except Exception:
                pass
        return alert

    def check_response(self, status, url=""):
        self._recent_statuses.append(status)

        if status in (401, 403):
            self._consecutive_auth_fails += 1
            if self._consecutive_auth_fails >= self._auth_fail_threshold:
                self._fire("auth_expired",
                           f"{self._consecutive_auth_fails}x {status} on {url}")
                self._consecutive_auth_fails = 0
        else:
            self._consecutive_auth_fails = 0

        if status == 429:
            self._consecutive_rate_limits += 1
            if self._consecutive_rate_limits >= self._rate_limit_threshold:
                self._fire("rate_limited",
                           f"{self._consecutive_rate_limits}x 429 on {url}")
                self._consecutive_rate_limits = 0
        else:
            self._consecutive_rate_limits = 0

    def check_page_content(self, html):
        for pattern in _CAPTCHA_PATTERNS:
            if pattern.search(html):
                self._fire("captcha", f"detected pattern: {pattern.pattern}")
                return True
        return False

    def check_navigation(self, url):
        for pattern in _CAPTCHA_URL_PATTERNS:
            if pattern in url:
                self._fire("captcha", f"navigated to captcha URL: {url}")
                return True
        return False

    def on_crash(self, detail=""):
        self._fire("crashed", detail)

    def on_disconnect(self, detail=""):
        self._fire("disconnected", detail)

    @property
    def alerts(self):
        return list(self._alerts)

    def unacknowledged(self):
        return [a for a in self._alerts if not a.acknowledged]

    def acknowledge(self, index=None):
        if index is not None:
            if 0 <= index < len(self._alerts):
                self._alerts[index].acknowledged = True
        else:
            for a in self._alerts:
                a.acknowledged = True

    def clear(self):
        self._alerts.clear()
        self._consecutive_auth_fails = 0
        self._consecutive_rate_limits = 0

    def summary(self):
        total = len(self._alerts)
        unacked = len(self.unacknowledged())
        by_type = {}
        for a in self._alerts:
            by_type[a.type] = by_type.get(a.type, 0) + 1
        return {
            "session": self.session_name,
            "total_alerts": total,
            "unacknowledged": unacked,
            "by_type": by_type,
        }


def wire_context_to_health(context, monitor):
    def on_event(event_type, entry):
        if event_type == "network" and hasattr(entry, "status") and entry.status:
            monitor.check_response(entry.status, entry.url)
        elif event_type == "navigation" and hasattr(entry, "url"):
            monitor.check_navigation(entry.url)

    return context.subscribe(on_event)
