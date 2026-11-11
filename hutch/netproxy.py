"""Local forward proxy that enforces the private-network block where connections are made.

A session with ``block_private`` sends all of its browser traffic through this proxy, loopback
included. For each tunnel or request the proxy resolves the host once, refuses unless every
address is public (the rules in ``netguard.is_public_ip``), and connects to the address it
checked. That closes what request routing alone leaves open:

- DNS rebinding: Chromium never resolves the name itself, so an answer that changes between the
  check and the connection cannot redirect it;
- requests a service worker makes on its own, which Playwright does not route;
- WebSockets under the patchright driver, which has no WebSocket routing.

HTTPS and WebSockets arrive as CONNECT tunnels; the proxy never sees inside them. Plain HTTP
requests are forwarded one per connection. The proxy listens on loopback only and talks only to
the browser that was started with it.
"""

import asyncio
import contextlib
import ipaddress
import logging

from .netguard import _system_resolve, is_public_ip

_log = logging.getLogger(__name__)
_HOP_BY_HOP = {"proxy-connection", "proxy-authorization", "keep-alive", "connection"}
_MAX_HEAD = 64 * 1024


class _Refused(Exception):
    pass


def _split_host_port(authority, default_port):
    """'host:port', '[v6]:port' or 'host' to (host, port)."""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif authority.count(":") == 1:
        host, port = authority.split(":")
    else:
        host, port = authority, ""
    return host, int(port) if port else default_port


class PrivateNetworkProxy:
    def __init__(self, *, resolver=None, allow_ip=None, host="127.0.0.1", connect_timeout=15):
        self._resolve = resolver or _system_resolve
        self._allow_ip = allow_ip or is_public_ip
        self._connect_timeout = connect_timeout
        self.host = host
        self.port = None
        self.blocked = []  # refused host:port, newest last, at most 100
        self._server = None
        self._tasks = set()

    @property
    def url(self):
        return f"http://{self.host}:{self.port}"

    async def start(self, port=0):
        self._server = await asyncio.start_server(self._handle, self.host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            for task in list(self._tasks):
                task.cancel()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def vet(self, host, port):
        """The address to connect to. Raises _Refused for non-public hosts, OSError when unresolvable."""
        host = host.rstrip(".").lower()
        if not host or host == "localhost" or host.endswith(".localhost"):
            raise _Refused(host)
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            addresses = await self._resolve(host, port)
        if not addresses or not all(self._allow_ip(a) for a in addresses):
            raise _Refused(host)
        return addresses[0]

    def _note(self, target):
        self.blocked.append(target)
        del self.blocked[:-100]

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        upstream = None
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            if len(head) > _MAX_HEAD:
                return
            request_line, _, rest = head.decode("latin-1").partition("\r\n")
            method, target, version = request_line.split(" ", 2)
            if method.upper() == "CONNECT":
                host, port = _split_host_port(target, 443)
                address = await self._connect_vetted(writer, host, port)
                if address is None:
                    return
                upstream = await self._open(address, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._pipe(reader, writer, *upstream)
            else:
                await self._forward_http(reader, writer, method, target, version, rest)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.debug("proxy: %s: %s", type(exc).__name__, exc)
        finally:
            self._tasks.discard(task)
            for w in (writer, upstream[1] if upstream else None):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()

    async def _connect_vetted(self, writer, host, port):
        try:
            return await self.vet(host, port)
        except _Refused:
            self._note(f"{host}:{port}")
            await self._reply(writer, 403, "Forbidden", "blocked: private network")
        except OSError:
            await self._reply(writer, 502, "Bad Gateway", "could not resolve host")
        return None

    async def _open(self, address, port):
        return await asyncio.wait_for(asyncio.open_connection(address, port), self._connect_timeout)

    async def _forward_http(self, reader, writer, method, target, version, rest):
        if not target.lower().startswith("http://"):
            await self._reply(writer, 400, "Bad Request", "absolute http:// URL expected")
            return
        authority, _, path = target[len("http://"):].partition("/")
        host, port = _split_host_port(authority.rpartition("@")[2], 80)
        address = await self._connect_vetted(writer, host, port)
        if address is None:
            return
        headers = [line for line in rest.split("\r\n") if line]
        upgrade = any(line.lower().startswith("upgrade:") for line in headers)
        kept = [line for line in headers if line.split(":", 1)[0].strip().lower() not in _HOP_BY_HOP]
        # one request per connection: the next one may be for another host, and must be vetted
        kept.append("Connection: upgrade" if upgrade else "Connection: close")
        upstream = await self._open(address, port)
        try:
            head = f"{method} /{path} {version}\r\n" + "\r\n".join(kept) + "\r\n\r\n"
            upstream[1].write(head.encode("latin-1"))
            await upstream[1].drain()
            await self._pipe(reader, writer, *upstream)
        finally:
            with contextlib.suppress(Exception):
                upstream[1].close()

    async def _reply(self, writer, status, reason, body):
        data = body.encode()
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
                     f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _pipe(self, client_reader, client_writer, up_reader, up_writer):
        async def copy(src, dst):
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    if dst.can_write_eof():
                        dst.write_eof()

        await asyncio.gather(copy(client_reader, up_writer), copy(up_reader, client_writer))
