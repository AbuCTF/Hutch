import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest
import pytest_asyncio

from hutch import browser
from hutch.pool import Pool


@pytest_asyncio.fixture
async def base(tmp_path):
    yield str(tmp_path / "profiles")
    for name in os.listdir(tmp_path / "profiles") if (tmp_path / "profiles").exists() else []:
        await browser.stop(str(tmp_path / "profiles" / name))


def test_strict_args_route_loopback_through_the_proxy():
    args = browser.build_args("/p", proxy="http://127.0.0.1:8080", proxy_bypass="*.internal")
    assert "--proxy-bypass-list=<-loopback>;*.internal" in args and "--disable-background-networking" in args
    loose = browser.build_args("/p", proxy="http://127.0.0.1:8080", strict_proxy=False)
    assert not any("loopback" in a or "background-networking" in a for a in loose)
    assert not any("proxy" in a for a in browser.build_args("/p"))


@pytest.mark.asyncio
async def test_unreachable_proxy_fails_closed(base):
    async with Pool(base_dir=base) as pool:
        with pytest.raises(browser.BrowserError, match="unreachable"):
            await pool.create("closed", headless=True, proxy="http://127.0.0.1:9")
    assert browser.status(os.path.join(base, "closed")) is None


@pytest.mark.asyncio
async def test_required_proxy_and_proxyless_browser_are_refused(base):
    async with Pool(base_dir=base) as pool:
        with pytest.raises(RuntimeError, match="requires a proxy"):
            await pool.create("needs", headless=True, require_proxy=True)
        await pool.create("plain", headless=True)
    seen = []

    async def proxy(reader, writer):
        seen.append(await reader.readline())
        writer.close()
    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    profile = os.path.join(base, "plain")
    st = browser.spawn(profile, headless=True)
    try:
        from hutch.session import ProxyConfig, Session
        s = Session.from_profile_dir(profile)
        s.proxy = ProxyConfig(server=f"http://127.0.0.1:{port}")
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            with pytest.raises(RuntimeError, match="without its proxy"):
                await s.launch(pw)
    finally:
        server.close()
        await browser.stop(profile)


@pytest.mark.asyncio
async def test_page_cannot_reach_loopback_around_the_proxy(base):
    direct, proxied = [], []

    async def local(reader, writer):
        direct.append(await reader.readline())
        writer.close()

    async def proxy(reader, writer):
        line = await reader.readline()
        proxied.append(line.decode())
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
    local_srv = await asyncio.start_server(local, "127.0.0.1", 0)
    proxy_srv = await asyncio.start_server(proxy, "127.0.0.1", 0)
    lport, pport = local_srv.sockets[0].getsockname()[1], proxy_srv.sockets[0].getsockname()[1]
    try:
        async with Pool(base_dir=base) as pool:
            s = await pool.create("strict", headless=True, proxy=f"http://127.0.0.1:{pport}")
            await s.goto("http://page.test/")
            await s.evaluate(f"fetch('http://127.0.0.1:{lport}/x').catch(() => 0)")
            await asyncio.sleep(1)
            assert {h["host"] for h in s.hosts()} >= {"page.test", "127.0.0.1"}
    finally:
        local_srv.close()
        proxy_srv.close()
    assert direct == [] and any(f"127.0.0.1:{lport}/x" in p for p in proxied)


def _netns():
    if not sys.platform.startswith("linux") or not shutil.which("unshare") or not shutil.which("ip"):
        return False
    cmd = ["unshare", "--user", f"--map-user={os.getuid()}", f"--map-group={os.getgid()}", "--keep-caps",
           "--net", "ip", "link", "set", "lo", "up"]
    return subprocess.run(cmd, capture_output=True).returncode == 0


@pytest.mark.skipif(not _netns(), reason="needs unprivileged network namespaces")
def test_nothing_leaves_except_through_the_proxy(tmp_path):
    """In an empty network namespace every DNS query is a connection that skipped the proxy."""
    probe = os.path.join(os.path.dirname(__file__), "data", "leak_probe.py")
    ns = next((l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")), "127.0.0.53")
    script = (f"ip link set lo up && (ip addr add {ns}/32 dev lo || true) && "
              f"{sys.executable} {probe} {tmp_path} strict")
    subprocess.run(["unshare", "--user", f"--map-user={os.getuid()}", f"--map-group={os.getgid()}",
                    "--keep-caps", "--net", "bash", "-c", script], timeout=120, check=True)
    result = json.load(open(tmp_path / "strict.json"))
    assert result["dns"] == [] and result["direct"] == []
    assert any("page.leak.test" in p for p in result["proxied"])
