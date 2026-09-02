import argparse
import asyncio
import sys

from .fingerprint import generate, generate_for_program, list_presets
from .pool import Pool
from .session import Fingerprint, ProxyConfig


async def cmd_create(pool, args):
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
            k, _, v = t.partition("=")
            tags[k] = v

    s = await pool.create(
        args.name,
        proxy=proxy,
        fingerprint=fp,
        headless=not args.headed,
        ignore_https_errors=args.ignore_https_errors,
        tags=tags,
    )
    print(f"created: {s}")
    if args.url:
        page = await s.new_page()
        await page.goto(args.url)
        print(f"opened: {args.url}")


async def cmd_list(pool, args):
    sessions = pool.list(alive_only=args.alive)
    if not sessions:
        print("no sessions")
        return
    fmt = "{:<20} {:<8} {:<6} {:<10} {:<25}"
    print(fmt.format("NAME", "STATUS", "PAGES", "PROXY", "FINGERPRINT"))
    print("-" * 75)
    for s in sessions:
        st = s.status()
        print(fmt.format(
            st["name"][:20],
            "alive" if st["alive"] else "closed",
            str(st["pages"]),
            st["proxy"][:10] if st["proxy"] != "direct" else "direct",
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


async def _run(args):
    async with Pool(base_dir=args.base_dir, max_sessions=args.max_sessions) as pool:
        await args.func(pool, args)


def main():
    p = argparse.ArgumentParser(
        prog="hutch",
        description="isolated playwright session orchestrator",
    )
    p.add_argument("--base-dir", default=None, metavar="DIR",
                   help="profile storage directory (default: ~/.hutch/profiles)")
    p.add_argument("--max-sessions", type=int, default=5, metavar="N",
                   help="max concurrent browser sessions (default: 5)")

    sub = p.add_subparsers(dest="command", metavar="command")

    c = sub.add_parser("create", help="create a new session")
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

    c = sub.add_parser("list", aliases=["ls"], help="list sessions")
    c.add_argument("--alive", action="store_true", help="only show running sessions")
    c.set_defaults(func=cmd_list)

    c = sub.add_parser("status", help="show session details (JSON)")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_status)

    c = sub.add_parser("open", help="open a URL in a session")
    c.add_argument("name", help="session name")
    c.add_argument("url", help="URL to navigate to")
    c.set_defaults(func=cmd_open)

    c = sub.add_parser("screenshot", aliases=["ss"], help="take a screenshot")
    c.add_argument("name", help="session name")
    c.add_argument("output", default="screenshot.png", nargs="?", help="output file path")
    c.add_argument("--url", metavar="URL", help="navigate to URL before capturing")
    c.add_argument("--full-page", action="store_true", help="capture full scrollable page")
    c.set_defaults(func=cmd_screenshot)

    c = sub.add_parser("auth", help="open headed browser for manual login")
    c.add_argument("name", help="session name")
    c.add_argument("--url", metavar="URL", help="login page URL")
    c.add_argument("--proxy", metavar="URL", help="proxy server")
    c.add_argument("--preset", metavar="NAME", help="fingerprint preset")
    c.set_defaults(func=cmd_auth)

    c = sub.add_parser("close", help="close a running session")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_close)

    c = sub.add_parser("destroy", help="close and delete session profile")
    c.add_argument("name", help="session name")
    c.set_defaults(func=cmd_destroy)

    c = sub.add_parser("presets", help="list fingerprint presets")
    c.set_defaults(func=cmd_presets)

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
