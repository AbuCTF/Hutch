"""Open a page in a real browser, read it, and take a screenshot.

    python examples/hello.py
"""
import asyncio

from hutch import Pool


async def main():
    async with Pool() as pool:
        s = await pool.create("hello", headless=True)
        await s.goto("https://example.com")
        state = await s.page_state()
        print(state["title"], state["url"])
        await s.screenshot(path="hello.png")
        await pool.destroy("hello")  # closes the browser and deletes its profile


asyncio.run(main())
