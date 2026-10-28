"""Access-control testing: replay one identity's request as other identities and as nobody."""

import asyncio
import difflib
import re
from urllib.parse import urlparse

from .differ import _AUTHZ_INDICATORS

SAFE = ("GET", "HEAD", "OPTIONS")
_AUTH_HEADER = re.compile(r"auth|token|csrf|xsrf|session|api[-_]?key|^cookie$", re.I)
_DROP = {"host", "content-length", "connection", "accept-encoding", "cookie"}
_STATIC = re.compile(r"\.(?:js|mjs|css|png|jpe?g|gif|svg|ico|webp|avif|woff2?|ttf|map|mp4|webm)(?:\?|$)", re.I)
_NOISE = re.compile(r"\d{4}-\d\d-\d\dT[\d:.]+Z?|\b\d{10,13}\b|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                    r"|\b[0-9a-f]{32,}\b|\b[A-Za-z0-9_-]{40,}\b", re.I)


def parse_raw(raw, scheme="https"):
    """A raw HTTP/1.x request (as Caido stores it) -> {method, url, headers, body}."""
    head, _, body = raw.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    method, target, _ = (lines[0].split(" ") + ["", ""])[:3]
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        if k.strip():
            headers[k.strip()] = v.strip()
    host = next((v for k, v in headers.items() if k.lower() == "host"), "")
    url = target if "://" in target else f"{scheme}://{host}{target}"
    return {"method": method.upper(), "url": url, "headers": headers, "body": body or None}


def _auth_headers(session, host):
    """The newest auth-looking request headers this identity sent to the host."""
    if not session.context:
        return {}
    for e in reversed(session.context.network.all()):
        if urlparse(e.url).hostname != host:
            continue
        found = {k: v for k, v in (e.request_headers or {}).items()
                 if _AUTH_HEADER.search(k) and k.lower() != "cookie"}
        if found:
            return found
    return {}


async def _send(playwright, session, request, *, timeout=20000):
    """Send as this identity (its cookies, auth headers and proxy), or as nobody when session is None."""
    url = request["url"]
    host = urlparse(url).hostname
    headers = {k: v for k, v in request.get("headers", {}).items()
               if k.lower() not in _DROP and not _AUTH_HEADER.search(k)}
    options = {"ignore_https_errors": True}
    proxy = getattr(session, "proxy", None) or request.get("_proxy")
    if proxy:
        options["proxy"] = {"server": proxy.server}
    if session is not None:
        headers.update(_auth_headers(session, host))
        options["storage_state"] = {"cookies": await session._context.cookies(url), "origins": []}
    ctx = await playwright.request.new_context(**options)
    try:
        r = await ctx.fetch(url, method=request["method"], headers=headers, data=request.get("body"),
                            max_redirects=0, fail_on_status_code=False, timeout=timeout)
        body = await r.text()
        return {"status": r.status, "length": len(body), "body": body,
                "location": r.headers.get("location")}
    except Exception as exc:
        return {"status": 0, "length": 0, "body": "", "error": str(exc).splitlines()[0]}
    finally:
        await ctx.dispose()


def _similarity(a, b):
    a, b = _NOISE.sub("", a[:30000]), _NOISE.sub("", b[:30000])
    if a == b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)


def _verdict(base, other):
    if other.get("error"):
        return "error"
    s = other["status"]
    if s in (401, 403) or (s == 404 and base["status"] != 404):
        return "enforced"
    if 300 <= s < 400 and not 300 <= base["status"] < 400:
        return "enforced"
    if _AUTHZ_INDICATORS.search(other["body"][:5000]) and not _AUTHZ_INDICATORS.search(base["body"][:5000]):
        return "enforced"
    if s != base["status"]:
        return "check"
    if other["similarity"] >= 0.95 and base["length"] > 0:
        return "bypassed"
    return "check"


async def replay(pool, owner, identities, request, *, anonymous=True, unsafe=False):
    """Run request as owner (baseline), each identity, and nobody; classify each against the baseline.

    bypassed: same answer as the owner, so the resource is not protected from that identity.
    public: nobody logged in gets the same answer either, so there is nothing to protect.
    enforced: denied, redirected away, not found, or access-denied language.
    check: answered differently in a way only a human can judge.
    """
    if request["method"].upper() not in SAFE and not unsafe:
        raise ValueError(f"{request['method']} changes state; pass unsafe=True to replay it")
    owner_s = await pool.get(owner, launch=True)
    others = [await pool.get(n, launch=True) for n in identities]
    request = {**request, "_proxy": owner_s.proxy}
    base = await _send(pool._playwright, owner_s, request)
    results = []
    targets = [(s.name, s) for s in others] + ([("anonymous", None)] if anonymous else [])
    for name, s in targets:
        r = await _send(pool._playwright, s, request)
        r["similarity"] = _similarity(base["body"], r["body"])
        r["verdict"] = _verdict(base, r)
        r["as"] = name
        results.append(r)
    if any(r["as"] == "anonymous" and r["verdict"] == "bypassed" for r in results):
        # nobody logged in gets the same answer: a public resource, not a bypass
        for r in results:
            if r["verdict"] == "bypassed":
                r["verdict"] = "public"
    for r in [base] + results:
        r["excerpt"] = r.pop("body")[:300]
    verdicts = {r["verdict"] for r in results}
    verdict = next((v for v in ("bypassed", "check", "public") if v in verdicts), "enforced")
    return {"method": request["method"], "url": request["url"], "owner": owner,
            "baseline": base, "results": results, "verdict": verdict}


async def scan(pool, owner, identities, *, since=0, host=None, anonymous=True, unsafe=False,
               limit=40, concurrency=3):
    """Replay the owner's captured API and page requests (safe methods unless unsafe)."""
    owner_s = await pool.get(owner, launch=True)
    if not owner_s.context:
        raise RuntimeError(f"session '{owner}' has no traffic capture")
    seen, todo = set(), []
    for e in owner_s.context.network.all():
        if e.seq <= since or not e.url.startswith("http") or _STATIC.search(e.url):
            continue
        if e.resource_type not in ("xhr", "fetch", "document"):
            continue
        if host and urlparse(e.url).hostname != host:
            continue
        if e.method not in SAFE and not unsafe:
            continue
        key = (e.method, e.url)
        if key in seen:
            continue
        seen.add(key)
        todo.append({"method": e.method, "url": e.url, "headers": e.request_headers or {},
                     "body": e.request_body})
    todo = todo[-limit:]
    gate = asyncio.Semaphore(concurrency)

    async def one(req):
        async with gate:
            return await replay(pool, owner, identities, req, anonymous=anonymous, unsafe=unsafe)
    reports = await asyncio.gather(*(one(r) for r in todo))
    order = {"bypassed": 0, "check": 1, "enforced": 2, "public": 3}
    return sorted(reports, key=lambda r: order[r["verdict"]])


def format_reports(reports):
    if not reports:
        return "no requests to replay"
    out = []
    for r in reports:
        b = r["baseline"]
        out.append(f"{r['verdict'].upper():<9} {r['method']} {r['url']}  "
                   f"(owner {r['owner']}: {b['status']}, {b['length']}B)")
        for x in r["results"]:
            out.append(f"  {x['verdict']:<9} as {x['as']}: {x['status']}, {x['length']}B, "
                       f"similarity {x['similarity']}" + (f", {x['error']}" if x.get("error") else ""))
            if x["verdict"] in ("bypassed", "check") and x.get("excerpt"):
                out.append("    " + x["excerpt"][:160].replace("\n", " "))
    return "\n".join(out)
