"""hutch — isolated Playwright session orchestrator.

Create, manage, and orchestrate multiple isolated browser sessions with
persistent state, per-session proxy routing, and fingerprint management.

    from hutch import Pool

    async with Pool() as pool:
        s = await pool.create("my-session", proxy="http://127.0.0.1:8081")
        page = await s.new_page()
        await page.goto("https://example.com")
        await s.save_state()    # persist cookies/storage to disk
        await s.close()         # close browser, profile survives

        # later — reopen with full auth state restored:
        s = await pool.get("my-session", launch=True)
        page = await s.new_page()
        # cookies, localStorage, everything is back
"""

__version__ = "0.1.0"

from .fingerprint import Fingerprint, generate, generate_for_program  # noqa: F401
from .pool import Pool  # noqa: F401
from .session import ProxyConfig, Session  # noqa: F401
