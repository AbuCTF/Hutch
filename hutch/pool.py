"""hutch/pool.py — the browser pool manager.

The Pool is the central orchestrator. It:
1. Manages the single Playwright instance (one process, many contexts)
2. Creates/destroys Sessions by name
3. Discovers existing sessions on disk (survive restarts)
4. Enforces concurrency limits (too many browsers = OOM)
5. Handles LRU eviction of idle sessions

Architecture:
    Pool (one per process)
    +-- Session "target-a"  (persistent context + profile dir)
    +-- Session "target-b"  (persistent context + profile dir)
    +-- Session "target-c"  (persistent context + profile dir)

Each Session is fully isolated — different cookies, different proxy,
different fingerprint. They can run simultaneously or be started/stopped
independently.

The Pool manages the Playwright lifecycle so you never have to think
about it. Just call pool.create() and pool.get() and it handles the rest.
"""

import asyncio
import os
import shutil
from typing import Optional

from playwright.async_api import async_playwright

from .session import Fingerprint, ProxyConfig, Session


_DEFAULT_BASE_DIR = os.path.expanduser("~/.hutch/profiles")


class Pool:
    """Manages multiple isolated browser sessions.

    Usage:
        async with Pool() as pool:
            s = await pool.create("my-session")
            page = await s.new_page()
            await page.goto("https://example.com")
            await s.save_state()
            await s.close()

    Or without async context manager:
        pool = Pool()
        await pool.start()
        ...
        await pool.stop()
    """

    def __init__(self, base_dir=None, max_sessions=5):
        self.base_dir = base_dir or _DEFAULT_BASE_DIR
        self.max_sessions = max_sessions
        self._sessions = {}
        self._playwright = None
        self._pw_context = None

        os.makedirs(self.base_dir, exist_ok=True)

    async def start(self):
        """Initialize Playwright. Call once before creating sessions."""
        if self._pw_context:
            return
        self._pw_context = async_playwright()
        self._playwright = await self._pw_context.start()
        self._discover_existing()

    async def stop(self):
        """Close all sessions and tear down Playwright."""
        for s in list(self._sessions.values()):
            await s.close()
        if self._pw_context:
            await self._playwright.stop()
            self._pw_context = None
            self._playwright = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.stop()

    def _discover_existing(self):
        """Scan the base directory for previously created sessions.

        This is how hutch survives restarts — profile dirs persist on disk,
        and each one has a hutch_meta.json with the session config. On pool
        start, we reconstruct Session objects from those files (but don't
        launch the browsers — that's explicit).
        """
        if not os.path.isdir(self.base_dir):
            return
        for name in os.listdir(self.base_dir):
            profile_dir = os.path.join(self.base_dir, name)
            if not os.path.isdir(profile_dir):
                continue
            if name in self._sessions:
                continue
            session = Session.from_profile_dir(profile_dir)
            if session:
                self._sessions[session.name] = session

    async def create(self, name, *, proxy=None, fingerprint=None,
                     headless=True, ignore_https_errors=False,
                     launch=True, tags=None):
        """Create a new isolated browser session.

        Args:
            name: unique session identifier (used as profile dir name)
            proxy: ProxyConfig or proxy URL string (e.g. "http://127.0.0.1:8081")
            fingerprint: Fingerprint config or None for defaults
            headless: run without visible window (default True)
            ignore_https_errors: for proxy MITM CA certs (default False)
            launch: immediately open the browser (default True)
            tags: arbitrary metadata dict (e.g. {"program": "yandex"})

        Returns:
            Session object, launched if launch=True
        """
        if name in self._sessions:
            raise ValueError(f"session '{name}' already exists — use get() or destroy() first")

        alive_count = sum(1 for s in self._sessions.values() if s.is_alive)
        if launch and alive_count >= self.max_sessions:
            raise RuntimeError(
                f"max {self.max_sessions} simultaneous sessions — "
                f"close one first or raise max_sessions"
            )

        if isinstance(proxy, str):
            proxy = ProxyConfig(server=proxy)

        profile_dir = os.path.join(self.base_dir, name)
        session = Session(
            name=name,
            profile_dir=profile_dir,
            proxy=proxy,
            fingerprint=fingerprint,
            headless=headless,
            ignore_https_errors=ignore_https_errors,
            tags=tags,
        )
        self._sessions[name] = session

        if launch:
            if not self._playwright:
                raise RuntimeError("pool not started — call start() first")
            await session.launch(self._playwright)

        return session

    async def get(self, name, *, launch=False):
        """Get an existing session by name.

        If launch=True and the session isn't running, opens the browser.
        """
        session = self._sessions.get(name)
        if not session:
            raise KeyError(f"no session named '{name}' — use create() or list()")
        if launch and not session.is_alive:
            if not self._playwright:
                raise RuntimeError("pool not started")
            alive_count = sum(1 for s in self._sessions.values() if s.is_alive)
            if alive_count >= self.max_sessions:
                raise RuntimeError(f"max {self.max_sessions} simultaneous sessions")
            await session.launch(self._playwright)
        return session

    async def launch(self, name):
        """Launch an existing session's browser."""
        return await self.get(name, launch=True)

    async def close(self, name):
        """Close a session's browser (profile survives on disk)."""
        session = self._sessions.get(name)
        if session:
            await session.close()

    async def destroy(self, name):
        """Close browser AND delete the profile directory permanently."""
        session = self._sessions.pop(name, None)
        if not session:
            raise KeyError(f"no session named '{name}'")
        await session.close()
        if os.path.isdir(session.profile_dir):
            shutil.rmtree(session.profile_dir)

    def list(self, *, alive_only=False, tag=None):
        """List all sessions, optionally filtered.

        Args:
            alive_only: only show running sessions
            tag: filter by tag key-value, e.g. tag=("program", "yandex")
        """
        sessions = list(self._sessions.values())
        if alive_only:
            sessions = [s for s in sessions if s.is_alive]
        if tag:
            k, v = tag
            sessions = [s for s in sessions if s.tags.get(k) == v]
        return sessions

    def status(self):
        """Get a status summary of all sessions."""
        return {
            "base_dir": self.base_dir,
            "max_sessions": self.max_sessions,
            "total": len(self._sessions),
            "alive": sum(1 for s in self._sessions.values() if s.is_alive),
            "sessions": [s.status() for s in self._sessions.values()],
        }

    def __contains__(self, name):
        return name in self._sessions

    def __len__(self):
        return len(self._sessions)

    def __repr__(self):
        alive = sum(1 for s in self._sessions.values() if s.is_alive)
        return f"<Pool {alive}/{len(self._sessions)} alive base={self.base_dir}>"
