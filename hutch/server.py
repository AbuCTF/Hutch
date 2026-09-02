import asyncio
import json
import os
import signal
import time
from dataclasses import asdict

from .artifacts import ArtifactStore
from .context import Snapshot, Diff
from .fingerprint import generate, generate_for_program
from .health import HealthMonitor, wire_context_to_health
from .pool import Pool
from .session import ProxyConfig


_DEFAULT_SOCK = os.path.expanduser("~/.hutch/hutch.sock")
_DEFAULT_PID = os.path.expanduser("~/.hutch/hutch.pid")


class HutchDaemon:

    def __init__(self, *, base_dir=None, max_sessions=10,
                 idle_timeout=900, sock_path=None):
        self.pool = Pool(base_dir=base_dir, max_sessions=max_sessions)
        self.artifacts = ArtifactStore()
        self.sock_path = sock_path or _DEFAULT_SOCK
        self.idle_timeout = idle_timeout
        self._health = {}
        self._server = None
        self._idle_task = None
        self._running = False

    async def start(self):
        await self.pool.start()
        self._running = True
        os.makedirs(os.path.dirname(self.sock_path), exist_ok=True)
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.sock_path)
        os.chmod(self.sock_path, 0o600)
        if self.idle_timeout > 0:
            self._idle_task = asyncio.create_task(self._idle_loop())
        self._write_pid()

    async def stop(self):
        self._running = False
        if self._idle_task:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        await self.pool.stop()
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        self._remove_pid()

    async def serve_forever(self):
        await self.start()
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
        await self.stop()

    def _write_pid(self):
        os.makedirs(os.path.dirname(_DEFAULT_PID), exist_ok=True)
        with open(_DEFAULT_PID, "w") as f:
            f.write(str(os.getpid()))

    def _remove_pid(self):
        if os.path.exists(_DEFAULT_PID):
            os.unlink(_DEFAULT_PID)

    async def _idle_loop(self):
        while self._running:
            await asyncio.sleep(60)
            now = time.time()
            for s in self.pool.list():
                if s.is_alive and (now - s._last_activity) > self.idle_timeout:
                    await s.close()

    def _get_health(self, name):
        if name not in self._health:
            self._health[name] = HealthMonitor(name)
            session = self.pool._sessions.get(name)
            if session and session.context:
                wire_context_to_health(session.context, self._health[name])
        return self._health[name]

    async def _handle_client(self, reader, writer):
        buf = b""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError:
                        await self._send(writer, {"error": "invalid json"})
                        continue
                    resp = await self._dispatch(req)
                    await self._send(writer, resp)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _send(self, writer, data):
        writer.write(json.dumps(data, default=_serialize).encode() + b"\n")
        await writer.drain()

    async def _dispatch(self, req):
        method = req.get("method", "")
        params = req.get("params", {})
        req_id = req.get("id")

        try:
            result = await self._call(method, params)
            resp = {"id": req_id, "result": result}
        except Exception as e:
            resp = {"id": req_id, "error": str(e)}
        return resp

    async def _call(self, method, params):
        handlers = {
            "create": self._rpc_create,
            "session": self._rpc_session,
            "list": self._rpc_list,
            "close": self._rpc_close,
            "destroy": self._rpc_destroy,
            "goto": self._rpc_goto,
            "click": self._rpc_click,
            "fill": self._rpc_fill,
            "evaluate": self._rpc_evaluate,
            "snapshot": self._rpc_snapshot,
            "diff": self._rpc_diff,
            "screenshot": self._rpc_screenshot,
            "network": self._rpc_network,
            "console": self._rpc_console,
            "errors": self._rpc_errors,
            "export_har": self._rpc_export_har,
            "note": self._rpc_note,
            "notes": self._rpc_notes,
            "alerts": self._rpc_alerts,
            "status": self._rpc_status,
            "ping": self._rpc_ping,
        }
        handler = handlers.get(method)
        if not handler:
            raise ValueError(f"unknown method: {method}")
        return await handler(params)

    async def _rpc_ping(self, params):
        return {"pong": True, "uptime": time.time()}

    async def _rpc_create(self, params):
        name = params["name"]
        fp = None
        if params.get("preset"):
            fp = generate(preset=params["preset"],
                          locale=params.get("locale"),
                          timezone=params.get("timezone"))
        elif params.get("program"):
            fp = generate_for_program(params["program"],
                                      locale=params.get("locale"),
                                      timezone=params.get("timezone"))
        proxy = None
        if params.get("proxy"):
            proxy = ProxyConfig(server=params["proxy"])
        s = await self.pool.create(
            name, proxy=proxy, fingerprint=fp,
            headless=params.get("headless", True),
            ignore_https_errors=params.get("ignore_https_errors", False),
            tags=params.get("tags", {}),
        )
        self._get_health(name)
        return _session_info(s)

    async def _rpc_session(self, params):
        name = params["name"]
        s = await self.pool.get(name, launch=True)
        self._get_health(name)
        return _session_info(s)

    async def _rpc_list(self, params):
        sessions = self.pool.list(alive_only=params.get("alive_only", False))
        return [_session_info(s) for s in sessions]

    async def _rpc_close(self, params):
        await self.pool.close(params["name"])
        return {"closed": params["name"]}

    async def _rpc_destroy(self, params):
        name = params["name"]
        await self.pool.destroy(name)
        self._health.pop(name, None)
        self.artifacts.purge(name)
        return {"destroyed": name}

    async def _get_page(self, name):
        s = await self.pool.get(name, launch=True)
        s._last_activity = time.time()
        pages = [p for p in s._pages if not p.is_closed()]
        if not pages:
            page = await s.new_page()
            return s, page
        return s, pages[-1]

    async def _rpc_goto(self, params):
        s, page = await self._get_page(params["name"])
        await page.goto(params["url"], wait_until=params.get("wait_until", "load"))
        return {"url": page.url, "title": await page.title()}

    async def _rpc_click(self, params):
        _, page = await self._get_page(params["name"])
        await page.click(params["selector"])
        return {"clicked": params["selector"]}

    async def _rpc_fill(self, params):
        _, page = await self._get_page(params["name"])
        await page.fill(params["selector"], params["value"])
        return {"filled": params["selector"]}

    async def _rpc_evaluate(self, params):
        _, page = await self._get_page(params["name"])
        result = await page.evaluate(params["expression"])
        return {"result": result}

    async def _rpc_snapshot(self, params):
        name = params["name"]
        s = await self.pool.get(name, launch=True)
        snap = await s.snapshot(
            screenshot=params.get("screenshot", False),
            storage=params.get("storage", False),
            full=params.get("full", False),
        )
        if params.get("save"):
            self.artifacts.save_snapshot(name, asdict(snap))
        return asdict(snap)

    async def _rpc_diff(self, params):
        name = params["name"]
        s = await self.pool.get(name, launch=True)
        d = await s.diff(since=params.get("since"))
        return asdict(d)

    async def _rpc_screenshot(self, params):
        s, page = await self._get_page(params["name"])
        png = await page.screenshot(full_page=params.get("full_page", False))
        if params.get("save", True):
            meta = self.artifacts.save_screenshot(params["name"], png,
                                                  label=params.get("label"))
            return {"path": meta.path, "size": len(png)}
        return {"size": len(png)}

    async def _rpc_network(self, params):
        name = params["name"]
        s = await self.pool.get(name)
        if not s.context:
            return []
        entries = s.context.query_network(
            pattern=params.get("pattern"),
            method=params.get("method"),
            since=params.get("since"),
        )
        return [asdict(e) for e in entries]

    async def _rpc_console(self, params):
        name = params["name"]
        s = await self.pool.get(name)
        if not s.context:
            return []
        entries = s.context.query_console(
            level=params.get("level"),
            pattern=params.get("pattern"),
            since=params.get("since"),
        )
        return [asdict(e) for e in entries]

    async def _rpc_errors(self, params):
        name = params["name"]
        s = await self.pool.get(name)
        if not s.context:
            return []
        return [asdict(e) for e in s.context.errors.all()]

    async def _rpc_export_har(self, params):
        name = params["name"]
        s = await self.pool.get(name)
        if not s.context:
            return {"error": "no context capture"}
        entries = s.context.network.all()
        har = _build_har(entries)
        meta = self.artifacts.save_har(name, har, label=params.get("label"))
        return {"path": meta.path, "entries": len(entries)}

    async def _rpc_note(self, params):
        self.artifacts.note(params["name"], params["key"], params["value"])
        return {"saved": params["key"]}

    async def _rpc_notes(self, params):
        return self.artifacts.notes(params["name"])

    async def _rpc_alerts(self, params):
        name = params.get("name")
        if name:
            mon = self._get_health(name)
            return [asdict(a) for a in mon.unacknowledged()]
        all_alerts = []
        for mon in self._health.values():
            all_alerts.extend(mon.unacknowledged())
        all_alerts.sort(key=lambda a: a["timestamp"], reverse=True)
        return all_alerts

    async def _rpc_status(self, params):
        pool_status = self.pool.status()
        pool_status["health"] = {
            name: mon.summary() for name, mon in self._health.items()
        }
        return pool_status


def _session_info(s):
    return {
        "name": s.name,
        "alive": s.is_alive,
        "pages": s.page_count,
        "headless": s.headless,
        "proxy": s.proxy.server if s.proxy else None,
        "fingerprint": f"{s.fingerprint.viewport_width}x{s.fingerprint.viewport_height}",
        "tags": s.tags,
    }


def _build_har(entries):
    har_entries = []
    for e in entries:
        har_entries.append({
            "startedDateTime": time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(e.timestamp)),
            "time": e.timing,
            "request": {
                "method": e.method,
                "url": e.url,
                "headers": [{"name": k, "value": v}
                            for k, v in (e.request_headers or {}).items()],
                "postData": {"text": e.request_body} if e.request_body else None,
            },
            "response": {
                "status": e.status or 0,
                "headers": [{"name": k, "value": v}
                            for k, v in (e.response_headers or {}).items()],
                "content": {"text": e.response_body} if e.response_body else {},
                "bodySize": e.size,
            },
            "resourceType": e.resource_type,
        })
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "hutch", "version": "0.2.0"},
            "entries": har_entries,
        }
    }


def _serialize(obj):
    if isinstance(obj, bytes):
        return None
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(f"not serializable: {type(obj)}")


def is_daemon_running(sock_path=None):
    sock = sock_path or _DEFAULT_SOCK
    return os.path.exists(sock)
