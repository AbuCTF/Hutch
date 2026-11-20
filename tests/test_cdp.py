import asyncio
import json

import pytest
from aiohttp import web

from hutch.cdp import Browser, CDPError


async def _server(handler):
    app = web.Application()

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            await handler(ws, json.loads(msg.data))
        return ws

    app.router.add_get("/devtools/browser/x", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"ws://127.0.0.1:{port}/devtools/browser/x"


@pytest.mark.asyncio
async def test_commands_are_matched_to_answers_and_errors_raise():
    async def handler(ws, msg):
        if msg["method"] == "Boom":
            await ws.send_json({"id": msg["id"], "error": {"code": -32000, "message": "no way"}})
        else:
            await ws.send_json({"id": msg["id"], "result": {"echo": msg["params"], "session": msg.get("sessionId")}})

    runner, url = await _server(handler)
    try:
        async with await Browser.connect(url) as b:
            got = await asyncio.gather(b.conn.send("A", {"n": 1}), b.conn.send("B", {"n": 2}, session="s1"))
            assert got[0]["echo"] == {"n": 1} and got[1]["session"] == "s1"
            with pytest.raises(CDPError, match="no way"):
                await b.conn.send("Boom")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_events_reach_the_session_they_belong_to():
    async def handler(ws, msg):
        await ws.send_json({"id": msg["id"], "result": {}})
        await ws.send_json({"method": "Page.loadEventFired", "params": {"ts": 1}, "sessionId": "mine"})
        await ws.send_json({"method": "Page.loadEventFired", "params": {"ts": 2}, "sessionId": "other"})

    runner, url = await _server(handler)
    try:
        async with await Browser.connect(url) as b:
            from hutch.cdp import Target
            target = Target(b.conn, "t", "mine")
            waiter = asyncio.ensure_future(target.wait_event("Page.loadEventFired", timeout=2))
            await asyncio.sleep(0.05)
            await target.send("Page.reload")
            assert (await waiter) == {"ts": 1}
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_silent_browser_times_out_and_a_closed_one_fails_pending_commands():
    async def handler(ws, msg):
        if msg["method"] == "Die":
            await ws.close()

    runner, url = await _server(handler)
    try:
        b = await Browser.connect(url)
        with pytest.raises(TimeoutError):
            await b.conn.send("Silent", timeout=0.2)
        with pytest.raises(ConnectionError):
            await b.conn.send("Die", timeout=2)
        with pytest.raises(ConnectionError):
            await b.conn.send("After")
        await b.close()
    finally:
        await runner.cleanup()


def test_the_client_never_enables_runtime_or_console():
    import inspect
    from hutch import cdp
    src = inspect.getsource(cdp)
    assert '"Runtime.enable"' not in src and '"Console.enable"' not in src
