import asyncio
import json
import os
import stat
import sys

import pytest

from hutch import browser, server
from hutch.client import HutchClient


@pytest.mark.asyncio
async def test_tcp_transport_needs_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_TCP", True)
    endpoint = str(tmp_path / "hutch.endpoint")
    d = server.HutchDaemon(base_dir=str(tmp_path / "profiles"), idle_timeout=0, sock_path=endpoint)
    await d.start()
    try:
        info = json.load(open(endpoint))
        assert info["pid"] == os.getpid()
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(endpoint).st_mode) == 0o600
        async with HutchClient(sock_path=endpoint) as c:
            assert (await c.ping())["pong"] is True

        reader, writer = await asyncio.open_connection("127.0.0.1", info["port"])
        writer.write(b'{"auth": "wrong"}\n{"id": 1, "method": "ping"}\n')
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
    finally:
        await d.stop()
    assert not os.path.exists(endpoint)


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGUSR1")
@pytest.mark.asyncio
async def test_signal_fallback_runs_on_the_loop(monkeypatch):
    import signal
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a: (_ for _ in ()).throw(NotImplementedError))
    hit = asyncio.Event()
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        browser.on_signal(hit.set, signal.SIGUSR1)
        os.kill(os.getpid(), signal.SIGUSR1)
        await asyncio.wait_for(hit.wait(), 2)
    finally:
        signal.signal(signal.SIGUSR1, previous)


@pytest.mark.skipif(sys.platform != "win32", reason="windows lock file")
def test_windows_lockfile_is_not_a_running_browser(tmp_path):
    (tmp_path / "lockfile").write_text("")
    assert browser.lock_pid(str(tmp_path)) is None
