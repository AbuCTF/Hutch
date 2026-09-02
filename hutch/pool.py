import asyncio
import os
import shutil
from typing import Optional

from playwright.async_api import async_playwright

from .session import Fingerprint, ProxyConfig, Session


_DEFAULT_BASE_DIR = os.path.expanduser("~/.hutch/profiles")


class Pool:

    def __init__(self, base_dir=None, max_sessions=5):
        self.base_dir = base_dir or _DEFAULT_BASE_DIR
        self.max_sessions = max_sessions
        self._sessions = {}
        self._playwright = None
        self._pw_context = None

        os.makedirs(self.base_dir, exist_ok=True)

    async def start(self):
        if self._pw_context:
            return
        self._pw_context = async_playwright()
        self._playwright = await self._pw_context.start()
        self._discover_existing()

    async def stop(self):
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
        if name in self._sessions:
            raise ValueError(f"session '{name}' already exists")

        alive_count = sum(1 for s in self._sessions.values() if s.is_alive)
        if launch and alive_count >= self.max_sessions:
            raise RuntimeError(
                f"max {self.max_sessions} simultaneous sessions"
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
                raise RuntimeError("pool not started")
            await session.launch(self._playwright)

        return session

    async def get(self, name, *, launch=False):
        session = self._sessions.get(name)
        if not session:
            raise KeyError(f"no session named '{name}'")
        if launch and not session.is_alive:
            if not self._playwright:
                raise RuntimeError("pool not started")
            alive_count = sum(1 for s in self._sessions.values() if s.is_alive)
            if alive_count >= self.max_sessions:
                raise RuntimeError(f"max {self.max_sessions} simultaneous sessions")
            await session.launch(self._playwright)
        return session

    async def launch(self, name):
        return await self.get(name, launch=True)

    async def close(self, name):
        session = self._sessions.get(name)
        if session:
            await session.close()

    async def destroy(self, name):
        session = self._sessions.pop(name, None)
        if not session:
            raise KeyError(f"no session named '{name}'")
        await session.close()
        if os.path.isdir(session.profile_dir):
            shutil.rmtree(session.profile_dir)

    def list(self, *, alive_only=False, tag=None):
        sessions = list(self._sessions.values())
        if alive_only:
            sessions = [s for s in sessions if s.is_alive]
        if tag:
            k, v = tag
            sessions = [s for s in sessions if s.tags.get(k) == v]
        return sessions

    def status(self):
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
