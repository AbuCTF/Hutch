"""A thin Chrome DevTools Protocol client over one websocket.

Hutch's own, written from the protocol reference (https://chromedevtools.github.io/devtools-protocol/),
for sessions that must not look automated to the page. Automation libraries announce themselves
through what they switch on: ``Runtime.enable`` makes the page's console serialise objects it
would otherwise leave alone, which scripts probe for, and the init scripts and emulation overrides a
test framework installs are visible too. This client sends only the commands a caller asks for, so a
page sees what a person's browser would show plus whatever the caller does on purpose.

    async with Browser.connect(ws_url) as cdp:
        target = await cdp.new_page("https://example.com")
        await target.send("Page.navigate", {"url": "https://example.org"})
"""

import asyncio
import json

import aiohttp


class CDPError(RuntimeError):
    def __init__(self, method, error):
        self.code = (error or {}).get("code")
        super().__init__(f"{method}: {(error or {}).get('message', error)}")


class Connection:
    """Request/response matching and event fan-out for one browser websocket."""

    def __init__(self, ws, http):
        self._ws = ws
        self._http = http
        self._next = 0
        self._pending = {}
        self._handlers = {}
        self._reader = asyncio.ensure_future(self._read())
        self.closed = asyncio.get_running_loop().create_future()

    @classmethod
    async def open(cls, ws_url, *, timeout=10.0):
        http = aiohttp.ClientSession()
        try:
            ws = await asyncio.wait_for(http.ws_connect(ws_url, max_msg_size=0, heartbeat=30), timeout)
        except BaseException:
            await http.close()
            raise
        return cls(ws, http)

    async def _read(self):
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "id" in data:
                    fut = self._pending.pop(data["id"], None)
                    if fut and not fut.done():
                        if "error" in data:
                            fut.set_exception(CDPError(fut.method, data["error"]))
                        else:
                            fut.set_result(data.get("result", {}))
                else:
                    for handler in list(self._handlers.get((data.get("sessionId"), data.get("method")), ())):
                        try:
                            res = handler(data.get("params", {}))
                            if asyncio.iscoroutine(res):
                                asyncio.ensure_future(res)
                        except Exception:
                            pass
        except (asyncio.CancelledError, aiohttp.ClientError):
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("browser connection closed"))
            self._pending.clear()
            if not self.closed.done():
                self.closed.set_result(True)

    async def send(self, method, params=None, *, session=None, timeout=30.0):
        if self.closed.done():
            raise ConnectionError("browser connection closed")
        self._next += 1
        msg = {"id": self._next, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        fut = asyncio.get_running_loop().create_future()
        fut.method = method
        self._pending[self._next] = fut
        await self._ws.send_str(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg["id"], None)
            raise TimeoutError(f"{method} did not answer within {timeout:.0f}s") from None

    def on(self, method, handler, session=None):
        self._handlers.setdefault((session, method), []).append(handler)

    def off(self, method, handler, session=None):
        with_ = self._handlers.get((session, method), [])
        if handler in with_:
            with_.remove(handler)

    async def close(self):
        self._reader.cancel()
        try:
            await self._ws.close()
        finally:
            await self._http.close()
        await asyncio.gather(self._reader, return_exceptions=True)


class Target:
    """One page, spoken to through a flat session on the browser connection."""

    def __init__(self, conn, target_id, session_id):
        self.conn = conn
        self.id = target_id
        self.session = session_id

    async def send(self, method, params=None, *, timeout=30.0):
        return await self.conn.send(method, params, session=self.session, timeout=timeout)

    def on(self, method, handler):
        self.conn.on(method, handler, self.session)

    async def wait_event(self, method, timeout=30.0, predicate=None):
        fut = asyncio.get_running_loop().create_future()

        def handler(params):
            if not fut.done() and (predicate is None or predicate(params)):
                fut.set_result(params)

        self.on(method, handler)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.conn.off(method, handler, self.session)


class Browser:
    """A browser reached through its DevTools websocket."""

    def __init__(self, conn):
        self.conn = conn

    @classmethod
    async def connect(cls, ws_url, *, timeout=10.0):
        return cls(await Connection.open(ws_url, timeout=timeout))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def pages(self):
        info = await self.conn.send("Target.getTargets")
        return [t for t in info["targetInfos"] if t["type"] == "page" and not t["url"].startswith("devtools://")]

    async def attach(self, target_id):
        got = await self.conn.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return Target(self.conn, target_id, got["sessionId"])

    async def new_page(self, url="about:blank"):
        got = await self.conn.send("Target.createTarget", {"url": url})
        return await self.attach(got["targetId"])

    async def first_page(self):
        pages = await self.pages()
        return await self.attach(pages[0]["targetId"]) if pages else await self.new_page()

    async def close_browser(self):
        try:
            await self.conn.send("Browser.close", timeout=5.0)
        except (ConnectionError, TimeoutError):
            pass

    async def close(self):
        await self.conn.close()
