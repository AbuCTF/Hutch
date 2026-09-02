"""hutch/cli.py — command-line interface for managing browser sessions.

Usage:
    hutch create my-session                     # create + launch
    hutch create my-session --proxy http://127.0.0.1:8081
    hutch create my-session --preset win-desktop-1080p --headed
    hutch list                                  # show all sessions
    hutch status my-session                     # detailed status
    hutch open my-session https://example.com   # open URL in session
    hutch screenshot my-session out.png         # capture screenshot
    hutch auth my-session                       # open headed for manual login
    hutch close my-session                      # close browser (profile kept)
    hutch destroy my-session                    # close + delete profile
    hutch presets                               # list fingerprint presets
"""

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
    pages = [p for p in s._pages if not p.is_closed()]
    if not pages:
        print("no open pages — open a URL first")
        return
    page = pages[-1]
    await page.screenshot(path=args.output, full_page=args.full_page)
    print(f"saved: {args.output}")


async def cmd_auth(pool, args):
    """Open a headed browser for manual login, then save state."""
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

    print(f"browser open for '{name}' — log in manually, then press Enter here to save state")
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
    for p in list_presets():
        print(f"  {p['name']:<25} {p['viewport']:<12} {p['platform']:<15} {p['locale']}")


async def _run(args):
    async with Pool(base_dir=args.base_dir, max_sessions=args.max_sessions) as pool:
        await args.func(pool, args)


def main():
    p = argparse.ArgumentParser(
        prog="hutch",
        description="Hutch — isolated Playwright session manager",
    )
    p.add_argument("--base-dir", default=None, help="profile storage dir (default ~/.hutch/profiles)")
    p.add_argument("--max-sessions", type=int, default=5, help="max simultaneous browsers")

    sub = p.add_subparsers(dest="command")

    # create
    c = sub.add_parser("create", help="create a new session")
    c.add_argument("name")
    c.add_argument("--proxy", help="proxy URL (e.g. http://127.0.0.1:8081)")
    c.add_argument("--bypass", help="proxy bypass list")
    c.add_argument("--preset", help="fingerprint preset name")
    c.add_argument("--program", help="program name (deterministic fingerprint)")
    c.add_argument("--locale", help="override locale")
    c.add_argument("--timezone", help="override timezone")
    c.add_argument("--headed", action="store_true", help="show browser window")
    c.add_argument("--ignore-https-errors", action="store_true")
    c.add_argument("--url", help="open URL after creating")
    c.add_argument("--tag", action="append", help="key=value tag")
    c.set_defaults(func=cmd_create)

    # list
    c = sub.add_parser("list", aliases=["ls"], help="list sessions")
    c.add_argument("--alive", action="store_true", help="only show running")
    c.set_defaults(func=cmd_list)

    # status
    c = sub.add_parser("status", help="show session details")
    c.add_argument("name")
    c.set_defaults(func=cmd_status)

    # open
    c = sub.add_parser("open", help="open URL in session")
    c.add_argument("name")
    c.add_argument("url")
    c.set_defaults(func=cmd_open)

    # screenshot
    c = sub.add_parser("screenshot", aliases=["ss"], help="capture screenshot")
    c.add_argument("name")
    c.add_argument("output", default="screenshot.png", nargs="?")
    c.add_argument("--full-page", action="store_true")
    c.set_defaults(func=cmd_screenshot)

    # auth
    c = sub.add_parser("auth", help="open headed browser for manual login")
    c.add_argument("name")
    c.add_argument("--url", help="navigate to this URL for login")
    c.add_argument("--proxy", help="proxy URL")
    c.add_argument("--preset", help="fingerprint preset")
    c.set_defaults(func=cmd_auth)

    # close
    c = sub.add_parser("close", help="close session browser (keep profile)")
    c.add_argument("name")
    c.set_defaults(func=cmd_close)

    # destroy
    c = sub.add_parser("destroy", help="close + delete session permanently")
    c.add_argument("name")
    c.set_defaults(func=cmd_destroy)

    # presets
    c = sub.add_parser("presets", help="list fingerprint presets")
    c.set_defaults(func=cmd_presets)

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
