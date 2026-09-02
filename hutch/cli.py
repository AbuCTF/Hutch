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
        print("no open pages")
        return
    page = pages[-1]
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
    for p in list_presets():
        print(f"  {p['name']:<25} {p['viewport']:<12} {p['platform']:<15} {p['locale']}")


async def _run(args):
    async with Pool(base_dir=args.base_dir, max_sessions=args.max_sessions) as pool:
        await args.func(pool, args)


def main():
    p = argparse.ArgumentParser(prog="hutch")
    p.add_argument("--base-dir", default=None)
    p.add_argument("--max-sessions", type=int, default=5)

    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("create")
    c.add_argument("name")
    c.add_argument("--proxy")
    c.add_argument("--bypass")
    c.add_argument("--preset")
    c.add_argument("--program")
    c.add_argument("--locale")
    c.add_argument("--timezone")
    c.add_argument("--headed", action="store_true")
    c.add_argument("--ignore-https-errors", action="store_true")
    c.add_argument("--url")
    c.add_argument("--tag", action="append")
    c.set_defaults(func=cmd_create)

    c = sub.add_parser("list", aliases=["ls"])
    c.add_argument("--alive", action="store_true")
    c.set_defaults(func=cmd_list)

    c = sub.add_parser("status")
    c.add_argument("name")
    c.set_defaults(func=cmd_status)

    c = sub.add_parser("open")
    c.add_argument("name")
    c.add_argument("url")
    c.set_defaults(func=cmd_open)

    c = sub.add_parser("screenshot", aliases=["ss"])
    c.add_argument("name")
    c.add_argument("output", default="screenshot.png", nargs="?")
    c.add_argument("--full-page", action="store_true")
    c.set_defaults(func=cmd_screenshot)

    c = sub.add_parser("auth")
    c.add_argument("name")
    c.add_argument("--url")
    c.add_argument("--proxy")
    c.add_argument("--preset")
    c.set_defaults(func=cmd_auth)

    c = sub.add_parser("close")
    c.add_argument("name")
    c.set_defaults(func=cmd_close)

    c = sub.add_parser("destroy")
    c.add_argument("name")
    c.set_defaults(func=cmd_destroy)

    c = sub.add_parser("presets")
    c.set_defaults(func=cmd_presets)

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
