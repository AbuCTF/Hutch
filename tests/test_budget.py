import asyncio
import http.server
import os
import threading
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from hutch import browser, gc
from hutch.artifacts import ArtifactStore
from hutch.context import Context, NetworkEntry, _PENDING_MAX
from hutch.pool import Pool


def _entry(seq, body):
    return NetworkEntry(seq=seq, method="GET", url=f"http://x/{seq}", response_body=body)


def test_bodies_stay_within_the_byte_budget():
    ctx = Context(network_cap=5, body_budget=1000)
    for i in range(10):
        ctx._add_network(_entry(i, "x" * 300))
        assert ctx.body_bytes <= 1000
    entries = ctx.network.all()
    assert [e.seq for e in entries] == [5, 6, 7, 8, 9]
    assert entries[-1].response_body and entries[0].response_body is None
    assert ctx.body_bytes == sum(len(e.response_body or "") for e in entries)
    ctx.clear()
    assert ctx.body_bytes == 0


class _Request:
    post_data, method, headers, resource_type = None, "GET", {}, "fetch"

    def __init__(self, url):
        self.url = url


def test_pending_requests_and_console_are_bounded():
    ctx = Context()
    for i in range(_PENDING_MAX + 50):
        ctx._on_request(_Request(f"http://x/{i}"))
    assert len(ctx._pending) == _PENDING_MAX
    ctx._on_console(SimpleNamespace(type="log", text="y" * 100_000, location={}))
    assert len(ctx.console.all()[0].text) < 20_000


class _Big(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"v": "' + b"a" * 500_000 + b'"}' if self.path.startswith("/big") else \
            b"<title>t</title>"
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path.startswith("/big") else "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Big)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest_asyncio.fixture
async def base(tmp_path):
    yield str(tmp_path / "profiles")
    for name in os.listdir(tmp_path / "profiles") if (tmp_path / "profiles").exists() else []:
        await browser.stop(str(tmp_path / "profiles" / name))


@pytest.mark.asyncio
async def test_live_capture_respects_the_budget(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("big", headless=True)
        s.context._body_budget = 2_000_000
        await s.goto(site)
        await s.evaluate("Promise.all([...Array(12).keys()].map(i => fetch('/big?' + i).then(r => r.text())))")
        for _ in range(50):
            if len([e for e in s.context.network.all() if "/big" in e.url]) == 12:
                break
            await asyncio.sleep(0.1)
        big = [e for e in s.context.network.all() if "/big" in e.url]
        assert len(big) == 12
        assert s.context.body_bytes <= 2_000_000
        assert big[-1].response_body and sum(1 for e in big if e.response_body is None) >= 8


def test_artifacts_keep_the_newest(tmp_path):
    store = ArtifactStore(base_dir=str(tmp_path), keep=3)
    for i in range(5):
        store.save_screenshot("s", b"png%d" % i, label=f"n{i}")
        time.sleep(0.01)
    files = sorted(os.listdir(tmp_path / "s" / "screenshots"))
    assert len(files) == 3 and all(f.endswith(("n2.png", "n3.png", "n4.png")) for f in files)


def test_gc_plans_and_removes_without_touching_running_profiles(tmp_path, monkeypatch, capsys):
    base, arts = tmp_path / "profiles", tmp_path / "artifacts"
    old = time.time() - 40 * 86400
    for name in ("stopped", "running"):
        p = base / name
        (p / "Default" / "Cache").mkdir(parents=True)
        (p / "Default" / "Cache" / "blob").write_bytes(b"x" * 2048)
        (p / "Default" / "Cookies").write_text("keep")
        (p / "DevToolsActivePort").write_text("1\n/x")
        (p / "downloads").mkdir()
        (p / "downloads" / "old.bin").write_text("old")
        os.utime(p / "downloads" / "old.bin", (old, old))
        (p / "downloads" / "new.bin").write_text("new")
    (arts / "s" / "screenshots").mkdir(parents=True)
    (arts / "s" / "screenshots" / "a.png").write_text("a")
    os.utime(arts / "s" / "screenshots" / "a.png", (old, old))
    (arts / "s" / "notes.json").write_text("{}")
    os.utime(arts / "s" / "notes.json", (old, old))
    monkeypatch.setattr(browser, "status", lambda p: {"pid": 1} if p.endswith("running") else None)

    items = gc.run(str(base), str(arts), dry_run=True)
    assert {(k, os.path.relpath(p, tmp_path).replace(os.sep, "/")) for k, p, _ in items} == {
        ("artifacts", "artifacts/s/screenshots/a.png"),
        ("downloads", "profiles/stopped/downloads/old.bin"),
        ("downloads", "profiles/running/downloads/old.bin"),
        ("stale state", "profiles/stopped/DevToolsActivePort"),
    }
    assert (arts / "s" / "screenshots" / "a.png").exists()

    items = gc.run(str(base), str(arts), caches=True)
    assert ("caches", str(base / "stopped" / "Default" / "Cache"), 2048) in items
    assert not (base / "stopped" / "Default" / "Cache").exists()
    assert (base / "running" / "Default" / "Cache").exists()
    assert (base / "stopped" / "Default" / "Cookies").read_text() == "keep"
    assert (base / "stopped" / "downloads" / "new.bin").exists()
    assert (arts / "s" / "notes.json").exists() and not (arts / "s" / "screenshots").exists()
    assert "freed" in capsys.readouterr().out


def test_disk_cache_is_capped():
    assert any(a.startswith("--disk-cache-size=") for a in browser.build_args("/p"))
