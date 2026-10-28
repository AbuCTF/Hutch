import argparse
import asyncio
import json
import os
import re
import shutil
import sys

from . import __version__, browser
from .fingerprint import generate, generate_for_program, list_presets
from .pool import Pool, _DEFAULT_BASE_DIR
from .session import ProxyConfig, Session

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_CAIDO_DEFAULT_PORT = 8080


def _validate_name(name):
    if not name or not _NAME_RE.match(name):
        raise SystemExit(
            f"hutch: invalid session name '{name}', use letters, digits, '-', '_' or '.'"
        )
    return name


def _parse_tag(value):
    if "=" not in value:
        raise SystemExit(f"hutch: invalid tag '{value}', expected key=value")
    k, _, v = value.partition("=")
    return k, v


def _resolve_proxy(args):
    if getattr(args, "caido", None) is not None:
        port = args.caido
        if getattr(args, "proxy", None) and not args.proxy.endswith(f":{port}"):
            raise SystemExit("hutch: --caido and --proxy are mutually exclusive")
        args.proxy = f"http://127.0.0.1:{port}"
        args.ignore_https_errors = True
    proxy = ProxyConfig(server=args.proxy, bypass=getattr(args, "bypass", None)) if getattr(args, "proxy", None) else None
    return proxy


def _base_dir(args):
    return args.base_dir or _DEFAULT_BASE_DIR


def _profiles(args):
    base = _base_dir(args)
    if not os.path.isdir(base):
        return {}
    out = {}
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        if os.path.isfile(os.path.join(d, "hutch_meta.json")):
            try:
                out[name] = Session.from_profile_dir(d)
            except Exception:
                continue
    return out


def _load(args, name, *, create=False):
    """Load a profile, creating it (no browser launch) when allowed."""
    _validate_name(name)
    sessions = _profiles(args)
    if name in sessions:
        s = sessions[name]
        changed = False
        if getattr(args, "browser", None) and args.browser != s.browser:
            if not getattr(args, "force", False):
                raise SystemExit(
                    f"hutch: {name} uses {s.browser}, switching can corrupt the profile. "
                    "Use --force or hutch clone")
            s.browser, changed = args.browser, True
        if getattr(args, "proxy", None) or getattr(args, "caido", None) is not None:
            s.proxy = _resolve_proxy(args)
            s.ignore_https_errors = s.ignore_https_errors or args.ignore_https_errors
            changed = True
        if changed:
            s._save_meta()
        return s
    if not create:
        raise SystemExit(f"hutch: no session named '{name}'")
    fp = None
    if getattr(args, "preset", None):
        fp = generate(preset=args.preset, locale=getattr(args, "locale", None),
                      timezone=getattr(args, "timezone", None))
    elif getattr(args, "program", None):
        fp = generate_for_program(args.program, locale=getattr(args, "locale", None),
                                  timezone=getattr(args, "timezone", None))
    tags = {}
    if getattr(args, "program", None):
        tags["program"] = args.program
    for t in getattr(args, "tag", None) or []:
        k, v = _parse_tag(t)
        tags[k] = v
    s = Session(
        name, os.path.join(_base_dir(args), name),
        proxy=_resolve_proxy(args) if hasattr(args, "proxy") else None,
        fingerprint=fp,
        headless=args.headless if getattr(args, "headless", None) is not None
        else not getattr(args, "headed", False),
        ignore_https_errors=getattr(args, "ignore_https_errors", False),
        tags=tags,
        browser=getattr(args, "browser", None) or "chromium",
    )
    print(f"created: {name}")
    return s


def _launch(s, *, headless=None, url=None, debug=True):
    if headless is not None and headless != s.headless:
        s.headless = headless
        s._save_meta()
    kw = s.spawn_options()
    try:
        return browser.spawn(s.profile_dir, debug=debug, url=url, **kw)
    except browser.BrowserError as e:
        raise SystemExit(f"hutch: {e}")


async def _open_tab(st, url):
    await browser.cdp_call(st["ws"], "Target.createTarget", {"url": url or "about:blank"})


def _mode(st):
    if st.get("headless") is None:
        return "?"
    return "headless" if st["headless"] else "headed"


async def cmd_browse(_pool, args):
    s = _load(args, args.name, create=True)
    st = browser.status(s.profile_dir)
    if st:
        if not st["ws"]:
            raise SystemExit(f"hutch: {s.name} is open in login mode, close that window first")
        if args.url:
            await _open_tab(st, args.url)
            print(f"{s.name}: opened {args.url}")
        else:
            print(f"{s.name}: already running {_mode(st)}")
        return
    st = _launch(s, headless=args.headless, url=args.url)
    print(f"{s.name}: running {_mode(st)}, pid {st['pid']}")
    if args.wait:
        await browser.wait_exit(s.profile_dir, st["pid"], timeout=float("inf"))


async def cmd_login(_pool, args):
    s = _load(args, args.name, create=True)
    if browser.status(s.profile_dir):
        raise SystemExit(f"hutch: {s.name} is running, stop it first")
    st = _launch(s, headless=False, url=args.url, debug=False)
    print(f"{s.name}: sign in, then close the window to save")
    try:
        st["process"].wait()
    except KeyboardInterrupt:
        st["process"].terminate()
        st["process"].wait()
    print(f"saved: {s.name}")


async def cmd_stop(_pool, args):
    names = list(_profiles(args)) if args.all else [args.name]
    if not args.all and not args.name:
        raise SystemExit("hutch: give a session name or --all")
    stopped = 0
    for name in names:
        s = _load(args, name)
        if await browser.stop(s.profile_dir):
            stopped += 1
            print(f"stopped: {name}")
    if not stopped:
        print("nothing running")


async def cmd_ps(_pool, args):
    rows = []
    for name, s in _profiles(args).items():
        st = browser.status(s.profile_dir)
        if st:
            mem = browser.memory_mb(st["pid"])
            rows.append((name, str(st["pid"] or "?"), _mode(st), s.browser,
                         str(st["port"] or "-"), f"{mem} MB" if mem is not None else "?"))
    if not rows:
        print("no running browsers")
        return
    fmt = "{:<20} {:<8} {:<9} {:<9} {:<7} {}"
    print(fmt.format("NAME", "PID", "MODE", "BROWSER", "PORT", "MEMORY"))
    for r in rows:
        print(fmt.format(_truncate(r[0], 20), *r[1:]))


async def _switch_mode(args, headless):
    s = _load(args, args.name)
    st = browser.status(s.profile_dir)
    if st and not st["ws"]:
        raise SystemExit(f"hutch: {s.name} is open in login mode, close that window first")
    if st and st.get("headless") == headless:
        print(f"{s.name}: already {_mode(st)}")
        return
    if st:
        await browser.stop(s.profile_dir)
    st = _launch(s, headless=headless)
    print(f"{s.name}: running {_mode(st)}, pid {st['pid']}")


async def cmd_headed(_pool, args):
    await _switch_mode(args, False)


async def cmd_headless(_pool, args):
    await _switch_mode(args, True)


async def cmd_clone(_pool, args):
    src = _load(args, args.src)
    _validate_name(args.dst)
    dst_dir = os.path.join(_base_dir(args), args.dst)
    try:
        browser.clone_profile(src.profile_dir, dst_dir)
    except browser.BrowserError as e:
        raise SystemExit(f"hutch: {e}")
    s = Session.from_profile_dir(dst_dir)
    s.name = args.dst
    s._save_meta()
    print(f"cloned: {args.src} -> {args.dst}")


async def cmd_tabs(_pool, args):
    if args.name in _profiles(args):
        st = browser.status(_load(args, args.name).profile_dir)
        ws = st and st["ws"]
    else:
        try:
            ws = browser.resolve_endpoint(args.name)
        except browser.BrowserError as e:
            raise SystemExit(f"hutch: {e}")
    if not ws:
        raise SystemExit(f"hutch: {args.name} is not running")
    targets = (await browser.cdp_call(ws, "Target.getTargets"))["targetInfos"]
    pages = [t for t in targets if t["type"] == "page"]
    for i, t in enumerate(pages):
        print(f"[{i}] {_truncate(t.get('title') or '-', 40):<40} {t['url']}")
    if not pages:
        print("no tabs")


async def cmd_attach(_pool, args):
    try:
        ws = browser.resolve_endpoint(args.target)
    except browser.BrowserError as e:
        raise SystemExit(f"hutch: {e}")
    targets = (await browser.cdp_call(ws, "Target.getTargets"))["targetInfos"]
    pages = [t for t in targets if t["type"] == "page"]
    print(f"{args.target}: {len(pages)} tabs, {ws}")
    if args.name:
        _validate_name(args.name)
        Session(args.name, os.path.join(_base_dir(args), args.name), endpoint=args.target)
        print(f"registered: {args.name}")


async def cmd_create(_pool, args):
    s = _load(args, args.name, create=True)
    fp = s.fingerprint
    if s.persona:
        print(f"  fingerprint: {fp.viewport_width}x{fp.viewport_height} "
              f"{fp.platform or 'default'} {fp.locale} {fp.timezone}")
    if s.proxy:
        label = "caido" if getattr(args, "caido", None) is not None else "proxy"
        print(f"  {label}: {s.proxy.server}")
    if args.url or args.headed:
        st = _launch(s, headless=not args.headed, url=args.url)
        print(f"  running: {_mode(st)} pid {st['pid']}")


def _truncate(s, width):
    if len(s) <= width:
        return s
    return s[:width - 1] + "…"


async def cmd_list(_pool, args):
    sessions = _profiles(args)
    rows = []
    for name, s in sessions.items():
        st = None if s.endpoint else browser.status(s.profile_dir)
        if args.alive and not st:
            continue
        rows.append((name, _mode(st) if st else "stopped", s.browser if not s.endpoint else "external",
                     s.proxy.server if s.proxy else "direct", "persona" if s.persona else "real"))
    if not rows:
        print("no sessions")
        return
    fmt = "{:<20} {:<9} {:<9} {:<20} {}"
    print(fmt.format("NAME", "STATUS", "BROWSER", "PROXY", "IDENTITY"))
    for r in rows:
        print(fmt.format(_truncate(r[0], 20), r[1], r[2], _truncate(r[3], 20), r[4]))


async def cmd_status(_pool, args):
    s = _load(args, args.name)
    info = s.status()
    st = None if s.endpoint else browser.status(s.profile_dir)
    if st:
        info.update(alive=True, pid=st["pid"], port=st["port"], headless=st.get("headless"),
                    memory_mb=browser.memory_mb(st["pid"]))
    info.pop("owned", None)
    print(json.dumps(info, indent=2))


async def cmd_open(_pool, args):
    args.headless = False
    args.wait = False
    await cmd_browse(None, args)


def _arg(value):
    """Numbers, true, false and null become JSON values; everything else stays a string."""
    if value in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", value):
        return json.loads(value)
    return value


async def cmd_call(pool, args):
    from . import commands
    if args.name == "help" or args.cmd in (None, "help"):
        for row in commands.describe(args.cmd if args.cmd not in (None, "help") else None):
            print(f"{row['signature']}  {row['doc']}".rstrip())
        return
    known = commands.parameters(args.cmd)
    positional, kw = [], {}
    for token in args.args:
        key, eq, value = token.partition("=")
        if eq and key.endswith(":") and key[:-1] in known:
            kw[key[:-1]] = json.loads(value)
        elif eq and key in known:
            kw[key] = _arg(value)
        else:
            positional.append(_arg(token))
    s = _load(args, args.name)
    if not s.endpoint and not browser.status(s.profile_dir):
        _launch(s, headless=True)
    s = await pool.get(args.name, launch=True)
    result = await commands.call(s, args.cmd, kw, positional)
    if isinstance(result, dict) and "dom" in result:
        print(f"{result.get('url', '')}  {result.get('title', '')}")
        if result.get("changes") is not None:
            print(result["changes"] or "no changes")
        elif result.get("dom"):
            print(result["dom"])
    elif isinstance(result, str):
        print(result)
    elif result is not None:
        print(json.dumps(result, indent=2))


async def cmd_secret(_pool, args):
    from . import secrets
    try:
        if args.action == "set":
            if not args.name:
                raise SystemExit("hutch: give a secret name")
            if sys.stdin.isatty():
                import getpass
                value = getpass.getpass(f"{args.name}: ")
            else:
                value = sys.stdin.read().rstrip("\n")
            if not value:
                raise SystemExit("hutch: empty value")
            secrets.set_secret(args.name, value, args.domain)
            print(f"saved: {args.name}")
        elif args.action == "rm":
            secrets.remove(args.name)
            print(f"removed: {args.name}")
        else:
            rows = secrets.listing()
            for r in rows:
                print(f"{r['name']:<24} {', '.join(r['domains']) or 'any site'}")
            if not rows:
                print("no secrets")
    except secrets.SecretError as e:
        raise SystemExit(f"hutch: {e}")


def _running(args, headless):
    s = _load(args, args.name)
    st = s.endpoint or browser.status(s.profile_dir)
    if st and not s.endpoint and not st["ws"]:
        raise SystemExit(f"hutch: {s.name} is open in login mode, close that window first")
    if not st:
        _launch(s, headless=headless)
    return s


async def cmd_record(pool, args):
    import signal
    import time
    s = _running(args, headless=False)
    s = await pool.get(s.name, launch=True)
    await s.start_recording()
    print(f"{s.name}: recording, Ctrl+C to stop", flush=True)
    done = asyncio.Event()
    browser.on_signal(done.set, signal.SIGINT)
    shown = 0
    while not done.is_set() and s.is_alive:
        steps = s._recording["steps"]
        for step in steps[shown:]:
            print("  " + " ".join(str(step.get(k)) for k in ("action", "selector", "key", "url")
                                  if step.get(k) is not None), flush=True)
        shown = len(steps)
        try:
            await asyncio.wait_for(done.wait(), 0.3)
        except asyncio.TimeoutError:
            pass
    recording = s.stop_recording()
    out = args.output or os.path.join(os.path.dirname(_base_dir(args)), "recordings",
                                      f"{s.name}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump(recording, f, indent=2)
    print(f"saved: {out}, {len(recording['steps'])} steps")


async def cmd_replay(pool, args):
    s = _running(args, headless=not args.headed)
    s = await pool.get(s.name, launch=True)
    result = await s.replay(args.file)
    if result["ok"]:
        print(f"{s.name}: replayed {result['done']} steps, now at {result['url']}")
    else:
        failed = result["failed"] or {}
        raise SystemExit(f"hutch: step {result['done'] + 1} {failed.get('action')} "
                         f"{failed.get('selector') or failed.get('url') or ''} failed: {result['error']}")


async def cmd_export(_pool, args):
    from . import recorder
    print(recorder.to_python(recorder.load(args.file)), end="")


async def cmd_screenshot(pool, args):
    s = await pool.get(args.name, launch=True)
    page = None
    if args.url:
        page = await s.new_page()
        await page.goto(args.url)
    else:
        page = s._active_page()
    if not page:
        print("no open pages, pass --url")
        return
    await page.screenshot(path=args.output, full_page=args.full_page)
    print(f"saved: {args.output}")


async def cmd_close(_pool, args):
    args.all = False
    await cmd_stop(None, args)


async def cmd_destroy(_pool, args):
    s = _load(args, args.name)
    if not s.endpoint and browser.status(s.profile_dir):
        await browser.stop(s.profile_dir)
    shutil.rmtree(s.profile_dir)
    print(f"destroyed: {args.name}")


async def cmd_view(_pool, args):
    from .live import LiveServer
    s = _load(args, args.name)
    st = s.endpoint or browser.status(s.profile_dir)
    if st and not s.endpoint and not st["ws"]:
        raise SystemExit(f"hutch: {s.name} is open in login mode, close that window first")
    if not st:
        st = _launch(s, headless=True)
    live = LiveServer(args.host, args.port)
    live.add(s.name, s.cdp_endpoint)
    await live.start()
    url = live.url(s.name)
    print(f"{s.name}: {url}", flush=True)
    if not args.no_open:
        browser.open_url(url)
    try:
        while s.endpoint or browser.status(s.profile_dir):
            await asyncio.sleep(2)
        print(f"{s.name}: stopped")
    finally:
        await live.stop()


async def cmd_gc(_pool, args):
    from . import gc
    from .artifacts import _DEFAULT_ARTIFACTS_DIR
    gc.run(_base_dir(args), _DEFAULT_ARTIFACTS_DIR, days=args.days, caches=args.caches,
           dry_run=args.dry_run)


async def cmd_doctor(_pool, args):
    from . import doctor
    sys.exit(doctor.run(_base_dir(args)))


async def cmd_presets(_pool, _args):
    fmt = "{:<25} {:<12} {:<15} {}"
    print(fmt.format("NAME", "VIEWPORT", "PLATFORM", "LOCALE"))
    print("-" * 60)
    for p in list_presets():
        print(fmt.format(p["name"], p["viewport"], p["platform"], p["locale"]))


async def cmd_caido(_pool, args):
    from .caido import CaidoClient, CaidoConfig, CaidoError
    sub = args.caido_sub
    if not sub:
        print("usage: hutch caido <subcommand>")
        print("subcommands: status, projects, scopes, history, findings, scope-check")
        return
    port = getattr(args, "port", _CAIDO_DEFAULT_PORT) or _CAIDO_DEFAULT_PORT
    config = CaidoConfig(url=f"http://127.0.0.1:{port}")
    try:
        async with CaidoClient(config) as caido:

            if sub == "status":
                info = await caido.instance_info()
                projects = await caido.list_projects()
                scopes = await caido.list_scopes()
                print(f"caido: {info.get('version', '?')} ({info.get('platform', '?')})")
                print(f"  url: {config.url}")
                print(f"  projects: {len(projects)}/2")
                for p in projects:
                    print(f"    {p['name']} ({p.get('status', '?')})")
                print(f"  scopes: {len(scopes)}")
                for s in scopes:
                    print(f"    {s['name']}: {', '.join(s.get('allowlist', []))}")

            elif sub == "projects":
                projects = await caido.list_projects()
                if not projects:
                    print("no projects")
                    return
                for p in projects:
                    size = p.get("size", 0)
                    size_mb = f"{size / 1024 / 1024:.1f}MB" if size else "?"
                    print(f"  {p['name']}  {p.get('status', '?')}  {size_mb}")

            elif sub == "scopes":
                scopes = await caido.list_scopes()
                if not scopes:
                    print("no scopes")
                    return
                for s in scopes:
                    allow = ", ".join(s.get("allowlist", []))
                    deny = ", ".join(s.get("denylist", []))
                    print(f"  {s['name']}")
                    print(f"    allow: {allow or '(none)'}")
                    if deny:
                        print(f"    deny:  {deny}")

            elif sub == "history":
                httpql = args.filter or None
                result = await caido.list_requests(httpql=httpql, first=args.count)
                if not result["items"]:
                    print("no requests")
                    return
                fmt = "{:<6} {:<6} {:<30} {:<40} {}"
                print(fmt.format("ID", "CODE", "HOST", "PATH", "METHOD"))
                for r in result["items"]:
                    code = str(r.get("response", {}).get("statusCode", "")) if r.get("response") else ""
                    print(fmt.format(
                        str(r["id"])[:6],
                        code,
                        r["host"][:30],
                        r["path"][:40],
                        r["method"],
                    ))

            elif sub == "findings":
                findings = await caido.list_findings()
                if not findings:
                    print("no findings")
                    return
                for f in findings:
                    req = f.get("request", {})
                    print(f"  [{f['id'][:8]}] {f['title']}")
                    print(f"    {req.get('method', '?')} {req.get('host', '?')}{req.get('path', '?')}")
                    print(f"    reporter: {f.get('reporter', '?')}")

            elif sub == "scope-check":
                result = await caido.is_in_scope(args.url)
                print(f"{'in scope' if result else 'OUT OF SCOPE'}: {args.url}")

            else:
                print(f"hutch caido: unknown subcommand '{sub}'")

    except CaidoError as e:
        print(f"hutch caido: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        if "Cannot connect" in str(e) or "Connection refused" in str(e):
            print(f"hutch caido: cannot connect to Caido at {config.url}", file=sys.stderr)
            print(f"  is Caido running?", file=sys.stderr)
            sys.exit(1)
        raise


async def cmd_mcp(_pool, _args):
    from .mcp import main as mcp_main
    mcp_main()


async def cmd_serve(_pool, args):
    from .server import HutchDaemon
    daemon = HutchDaemon(
        base_dir=args.base_dir,
        max_sessions=args.max_sessions,
        idle_timeout=args.idle_timeout,
        exit_when_idle=args.exit_when_idle,
    )
    print(f"hutch daemon starting")
    print(f"  socket: {daemon.sock_path}")
    print(f"  max sessions: {args.max_sessions}")
    print(f"  idle timeout: {args.idle_timeout}s")
    print(f"  profiles: {daemon.pool.base_dir}")
    await daemon.serve_forever()


async def _run(args):
    if args.command in ("serve", "mcp", "caido", "presets") or args.command not in _POOL_COMMANDS:
        await args.func(None, args)
        return
    try:
        async with Pool(base_dir=args.base_dir, max_sessions=args.max_sessions) as pool:
            await args.func(pool, args)
    except Exception as e:
        message = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        print(f"hutch: {message.splitlines()[0] if message else type(e).__name__}", file=sys.stderr)
        sys.exit(1)


_POOL_COMMANDS = {"screenshot", "call", "record", "replay"}


def main():
    class _Parser(argparse.ArgumentParser):
        def error(self, message):
            if "invalid choice" in message:
                cmd = message.split("'")[1] if "'" in message else ""
                self.exit(1, f"hutch: unknown command '{cmd}'\n"
                             f"run 'hutch --help' for usage\n")
            super().error(message)

    p = _Parser(
        prog="hutch",
        usage="hutch [options] <command> [<args>]",
        description="isolated playwright session orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "browser:\n"
            "  browse <name> [url]   open a persistent browser\n"
            "  login <name> [url]    sign in to Google or SSO\n"
            "  headed <name>         show a headless browser\n"
            "  headless <name>       hide a browser\n"
            "  view <name>           watch and drive a browser from a web page\n"
            "  call <name> <cmd> ... run any session command: hutch call help\n"
            "  record <name>         record what happens in the browser until Ctrl+C\n"
            "  replay <name> <file>  run a recording again\n"
            "  export <file>         recording as a standalone playwright script\n"
            "  secret set|list|rm    values agents type as {{name}} but never see\n"
            "  tabs <name|target>    list open tabs\n"
            "  ps                    running browsers and memory\n"
            "  stop <name>|--all     close a browser, keep the profile\n"
            "  clone <src> <dst>     copy a profile with its logins\n"
            "  attach <target>       connect to your chrome, brave or edge\n"
            "\n"
            "sessions:\n"
            "  create          create a session\n"
            "  list            list sessions\n"
            "  status          show session details\n"
            "  open            open a URL in a session\n"
            "  drive           browse and wait until the window closes\n"
            "  screenshot      take a screenshot\n"
            "  close           same as stop\n"
            "  destroy         stop and delete a profile\n"
            "  doctor          check browsers, libraries, display and networking\n"
            "  gc              free disk: old artifacts, downloads, stale state, --caches\n"
            "\n"
            "daemon:\n"
            "  serve           start persistent daemon\n"
            "  mcp             start MCP server over stdio\n"
            "\n"
            "caido:\n"
            "  caido status    show caido connection info\n"
            "  caido projects  list caido projects\n"
            "  caido scopes    list scope definitions\n"
            "  caido history   query captured requests\n"
            "  caido findings  list findings\n"
            "  caido scope-check <url>  check if URL is in scope\n"
            "\n"
            "other:\n"
            "  presets          list fingerprint presets"
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--base-dir", default=None, metavar="DIR",
                   help="profile directory, default ~/.hutch/profiles")
    p.add_argument("--max-sessions", type=int, default=5, metavar="N",
                   help="max concurrent sessions, default 5")

    sub = p.add_subparsers(dest="command")
    for g in p._action_groups:
        for a in g._group_actions:
            if isinstance(a, argparse._SubParsersAction):
                a.help = argparse.SUPPRESS

    def _sub(name, **kw):
        return sub.add_parser(name, prog=f"hutch {name}", **kw)

    def _common(c, *, persona=False):
        c.add_argument("--browser", metavar="NAME",
                       help="chromium, chrome, brave, edge or a binary path")
        c.add_argument("--force", action="store_true", help="allow changing a profile's browser")
        c.add_argument("--proxy", metavar="URL", help="proxy server (http/socks5)")
        c.add_argument("--caido", type=int, nargs="?", const=_CAIDO_DEFAULT_PORT,
                       metavar="PORT", help="route through Caido, default port 8080")
        c.add_argument("--bypass", metavar="HOSTS", help="proxy bypass list")
        c.add_argument("--ignore-https-errors", action="store_true", help="ignore TLS errors")
        if persona:
            c.add_argument("--preset", metavar="NAME", help="fingerprint preset")
            c.add_argument("--program", metavar="NAME", help="deterministic persona per program")
            c.add_argument("--locale", metavar="CODE", help="persona locale, e.g. ja-JP")
            c.add_argument("--timezone", metavar="TZ", help="persona timezone, e.g. Asia/Tokyo")
            c.add_argument("--tag", action="append", metavar="K=V", help="attach metadata tag")

    c = _sub("browse")
    c.add_argument("name", help="session name")
    c.add_argument("url", nargs="?", help="URL to open")
    c.add_argument("--headless", action="store_true", help="run without a window")
    c.add_argument("--wait", action="store_true", help="block until the browser exits")
    _common(c, persona=True)
    c.set_defaults(func=cmd_browse)

    c = _sub("login")
    c.add_argument("name", help="session name")
    c.add_argument("url", nargs="?", default="https://accounts.google.com/",
                   help="sign-in page, default Google")
    _common(c)
    c.set_defaults(func=cmd_login)

    c = _sub("auth")
    c.add_argument("name", help="session name")
    c.add_argument("--url", default="https://accounts.google.com/", help="sign-in page")
    _common(c)
    c.set_defaults(func=cmd_login)

    c = _sub("headed")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_headed)

    c = _sub("headless")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_headless)

    c = _sub("stop")
    c.add_argument("name", nargs="?", help="session name")
    c.add_argument("--all", action="store_true", help="stop every running browser")
    c.set_defaults(func=cmd_stop)

    c = _sub("ps")
    c.set_defaults(func=cmd_ps)

    c = _sub("tabs")
    c.add_argument("name", help="session name or attach target (brave, chrome, edge, port)")
    c.set_defaults(func=cmd_tabs)

    c = _sub("secret")
    c.add_argument("action", choices=["set", "list", "rm"])
    c.add_argument("name", nargs="?", help="secret name, typed as {{name}}")
    c.add_argument("--domain", action="append", metavar="HOST", help="only type it on this site, repeatable")
    c.set_defaults(func=cmd_secret)

    c = _sub("record")
    c.add_argument("name", help="session name")
    c.add_argument("-o", "--output", metavar="FILE", help="default ~/.hutch/recordings/<name>-<time>.json")
    c.set_defaults(func=cmd_record)

    c = _sub("replay")
    c.add_argument("name", help="session name")
    c.add_argument("file", help="recording file")
    c.add_argument("--headed", action="store_true", help="start the browser with a window")
    c.set_defaults(func=cmd_replay)

    c = _sub("export")
    c.add_argument("file", help="recording file")
    c.set_defaults(func=cmd_export)

    c = _sub("call")
    c.add_argument("name", help="session name, or help")
    c.add_argument("cmd", nargs="?", help="command, or help")
    c.add_argument("args", nargs=argparse.REMAINDER, help="values, key=value strings, key:=json")
    c.set_defaults(func=cmd_call)

    c = _sub("view")
    c.add_argument("name", help="session name")
    c.add_argument("--host", default="127.0.0.1", help="listen address, default 127.0.0.1")
    c.add_argument("--port", type=int, default=0, help="listen port, default random")
    c.add_argument("--no-open", action="store_true", help="print the URL without opening it")
    c.set_defaults(func=cmd_view)

    c = _sub("clone")
    c.add_argument("src", help="existing session")
    c.add_argument("dst", help="new session name")
    c.set_defaults(func=cmd_clone)

    c = _sub("attach")
    c.add_argument("target", help="chrome | brave | edge | port | http://host:port | ws://...")
    c.add_argument("--name", help="register as a session for agents")
    c.set_defaults(func=cmd_attach)

    c = _sub("create")
    c.add_argument("name", help="session name")
    c.add_argument("--headed", action="store_true", help="launch with a window now")
    c.add_argument("--url", metavar="URL", help="launch and navigate")
    _common(c, persona=True)
    c.set_defaults(func=cmd_create)

    c = _sub("list")
    c.add_argument("--alive", action="store_true", help="only show running sessions")
    c.set_defaults(func=cmd_list)

    c = _sub("status")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_status)

    c = _sub("open")
    c.add_argument("name", help="session name")
    c.add_argument("url", help="URL to navigate to")
    _common(c)
    c.set_defaults(func=cmd_open)

    c = _sub("drive")
    c.add_argument("name", help="session name")
    c.add_argument("--url", metavar="URL", help="navigate to URL")
    _common(c, persona=True)
    c.set_defaults(func=cmd_browse, headless=False, wait=True)

    c = _sub("screenshot")
    c.add_argument("name", help="session name")
    c.add_argument("output", nargs="?", default="screenshot.png", metavar="FILE",
                   help="output file, default screenshot.png")
    c.add_argument("--url", metavar="URL", help="navigate to URL before capturing")
    c.add_argument("--full-page", action="store_true", help="capture full scrollable page")
    c.set_defaults(func=cmd_screenshot)

    c = _sub("close")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_close)

    c = _sub("destroy")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_destroy)

    c = _sub("gc")
    c.add_argument("--days", type=float, default=30, help="remove artifacts and downloads older than this, default 30")
    c.add_argument("--caches", action="store_true", help="also clear browser caches of stopped profiles")
    c.add_argument("--dry-run", action="store_true", help="show what would be removed")
    c.set_defaults(func=cmd_gc)

    c = _sub("doctor")
    c.set_defaults(func=cmd_doctor)

    c = _sub("serve")
    c.add_argument("--idle-timeout", type=int, default=900, metavar="SEC",
                   help="hibernate idle headless sessions after N seconds, default 900, 0 disables")
    c.add_argument("--exit-when-idle", action="store_true",
                   help="shut the daemon down once nothing has run for --idle-timeout")
    c.set_defaults(func=cmd_serve)

    c = _sub("mcp", help="start MCP server over stdio")
    c.set_defaults(func=cmd_mcp)

    c = _sub("presets")
    c.set_defaults(func=cmd_presets)

    c = _sub("caido", help="manage caido proxy integration")
    c.add_argument("--port", type=int, default=_CAIDO_DEFAULT_PORT,
                   help="caido port, default 8080")
    caido_sub = c.add_subparsers(dest="caido_sub")
    cs = caido_sub.add_parser("status", help="show caido connection info")
    cs = caido_sub.add_parser("projects", help="list caido projects")
    cs = caido_sub.add_parser("scopes", help="list caido scopes")
    cs = caido_sub.add_parser("history", help="query captured requests")
    cs.add_argument("--filter", metavar="HTTPQL", help="HTTPQL filter query")
    cs.add_argument("--count", type=int, default=20, help="max results, default 20")
    cs = caido_sub.add_parser("findings", help="list caido findings")
    cs = caido_sub.add_parser("scope-check", help="check if URL is in scope")
    cs.add_argument("url", help="URL to check")
    c.set_defaults(func=cmd_caido)

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
