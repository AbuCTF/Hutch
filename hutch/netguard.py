"""Opt-in guard that keeps a session's browser off private networks.

For sessions that open URLs from untrusted sources, such as a public service that reads pages for
its users. Every request and WebSocket the browser makes is checked. Hostnames are resolved, and a
request is refused unless every address is globally routable: no loopback, private, link-local
(including 169.254.169.254), shared, multicast, reserved or unspecified addresses, and no
IPv4-mapped, NAT64, 6to4 or Teredo forms of them.

This routing guard is the first line: it refuses early, with ERR_BLOCKED_BY_CLIENT. Sessions Hutch
launches itself also send all traffic through ``netproxy.PrivateNetworkProxy``, which checks again
where connections are made and connects only to the address it checked. That covers what routing
cannot: DNS rebinding, requests a service worker makes on its own, WebSockets under patchright,
and page.route() handlers that run before this guard.

Without the proxy (an attached browser, or a session with its own upstream proxy) these limits
remain:
- Chromium resolves names again after the check, so a DNS server that changes its answer between
  the two lookups could slip a request through.
- Requests a service worker makes on its own are not routed by Playwright, so they are not checked.
- Routes added straight on a page with page.route() run before the guard; use Session.intercept().

Either way the guard runs before the session's intercept rules, and a rule that rewrites a
request's URL is trusted, like any other rule the operator wrote.
"""

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_PASS_SCHEMES = ("data", "blob", "about", "chrome", "chrome-extension", "devtools")
_NET_SCHEMES = ("http", "https", "ws", "wss")


def is_public_ip(ip):
    """True only for globally routable unicast addresses, including the IPv4 inside IPv6 forms."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip in _NAT64:
            return is_public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.sixtofour is not None and not is_public_ip(ip.sixtofour):
            return False
        if ip.teredo is not None and not all(is_public_ip(part) for part in ip.teredo):
            return False
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


async def _system_resolve(host, port):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))


class PrivateNetworkGuard:
    def __init__(self, resolver=None, ttl=300):
        self._resolve = resolver or _system_resolve
        self._ttl = ttl
        self._cache = {}
        self.blocked = []  # refused urls, newest last, at most 100

    async def allowed(self, url):
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme in _PASS_SCHEMES:
            return True
        if scheme not in _NET_SCHEMES:
            return False
        host = (parts.hostname or "").rstrip(".").lower()
        if not host or host == "localhost" or host.endswith(".localhost"):
            return False
        try:
            return is_public_ip(ipaddress.ip_address(host))
        except ValueError:
            pass
        cached = self._cache.get(host)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            addresses = await self._resolve(host, parts.port or (443 if scheme in ("https", "wss") else 80))
        except OSError:
            addresses = []
        ok = bool(addresses) and all(is_public_ip(a) for a in addresses)
        self._cache[host] = (time.monotonic() + self._ttl, ok)
        return ok

    def _note(self, url):
        self.blocked.append(url)
        del self.blocked[:-100]

    async def route(self, route):
        url = route.request.url
        if await self.allowed(url):
            await route.fallback()  # on to the session's own intercept rules, if any match
        else:
            self._note(url)
            await route.abort("blockedbyclient")

    async def websocket(self, ws):
        if await self.allowed(ws.url):
            ws.connect_to_server()
        else:
            self._note(ws.url)
            await ws.close(code=1008, reason="blocked: private network")
