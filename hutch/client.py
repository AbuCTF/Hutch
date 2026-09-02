import asyncio
import json
import os
from dataclasses import dataclass
from typing import Optional

_DEFAULT_SOCK = os.path.expanduser("~/.hutch/hutch.sock")


class HutchError(Exception):
    pass


class SessionHandle:

    def __init__(self, client, name):
        self._client = client
        self.name = name

    async def goto(self, url, *, wait_until="load"):
        return await self._client._call("goto", {
            "name": self.name, "url": url, "wait_until": wait_until})

    async def click(self, selector, *, wait_after="networkidle", timeout=5000):
        return await self._client._call("click", {
            "name": self.name, "selector": selector,
            "wait_after": wait_after, "timeout": timeout})

    async def fill(self, selector, value, *, press_enter=False):
        return await self._client._call("fill", {
            "name": self.name, "selector": selector,
            "value": value, "press_enter": press_enter})

    async def type_text(self, selector, text, *, delay=50):
        return await self._client._call("type", {
            "name": self.name, "selector": selector,
            "text": text, "delay": delay})

    async def press(self, selector, key):
        return await self._client._call("press", {
            "name": self.name, "selector": selector, "key": key})

    async def select_option(self, selector, value):
        return await self._client._call("select_option", {
            "name": self.name, "selector": selector, "value": value})

    async def wait_for(self, selector=None, *, state="visible",
                       url=None, timeout=30000):
        return await self._client._call("wait_for", {
            "name": self.name, "selector": selector,
            "state": state, "url": url, "timeout": timeout})

    async def evaluate(self, expression):
        r = await self._client._call("evaluate", {
            "name": self.name, "expression": expression})
        return r.get("result")

    async def cookies(self, urls=None):
        return await self._client._call("cookies", {
            "name": self.name, "urls": urls})

    async def set_cookie(self, cookie_name, value, **kwargs):
        return await self._client._call("set_cookie", {
            "name": self.name, "cookie_name": cookie_name,
            "value": value, **kwargs})

    async def delete_cookies(self):
        return await self._client._call("delete_cookies", {
            "name": self.name})

    async def storage(self):
        return await self._client._call("storage", {"name": self.name})

    async def set_storage(self, key, value, *, session_storage=False):
        return await self._client._call("set_storage", {
            "name": self.name, "key": key, "value": value,
            "session_storage": session_storage})

    async def observe(self):
        return await self._client._call("observe", {"name": self.name})

    async def page_state(self):
        return await self._client._call("snapshot", {
            "name": self.name, "full": False})

    async def snapshot(self, *, screenshot=False, storage=False, full=False, save=False):
        return await self._client._call("snapshot", {
            "name": self.name, "screenshot": screenshot,
            "storage": storage, "full": full, "save": save})

    async def diff(self, *, since=None):
        return await self._client._call("diff", {
            "name": self.name, "since": since})

    async def screenshot(self, *, full_page=False, save=True, label=None):
        return await self._client._call("screenshot", {
            "name": self.name, "full_page": full_page,
            "save": save, "label": label})

    async def network(self, *, pattern=None, method=None, since=None):
        return await self._client._call("network", {
            "name": self.name, "pattern": pattern,
            "method": method, "since": since})

    async def console(self, *, level=None, pattern=None, since=None):
        return await self._client._call("console", {
            "name": self.name, "level": level,
            "pattern": pattern, "since": since})

    async def errors(self):
        return await self._client._call("errors", {"name": self.name})

    async def export_har(self, *, label=None):
        return await self._client._call("export_har", {
            "name": self.name, "label": label})

    async def note(self, key, value):
        return await self._client._call("note", {
            "name": self.name, "key": key, "value": value})

    async def notes(self):
        return await self._client._call("notes", {"name": self.name})

    async def set_headers(self, headers):
        return await self._client._call("set_headers", {
            "name": self.name, **headers})

    async def block_urls(self, patterns):
        return await self._client._call("block_urls", {
            "name": self.name, "patterns": patterns})

    async def modify_headers(self, headers, *, pattern="**/*"):
        return await self._client._call("modify_headers", {
            "name": self.name, "pattern": pattern, "headers": headers})

    async def clear_intercepts(self):
        return await self._client._call("clear_intercepts", {
            "name": self.name})

    async def pause(self, *, reason="manual"):
        return await self._client._call("pause", {
            "name": self.name, "reason": reason})

    async def resume(self):
        return await self._client._call("resume", {"name": self.name})

    async def handoff(self):
        return await self._client._call("handoff", {"name": self.name})

    async def close(self):
        return await self._client._call("close", {"name": self.name})

    async def destroy(self):
        return await self._client._call("destroy", {"name": self.name})

    def __repr__(self):
        return f"<SessionHandle '{self.name}'>"


class HutchClient:

    def __init__(self, sock_path=None):
        self.sock_path = sock_path or _DEFAULT_SOCK
        self._reader = None
        self._writer = None
        self._req_id = 0
        self._lock = asyncio.Lock()

    async def connect(self):
        if not os.path.exists(self.sock_path):
            raise HutchError(
                f"daemon not running (no socket at {self.sock_path}) — "
                "start it with 'hutch serve'")
        self._reader, self._writer = await asyncio.open_unix_connection(
            self.sock_path)

    async def close(self):
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _call(self, method, params=None):
        if not self._writer:
            raise HutchError("not connected")
        async with self._lock:
            self._req_id += 1
            req = {"id": self._req_id, "method": method, "params": params or {}}
            self._writer.write(json.dumps(req).encode() + b"\n")
            await self._writer.drain()
            line = await self._reader.readline()
            if not line:
                raise HutchError("connection closed")
            resp = json.loads(line)
        if "error" in resp:
            raise HutchError(resp["error"])
        return resp.get("result")

    async def ping(self):
        return await self._call("ping")

    async def create(self, name, *, program=None, preset=None, proxy=None,
                     headless=True, ignore_https_errors=False,
                     locale=None, timezone=None, tags=None):
        result = await self._call("create", {
            "name": name, "program": program, "preset": preset,
            "proxy": proxy, "headless": headless,
            "ignore_https_errors": ignore_https_errors,
            "locale": locale, "timezone": timezone,
            "tags": tags or {},
        })
        return SessionHandle(self, name)

    async def session(self, name):
        await self._call("session", {"name": name})
        return SessionHandle(self, name)

    async def list(self, *, alive_only=False):
        return await self._call("list", {"alive_only": alive_only})

    async def destroy(self, name):
        return await self._call("destroy", {"name": name})

    async def alerts(self, *, session=None):
        return await self._call("alerts", {"name": session})

    async def status(self):
        return await self._call("status", {})


async def connect(sock_path=None):
    c = HutchClient(sock_path=sock_path)
    await c.connect()
    return c
