import argparse
import asyncio
import re
import sys

from .fingerprint import generate, generate_for_program, list_presets
from .pool import Pool
from .session import Fingerprint, ProxyConfig

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


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


async def cmd_create(pool, args):
    _validate_name(args.name)
    fp = None
    if args.preset:
        fp = generate(preset=args.preset, locale=args.locale, timezone=args.timezone)
    elif args.program:
        fp = generate_for_program(args.program, locale=args.locale, timezone=args.timezone)

    proxy = None
    if args.proxy:
        proxy = ProxyConfig(server=args.proxy, bypass=args.bypass)

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
        print(f"  proxy: {proxy.server}")
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
    print("-" * 75)
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
        proxy = ProxyConfig(server=args.proxy) if args.proxy else None
        s = await pool.create(
            name, proxy=proxy, fingerprint=fp,
            headless=False, ignore_https_errors=bool(args.proxy),
        )
    else:
        s = await pool.get(name)
        if s.is_alive:
            await s.close()
        s.headless = False
        await s.launch(pool._playwright)

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
    p = argparse.ArgumentParser(
        prog="hutch",
        usage="hutch [options] <command> [<args>]",
        description="isolated playwright session orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "session commands:\n"
            "  create          create a new session\n"
            "  list (ls)       list sessions\n"
            "  status          show session details\n"
            "  open            open a URL in a session\n"
            "  screenshot (ss) take a screenshot\n"
            "  auth            open headed browser for manual login\n"
            "  close           close a running session\n"
            "  destroy         close and delete session profile\n"
            "\n"
            "daemon:\n"
            "  serve           start persistent daemon\n"
            "\n"
            "other:\n"
            "  presets          list fingerprint presets"
        ),
    )
    p.add_argument("--version", action="version", version="%(prog)s 0.2.0")
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

    c = _sub("list", aliases=["ls"])
    c.add_argument("--alive", action="store_true", help="only show running sessions")
    c.set_defaults(func=cmd_list)

    c = _sub("status")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_status)

    c = _sub("open")
    c.add_argument("name", help="session name")
    c.add_argument("url", help="URL to navigate to")
    c.set_defaults(func=cmd_open)

    c = _sub("screenshot", aliases=["ss"])
    c.add_argument("name", help="session name")
    c.add_argument("-o", "--output", default="screenshot.png", metavar="FILE",
                   help="output file path (default: screenshot.png)")
    c.add_argument("--url", metavar="URL", help="navigate to URL before capturing")
    c.add_argument("--full-page", action="store_true", help="capture full scrollable page")
    c.set_defaults(func=cmd_screenshot)

    c = _sub("auth")
    c.add_argument("name", help="session name")
    c.add_argument("--url", metavar="URL", help="login page URL")
    c.add_argument("--proxy", metavar="URL", help="proxy server")
    c.add_argument("--preset", metavar="NAME", help="fingerprint preset")
    c.set_defaults(func=cmd_auth)

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

    c = _sub("presets")
    c.set_defaults(func=cmd_presets)

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
