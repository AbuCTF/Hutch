import asyncio
import logging
import os
import shutil
from dataclasses import asdict
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from .caido import CaidoClient, CaidoConfig
from .differ import diff_responses
from .session import Fingerprint, ProxyConfig, Session


_DEFAULT_BASE_DIR = os.path.expanduser("~/.hutch/profiles")
_log = logging.getLogger(__name__)


class Pool:

    def __init__(self, base_dir=None, max_sessions=5):
        self.base_dir = base_dir or _DEFAULT_BASE_DIR
        self.max_sessions = max_sessions
        self._sessions = {}
        self._launching = 0
        self._playwright = None
        self._pw_context = None
        self.caido = None

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
        if self.caido:
            await self.caido.close()
            self.caido = None
        if self._playwright:
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
            try:
                session = Session.from_profile_dir(profile_dir)
            except Exception:
                _log.warning("skipping corrupted profile: %s", profile_dir)
                continue
            if session:
                self._sessions[session.name] = session

    async def create(self, name, *, proxy=None, fingerprint=None,
                     headless=True, ignore_https_errors=False,
                     launch=True, tags=None, caido=False):
        if name in self._sessions:
            raise ValueError(f"session '{name}' already exists")

        if caido:
            port = caido if isinstance(caido, int) else 8080
            proxy = ProxyConfig(server=f"http://127.0.0.1:{port}")
            ignore_https_errors = True
        elif isinstance(proxy, str):
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

        if launch:
            self._launching += 1
            try:
                alive_count = sum(1 for s in self._sessions.values() if s.is_alive)
                if alive_count + self._launching > self.max_sessions:
                    raise RuntimeError(
                        f"max {self.max_sessions} simultaneous sessions"
                    )
                if not self._playwright:
                    raise RuntimeError("pool not started")
                await session.launch(self._playwright)
            finally:
                self._launching -= 1

        self._sessions[name] = session
        return session

    async def get(self, name, *, launch=False):
        session = self._sessions.get(name)
        if not session:
            raise KeyError(f"no session named '{name}'")
        if launch and not session.is_alive:
            self._launching += 1
            try:
                if not self._playwright:
                    raise RuntimeError("pool not started")
                alive_count = sum(1 for s in self._sessions.values() if s.is_alive)
                if alive_count + self._launching > self.max_sessions:
                    raise RuntimeError(f"max {self.max_sessions} simultaneous sessions")
                await session.launch(self._playwright)
            finally:
                self._launching -= 1
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

    async def parallel(self, names, action):
        sessions = [await self.get(n, launch=True) for n in names]
        tasks = [asyncio.create_task(action(s)) for s in sessions]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return dict(zip(names, results))

    async def parallel_goto(self, names, url, *, wait_until="load"):
        async def go(s):
            return await s.goto(url, wait_until=wait_until)
        return await self.parallel(names, go)

    async def compare(self, names, url, *, wait_until="load"):
        results = await self.parallel_goto(names, url, wait_until=wait_until)
        comparison = {}
        for name, state in results.items():
            if isinstance(state, Exception):
                comparison[name] = {"error": str(state)}
                continue
            s = self._sessions[name]
            network = []
            if s.context:
                network = [
                    {"url": e.url, "status": e.status, "method": e.method}
                    for e in s.context.network.all()
                    if e.url and url in e.url
                ]
            comparison[name] = {
                "url": state.get("url", ""),
                "title": state.get("title", ""),
                "network": network,
            }
        return comparison

    async def diff_responses(self, name_a, name_b, *,
                              url_pattern=None, ignore_noise=True):
        sa = await self.get(name_a)
        sb = await self.get(name_b)
        if not sa.context or not sb.context:
            raise RuntimeError("both sessions need context capture enabled")
        entries_a = sa.context.network.all()
        entries_b = sb.context.network.all()
        return diff_responses(
            entries_a, entries_b, name_a, name_b,
            url_pattern=url_pattern, ignore_noise=ignore_noise)

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

    async def setup_caido(self, *, url=None, token=None, project=None,
                          scope_name=None, allowlist=None, denylist=None,
                          port=8080):
        config = CaidoConfig(
            url=url or f"http://127.0.0.1:{port}",
            token=token,
        )
        self.caido = CaidoClient(config)
        await self.caido.connect()
        result = {"connected": True, "proxy": config.url}
        if project:
            p = await self.caido.ensure_project(project)
            result["project"] = p
        if scope_name and allowlist:
            s = await self.caido.ensure_scope(scope_name, allowlist, denylist)
            result["scope"] = s
        return result

    async def caido_create_session(self, name, *, fingerprint=None,
                                    headless=True, tags=None, **kw):
        if not self.caido:
            raise RuntimeError("call setup_caido() first")
        port = urlparse(self.caido.config.url).port or 8080
        return await self.create(
            name, caido=port, fingerprint=fingerprint,
            headless=headless, tags=tags, **kw)

    def __contains__(self, name):
        return name in self._sessions

    def __len__(self):
        return len(self._sessions)

    def __repr__(self):
        alive = sum(1 for s in self._sessions.values() if s.is_alive)
        return f"<Pool {alive}/{len(self._sessions)} alive base={self.base_dir}>"
