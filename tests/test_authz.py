import http.server
import json
import os
import sys
import threading
import time
from http.cookies import SimpleCookie

import pytest
import pytest_asyncio

from hutch import authz, browser
from hutch.pool import Pool
from hutch.session import Session

_APP = b"""<title>app</title><script>
const h = {Authorization: 'Bearer ' + localStorage.t};
Promise.all(['/api/orders/1', '/api/admin', '/api/me', '/static/app.js'].map(u => fetch(u, {headers: h})))
  .then(() => document.title = 'loaded');
</script>"""


class _App(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        sid = SimpleCookie(self.headers.get("Cookie", "")).get("sid")
        user = sid.value if sid else None
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        path = self.path.split("?")[0]
        if path == "/login":
            name = self.path.split("user=")[1]
            page = f"<script>localStorage.t = 'tok-{name}'; location = '/app'</script>".encode()
            return self._send(200, page, "text/html", [("Set-Cookie", f"sid={name}; Path=/; HttpOnly")])
        if path == "/app":
            return self._send(200, _APP, "text/html")
        if path == "/static/app.js":
            return self._send(200, b"1", "text/javascript")
        if path == "/home":
            if not user:
                return self._send(302, b"", "text/html", [("Location", "/login?next=/home")])
            return self._send(200, f"<h1>Welcome {user}</h1>".encode(), "text/html")
        if not user:
            return self._send(401, {"error": "login required"})
        if path == "/api/orders/1":
            return self._send(200, {"id": 1, "owner": "alice", "card": "4111 **** 1111", "total": 99,
                                    "ts": int(time.time() * 1000)})
        if path == "/api/orders/2":
            return self._send(200, {"id": 2, "owner": "bob"}) if user == "bob" else self._send(404, {})
        if path == "/api/admin":
            if user == "alice" and token == "tok-alice":
                return self._send(200, {"users": ["alice", "bob"], "secret": "admin panel"})
            return self._send(403, {"error": "forbidden"})
        if path == "/api/me":
            if token != f"tok-{user}":
                return self._send(401, {"error": "bad token"})
            return self._send(200, {"user": user, "email": f"{user}@corp.test", "plan": "pro",
                                    "ts": int(time.time() * 1000)})
        return self._send(404, {})

    def do_POST(self):
        self._send(200, {"ok": True})


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _App)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest_asyncio.fixture
async def base(tmp_path):
    yield str(tmp_path / "profiles")
    for name in os.listdir(tmp_path / "profiles") if (tmp_path / "profiles").exists() else []:
        await browser.stop(str(tmp_path / "profiles" / name))


async def _login(pool, name, site):
    s = await pool.create(name, headless=True)
    await s.goto(f"{site}/login?user={name}")
    await s._active_page().wait_for_function("document.title === 'loaded'")
    return s


def test_parse_raw():
    r = authz.parse_raw("POST /api/x?a=1 HTTP/1.1\r\nHost: t.test\r\nAuthorization: Bearer z\r\n\r\n{\"a\":1}")
    assert r == {"method": "POST", "url": "https://t.test/api/x?a=1",
                 "headers": {"Host": "t.test", "Authorization": "Bearer z"}, "body": '{"a":1}'}


@pytest.mark.asyncio
async def test_replay_classifies_each_identity(base, site):
    async with Pool(base_dir=base) as pool:
        await _login(pool, "alice", site)
        await _login(pool, "bob", site)

        idor = await pool.authz("alice", ["bob"], {"method": "GET", "url": f"{site}/api/orders/1"})
        got = {r["as"]: r["verdict"] for r in idor["results"]}
        assert got == {"bob": "bypassed", "anonymous": "enforced"} and idor["verdict"] == "bypassed"

        admin = await pool.authz("alice", ["bob"], {"method": "GET", "url": f"{site}/api/admin"})
        assert admin["baseline"]["status"] == 200, "owner's bearer token was not learned"
        assert {r["as"]: (r["status"], r["verdict"]) for r in admin["results"]} == {
            "bob": (403, "enforced"), "anonymous": (401, "enforced")}

        me = await pool.authz("alice", ["bob"], {"method": "GET", "url": f"{site}/api/me"})
        bob = me["results"][0]
        assert bob["status"] == 200 and '"bob"' in bob["excerpt"] and bob["verdict"] == "check"

        theirs = await pool.authz("bob", ["alice"], {"method": "GET", "url": f"{site}/api/orders/2"})
        assert theirs["verdict"] == "enforced"

        with pytest.raises(ValueError, match="changes state"):
            await pool.authz("alice", ["bob"], {"method": "POST", "url": f"{site}/api/orders/1"})


@pytest.mark.asyncio
async def test_scan_replays_captured_api_calls(base, site):
    async with Pool(base_dir=base) as pool:
        await _login(pool, "alice", site)
        await _login(pool, "bob", site)
        reports = await pool.authz_scan("alice", ["bob"], host="127.0.0.1")
        urls = [r["url"].replace(site, "") for r in reports]
        assert "/static/app.js" not in urls and "/api/admin" in urls
        assert urls[0] == "/api/orders/1" and reports[0]["verdict"] == "bypassed"
        verdicts = {r["url"].replace(site, ""): r["verdict"] for r in reports}
        assert verdicts["/app"] == "public" and verdicts["/api/admin"] == "enforced"
        text = authz.format_reports(reports)
        assert text.startswith("BYPASSED  GET") and "as bob: 200" in text


@pytest.mark.asyncio
async def test_mcp_authz_tool(base, site):
    pytest.importorskip("mcp")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "hutch.mcp"],
                                   env={**os.environ, "HUTCH_BASE_DIR": base})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as client:
        await client.initialize()
        for name in ("alice", "bob"):
            await client.call_tool("hutch_sessions", {"action": "create", "name": name, "headless": True})
            await client.call_tool("hutch_navigate", {"session": name, "url": f"{site}/login?user={name}"})
            await client.call_tool("hutch_navigate", {"session": name, "action": "wait_for",
                                                     "selector": "title:text('loaded')", "state": "attached"})
        res = await client.call_tool("hutch_authz", {"owner": "alice", "as": ["bob"],
                                                     "url": f"{site}/api/orders/1"})
        assert res.content[0].text.startswith("BYPASSED")
        res = await client.call_tool("hutch_authz", {"owner": "alice", "as": ["bob"], "action": "scan"})
        assert "/api/admin" in res.content[0].text
        for name in ("alice", "bob"):
            await client.call_tool("hutch_sessions", {"action": "close", "name": name})


@pytest.mark.asyncio
async def test_login_health_and_keep_alive(base, site, tmp_path):
    login = tmp_path / "login.json"
    login.write_text(json.dumps({"version": 1, "start": None, "steps": [
        {"action": "goto", "url": f"{site}/login?user=alice"},
        {"action": "wait_url", "url": f"{site}/app"}]}))
    async with Pool(base_dir=base) as pool:
        s = await _login(pool, "alice", site)
        with pytest.raises(RuntimeError, match="no login probe"):
            await s.auth_check()
        s.set_auth(f"{site}/home", expect="Welcome", login=str(login))
        assert (await s.auth_check())["state"] == "in"
        tabs = len(s._pages)

        await s._context.clear_cookies()
        out = await s.auth_check()
        assert out["state"] == "out" and "/login" in out["reason"]
        assert s.events.since(0, {"auth"})[-1]["state"] == "out"

        back = await s.keep_alive()
        assert back["state"] == "in" and back["relogin"]["ok"]
        assert len([p for p in s._pages if not p.is_closed()]) == tabs

        s.set_auth(f"{site}/api/me")
        assert (await s.auth_check())["state"] == "in", "learned bearer token not sent"
        s.set_auth(f"{site}/api/me", expect="bob@corp.test")
        assert (await s.auth_check())["reason"] == "'bob@corp.test' missing from the probe"
        found = await s.find_probe()
        assert found["probe"] == f"{site}/api/me" and found["why"] == "200 with this session, 401 without"
        assert s.auth["probe"] == f"{site}/api/me" and s.auth["login"] == str(login)
        s.set_auth(f"{site}/api/me", expect="bob@corp.test")
        await s.auth_check()
    reloaded = Session.from_profile_dir(os.path.join(base, "alice"))
    assert reloaded.auth["probe"].endswith("/api/me") and reloaded.auth["last"]["state"] == "out"
