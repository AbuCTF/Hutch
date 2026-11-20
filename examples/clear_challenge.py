"""Click a "verify you are human" checkbox on a page you are allowed to use, then carry on.

    python examples/clear_challenge.py https://example.com/signup

Needs Google Chrome. For the best results run it headed on a virtual screen (Linux, needs Xvfb):
the pointer then reaches the page through the X server, like a real mouse.
"""
import asyncio
import sys

from hutch import Pool


async def main(url):
    async with Pool() as pool:
        s = await pool.create("challenge-demo", headless=False, display="virtual", driver="patchright")
        await s.goto(url)
        result = await s.clear_challenge(timeout=60)
        print(result)  # {"status": "passed" | "clear" | "grid-required" | "hitl" | "timeout", ...}
        if result["status"] == "grid-required":
            print(await s.await_human("image grid", until="challenge", timeout=300))
        await pool.destroy("challenge-demo")


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "https://example.com"))
