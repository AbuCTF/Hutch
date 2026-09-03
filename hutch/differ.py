import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ResponseDiff:
    url: str
    session_a: str
    session_b: str
    status_match: bool
    status_a: Optional[int] = None
    status_b: Optional[int] = None
    header_diffs: list = field(default_factory=list)
    body_diff: Optional[str] = None
    body_match: bool = True
    size_a: int = 0
    size_b: int = 0
    interesting: bool = False
    notes: list = field(default_factory=list)


_NOISE_HEADERS = frozenset({
    "date", "age", "x-request-id", "x-trace-id", "x-amz-request-id",
    "cf-ray", "server-timing", "x-served-by", "x-cache",
    "x-cache-hits", "x-timer", "via", "set-cookie",
    "report-to", "nel", "alt-svc", "expect-ct",
})

_AUTHZ_INDICATORS = re.compile(
    r"(forbidden|unauthorized|access.denied|not.allowed|permission|"
    r"insufficient.privileges|login.required)", re.I)


def diff_responses(entries_a, entries_b, session_a, session_b, *,
                   url_pattern=None, ignore_noise=True):
    by_url_a = {}
    for e in entries_a:
        if url_pattern and url_pattern not in _get_url(e):
            continue
        url = _get_url(e)
        method = _get_method(e)
        key = f"{method} {url}"
        by_url_a[key] = e

    by_url_b = {}
    for e in entries_b:
        if url_pattern and url_pattern not in _get_url(e):
            continue
        url = _get_url(e)
        method = _get_method(e)
        key = f"{method} {url}"
        by_url_b[key] = e

    diffs = []
    for key in set(by_url_a) | set(by_url_b):
        a = by_url_a.get(key)
        b = by_url_b.get(key)
        if not a or not b:
            continue

        status_a = _get_status(a)
        status_b = _get_status(b)
        status_match = status_a == status_b

        hdiffs = []
        if not ignore_noise:
            hdiffs = _diff_headers(
                _get_resp_headers(a), _get_resp_headers(b))
        else:
            hdiffs = _diff_headers(
                _get_resp_headers(a), _get_resp_headers(b),
                ignore=_NOISE_HEADERS)

        body_a = _get_body(a)
        body_b = _get_body(b)
        body_match = body_a == body_b

        body_diff = None
        if not body_match and body_a and body_b:
            if len(body_a) < 50000 and len(body_b) < 50000:
                body_diff = "\n".join(difflib.unified_diff(
                    body_a.splitlines(), body_b.splitlines(),
                    fromfile=session_a, tofile=session_b, lineterm=""))

        rd = ResponseDiff(
            url=key,
            session_a=session_a,
            session_b=session_b,
            status_match=status_match,
            status_a=status_a,
            status_b=status_b,
            header_diffs=hdiffs,
            body_diff=body_diff,
            body_match=body_match,
            size_a=len(body_a) if body_a else 0,
            size_b=len(body_b) if body_b else 0,
        )

        _check_interesting(rd, body_a, body_b)
        diffs.append(rd)

    diffs.sort(key=lambda d: (not d.interesting, d.status_match, d.url))
    return diffs


def _check_interesting(rd, body_a, body_b):
    notes = []

    if not rd.status_match:
        if rd.status_a == 200 and rd.status_b in (401, 403):
            notes.append("authz_bypass: session_a gets 200 while session_b gets denied")
            rd.interesting = True
        elif rd.status_b == 200 and rd.status_a in (401, 403):
            notes.append("authz_bypass: session_b gets 200 while session_a gets denied")
            rd.interesting = True
        elif rd.status_a != rd.status_b:
            notes.append(f"status_diff: {rd.status_a} vs {rd.status_b}")
            rd.interesting = True

    if rd.body_match is False and body_a is not None and body_b is not None:
        if rd.size_a > 0 and rd.size_b > 0:
            ratio = min(rd.size_a, rd.size_b) / max(rd.size_a, rd.size_b)
            if ratio < 0.5:
                notes.append(f"size_disparity: {rd.size_a}B vs {rd.size_b}B")
                rd.interesting = True

        if body_a and _AUTHZ_INDICATORS.search(body_a) and body_b and not _AUTHZ_INDICATORS.search(body_b):
            notes.append("authz_asymmetry: only session_a contains access-denied language")
            rd.interesting = True
        elif body_b and _AUTHZ_INDICATORS.search(body_b) and body_a and not _AUTHZ_INDICATORS.search(body_a):
            notes.append("authz_asymmetry: only session_b contains access-denied language")
            rd.interesting = True

    rd.notes = notes


def _get_url(e):
    if isinstance(e, dict):
        return e.get("url", "")
    return getattr(e, "url", "")


def _get_method(e):
    if isinstance(e, dict):
        return e.get("method", "GET")
    return getattr(e, "method", "GET")


def _get_status(e):
    if isinstance(e, dict):
        return e.get("status")
    return getattr(e, "status", None)


def _get_resp_headers(e):
    if isinstance(e, dict):
        return e.get("response_headers", {}) or {}
    return getattr(e, "response_headers", {}) or {}


def _get_body(e):
    if isinstance(e, dict):
        return e.get("response_body")
    return getattr(e, "response_body", None)


def _diff_headers(a, b, ignore=None):
    ignore = ignore or frozenset()
    all_keys = set(a) | set(b)
    diffs = []
    for k in sorted(all_keys):
        if k.lower() in ignore:
            continue
        va = a.get(k)
        vb = b.get(k)
        if va != vb:
            diffs.append({"header": k, "a": va, "b": vb})
    return diffs
