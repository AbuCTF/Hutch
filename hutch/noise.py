"""What changes between two answers without meaning anything: tokens, ids, clocks, cursors.

Diffs (authz verdicts, differ, monitoring) compare answers after these are stripped. Defaults come from
live targets (Cloudflare, NetScaler, PC SOFT WEBDEV, Kiteworks, common CSRF and trace names); add rules
per session or per call, or in the file named by HUTCH_NOISE ({"headers": [], "keys": [], "patterns": []}).
"""

import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HEADERS = (
    "date", "age", "expires", "last-modified", "etag", "set-cookie", "cf-ray", "cf-cache-status",
    "report-to", "nel", "server-timing", "alt-svc", "via", "x-cache", "x-cache-hits", "x-served-by",
    "x-timer", "x-request-id", "x-amzn-requestid", "x-amzn-trace-id", "x-amz-request-id", "x-amz-cf-id",
    "x-amz-cf-pop", "traceparent", "tracestate", "x-datadog-trace-id", "x-datadog-parent-id", "x-trace-id",
    "x-correlation-id", "x-accellion-transaction-id", "x-runtime", "x-envoy-upstream-service-time", "nsc_*",
)
KEYS = (
    "_csrf", "csrf", "csrf_token", "csrftoken", "csrfmiddlewaretoken", "authenticity_token",
    "__requestverificationtoken", "xsrf", "xsrf_token", "_token", "nonce", "state_token", "cursor",
    "next_cursor", "prev_cursor", "page_token", "pagetoken", "next_page_token", "continuation",
    "timestamp", "ts", "_", "request_id", "requestid", "trace_id", "traceid", "correlation_id",
    "server_time", "generated_at", "_wd_*", "wd_*",
)
PATTERNS = (
    r"\d{4}-\d\d-\d\d[T ][\d:.]+(?:Z|[+-]\d\d:?\d\d)?",
    r"\b1\d{9}(?:\d{3})?\b",
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    r"\b[0-9a-f]{32,}\b",
    r"\b[A-Za-z0-9_-]{40,}\b",
    r"\b(?:__cf_bm|cf_clearance|NSC_\w+)=[^;\s\"']+",
    r"\b_?WD_\w+=[^&\"'\s]*",
    r"[?&]_=\d+",
)

_KEEP = object()


def _matcher(names):
    exact = {n.lower() for n in names if not n.endswith("*")}
    prefixes = tuple(n[:-1].lower() for n in names if n.endswith("*"))
    return lambda name: name.lower() in exact or (bool(prefixes) and name.lower().startswith(prefixes))


class Noise:
    def __init__(self, headers=(), keys=(), patterns=(), *, defaults=True):
        extra = _file_rules()
        h = list(headers) + extra.get("headers", []) + (list(HEADERS) if defaults else [])
        k = list(keys) + extra.get("keys", []) + (list(KEYS) if defaults else [])
        p = list(patterns) + extra.get("patterns", []) + (list(PATTERNS) if defaults else [])
        self.rules = {"headers": h, "keys": k, "patterns": p}
        self._header = _matcher(h)
        self._key = _matcher(k)
        self._patterns = [re.compile(x, re.I) for x in p]

    def merged(self, rules):
        """This set plus more rules ({headers, keys, patterns}), without repeating the defaults."""
        if not rules:
            return self
        return Noise(self.rules["headers"] + list(rules.get("headers", ())),
                     self.rules["keys"] + list(rules.get("keys", ())),
                     self.rules["patterns"] + list(rules.get("patterns", ())), defaults=False)

    def headers(self, headers):
        return {k: v for k, v in (headers or {}).items() if not self._header(k)}

    def text(self, value):
        for p in self._patterns:
            value = p.sub("~", value)
        return value

    def url(self, url):
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not self._key(k)]
        return self.text(urlunsplit(parts._replace(query=urlencode(query))))

    def body(self, body):
        """A canonical form: JSON without noisy keys (sorted), or text with tokens and noisy fields blanked."""
        if not body:
            return body or ""
        stripped = body.lstrip()
        if stripped[:1] in "{[":
            try:
                return json.dumps(self._json(json.loads(body)), sort_keys=True, separators=(",", ":"))
            except ValueError:
                pass
        out = re.sub(r"""(<input\b[^>]*\bname=["']?([\w.-]+)["']?[^>]*\bvalue=)(["'])[^"']*\3""",
                     lambda m: m.group(1) + '""' if self._key(m.group(2)) else m.group(0), body, flags=re.I)
        out = re.sub(r"""(<meta\b[^>]*\bname=["']?([\w.-]+)["']?[^>]*\bcontent=)(["'])[^"']*\3""",
                     lambda m: m.group(1) + '""' if self._key(m.group(2)) else m.group(0), out, flags=re.I)
        out = re.sub(r"([?&;]|\b)([\w.-]+)=([^&\s\"'<>]*)",
                     lambda m: m.group(1) + m.group(2) + "=" if self._key(m.group(2)) else m.group(0), out)
        return self.text(out)

    def _json(self, value):
        if isinstance(value, dict):
            return {k: self._json(v) for k, v in value.items() if not self._key(str(k))}
        if isinstance(value, list):
            return [self._json(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _file_rules():
    path = os.environ.get("HUTCH_NOISE")
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


DEFAULT = Noise()
