import argparse
import asyncio
import re
import sys

from . import __version__
from .fingerprint import generate, generate_for_program, list_presets
from .pool import Pool
from .session import Fingerprint, ProxyConfig

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_CAIDO_DEFAULT_PORT = 8080


def _validate_name(name):
    if not name or not _NAME_RE.match(name):
        raise SystemExit(
            f"hutch: invalid session name '{name}' — "
            "use alphanumeric, hyphens, underscores, dots"
        )
    return name


def _parse_tag(value):
    if "=" not in value:
        raise SystemExit(f"hutch: invalid tag '{value}' — expected key=value")
    k, _, v = value.partition("=")
    return k, v


def _resolve_proxy(args):
    if getattr(args, "caido", None) is not None:
        port = args.caido
        if args.proxy:
            raise SystemExit("hutch: --caido and --proxy are mutually exclusive")
        args.proxy = f"http://127.0.0.1:{port}"
        args.ignore_https_errors = True
    proxy = ProxyConfig(server=args.proxy, bypass=getattr(args, "bypass", None)) if args.proxy else None
    return proxy


async def cmd_create(pool, args):
    _validate_name(args.name)
    fp = None
    if args.preset:
        fp = generate(preset=args.preset, locale=args.locale, timezone=args.timezone)
    elif args.program:
        fp = generate_for_program(args.program, locale=args.locale, timezone=args.timezone)

    proxy = _resolve_proxy(args)

    tags = {}
    if args.program:
        tags["program"] = args.program
    if args.tag:
        for t in args.tag:
            k, v = _parse_tag(t)
            tags[k] = v

    s = await pool.create(
        args.name,
        proxy=proxy,
        fingerprint=fp,
        headless=not args.headed,
        ignore_https_errors=args.ignore_https_errors,
        tags=tags,
    )
    fp_info = s.fingerprint
    print(f"created: {s.name}")
    print(f"  fingerprint: {fp_info.viewport_width}x{fp_info.viewport_height} "
          f"{fp_info.platform or 'default'} {fp_info.locale} {fp_info.timezone}")
    if proxy:
        label = "caido" if getattr(args, "caido", None) is not None else "proxy"
        print(f"  {label}: {proxy.server}")
    if args.url:
        page = await s.new_page()
        await page.goto(args.url)
        print(f"  opened: {args.url}")


def _truncate(s, width):
    if len(s) <= width:
        return s
    return s[:width - 1] + "…"


async def cmd_list(pool, args):
    sessions = pool.list(alive_only=args.alive)
    if not sessions:
        print("no sessions")
        return
    fmt = "{:<20} {:<8} {:<6} {:<20} {:<15}"
    print(fmt.format("NAME", "STATUS", "PAGES", "PROXY", "FINGERPRINT"))
    for s in sessions:
        st = s.status()
        print(fmt.format(
            _truncate(st["name"], 20),
            "alive" if st["alive"] else "closed",
            str(st["pages"]),
            _truncate(st["proxy"], 20),
            st["fingerprint"],
        ))


async def cmd_status(pool, args):
    s = await pool.get(args.name)
    import json
    print(json.dumps(s.status(), indent=2))


async def cmd_open(pool, args):
    s = await pool.get(args.name, launch=True)
    page = await s.new_page()
    await page.goto(args.url)
    print(f"opened {args.url} in '{args.name}'")


async def cmd_screenshot(pool, args):
    s = await pool.get(args.name, launch=True)
    page = None
    if args.url:
        page = await s.new_page()
        await page.goto(args.url)
    else:
        pages = [p for p in s._pages if not p.is_closed()]
        page = pages[-1] if pages else None
    if not page:
        print("no open pages — use --url to navigate first")
        return
    await page.screenshot(path=args.output, full_page=args.full_page)
    print(f"saved: {args.output}")


async def cmd_auth(pool, args):
    name = args.name
    _validate_name(name)
    if name not in pool:
        fp = None
        if args.preset:
            fp = generate(preset=args.preset)
        proxy = _resolve_proxy(args)
        s = await pool.create(
            name, proxy=proxy, fingerprint=fp,
            headless=False, ignore_https_errors=args.ignore_https_errors or bool(args.proxy),
        )
    else:
        s = await pool.get(name)
        if s.is_alive:
            await s.close()
        s.headless = False
        await pool.get(name, launch=True)

    if args.url:
        page = await s.new_page()
        await page.goto(args.url)

    print(f"browser open for '{name}' — log in manually, then press Enter to save state")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    await s.save_state()
    await s.close()
    print(f"auth state saved for '{name}'")


async def cmd_drive(pool, args):
    name = args.name
    _validate_name(name)
    if name not in pool:
        fp = None
        if args.preset:
            fp = generate(preset=args.preset, locale=args.locale,
                          timezone=args.timezone)
        elif args.program:
            fp = generate_for_program(args.program, locale=args.locale,
                                      timezone=args.timezone)
        proxy = _resolve_proxy(args)
        s = await pool.create(
            name, proxy=proxy, fingerprint=fp,
            headless=False,
            ignore_https_errors=args.ignore_https_errors,
        )
        via = " via caido" if getattr(args, "caido", None) is not None else ""
        print(f"created '{name}' (headed{via})")
    else:
        s = await pool.get(name)
        if s.is_alive and s.headless:
            await s.close()
        s.headless = False
        if not s.is_alive:
            await pool.get(name, launch=True)
        print(f"reopened '{name}' (headed)")

    if args.url:
        page = await s.new_page()
        await page.goto(args.url)
        print(f"navigated to {args.url}")

    print(f"\nbrowser is open — interact freely")
    print(f"press Enter to save state and close, or Ctrl+C to close without saving")
    save = False
    try:
        input()
        save = True
    except (EOFError, KeyboardInterrupt):
        print()
    if save:
        try:
            await s.save_state()
            print(f"state saved for '{name}'")
        except Exception:
            pass
    try:
        await s.close()
    except Exception:
        pass
    print(f"closed '{name}'")


async def cmd_close(pool, args):
    if args.name not in pool:
        raise SystemExit(f"hutch: no session named '{args.name}'")
    await pool.close(args.name)
    print(f"closed: {args.name}")


async def cmd_destroy(pool, args):
    await pool.destroy(args.name)
    print(f"destroyed: {args.name}")


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
    )
    print(f"hutch daemon starting")
    print(f"  socket: {daemon.sock_path}")
    print(f"  max sessions: {args.max_sessions}")
    print(f"  idle timeout: {args.idle_timeout}s")
    print(f"  profiles: {daemon.pool.base_dir}")
    await daemon.serve_forever()


async def _run(args):
    if args.command == "serve":
        await cmd_serve(None, args)
        return
    if args.command == "mcp":
        await cmd_mcp(None, args)
        return
    if args.command == "caido":
        await cmd_caido(None, args)
        return
    if args.command == "presets":
        await cmd_presets(None, args)
        return
    try:
        async with Pool(base_dir=args.base_dir, max_sessions=args.max_sessions) as pool:
            await args.func(pool, args)
    except KeyError as e:
        print(f"hutch: {e.args[0]}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"hutch: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"hutch: {e}", file=sys.stderr)
        sys.exit(1)


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
            "session commands:\n"
            "  create          create a new session\n"
            "  list            list sessions\n"
            "  status          show session details\n"
            "  open            open a URL in a session\n"
            "  drive           open headed browser for interactive use\n"
            "  screenshot      take a screenshot\n"
            "  auth            open headed browser for manual login\n"
            "  close           close a running session\n"
            "  destroy         close and delete session profile\n"
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
                   help="profile storage directory (default: ~/.hutch/profiles)")
    p.add_argument("--max-sessions", type=int, default=5, metavar="N",
                   help="max concurrent browser sessions (default: 5)")

    sub = p.add_subparsers(dest="command")
    for g in p._action_groups:
        for a in g._group_actions:
            if isinstance(a, argparse._SubParsersAction):
                a.help = argparse.SUPPRESS

    def _sub(name, **kw):
        return sub.add_parser(name, prog=f"hutch {name}", **kw)

    c = _sub("create")
    c.add_argument("name", help="session name")
    c.add_argument("--proxy", metavar="URL", help="proxy server (http/socks5)")
    c.add_argument("--caido", type=int, nargs="?", const=_CAIDO_DEFAULT_PORT,
                   metavar="PORT", help="route through Caido (default port 8080)")
    c.add_argument("--bypass", metavar="HOSTS", help="proxy bypass list")
    c.add_argument("--preset", metavar="NAME", help="fingerprint preset")
    c.add_argument("--program", metavar="NAME", help="program name (deterministic fingerprint)")
    c.add_argument("--locale", metavar="CODE", help="override locale (e.g. ja-JP)")
    c.add_argument("--timezone", metavar="TZ", help="override timezone (e.g. Asia/Tokyo)")
    c.add_argument("--headed", action="store_true", help="run with visible browser")
    c.add_argument("--ignore-https-errors", action="store_true", help="ignore TLS errors")
    c.add_argument("--url", metavar="URL", help="navigate after creation")
    c.add_argument("--tag", action="append", metavar="K=V", help="attach metadata tag")
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
    c.set_defaults(func=cmd_open)

    c = _sub("screenshot")
    c.add_argument("name", help="session name")
    c.add_argument("output", nargs="?", default="screenshot.png", metavar="FILE",
                   help="output file path (default: screenshot.png)")
    c.add_argument("--url", metavar="URL", help="navigate to URL before capturing")
    c.add_argument("--full-page", action="store_true", help="capture full scrollable page")
    c.set_defaults(func=cmd_screenshot)

    c = _sub("auth")
    c.add_argument("name", help="session name")
    c.add_argument("--url", metavar="URL", help="login page URL")
    c.add_argument("--proxy", metavar="URL", help="proxy server")
    c.add_argument("--caido", type=int, nargs="?", const=_CAIDO_DEFAULT_PORT,
                   metavar="PORT", help="route through Caido (default port 8080)")
    c.add_argument("--preset", metavar="NAME", help="fingerprint preset")
    c.add_argument("--ignore-https-errors", action="store_true", help="ignore TLS errors")
    c.set_defaults(func=cmd_auth)

    c = _sub("drive")
    c.add_argument("name", help="session name (creates if new)")
    c.add_argument("--url", metavar="URL", help="navigate to URL")
    c.add_argument("--proxy", metavar="URL", help="proxy server")
    c.add_argument("--caido", type=int, nargs="?", const=_CAIDO_DEFAULT_PORT,
                   metavar="PORT", help="route through Caido (default port 8080)")
    c.add_argument("--preset", metavar="NAME", help="fingerprint preset")
    c.add_argument("--program", metavar="NAME", help="program name")
    c.add_argument("--locale", metavar="CODE", help="override locale")
    c.add_argument("--timezone", metavar="TZ", help="override timezone")
    c.add_argument("--ignore-https-errors", action="store_true")
    c.set_defaults(func=cmd_drive)

    c = _sub("close")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_close)

    c = _sub("destroy")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_destroy)

    c = _sub("serve")
    c.add_argument("--idle-timeout", type=int, default=900, metavar="SEC",
                   help="hibernate sessions after N seconds idle (default: 900, 0=disable)")
    c.set_defaults(func=cmd_serve)

    c = _sub("mcp", help="start MCP server over stdio")
    c.set_defaults(func=cmd_mcp)

    c = _sub("presets")
    c.set_defaults(func=cmd_presets)

    c = _sub("caido", help="manage caido proxy integration")
    c.add_argument("--port", type=int, default=_CAIDO_DEFAULT_PORT,
                   help="caido instance port (default: 8080)")
    caido_sub = c.add_subparsers(dest="caido_sub")
    cs = caido_sub.add_parser("status", help="show caido connection info")
    cs = caido_sub.add_parser("projects", help="list caido projects")
    cs = caido_sub.add_parser("scopes", help="list caido scopes")
    cs = caido_sub.add_parser("history", help="query captured requests")
    cs.add_argument("--filter", metavar="HTTPQL", help="HTTPQL filter query")
    cs.add_argument("--count", type=int, default=20, help="max results (default: 20)")
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
