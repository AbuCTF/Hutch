import dataclasses
import json
import os
import subprocess
import sys

import pytest
import pytest_asyncio

from hutch import browser, commands
from hutch.client import HutchClient
from hutch.server import HutchDaemon
from hutch.session import SessionState


def test_registry_exposes_session_commands_only():
    names = commands.names()
    for n in ("click", "type", "type_text", "mouse_down", "keyboard_insert_text", "set_cookies",
              "websocket_log", "aria_snapshot", "arm_dialog"):
        assert n in names
    for n in ("launch", "close", "intercept", "expose_function", "spawn_options", "_ref"):
        assert n not in names and not commands.exists(n)
    sig = commands.describe("click")[0]["signature"]
    assert sig == "click(selector, *, wait_after='networkidle', timeout=5000)"
    assert commands.describe("type")[0]["signature"].startswith("type(selector, text")


def test_plain_makes_results_json_safe():
    @dataclasses.dataclass
    class D:
        a: bytes
        b: tuple

    out = commands.plain({"d": D(b"\x89PNG", (1, SessionState.ACTIVE)), 2: {3}})
    assert out == {"d": {"a": {"base64": "iVBORw=="}, "b": [1, "active"]}, "2": [3]}
    json.dumps(out)


@pytest.mark.asyncio
async def test_call_checks_arguments_against_the_signature():
    class Fake:
        async def click(self, selector, *, wait_after="networkidle", timeout=5000):
            return selector

    assert await commands.call(Fake(), "click", {"selector": "e1"}) == "e1"
    assert await commands.call(Fake(), "click", positional=["e2"]) == "e2"
    with pytest.raises(ValueError, match="click\\(selector"):
        await commands.call(Fake(), "click", {"bogus": 1})
    with pytest.raises(KeyError):
        await commands.call(Fake(), "launch", {})


@pytest_asyncio.fixture
async def daemon(tmp_path):
    sock = str(tmp_path / "d.sock")
    d = HutchDaemon(base_dir=str(tmp_path / "profiles"), idle_timeout=0, sock_path=sock)
    await d.start()
    c = HutchClient(sock_path=sock)
    await c.connect()
    yield c
    await c.close()
    await d.stop()


@pytest.mark.asyncio
async def test_daemon_routes_through_the_registry(daemon):
    s = await daemon.create("reg", headless=True)
    await s.set_content("<input id=q><button onclick=\"document.title='hit'\">b</button>")
    state = await s.fill("#q", "abc")
    assert "abc" in state["changes"]
    await s.type("#q", "d", delay=0)
    assert await s.input_value("#q") == "abcd"
    await s.keyboard_insert_text(text="!")
    assert (await s.evaluate("q.value")) == "abcd!"
    await s.mouse_move(x=5, y=5)
    await s.click("button")
    assert await s.evaluate("document.title") == "hit"
    rows = await daemon.commands()
    assert any(r["name"] == "mouse_down" for r in rows)
    with pytest.raises(Exception, match="unknown"):
        await s.frobnicate()


def test_cli_call(tmp_path):
    base = str(tmp_path / "profiles")
    run = lambda *a: subprocess.run([sys.executable, "-m", "hutch.cli", "--base-dir", base, *a],
                                    capture_output=True, text=True, timeout=90)
    try:
        assert "mouse_down(" in run("call", "help").stdout
        assert run("create", "c").returncode == 0
        out = run("call", "c", "set_content", "<h1>hi</h1><p>x</p>")
        assert out.returncode == 0, out.stderr
        assert run("call", "c", "inner_text", "h1").stdout.strip() == "hi"
        assert json.loads(run("call", "c", "evaluate", "[1, 2]").stdout) == [1, 2]
        bad = run("call", "c", "click", "wait_after=load")
        assert bad.returncode == 1 and bad.stderr.startswith("hutch: click(selector")
        assert browser.status(os.path.join(base, "c"))
    finally:
        run("stop", "--all")


@pytest.mark.asyncio
async def test_mcp_call_tool(tmp_path):
    pytest.importorskip("mcp")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    base = str(tmp_path / "profiles")
    params = StdioServerParameters(command=sys.executable, args=["-m", "hutch.mcp"],
                                   env={**os.environ, "HUTCH_BASE_DIR": base})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as client:
        await client.initialize()
        res = await client.call_tool("hutch_call", {"command": "help"})
        assert "keyboard_insert_text(text)" in res.content[0].text
        await client.call_tool("hutch_sessions", {"action": "create", "name": "m", "headless": True})
        await client.call_tool("hutch_call", {"session": "m", "command": "set_content",
                                              "args": {"html": "<input id=q autofocus>"}})
        await client.call_tool("hutch_call", {"session": "m", "command": "keyboard_insert_text",
                                              "args": {"text": "typed"}})
        res = await client.call_tool("hutch_call", {"session": "m", "command": "input_value",
                                                    "args": {"selector": "#q"}})
        assert res.content[0].text.strip('"') == "typed"
        res = await client.call_tool("hutch_call", {"session": "m", "command": "screenshot"})
        assert res.content[0].type == "image"
        res = await client.call_tool("hutch_call", {"session": "m", "command": "click", "args": {"x": 1}})
        assert "error" in res.content[0].text and "click(selector" in res.content[0].text
        await client.call_tool("hutch_sessions", {"action": "close", "name": "m"})
