import http.server
import json
import os
import stat
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from hutch import browser, recorder, secrets
from hutch.client import HutchClient
from hutch.pool import Pool
from hutch.server import HutchDaemon

_LOGIN = b"""<title>login</title>
<form action="/home" method="get">
  <label>Email <input name=email></label>
  <label>Password <input name=pw type=password></label>
  <label><input type=checkbox name=remember> Remember me</label>
  <select name=plan><option>free</option><option>pro</option></select>
  <button>Sign in</button>
</form>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if self.path.startswith("/home"):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            self.wfile.write(f"<title>home</title><pre id=got>{json.dumps(q)}</pre>".encode())
        else:
            self.wfile.write(_LOGIN)


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("HUTCH_SECRETS", str(tmp_path / "secrets.json"))
    yield tmp_path / "secrets.json"


@pytest_asyncio.fixture
async def base(tmp_path):
    yield str(tmp_path / "profiles")
    for name in os.listdir(tmp_path / "profiles") if (tmp_path / "profiles").exists() else []:
        await browser.stop(str(tmp_path / "profiles" / name))


def test_vault_file_resolve_domains_and_redaction(vault):
    secrets.set_secret("pw", 'hunter"2', ["Example.com"])
    secrets.set_secret("token", "tok-123456")
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(vault).st_mode) == 0o600
    assert secrets.listing() == [{"name": "pw", "domains": ["example.com"]},
                                 {"name": "token", "domains": []}]
    assert secrets.resolve("{{pw}}", "https://login.example.com/x") == 'hunter"2'
    assert secrets.resolve("Bearer {{ token }}", "http://anything") == "Bearer tok-123456"
    with pytest.raises(secrets.SecretError, match="not allowed on evil.com"):
        secrets.resolve("{{pw}}", "https://evil.com/example.com")
    with pytest.raises(secrets.SecretError, match="hutch secret set nope"):
        secrets.resolve("{{nope}}", "https://example.com")
    assert secrets.resolve("plain {text}", "x") == "plain {text}"
    out = secrets.redact(json.dumps({"a": 'pw is hunter"2', "b": "tok-123456"}))
    assert "hunter" not in out and "tok-123456" not in out and out.count("{{") == 2
    assert secrets.placeholder_for("tok-123456") == "{{token}}"
    secrets.remove("token")
    with pytest.raises(secrets.SecretError):
        secrets.remove("token")


@pytest.mark.asyncio
async def test_agents_type_secrets_but_never_see_them(base, site, tmp_path):
    secrets.set_secret("email", "abu@corp.test", ["127.0.0.1"])
    secrets.set_secret("elsewhere", "nope-value", ["example.com"])
    sock = str(tmp_path / "d.sock")
    d = HutchDaemon(base_dir=base, idle_timeout=0, sock_path=sock)
    await d.start()
    c = HutchClient(sock_path=sock)
    await c.connect()
    try:
        s = await c.create("vault", headless=True)
        await s.goto(site)
        state = await s.fill("input[name=email]", "{{email}}")
        assert "abu@corp.test" not in json.dumps(state) and "{{email}}" in state["changes"]
        assert await s.evaluate("document.querySelector('[name=email]').value") == "{{email}}"
        with pytest.raises(Exception, match="not allowed on 127.0.0.1"):
            await s.fill("input[name=pw]", "{{elsewhere}}")
        assert await s.evaluate("document.querySelector('[name=pw]').value") == ""
    finally:
        await c.close()
        await d.stop()


@pytest.mark.asyncio
async def test_record_replay_and_export(base, site):
    secrets.set_secret("pw", "s3cret-pass")
    async with Pool(base_dir=base) as pool:
        s = await pool.create("rec", headless=True)
        await s.goto(site)
        await s.start_recording()
        page = s._active_page()
        await page.fill("input[name=email]", "abu@corp.test")
        await page.fill("input[name=pw]", "s3cret-pass")
        await page.check("input[name=remember]")
        await page.select_option("select", "pro")
        await page.click("button")
        await page.wait_for_url("**/home**")
        rec = s.stop_recording()
        actions = [(st["action"], st.get("value")) for st in rec["steps"]]
        assert actions == [("fill", "abu@corp.test"), ("fill", "{{pw}}"), ("check", None),
                           ("select", "pro"), ("click", None), ("wait_url", None)]
        assert rec["start"] == site + "/"
        assert all("t" not in st for st in rec["steps"])

        await s.goto("about:blank")
        result = await s.replay(rec)
        assert result["ok"], result
        got = json.loads(await s.inner_text("#got"))
        assert got == {"email": "abu@corp.test", "pw": "s3cret-pass", "remember": "on", "plan": "pro"}

        secrets.remove("pw")
        failed = await s.replay(rec)
        assert not failed["ok"] and failed["done"] == 1 and "secret 'pw' is not set" in failed["error"]

    code = recorder.to_python(rec)
    compile(code, "export.py", "exec")
    assert 'os.environ["HUTCH_SECRET_PW"]' in code and "s3cret" not in code


@pytest.mark.asyncio
async def test_password_without_vault_entry_is_never_stored(base, site):
    async with Pool(base_dir=base) as pool:
        s = await pool.create("pw", headless=True)
        await s.goto(site)
        await s.start_recording()
        await s._active_page().fill("input[name=pw]", "typed-by-hand")
        await s._active_page().press("input[name=pw]", "Enter")
        rec = s.stop_recording()
        assert "typed-by-hand" not in json.dumps(rec)
        assert rec["steps"][0] == {"action": "fill", "selector": rec["steps"][0]["selector"],
                                   "value": "{{password}}", "url": site + "/"}
        assert [st["action"] for st in rec["steps"]][:2] == ["fill", "press"]


def test_cli_secret_commands(tmp_path):
    env = {**os.environ, "HUTCH_SECRETS": str(tmp_path / "v.json")}
    run = lambda *a, input=None: subprocess.run([sys.executable, "-m", "hutch.cli", *a], input=input,
                                                capture_output=True, text=True, env=env, timeout=30)
    assert run("secret", "set", "gh", "--domain", "github.com", input="tok\n").stdout == "saved: gh\n"
    assert "gh" in run("secret", "list").stdout and "github.com" in run("secret", "list").stdout
    assert "tok" not in run("secret", "list").stdout
    assert run("secret", "rm", "gh").returncode == 0
    assert run("secret", "rm", "gh").stderr.startswith("hutch: no secret")
