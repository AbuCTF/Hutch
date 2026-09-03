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
            "name": self.name, "headers": headers})

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

    # --- navigation ---

    async def go_back(self, *, wait_until="load"):
        return await self._client._call("go_back", {
            "name": self.name, "wait_until": wait_until})

    async def go_forward(self, *, wait_until="load"):
        return await self._client._call("go_forward", {
            "name": self.name, "wait_until": wait_until})

    async def reload(self, *, wait_until="load"):
        return await self._client._call("reload", {
            "name": self.name, "wait_until": wait_until})

    # --- interaction ---

    async def hover(self, selector):
        return await self._client._call("hover", {
            "name": self.name, "selector": selector})

    async def dblclick(self, selector, *, wait_after="networkidle", timeout=5000):
        return await self._client._call("dblclick", {
            "name": self.name, "selector": selector,
            "wait_after": wait_after, "timeout": timeout})

    async def focus(self, selector):
        return await self._client._call("focus", {
            "name": self.name, "selector": selector})

    async def check(self, selector):
        return await self._client._call("check", {
            "name": self.name, "selector": selector})

    async def uncheck(self, selector):
        return await self._client._call("uncheck", {
            "name": self.name, "selector": selector})

    async def set_checked(self, selector, checked):
        return await self._client._call("set_checked", {
            "name": self.name, "selector": selector, "checked": checked})

    async def set_input_files(self, selector, files):
        return await self._client._call("set_input_files", {
            "name": self.name, "selector": selector, "files": files})

    async def right_click(self, selector):
        return await self._client._call("right_click", {
            "name": self.name, "selector": selector})

    async def scroll(self, *, direction="down", amount=500, selector=None):
        return await self._client._call("scroll", {
            "name": self.name, "direction": direction,
            "amount": amount, "selector": selector})

    async def drag_and_drop(self, source, target):
        return await self._client._call("drag_and_drop", {
            "name": self.name, "source": source, "target": target})

    async def tap(self, selector):
        return await self._client._call("tap", {
            "name": self.name, "selector": selector})

    async def dispatch_event(self, selector, event_type, event_init=None):
        return await self._client._call("dispatch_event", {
            "name": self.name, "selector": selector,
            "event_type": event_type, "event_init": event_init})

    # --- content extraction ---

    async def content(self):
        r = await self._client._call("content", {"name": self.name})
        return r.get("html")

    async def inner_text(self, selector):
        r = await self._client._call("inner_text", {
            "name": self.name, "selector": selector})
        return r.get("text")

    async def inner_html(self, selector):
        r = await self._client._call("inner_html", {
            "name": self.name, "selector": selector})
        return r.get("html")

    async def text_content(self, selector):
        r = await self._client._call("text_content", {
            "name": self.name, "selector": selector})
        return r.get("text")

    async def get_attribute(self, selector, attribute):
        r = await self._client._call("get_attribute", {
            "name": self.name, "selector": selector, "attribute": attribute})
        return r.get("value")

    async def input_value(self, selector):
        r = await self._client._call("input_value", {
            "name": self.name, "selector": selector})
        return r.get("value")

    # --- element state ---

    async def is_visible(self, selector):
        r = await self._client._call("is_visible", {
            "name": self.name, "selector": selector})
        return r.get("visible")

    async def is_checked(self, selector):
        r = await self._client._call("is_checked", {
            "name": self.name, "selector": selector})
        return r.get("checked")

    async def is_enabled(self, selector):
        r = await self._client._call("is_enabled", {
            "name": self.name, "selector": selector})
        return r.get("enabled")

    async def is_hidden(self, selector):
        r = await self._client._call("is_hidden", {
            "name": self.name, "selector": selector})
        return r.get("hidden")

    async def is_editable(self, selector):
        r = await self._client._call("is_editable", {
            "name": self.name, "selector": selector})
        return r.get("editable")

    # --- frames ---

    async def frames(self):
        return await self._client._call("frames", {"name": self.name})

    async def frame_evaluate(self, expression, *, frame_name=None, frame_url=None):
        r = await self._client._call("frame_evaluate", {
            "name": self.name, "expression": expression,
            "frame_name": frame_name, "frame_url": frame_url})
        return r.get("result")

    async def frame_click(self, selector, *, frame_name=None, frame_url=None):
        return await self._client._call("frame_click", {
            "name": self.name, "selector": selector,
            "frame_name": frame_name, "frame_url": frame_url})

    async def frame_fill(self, selector, value, *, frame_name=None, frame_url=None):
        return await self._client._call("frame_fill", {
            "name": self.name, "selector": selector, "value": value,
            "frame_name": frame_name, "frame_url": frame_url})

    async def frame_content(self, *, frame_name=None, frame_url=None):
        r = await self._client._call("frame_content", {
            "name": self.name, "frame_name": frame_name,
            "frame_url": frame_url})
        return r.get("html")

    # --- locator ---

    async def query(self, selector=None, *, text=None, role=None, label=None,
                    placeholder=None, alt_text=None, title=None, test_id=None):
        return await self._client._call("query", {
            "name": self.name, "selector": selector, "text": text,
            "role": role, "label": label, "placeholder": placeholder,
            "alt_text": alt_text, "title": title, "test_id": test_id})

    async def locator_click(self, *, text=None, role=None, locator_name=None,
                            label=None, nth=0):
        return await self._client._call("locator_click", {
            "name": self.name, "text": text, "role": role,
            "locator_name": locator_name, "label": label, "nth": nth})

    async def locator_fill(self, value, *, label=None, placeholder=None,
                           role=None, locator_name=None, nth=0):
        return await self._client._call("locator_fill", {
            "name": self.name, "value": value, "label": label,
            "placeholder": placeholder, "role": role,
            "locator_name": locator_name, "nth": nth})

    # --- advanced waits ---

    async def wait_for_function(self, expression, *, timeout=30000):
        return await self._client._call("wait_for_function", {
            "name": self.name, "expression": expression, "timeout": timeout})

    async def wait_for_load_state(self, state="load", *, timeout=30000):
        return await self._client._call("wait_for_load_state", {
            "name": self.name, "state": state, "timeout": timeout})

    async def wait_for_response(self, url_glob, *, timeout=30000):
        return await self._client._call("wait_for_response", {
            "name": self.name, "url_glob": url_glob, "timeout": timeout})

    async def wait_for_request(self, url_glob, *, timeout=30000):
        return await self._client._call("wait_for_request", {
            "name": self.name, "url_glob": url_glob, "timeout": timeout})

    # --- tabs/pages ---

    async def pages(self):
        return await self._client._call("pages", {"name": self.name})

    async def switch_page(self, index):
        return await self._client._call("switch_page", {
            "name": self.name, "index": index})

    async def close_page(self, index=None):
        return await self._client._call("close_page", {
            "name": self.name, "index": index})

    async def expect_popup(self, selector):
        return await self._client._call("expect_popup", {
            "name": self.name, "selector": selector})

    # --- downloads ---

    async def expect_download(self, selector, *, save_path=None):
        return await self._client._call("expect_download", {
            "name": self.name, "selector": selector, "save_path": save_path})

    # --- dialog ---

    async def handle_dialog(self, *, action="dismiss", prompt_text=None):
        return await self._client._call("handle_dialog", {
            "name": self.name, "action": action, "prompt_text": prompt_text})

    # --- page manipulation ---

    async def set_content(self, html, *, wait_until="load"):
        return await self._client._call("set_content", {
            "name": self.name, "html": html, "wait_until": wait_until})

    async def set_viewport_size(self, width, height):
        return await self._client._call("set_viewport_size", {
            "name": self.name, "width": width, "height": height})

    async def emulate_media(self, *, media=None, color_scheme=None,
                            reduced_motion=None):
        return await self._client._call("emulate_media", {
            "name": self.name, "media": media, "color_scheme": color_scheme,
            "reduced_motion": reduced_motion})

    async def add_script(self, *, url=None, content=None):
        return await self._client._call("add_script", {
            "name": self.name, "url": url, "content": content})

    async def add_style(self, *, url=None, content=None):
        return await self._client._call("add_style", {
            "name": self.name, "url": url, "content": content})

    async def add_init_script(self, script):
        return await self._client._call("add_init_script", {
            "name": self.name, "script": script})

    async def bring_to_front(self):
        return await self._client._call("bring_to_front", {
            "name": self.name})

    # --- context-level ---

    async def set_offline(self, offline):
        return await self._client._call("set_offline", {
            "name": self.name, "offline": offline})

    async def set_geolocation(self, latitude, longitude, *, accuracy=None):
        return await self._client._call("set_geolocation", {
            "name": self.name, "latitude": latitude, "longitude": longitude,
            "accuracy": accuracy})

    async def grant_permissions(self, permissions):
        return await self._client._call("grant_permissions", {
            "name": self.name, "permissions": permissions})

    async def clear_permissions(self):
        return await self._client._call("clear_permissions", {
            "name": self.name})

    # --- tracing ---

    async def start_tracing(self, *, screenshots=True, snapshots=True,
                            sources=False):
        return await self._client._call("start_tracing", {
            "name": self.name, "screenshots": screenshots,
            "snapshots": snapshots, "sources": sources})

    async def stop_tracing(self, *, path=None):
        return await self._client._call("stop_tracing", {
            "name": self.name, "path": path})

    # --- pdf ---

    async def pdf(self, *, path=None, format=None, landscape=False,
                  print_background=True):
        return await self._client._call("pdf", {
            "name": self.name, "path": path, "format": format,
            "landscape": landscape, "print_background": print_background})

    # --- mocking ---

    async def mock_response(self, pattern, *, status=200, headers=None,
                            body="", content_type="text/plain"):
        return await self._client._call("mock_response", {
            "name": self.name, "pattern": pattern, "status": status,
            "headers": headers, "body": body, "content_type": content_type})

    async def route_from_har(self, har_path, *, url=None, update=False,
                             not_found="abort"):
        return await self._client._call("route_from_har", {
            "name": self.name, "har_path": har_path, "url": url,
            "update": update, "not_found": not_found})

    async def modify_request(self, pattern, *, headers=None, post_data=None,
                             method=None):
        return await self._client._call("modify_request", {
            "name": self.name, "pattern": pattern, "headers": headers,
            "post_data": post_data, "method": method})

    # --- mouse/keyboard ---

    async def mouse_click(self, x, y, *, button="left", click_count=1, delay=0):
        return await self._client._call("mouse_click", {
            "name": self.name, "x": x, "y": y, "button": button,
            "click_count": click_count, "delay": delay})

    async def mouse_dblclick(self, x, y, *, button="left"):
        return await self._client._call("mouse_dblclick", {
            "name": self.name, "x": x, "y": y, "button": button})

    async def mouse_move(self, x, y, *, steps=1):
        return await self._client._call("mouse_move", {
            "name": self.name, "x": x, "y": y, "steps": steps})

    async def mouse_wheel(self, delta_x=0, delta_y=0):
        return await self._client._call("mouse_wheel", {
            "name": self.name, "delta_x": delta_x, "delta_y": delta_y})

    async def keyboard_press(self, key):
        return await self._client._call("keyboard_press", {
            "name": self.name, "key": key})

    async def keyboard_type(self, text, *, delay=0):
        return await self._client._call("keyboard_type", {
            "name": self.name, "text": text, "delay": delay})

    # --- cdp ---

    async def cdp_send(self, method, params=None):
        r = await self._client._call("cdp_send", {
            "name": self.name, "method": method, "params": params})
        return r.get("result")

    async def cdp_close(self):
        return await self._client._call("cdp_close", {"name": self.name})

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
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                self.sock_path)
        except ConnectionRefusedError:
            raise HutchError(
                f"stale socket at {self.sock_path} — daemon is not running. "
                "Remove the socket and start the daemon with 'hutch serve'"
            ) from None

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
                     locale=None, timezone=None, tags=None, caido=False):
        result = await self._call("create", {
            "name": name, "program": program, "preset": preset,
            "proxy": proxy, "headless": headless,
            "ignore_https_errors": ignore_https_errors,
            "locale": locale, "timezone": timezone,
            "tags": tags or {},
            "caido": caido,
        })
        return SessionHandle(self, name)

    async def session(self, name):
        await self._call("session", {"name": name})
        return SessionHandle(self, name)

    async def list(self, *, alive_only=False):
        return await self._call("list", {"alive_only": alive_only})

    async def destroy(self, name):
        return await self._call("destroy", {"name": name})

    async def parallel_goto(self, names, url, *, wait_until="load"):
        return await self._call("parallel_goto", {
            "names": names, "url": url, "wait_until": wait_until})

    async def diff_responses(self, session_a, session_b, *,
                              url_pattern=None, ignore_noise=True):
        return await self._call("diff_responses", {
            "session_a": session_a, "session_b": session_b,
            "url_pattern": url_pattern, "ignore_noise": ignore_noise})

    async def compare(self, names, url, *, wait_until="load"):
        return await self._call("compare", {
            "names": names, "url": url, "wait_until": wait_until})

    async def alerts(self, *, session=None):
        return await self._call("alerts", {"name": session})

    async def status(self):
        return await self._call("status", {})


async def connect(sock_path=None):
    c = HutchClient(sock_path=sock_path)
    await c.connect()
    return c
