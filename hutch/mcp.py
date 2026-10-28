"""MCP server -- exposes Hutch browser session tools to Claude/agents via MCP.

Sessions are real chromium processes reachable over CDP, so the MCP server, the daemon,
the CLI and a human's window can all work on the same profile at once.

Tools: hutch_sessions, hutch_navigate, hutch_observe, hutch_interact,
hutch_screenshot, hutch_page_state, hutch_network, hutch_cookies,
hutch_intercept, hutch_evaluate, hutch_content, hutch_diff, hutch_pages,
hutch_health, hutch_auth.

Manages its own Pool inline (like the daemon does).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import asdict

log = logging.getLogger(__name__)


def create_mcp_server():
    """Create an MCP server wrapping Hutch session primitives."""
    from mcp.server import Server
    from mcp.types import Tool, TextContent, ImageContent

    server = Server("hutch")
    _pool = None

    # ------------------------------------------------------------------ pool
    async def _get_pool():
        nonlocal _pool
        if _pool is None:
            from hutch.pool import Pool
            _pool = Pool()
            await _pool.start()
        return _pool

    async def _get_session(name: str):
        pool = await _get_pool()
        return await pool.get(name, launch=True)

    _event_cursor = {}

    def _event_line(e):
        t, tab = e["type"], e.get("tab")
        where = f" tab {tab}" if tab is not None else ""
        if t == "dialog":
            return (f"#{e['seq']} {e['kind']} on{where or ' a tab'}: {e['message']!r}, "
                    + ("left for the human" if e["handled"] == "human" else e["handled"]))
        if t == "download":
            target = e.get("path") or e.get("url")
            return f"#{e['seq']} download {e['state']}: {e.get('filename')} {target or ''}".rstrip()
        if t == "filechooser":
            return (f"#{e['seq']} file chooser on{where}, answer with hutch_events action=files"
                    + (" (multiple)" if e.get("multiple") else ""))
        if t == "popup":
            return f"#{e['seq']} popup{where} from tab {e.get('opener')}: {e.get('url')}"
        return f"#{e['seq']} {t}{where} {e.get('url', '')}".rstrip()

    def _new_events(name):
        """Events the agent has not seen yet, appended to every tool result for that session."""
        session = _pool._sessions.get(name) if _pool else None
        if session is None:
            return None
        found = session.events.since(_event_cursor.get(name, 0))
        if not found:
            return None
        _event_cursor[name] = found[-1]["seq"]
        return "events:\n" + "\n".join(_event_line(e) for e in found)

    async def _page_state(session) -> dict:
        """Grab url+title from the active page (cheap feedback)."""
        page = session._active_page()
        if not page:
            return {"url": "", "title": ""}
        try:
            title = await page.title()
        except Exception:
            title = ""
        return {"url": page.url, "title": title}

    def _session_info(s):
        return {
            "name": s.name,
            "alive": s.is_alive,
            "state": s.state.value,
            "pages": s.page_count,
            "headless": s.headless,
            "proxy": s.proxy.server if s.proxy else None,
            "fingerprint": f"{s.fingerprint.viewport_width}x{s.fingerprint.viewport_height}",
            "tags": s.tags,
            "launcher": s.launcher,
            "browser": s.browser,
            "owned": s._owned if s.is_alive else None,
            "running": s.running,
        }

    def _state(result) -> str:
        """Page state after an action: url, title and only what changed on the page."""
        if not isinstance(result, dict) or "dom" not in result:
            return _json(result)
        head = f"{result.get('url', '')} — {result.get('title', '')}"
        if result.get("changes") is not None:
            return head + ("\nchanges:\n" + result["changes"] if result["changes"] else "\nno changes")
        return head + ("\n" + result["dom"] if result.get("dom") else "")

    def _json(obj) -> str:
        return json.dumps(obj, indent=2, default=_serialize)

    def _serialize(obj):
        if isinstance(obj, bytes):
            return None
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        return str(obj)

    # --------------------------------------------------------------- tools
    @server.list_tools()
    async def list_tools():
        return [
            # 1. sessions
            Tool(
                name="hutch_sessions",
                description=(
                    "Manage browser sessions. Actions: list (default), create, close, destroy, "
                    "attach (join a browser hutch did not start: target chrome|brave|edge|port|url), "
                    "detach (disconnect, leave it running), headed / headless (relaunch the same "
                    "profile on/off screen; tabs, cookies and sessionStorage carry over), "
                    "view (live view URL the human opens to watch and take over without a relaunch). "
                    "A session the human opened with 'hutch browse' is joined automatically."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "create", "close", "destroy",
                                     "attach", "detach", "headed", "headless", "view"],
                            "default": "list",
                            "description": "Operation to perform",
                        },
                        "name": {
                            "type": "string",
                            "description": "Session name (required for create/close/destroy)",
                        },
                        "headless": {
                            "type": "boolean",
                            "default": True,
                            "description": "Run headless (create only)",
                        },
                        "browser": {
                            "type": "string",
                            "description": "chromium (default) | chrome | brave | edge | path (create only)",
                        },
                        "target": {
                            "type": "string",
                            "description": "attach only: chrome | brave | edge | port | http://host:port | ws://...",
                        },
                        "proxy": {
                            "type": "string",
                            "description": "Proxy URL e.g. http://127.0.0.1:8080 (create only)",
                        },
                        "preset": {
                            "type": "string",
                            "description": "Fingerprint preset name (create only)",
                        },
                        "program": {
                            "type": "string",
                            "description": "Program name for deterministic fingerprint (create only)",
                        },
                        "locale": {"type": "string", "description": "Locale override (create only)"},
                        "timezone": {"type": "string", "description": "Timezone override (create only)"},
                        "ignore_https_errors": {
                            "type": "boolean",
                            "default": False,
                            "description": "Ignore TLS errors (create only)",
                        },
                        "caido": {
                            "type": "boolean",
                            "default": False,
                            "description": "Route through Caido proxy (create only)",
                        },
                        "tags": {
                            "type": "object",
                            "description": "Metadata tags (create only)",
                        },
                    },
                },
            ),
            # 2. navigate
            Tool(
                name="hutch_navigate",
                description=(
                    "Navigate a session's browser. Supports goto URL, back, "
                    "forward, reload, and wait-for conditions. Returns page state."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "action": {
                            "type": "string",
                            "enum": ["goto", "back", "forward", "reload", "wait_for"],
                            "default": "goto",
                        },
                        "url": {"type": "string", "description": "URL to navigate to (goto) or URL pattern to wait for (wait_for)"},
                        "wait_until": {
                            "type": "string",
                            "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                            "default": "load",
                            "description": "Wait strategy for navigation",
                        },
                        "selector": {"type": "string", "description": "CSS selector to wait for (wait_for action)"},
                        "state": {
                            "type": "string",
                            "enum": ["visible", "hidden", "attached", "detached"],
                            "default": "visible",
                            "description": "Element state to wait for",
                        },
                        "timeout": {"type": "integer", "default": 30000, "description": "Timeout in ms"},
                    },
                    "required": ["session"],
                },
            ),
            # 3. observe
            Tool(
                name="hutch_observe",
                description=(
                    "Accessibility snapshot of the current tab with refs like [ref=e12]. "
                    "Pass a ref as the selector to hutch_interact; refs stay stable while the page "
                    "lives. filter=interactive lists only what can be acted on; diff=true returns only "
                    "what changed since the last look (actions already return their changes). "
                    "mode=elements returns the legacy indexed element list."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "mode": {"type": "string", "enum": ["aria", "elements"], "default": "aria"},
                        "selector": {"type": "string", "description": "Limit the snapshot to this element (ref or CSS)"},
                        "filter": {"type": "string", "enum": ["all", "interactive"], "default": "all"},
                        "diff": {"type": "boolean", "default": False},
                    },
                    "required": ["session"],
                },
            ),
            # 4. interact
            Tool(
                name="hutch_interact",
                description=(
                    "Interact with page elements. Actions: click, fill, type, "
                    "press, select, hover, scroll, dblclick, check, uncheck, "
                    "right_click, tap, focus, upload (set files on a file input: value is a "
                    "comma-separated list of absolute paths). Target by ref from hutch_observe, "
                    "CSS selector or element index. Returns page state after action."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "action": {
                            "type": "string",
                            "enum": [
                                "click", "fill", "type", "press", "select",
                                "hover", "scroll", "dblclick", "check",
                                "uncheck", "right_click", "tap", "focus", "upload",
                            ],
                            "description": "Interaction type",
                        },
                        "selector": {"type": "string", "description": "Ref from hutch_observe (e12), CSS/Playwright selector, or legacy element index"},
                        "value": {"type": "string", "description": "Value for fill/type/select/press"},
                        "press_enter": {"type": "boolean", "default": False, "description": "Press Enter after fill"},
                        "delay": {"type": "integer", "default": 50, "description": "Typing delay in ms (type action)"},
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                            "default": "down",
                            "description": "Scroll direction",
                        },
                        "amount": {"type": "integer", "default": 500, "description": "Scroll pixels"},
                        "timeout": {"type": "integer", "default": 5000, "description": "Wait timeout after click"},
                    },
                    "required": ["session", "action"],
                },
            ),
            # 5. screenshot
            Tool(
                name="hutch_screenshot",
                description=(
                    "Screenshot of the current tab. annotate=true boxes every actionable element "
                    "with its ref (e12) and lists them, so what you see maps straight to selectors."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "full_page": {"type": "boolean", "default": False, "description": "Capture full scrollable page"},
                        "annotate": {"type": "boolean", "default": False, "description": "Label actionable elements with refs (viewport only)"},
                    },
                    "required": ["session"],
                },
            ),
            # 6. page_state
            Tool(
                name="hutch_page_state",
                description=(
                    "Get current page state: URL, title, cookies, and optional "
                    "accessibility tree snapshot."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "include_cookies": {"type": "boolean", "default": False},
                        "include_dom": {"type": "boolean", "default": False, "description": "Include accessibility tree"},
                        "include_storage": {"type": "boolean", "default": False, "description": "Include localStorage/sessionStorage"},
                    },
                    "required": ["session"],
                },
            ),
            # 7. network
            Tool(
                name="hutch_network",
                description=(
                    "Get captured network requests. Filter by URL pattern, "
                    "HTTP method, or status code range. Returns request/response "
                    "pairs with headers and timing."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "pattern": {"type": "string", "description": "URL glob pattern filter"},
                        "method": {"type": "string", "description": "HTTP method filter (GET, POST, etc.)"},
                        "since": {"type": "integer", "description": "Cursor position -- only entries after this"},
                        "limit": {"type": "integer", "default": 50, "description": "Max entries to return"},
                    },
                    "required": ["session"],
                },
            ),
            # 8. cookies
            Tool(
                name="hutch_cookies",
                description=(
                    "Manage cookies. Actions: get (list cookies), set (add a cookie), "
                    "delete (clear all cookies). Returns current cookies after mutation."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "action": {
                            "type": "string",
                            "enum": ["get", "set", "delete"],
                            "default": "get",
                        },
                        "cookie_name": {"type": "string", "description": "Cookie name (set action)"},
                        "cookie_value": {"type": "string", "description": "Cookie value (set action)"},
                        "domain": {"type": "string", "description": "Cookie domain (set action)"},
                        "path": {"type": "string", "default": "/", "description": "Cookie path (set action)"},
                        "urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter cookies by URLs (get action)",
                        },
                    },
                    "required": ["session"],
                },
            ),
            # 9. intercept
            Tool(
                name="hutch_intercept",
                description=(
                    "Set up request interception. Actions: block (abort matching "
                    "requests), modify_headers (inject/override headers on matching "
                    "requests), mock (fulfill with custom response), clear (remove "
                    "all intercepts)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "action": {
                            "type": "string",
                            "enum": ["block", "modify_headers", "mock", "clear"],
                            "description": "Intercept action",
                        },
                        "pattern": {
                            "type": "string",
                            "default": "**/*",
                            "description": "URL glob pattern to match",
                        },
                        "headers": {
                            "type": "object",
                            "description": "Headers to inject/override (modify_headers action)",
                        },
                        "status": {"type": "integer", "default": 200, "description": "Response status (mock action)"},
                        "body": {"type": "string", "default": "", "description": "Response body (mock action)"},
                        "content_type": {
                            "type": "string",
                            "default": "text/plain",
                            "description": "Response content-type (mock action)",
                        },
                    },
                    "required": ["session", "action"],
                },
            ),
            # 10. evaluate
            Tool(
                name="hutch_evaluate",
                description="Run JavaScript in the page context. Returns the expression result.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "expression": {"type": "string", "description": "JavaScript expression to evaluate"},
                        "frame_name": {"type": "string", "description": "Evaluate in a named iframe instead of main frame"},
                        "frame_url": {"type": "string", "description": "Evaluate in an iframe matched by URL"},
                    },
                    "required": ["session", "expression"],
                },
            ),
            # 11. content
            Tool(
                name="hutch_content",
                description=(
                    "Get page text content. With no selector, returns full page "
                    "inner text. With a selector, returns that element's text."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "selector": {"type": "string", "description": "CSS selector (omit for full page)"},
                        "format": {
                            "type": "string",
                            "enum": ["text", "html", "inner_html"],
                            "default": "text",
                            "description": "Output format",
                        },
                    },
                    "required": ["session"],
                },
            ),
            # 12. diff
            Tool(
                name="hutch_diff",
                description=(
                    "Compare current page state with previous observation. "
                    "Shows new network requests, console messages, errors, "
                    "navigations, and cookie changes since last check."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "since": {"type": "integer", "description": "Cursor to diff from (omit for since last diff)"},
                    },
                    "required": ["session"],
                },
            ),
            # 13. pages
            Tool(
                name="hutch_pages",
                description=(
                    "Manage browser tabs. Actions: list, switch (by index), "
                    "close (by index), new (open blank tab)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "action": {
                            "type": "string",
                            "enum": ["list", "switch", "close", "new"],
                            "default": "list",
                        },
                        "index": {"type": "integer", "description": "Tab index (switch/close actions)"},
                    },
                    "required": ["session"],
                },
            ),
            # 14. health
            Tool(
                name="hutch_health",
                description="Session health check: pool status, alive sessions, uptime, resource usage.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {
                            "type": "string",
                            "description": "Session name (omit for pool-wide status)",
                        },
                    },
                },
            ),
            # 15. auth
            Tool(
                name="hutch_auth",
                description=(
                    "Manage auth state. Actions: export (save cookies + storage "
                    "state to JSON), import (restore from JSON), clear (delete "
                    "cookies and storage)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "action": {
                            "type": "string",
                            "enum": ["export", "import", "clear"],
                            "default": "export",
                        },
                        "cookies": {
                            "type": "array",
                            "description": "Cookies array to import (import action)",
                        },
                        "storage": {
                            "type": "object",
                            "description": "Storage state to import: {localStorage: {}, sessionStorage: {}} (import action)",
                        },
                    },
                    "required": ["session"],
                },
            ),
            Tool(
                name="hutch_events",
                description=(
                    "Dialogs, downloads, file choosers, popups and crashes, in order. New events are "
                    "also appended to every hutch tool result. Agent tabs answer dialogs "
                    "automatically: alerts and beforeunload accepted, confirm and prompt dismissed. "
                    "Actions: list (events after 'since'), wait (block until one of 'types' arrives), "
                    "dialog (answer the next dialog with accept/text, once), policy (accept, dismiss "
                    "or auto for every dialog), files (answer an open file chooser with paths)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "action": {
                            "type": "string",
                            "enum": ["list", "wait", "dialog", "policy", "files"],
                            "default": "list",
                        },
                        "since": {"type": "integer", "default": 0, "description": "Only events with a higher seq"},
                        "types": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["dialog", "download", "filechooser", "popup", "crash"]},
                        },
                        "timeout": {"type": "number", "default": 30, "description": "Seconds to wait"},
                        "accept": {"type": "boolean", "default": True},
                        "text": {"type": "string", "description": "Prompt answer"},
                        "policy": {"type": "string", "enum": ["accept", "dismiss", "auto"]},
                        "files": {"type": "array", "items": {"type": "string"}, "description": "Absolute file paths"},
                    },
                    "required": ["session"],
                },
            ),
        ]

    # ---------------------------------------------------------- call_tool
    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            result = await _dispatch(name, arguments)
        except Exception as e:
            log.exception("tool %s failed", name)
            result = [TextContent(type="text", text=f"error: {e}")]
        sname = arguments.get("session") or (arguments.get("name") if name == "hutch_sessions" else None)
        trailer = _new_events(sname) if sname and name != "hutch_events" else None
        if trailer:
            result = list(result) + [TextContent(type="text", text=trailer)]
        return result

    async def _dispatch(name: str, arguments: dict):
        from mcp.types import TextContent, ImageContent

        # ------ hutch_sessions
        if name == "hutch_sessions":
            pool = await _get_pool()
            action = arguments.get("action", "list")

            if action == "list":
                sessions = pool.list()
                return [TextContent(
                    type="text",
                    text=_json([_session_info(s) for s in sessions]),
                )]

            if action == "create":
                sname = arguments.get("name")
                if not sname:
                    return [TextContent(type="text", text="error: 'name' is required for create")]

                fp = None
                if arguments.get("preset"):
                    from hutch.fingerprint import generate
                    fp = generate(
                        preset=arguments["preset"],
                        locale=arguments.get("locale"),
                        timezone=arguments.get("timezone"),
                    )
                elif arguments.get("program"):
                    from hutch.fingerprint import generate_for_program
                    fp = generate_for_program(
                        arguments["program"],
                        locale=arguments.get("locale"),
                        timezone=arguments.get("timezone"),
                    )

                proxy = None
                if arguments.get("proxy"):
                    from hutch.session import ProxyConfig
                    proxy = ProxyConfig(server=arguments["proxy"])

                s = await pool.create(
                    sname,
                    proxy=proxy,
                    fingerprint=fp,
                    headless=arguments.get("headless", True),
                    ignore_https_errors=arguments.get("ignore_https_errors", False),
                    tags=arguments.get("tags", {}),
                    caido=arguments.get("caido", False),
                    browser=arguments.get("browser") or "chromium",
                )
                return [TextContent(type="text", text=_json(_session_info(s)))]

            if action == "view":
                sname = arguments.get("name")
                if not sname:
                    return [TextContent(type="text", text="error: 'name' is required for view")]
                return [TextContent(type="text", text=await pool.view(sname))]

            if action in ("attach", "detach", "headed", "headless"):
                sname = arguments.get("name")
                if not sname:
                    return [TextContent(type="text", text=f"error: 'name' is required for {action}")]
                if action == "attach":
                    if not arguments.get("target"):
                        return [TextContent(type="text", text="error: 'target' is required for attach")]
                    s = await pool.attach(sname, arguments["target"])
                elif action == "detach":
                    s = await pool.get(sname)
                    await s.detach()
                else:
                    s = await pool.set_headless(sname, action == "headless")
                info = _session_info(s)
                if s.is_alive:
                    info["tabs"] = await s.pages()
                return [TextContent(type="text", text=_json(info))]

            if action == "close":
                sname = arguments.get("name")
                if not sname:
                    return [TextContent(type="text", text="error: 'name' is required for close")]
                await pool.close(sname)
                return [TextContent(type="text", text=_json({"closed": sname}))]

            if action == "destroy":
                sname = arguments.get("name")
                if not sname:
                    return [TextContent(type="text", text="error: 'name' is required for destroy")]
                await pool.destroy(sname)
                return [TextContent(type="text", text=_json({"destroyed": sname}))]

            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        # ------ hutch_navigate
        if name == "hutch_navigate":
            session = await _get_session(arguments["session"])
            action = arguments.get("action", "goto")

            if action == "goto":
                url = arguments.get("url")
                if not url:
                    return [TextContent(type="text", text="error: 'url' required for goto")]
                result = await session.goto(
                    url,
                    wait_until=arguments.get("wait_until", "load"),
                )
                return [TextContent(type="text", text=_state(result))]

            if action == "back":
                result = await session.go_back(
                    wait_until=arguments.get("wait_until", "load"),
                )
                return [TextContent(type="text", text=_state(result))]

            if action == "forward":
                result = await session.go_forward(
                    wait_until=arguments.get("wait_until", "load"),
                )
                return [TextContent(type="text", text=_state(result))]

            if action == "reload":
                result = await session.reload(
                    wait_until=arguments.get("wait_until", "load"),
                )
                return [TextContent(type="text", text=_state(result))]

            if action == "wait_for":
                result = await session.wait_for(
                    arguments.get("selector"),
                    state=arguments.get("state", "visible"),
                    url=arguments.get("url"),
                    timeout=arguments.get("timeout", 30000),
                )
                return [TextContent(type="text", text=_state(result))]

            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        # ------ hutch_observe
        if name == "hutch_observe":
            session = await _get_session(arguments["session"])
            if arguments.get("mode", "aria") == "aria":
                snap = await session.aria_snapshot(
                    selector=arguments.get("selector"),
                    interactive=arguments.get("filter") == "interactive",
                    diff=bool(arguments.get("diff")))
                state = await _page_state(session)
                return [TextContent(type="text", text=f"{state['url']} — {state['title']}\n{snap}")]
            elements = await session.observe()
            state = await _page_state(session)
            return [TextContent(type="text", text=_json({
                "page": state,
                "elements": elements,
                "count": len(elements),
            }))]

        # ------ hutch_interact
        if name == "hutch_interact":
            session = await _get_session(arguments["session"])
            action = arguments["action"]
            selector = arguments.get("selector", "")

            # resolve element index to selector via observe
            if selector and selector.isdigit():
                idx = int(selector)
                elements = await session.observe()
                match = [e for e in elements if e.get("idx") == idx]
                if match:
                    selector = match[0].get("selector", selector)
                else:
                    return [TextContent(
                        type="text",
                        text=f"error: element index {idx} not found (page has {len(elements)} elements)",
                    )]

            if action == "click":
                result = await session.click(
                    selector,
                    wait_after=arguments.get("wait_after", "networkidle"),
                    timeout=arguments.get("timeout", 5000),
                )
                return [TextContent(type="text", text=_state(result))]

            if action == "fill":
                value = arguments.get("value", "")
                result = await session.fill(
                    selector, value,
                    press_enter=arguments.get("press_enter", False),
                )
                return [TextContent(type="text", text=_state(result))]

            if action == "type":
                value = arguments.get("value", "")
                result = await session.type_text(
                    selector, value,
                    delay=arguments.get("delay", 50),
                )
                return [TextContent(type="text", text=_state(result))]

            if action == "press":
                key = arguments.get("value", "Enter")
                result = await session.press(selector, key)
                return [TextContent(type="text", text=_state(result))]

            if action == "select":
                value = arguments.get("value", "")
                result = await session.select_option(selector, value)
                return [TextContent(type="text", text=_state(result))]

            if action == "upload":
                files = [f.strip() for f in arguments.get("value", "").split(",") if f.strip()]
                await session.set_input_files(selector, files)
                return [TextContent(type="text", text=_json(await _page_state(session)))]

            if action == "hover":
                result = await session.hover(selector)
                return [TextContent(type="text", text=_state(result))]

            if action == "scroll":
                result = await session.scroll(
                    direction=arguments.get("direction", "down"),
                    amount=arguments.get("amount", 500),
                    selector=selector if selector else None,
                )
                return [TextContent(type="text", text=_state(result))]

            if action == "dblclick":
                result = await session.dblclick(
                    selector,
                    wait_after=arguments.get("wait_after", "networkidle"),
                    timeout=arguments.get("timeout", 5000),
                )
                return [TextContent(type="text", text=_state(result))]

            if action == "check":
                result = await session.check(selector)
                return [TextContent(type="text", text=_state(result))]

            if action == "uncheck":
                result = await session.uncheck(selector)
                return [TextContent(type="text", text=_state(result))]

            if action == "right_click":
                result = await session.right_click(selector)
                return [TextContent(type="text", text=_state(result))]

            if action == "tap":
                result = await session.tap(selector)
                return [TextContent(type="text", text=_state(result))]

            if action == "focus":
                await session.focus(selector)
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({"focused": selector, **state}))]

            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        # ------ hutch_screenshot
        if name == "hutch_screenshot":
            session = await _get_session(arguments["session"])
            if arguments.get("annotate"):
                png, legend = await session.annotated_screenshot()
                return [ImageContent(type="image", data=base64.b64encode(png).decode(),
                                     mimeType="image/png"),
                        TextContent(type="text", text=legend or "no actionable elements in view")]
            png = await session.screenshot(
                full_page=arguments.get("full_page", False),
            )
            return [ImageContent(
                type="image",
                data=base64.b64encode(png).decode(),
                mimeType="image/png",
            )]

        # ------ hutch_page_state
        if name == "hutch_page_state":
            session = await _get_session(arguments["session"])
            result = await _page_state(session)

            if arguments.get("include_dom", False):
                page = session._active_page()
                if page:
                    try:
                        result["dom"] = await page.aria_snapshot(mode="ai")
                    except Exception:
                        result["dom"] = None

            if arguments.get("include_cookies", False):
                result["cookies"] = await session.cookies()

            if arguments.get("include_storage", False):
                result["storage"] = await session.storage()

            return [TextContent(type="text", text=_json(result))]

        # ------ hutch_network
        if name == "hutch_network":
            session = await _get_session(arguments["session"])
            if not session.context:
                return [TextContent(type="text", text=_json([]))]

            entries = session.context.query_network(
                pattern=arguments.get("pattern"),
                method=arguments.get("method"),
                since=arguments.get("since"),
            )
            limit = arguments.get("limit", 50)
            serialized = [asdict(e) for e in entries[:limit]]
            state = await _page_state(session)
            return [TextContent(type="text", text=_json({
                "page": state,
                "entries": serialized,
                "count": len(serialized),
                "total": len(entries),
            }))]

        # ------ hutch_cookies
        if name == "hutch_cookies":
            session = await _get_session(arguments["session"])
            action = arguments.get("action", "get")

            if action == "get":
                cookies = await session.cookies(urls=arguments.get("urls"))
                return [TextContent(type="text", text=_json(cookies))]

            if action == "set":
                cookie_name = arguments.get("cookie_name")
                cookie_value = arguments.get("cookie_value")
                if not cookie_name or cookie_value is None:
                    return [TextContent(type="text", text="error: cookie_name and cookie_value required")]
                cookie = {
                    "name": cookie_name,
                    "value": cookie_value,
                }
                if arguments.get("domain"):
                    cookie["domain"] = arguments["domain"]
                if arguments.get("path"):
                    cookie["path"] = arguments["path"]
                await session.set_cookie(**cookie)
                cookies = await session.cookies()
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({
                    "set": cookie_name, "cookies": cookies, **state,
                }))]

            if action == "delete":
                await session.delete_cookies()
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({"cleared": True, **state}))]

            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        # ------ hutch_intercept
        if name == "hutch_intercept":
            session = await _get_session(arguments["session"])
            action = arguments["action"]
            pattern = arguments.get("pattern", "**/*")

            if action == "block":
                async def _block(route):
                    await route.abort()
                await session.intercept(pattern, _block)
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({
                    "blocked": pattern, **state,
                }))]

            if action == "modify_headers":
                headers = dict(arguments.get("headers", {}))
                async def _modify(route):
                    merged = {**route.request.headers, **headers}
                    await route.continue_(headers=merged)
                await session.intercept(pattern, _modify)
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({
                    "intercepted": pattern, "headers": list(headers.keys()), **state,
                }))]

            if action == "mock":
                await session.mock_response(
                    pattern,
                    status=arguments.get("status", 200),
                    body=arguments.get("body", ""),
                    content_type=arguments.get("content_type", "text/plain"),
                )
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({
                    "mocked": pattern, **state,
                }))]

            if action == "clear":
                await session.clear_intercepts()
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({
                    "cleared": True, **state,
                }))]

            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        # ------ hutch_evaluate
        if name == "hutch_evaluate":
            session = await _get_session(arguments["session"])
            expression = arguments["expression"]

            if arguments.get("frame_name") or arguments.get("frame_url"):
                result = await session.frame_evaluate(
                    expression,
                    name=arguments.get("frame_name"),
                    url=arguments.get("frame_url"),
                )
            else:
                result = await session.evaluate(expression)

            state = await _page_state(session)
            return [TextContent(type="text", text=_json({
                "result": result, **state,
            }))]

        # ------ hutch_content
        if name == "hutch_content":
            session = await _get_session(arguments["session"])
            selector = arguments.get("selector")
            fmt = arguments.get("format", "text")

            if selector:
                if fmt == "html" or fmt == "inner_html":
                    text = await session.inner_html(selector)
                else:
                    text = await session.inner_text(selector)
            else:
                if fmt == "html" or fmt == "inner_html":
                    text = await session.content()
                else:
                    text = await session.inner_text("body")

            state = await _page_state(session)
            return [TextContent(type="text", text=_json({
                "content": text[:50000],
                "truncated": len(text) > 50000,
                **state,
            }))]

        # ------ hutch_diff
        if name == "hutch_diff":
            session = await _get_session(arguments["session"])
            d = await session.diff(since=arguments.get("since"))
            state = await _page_state(session)
            return [TextContent(type="text", text=_json({
                "diff": asdict(d),
                **state,
            }))]

        # ------ hutch_pages
        if name == "hutch_pages":
            session = await _get_session(arguments["session"])
            action = arguments.get("action", "list")

            if action == "list":
                pages = await session.pages()
                return [TextContent(type="text", text=_json(pages))]

            if action == "switch":
                index = arguments.get("index", 0)
                result = await session.switch_page(index)
                return [TextContent(type="text", text=_state(result))]

            if action == "close":
                index = arguments.get("index")
                result = await session.close_page(index)
                return [TextContent(type="text", text=_json(result or {"closed": True}))]

            if action == "new":
                page = await session.new_page()
                state = await _page_state(session)
                pages = await session.pages()
                return [TextContent(type="text", text=_json({
                    "new_tab_index": len(pages) - 1,
                    **state,
                }))]

            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        # ------ hutch_health
        if name == "hutch_health":
            pool = await _get_pool()
            session_name = arguments.get("session")

            if session_name:
                session = await pool.get(session_name)
                return [TextContent(type="text", text=_json(session.status()))]

            return [TextContent(type="text", text=_json(pool.status()))]

        # ------ hutch_auth
        if name == "hutch_events":
            session = await _get_session(arguments["session"])
            action = arguments.get("action", "list")
            types = set(arguments["types"]) if arguments.get("types") else None
            if action in ("list", "wait"):
                since = arguments.get("since", 0)
                if action == "wait":
                    found = await session.events.wait(since, types, float(arguments.get("timeout", 30)))
                else:
                    found = session.events.since(since, types)
                if found:
                    _event_cursor[session.name] = max(_event_cursor.get(session.name, 0), found[-1]["seq"])
                return [TextContent(type="text", text=_json(found) if found else "no events")]
            if action == "dialog":
                return [TextContent(type="text", text=_json(
                    session.arm_dialog(arguments.get("accept", True), arguments.get("text"))))]
            if action == "policy":
                session.handle_dialog(arguments.get("policy", "auto"), arguments.get("text"))
                return [TextContent(type="text", text=f"dialog policy: {arguments.get('policy', 'auto')}")]
            if action == "files":
                return [TextContent(type="text", text=_json(await session.set_files(arguments["files"])))]
            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        if name == "hutch_auth":
            session = await _get_session(arguments["session"])
            action = arguments.get("action", "export")

            if action == "export":
                cookies = await session.cookies()
                storage = await session.storage()
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({
                    "cookies": cookies,
                    "storage": storage,
                    **state,
                }))]

            if action == "import":
                imported = {}
                if arguments.get("cookies"):
                    await session.set_cookies(arguments["cookies"])
                    imported["cookies"] = len(arguments["cookies"])
                if arguments.get("storage"):
                    stor = arguments["storage"]
                    for k, v in stor.get("localStorage", {}).items():
                        await session.set_storage(k, v, session_storage=False)
                    for k, v in stor.get("sessionStorage", {}).items():
                        await session.set_storage(k, v, session_storage=True)
                    imported["storage_keys"] = (
                        len(stor.get("localStorage", {}))
                        + len(stor.get("sessionStorage", {}))
                    )
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({
                    "imported": imported, **state,
                }))]

            if action == "clear":
                await session.delete_cookies()
                page = session._active_page()
                if page:
                    await page.evaluate("localStorage.clear(); sessionStorage.clear()")
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({
                    "cleared": True, **state,
                }))]

            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        return [TextContent(type="text", text=f"error: unknown tool '{name}'")]

    return server


def main():
    """Run the MCP server over stdio."""
    logging.basicConfig(level=logging.INFO)  # logs go to stderr; stdout is the MCP channel

    try:
        import importlib.metadata as _md

        _mcp_ver = _md.version("mcp")
        if int(_mcp_ver.split(".", 1)[0]) >= 2:
            log.error(
                "mcp %s is not supported: this server targets the mcp 1.x low-level "
                "Server API. Pin 'mcp>=1.0,<2'.",
                _mcp_ver,
            )
    except Exception:
        pass

    try:
        server = create_mcp_server()
        asyncio.run(_run_mcp(server))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception:
        log.exception("MCP server failed to start")
        raise


async def _run_mcp(server):
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
