"""Live view: stream a browser's tabs to a local web page and forward mouse and keyboard back."""

import asyncio
import base64
import json
import secrets

import aiohttp
from aiohttp import web


class LiveError(Exception):
    pass


class _CDP:
    def __init__(self, ws):
        self.ws = ws
        self.events = asyncio.Queue()
        self._id = 0
        self._pending = {}

    async def send(self, method, params=None, session=None):
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        await self.ws.send_str(json.dumps(msg))
        return await fut

    async def pump(self):
        try:
            async for m in self.ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(m.data)
                fut = self._pending.pop(data.get("id"), None) if "id" in data else None
                if fut is None:
                    if "method" in data:
                        self.events.put_nowait(data)
                elif not fut.done():
                    if "error" in data:
                        fut.set_exception(LiveError(data["error"].get("message", "cdp error")))
                    else:
                        fut.set_result(data.get("result", {}))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(LiveError("browser closed"))
            self._pending.clear()


_SELECTION_JS = """(() => {
  const a = document.activeElement;
  try {
    if (a && a.selectionStart !== a.selectionEnd && typeof a.value === "string")
      return a.value.slice(a.selectionStart, a.selectionEnd);
  } catch (e) {}
  return String(getSelection());
})()"""


class _Bridge:
    def __init__(self, view, cdp, tab=None):
        self.view = view
        self.cdp = cdp
        self.tab = tab
        self.tabs = {}
        self.current = None
        self.session = None
        self.size = None

    async def run(self):
        pump = asyncio.create_task(self.cdp.pump())
        pump.add_done_callback(lambda _: asyncio.ensure_future(self._gone()))
        events = asyncio.create_task(self._events())
        try:
            await self.cdp.send("Target.setDiscoverTargets", {"discover": True})
            for t in (await self.cdp.send("Target.getTargets"))["targetInfos"]:
                if t["type"] == "page":
                    self.tabs[t["targetId"]] = t
            first = self.tab if self.tab in self.tabs else self._pick()
            if first:
                await self._attach(first)
            else:
                await self._new_tab()
            async for m in self.view:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    await self._input(json.loads(m.data))
                except (LiveError, KeyError, ValueError, TypeError):
                    pass
        except LiveError:
            await self._gone()
        finally:
            events.cancel()
            pump.cancel()

    def _pick(self):
        pages = list(self.tabs)
        real = [t for t in pages if not self.tabs[t]["url"].startswith(("about:", "chrome"))]
        return (real or pages or [None])[0]

    async def _send(self, obj):
        if not self.view.closed:
            await self.view.send_str(json.dumps(obj))

    async def _gone(self):
        if not self.view.closed:
            await self._send({"t": "gone"})
            await self.view.close()

    async def _push_tabs(self):
        tabs = [{"id": k, "title": v.get("title", ""), "url": v.get("url", "")}
                for k, v in self.tabs.items()]
        await self._send({"t": "tabs", "current": self.current, "tabs": tabs})

    async def _attach(self, target):
        if self.session:
            old, self.session = self.session, None
            try:
                await self.cdp.send("Page.stopScreencast", session=old)
                await self.cdp.send("Target.detachFromTarget", {"sessionId": old})
            except LiveError:
                pass
        self.current, self.size = target, None
        await self._push_tabs()
        if not target:
            return
        self.session = (await self.cdp.send(
            "Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
        await self.cdp.send("Target.activateTarget", {"targetId": target})
        await self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, self.session)
        await self.cdp.send("Page.enable", session=self.session)
        await self.cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 80, "maxWidth": 2560, "maxHeight": 1600}, self.session)

    async def _new_tab(self, url="about:blank"):
        target = (await self.cdp.send("Target.createTarget", {"url": url}))["targetId"]
        self.tabs.setdefault(target, {"targetId": target, "url": url, "title": ""})
        await self._attach(target)

    async def _events(self):
        while True:
            e = await self.cdp.events.get()
            method, p = e["method"], e.get("params", {})
            try:
                if method == "Page.screencastFrame":
                    await self._frame(e.get("sessionId"), p)
                elif method == "Page.javascriptDialogOpening" and e.get("sessionId") == self.session:
                    await self._send({"t": "dialog", "kind": p["type"], "message": p["message"],
                                      "default": p.get("defaultPrompt", "")})
                elif method == "Page.javascriptDialogClosed" and e.get("sessionId") == self.session:
                    await self._send({"t": "dialog_closed"})
                elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
                    info = p["targetInfo"]
                    if info["type"] != "page":
                        continue
                    self.tabs[info["targetId"]] = info
                    if method == "Target.targetCreated" and info.get("openerId") == self.current:
                        await self._attach(info["targetId"])
                    else:
                        await self._push_tabs()
                elif method == "Target.targetDestroyed":
                    if self.tabs.pop(p["targetId"], None) is None:
                        continue
                    if p["targetId"] == self.current:
                        self.session = None
                        await self._attach(self._pick())
                    else:
                        await self._push_tabs()
            except LiveError:
                pass

    async def _frame(self, session, p):
        try:
            if session == self.session:
                meta = p["metadata"]
                size = (meta["deviceWidth"], meta["deviceHeight"])
                if size != self.size:
                    self.size = size
                    await self._send({"t": "size", "w": size[0], "h": size[1]})
                if not self.view.closed:
                    await self.view.send_bytes(base64.b64decode(p["data"]))
        finally:
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, session)

    async def _input(self, d):
        t, s = d.get("t"), self.session
        if t == "mouse" and s:
            params = {k: d[k] for k in ("type", "x", "y", "button", "buttons", "clickCount",
                                        "modifiers", "deltaX", "deltaY") if k in d}
            await self.cdp.send("Input.dispatchMouseEvent", params, s)
        elif t == "key" and s:
            params = {k: d[k] for k in ("type", "key", "code", "text", "modifiers", "location",
                                        "autoRepeat") if d.get(k) is not None}
            if d.get("text"):
                params["unmodifiedText"] = d["text"]
            if d.get("keyCode"):
                params["windowsVirtualKeyCode"] = d["keyCode"]
            await self.cdp.send("Input.dispatchKeyEvent", params, s)
        elif t == "text" and s:
            await self.cdp.send("Input.insertText", {"text": d["text"]}, s)
        elif t == "copy" and s:
            r = await self.cdp.send("Runtime.evaluate", {"expression": _SELECTION_JS,
                                                         "returnByValue": True}, s)
            text = r.get("result", {}).get("value")
            if text:
                await self._send({"t": "clip", "text": text})
        elif t == "nav" and s:
            url = d["url"].strip()
            if "://" not in url and not url.startswith(("about:", "chrome:", "data:")):
                url = "https://" + url
            await self.cdp.send("Page.navigate", {"url": url}, s)
        elif t in ("back", "forward") and s:
            h = await self.cdp.send("Page.getNavigationHistory", session=s)
            i = h["currentIndex"] + (-1 if t == "back" else 1)
            if 0 <= i < len(h["entries"]):
                await self.cdp.send("Page.navigateToHistoryEntry",
                                    {"entryId": h["entries"][i]["id"]}, s)
        elif t == "dialog" and s:
            params = {"accept": bool(d.get("accept"))}
            if d.get("text") is not None:
                params["promptText"] = d["text"]
            await self.cdp.send("Page.handleJavaScriptDialog", params, s)
        elif t == "reload" and s:
            await self.cdp.send("Page.reload", session=s)
        elif t == "tab" and d.get("id") in self.tabs:
            await self._attach(d["id"])
        elif t == "newtab":
            await self._new_tab()
        elif t == "closetab" and d.get("id") in self.tabs:
            await self.cdp.send("Target.closeTarget", {"targetId": d["id"]})


class LiveServer:
    """One local HTTP server per process; each registered session gets /v/<name>."""

    def __init__(self, host="127.0.0.1", port=0):
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(16)
        self._sources = {}
        self._runner = None

    @property
    def running(self):
        return self._runner is not None

    def add(self, name, resolve):
        """resolve() returns the browser's CDP websocket URL, or None when it is not running."""
        self._sources[name] = resolve

    def url(self, name, tab=None):
        return (f"http://{self.host}:{self.port}/v/{name}?t={self.token}"
                + (f"&tab={tab}" if tab else ""))

    async def start(self):
        if self._runner:
            return
        app = web.Application()
        app.router.add_get("/v/{name}", self._page)
        app.router.add_get("/ws/{name}", self._socket)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def _check(self, request):
        name = request.match_info["name"]
        if not secrets.compare_digest(request.query.get("t", ""), self.token):
            raise web.HTTPForbidden()
        if name not in self._sources:
            raise web.HTTPNotFound()
        return name

    async def _page(self, request):
        name = self._check(request)
        return web.Response(text=_PAGE.replace("{{name}}", name), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def _socket(self, request):
        name = self._check(request)
        view = web.WebSocketResponse(heartbeat=20)
        await view.prepare(request)
        resolve = self._sources[name]
        try:
            ws_url = await asyncio.to_thread(resolve)
        except Exception:
            ws_url = None
        if not ws_url:
            await view.send_str(json.dumps({"t": "gone"}))
            await view.close()
            return view
        async with aiohttp.ClientSession() as http:
            try:
                cdp_ws = await http.ws_connect(ws_url, max_msg_size=0)
            except aiohttp.ClientError:
                await view.send_str(json.dumps({"t": "gone"}))
                await view.close()
                return view
            async with cdp_ws:
                await _Bridge(view, _CDP(cdp_ws), request.query.get("tab")).run()
        return view


_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{name}} · hutch</title>
<style>
:root{--bg:#0f1012;--bar:#18191c;--tab:#202226;--on:#2a2d32;--fg:#e4e5e7;--mute:#8b8f96;--line:#2c2f34;--ok:#3fb950;--off:#d29922}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.3 system-ui,-apple-system,"Segoe UI",sans-serif}
body{display:flex;flex-direction:column}
#tabs{display:flex;gap:2px;padding:6px 6px 0;background:var(--bar);overflow-x:auto;scrollbar-width:none}
.tab{display:flex;align-items:center;gap:8px;min-width:0;max-width:220px;padding:7px 10px;border-radius:7px 7px 0 0;background:var(--tab);color:var(--mute);cursor:pointer;flex:0 1 auto}
.tab span{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.tab.on{background:var(--on);color:var(--fg)}
.tab b{font-weight:400;opacity:.45;padding:0 2px}.tab b:hover{opacity:1}
#bar{display:flex;align-items:center;gap:4px;padding:6px 8px;background:var(--on);border-bottom:1px solid var(--line)}
button{background:none;border:0;color:var(--fg);font:16px/1 system-ui;width:30px;height:28px;border-radius:6px;cursor:pointer}
button:hover{background:#383b41}
#url{flex:1;min-width:0;height:28px;background:var(--bar);border:1px solid var(--line);border-radius:14px;color:var(--fg);padding:0 12px;font:inherit;outline:none}
#url:focus{border-color:#4c8dff}
#st{display:flex;align-items:center;gap:6px;color:var(--mute);padding:0 6px;white-space:nowrap}
#st i{width:8px;height:8px;border-radius:50%;background:var(--off)}
#st.live i{background:var(--ok)}
#wrap{position:relative;flex:1;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden}
#dlg{position:absolute;top:12px;left:50%;transform:translateX(-50%);display:none;gap:8px;align-items:center;max-width:min(640px,92%);padding:10px 12px;background:var(--on);border:1px solid var(--line);border-radius:8px;box-shadow:0 6px 24px #0008}
#dlg.on{display:flex;flex-wrap:wrap}
#dlg p{margin:0;flex:1 1 100%;white-space:pre-wrap;word-break:break-word}
#dlg input{flex:1;min-width:0;height:28px;background:var(--bar);border:1px solid var(--line);border-radius:6px;color:var(--fg);padding:0 8px;font:inherit}
#dlg button{width:auto;padding:0 12px;font-size:13px;border:1px solid var(--line)}
canvas{max-width:100%;max-height:100%;outline:none;background:#fff}
</style></head><body>
<div id="tabs"></div>
<div id="bar">
<button id="back" title="Back">&#8249;</button><button id="fwd" title="Forward">&#8250;</button><button id="rel" title="Reload">&#8635;</button>
<input id="url" spellcheck="false" autocomplete="off"><button id="new" title="New tab">+</button>
<span id="st"><i></i><span>connecting</span></span>
</div>
<div id="wrap"><canvas id="c" tabindex="0" width="1280" height="720"></canvas>
<div id="dlg"><p></p><input><button id="ok">OK</button><button id="no">Cancel</button></div></div>
<script>
const $ = id => document.getElementById(id);
const c = $("c"), ctx = c.getContext("2d"), url = $("url"), tabsEl = $("tabs"), st = $("st");
let ws, W = 0, H = 0, cur = null, gone = false;
const status = (text, live) => { st.lastChild.textContent = text; st.className = live ? "live" : ""; };
const send = o => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };

function connect() {
  ws = new WebSocket(location.href.replace(/^http/, "ws").replace("/v/", "/ws/"));
  ws.binaryType = "blob";
  ws.onopen = () => { gone = false; status("live", true); };
  ws.onclose = () => { status(gone ? "browser closed" : "reconnecting", false); setTimeout(connect, gone ? 4000 : 1000); };
  ws.onmessage = async e => {
    if (typeof e.data !== "string") {
      const b = await createImageBitmap(e.data);
      if (c.width !== b.width || c.height !== b.height) { c.width = b.width; c.height = b.height; }
      ctx.drawImage(b, 0, 0); b.close(); return;
    }
    const m = JSON.parse(e.data);
    if (m.t === "size") { W = m.w; H = m.h; }
    else if (m.t === "tabs") { cur = m.current; tabs(m.tabs); }
    else if (m.t === "clip") { navigator.clipboard && navigator.clipboard.writeText(m.text).catch(() => {}); }
    else if (m.t === "gone") { gone = true; }
    else if (m.t === "dialog") { dialog(m); }
    else if (m.t === "dialog_closed") { dlg.className = ""; }
  };
}

function tabs(list) {
  tabsEl.replaceChildren();
  for (const t of list) {
    const d = document.createElement("div"), s = document.createElement("span"), x = document.createElement("b");
    d.className = "tab" + (t.id === cur ? " on" : ""); d.title = t.url;
    s.textContent = t.title || t.url || "New tab"; x.textContent = "×";
    x.onclick = ev => { ev.stopPropagation(); send({ t: "closetab", id: t.id }); };
    d.onclick = () => send({ t: "tab", id: t.id });
    d.append(s, x); tabsEl.append(d);
    if (t.id === cur) {
      if (document.activeElement !== url) url.value = t.url === "about:blank" ? "" : t.url;
      document.title = (t.title || t.url || "New tab") + " · {{name}}";
    }
  }
}

const mods = e => (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
const BTN = ["left", "middle", "right"];
const held = b => b & 1 ? "left" : b & 4 ? "middle" : b & 2 ? "right" : "none";
function pos(e) {
  const r = c.getBoundingClientRect();
  return { x: (e.clientX - r.left) / r.width * (W || c.width), y: (e.clientY - r.top) / r.height * (H || c.height) };
}
const mouse = (type, e, extra) => send({ t: "mouse", type, ...pos(e), modifiers: mods(e), buttons: e.buttons, ...extra });

c.addEventListener("mousedown", e => { e.preventDefault(); c.focus(); mouse("mousePressed", e, { button: BTN[e.button] || "left", clickCount: e.detail || 1 }); });
c.addEventListener("mouseup", e => mouse("mouseReleased", e, { button: BTN[e.button] || "left", clickCount: e.detail || 1 }));
let moved = null;
c.addEventListener("mousemove", e => {
  if (!moved) requestAnimationFrame(() => { send(moved); moved = null; });
  moved = { t: "mouse", type: "mouseMoved", ...pos(e), modifiers: mods(e), buttons: e.buttons, button: held(e.buttons) };
});
c.addEventListener("wheel", e => {
  e.preventDefault();
  const k = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
  mouse("mouseWheel", e, { deltaX: e.deltaX * k, deltaY: e.deltaY * k });
}, { passive: false });
c.addEventListener("contextmenu", e => e.preventDefault());

const shortcut = e => e.ctrlKey || e.metaKey;
c.addEventListener("keydown", e => {
  if (shortcut(e) && e.key.toLowerCase() === "v") return;
  e.preventDefault();
  if (shortcut(e) && "cx".includes(e.key.toLowerCase())) send({ t: "copy" });
  const text = e.key === "Enter" ? "\r" : e.key.length === 1 && !shortcut(e) && !e.altKey ? e.key : undefined;
  send({ t: "key", type: text ? "keyDown" : "rawKeyDown", key: e.key, code: e.code, text, keyCode: e.keyCode, modifiers: mods(e), location: e.location, autoRepeat: e.repeat });
});
c.addEventListener("keyup", e => { e.preventDefault(); send({ t: "key", type: "keyUp", key: e.key, code: e.code, keyCode: e.keyCode, modifiers: mods(e), location: e.location }); });
document.addEventListener("paste", e => {
  if (document.activeElement !== c) return;
  const text = e.clipboardData.getData("text");
  if (text) send({ t: "text", text });
  e.preventDefault();
});

url.addEventListener("keydown", e => { if (e.key === "Enter") { send({ t: "nav", url: url.value }); c.focus(); } });
url.addEventListener("focus", () => url.select());
$("back").onclick = () => send({ t: "back" });
$("fwd").onclick = () => send({ t: "forward" });
$("rel").onclick = () => send({ t: "reload" });
$("new").onclick = () => { send({ t: "newtab" }); url.focus(); };
const dlg = $("dlg"), dlgIn = dlg.querySelector("input");
function dialog(m) {
  dlg.querySelector("p").textContent = m.message;
  dlgIn.style.display = m.kind === "prompt" ? "" : "none"; dlgIn.value = m.default || "";
  $("no").style.display = m.kind === "alert" ? "none" : "";
  dlg.className = "on"; (m.kind === "prompt" ? dlgIn : $("ok")).focus();
}
const answer = accept => { send({ t: "dialog", accept, text: dlgIn.style.display ? undefined : dlgIn.value }); dlg.className = ""; c.focus(); };
$("ok").onclick = () => answer(true);
$("no").onclick = () => answer(false);
dlgIn.addEventListener("keydown", e => { if (e.key === "Enter") answer(true); if (e.key === "Escape") answer(false); });
connect();
c.focus();
</script></body></html>
"""
