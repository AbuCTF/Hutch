"""MCP server -- exposes Hutch browser session tools to Claude/agents via MCP.

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
        }

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
                    "Manage browser sessions. Actions: list (default), "
                    "create, close, destroy. Returns session info."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "create", "close", "destroy"],
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
                    "Dump interactive elements on the current page as an indexed "
                    "list. Each entry has idx, tag, text, selector -- use idx or "
                    "selector with hutch_interact to click/fill/type."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
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
                    "right_click, tap, focus. Target by CSS selector or element "
                    "index from observe. Returns page state after action."
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
                                "uncheck", "right_click", "tap", "focus",
                            ],
                            "description": "Interaction type",
                        },
                        "selector": {"type": "string", "description": "CSS selector or element index from observe"},
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
                description="Take a screenshot of the current page. Returns base64 PNG image.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Session name"},
                        "full_page": {"type": "boolean", "default": False, "description": "Capture full scrollable page"},
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
        ]

    # ---------------------------------------------------------- call_tool
    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            return await _dispatch(name, arguments)
        except Exception as e:
            log.exception("tool %s failed", name)
            return [TextContent(type="text", text=f"error: {e}")]

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
                )
                return [TextContent(type="text", text=_json(_session_info(s)))]

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
                return [TextContent(type="text", text=_json(result))]

            if action == "back":
                result = await session.go_back(
                    wait_until=arguments.get("wait_until", "load"),
                )
                return [TextContent(type="text", text=_json(result))]

            if action == "forward":
                result = await session.go_forward(
                    wait_until=arguments.get("wait_until", "load"),
                )
                return [TextContent(type="text", text=_json(result))]

            if action == "reload":
                result = await session.reload(
                    wait_until=arguments.get("wait_until", "load"),
                )
                return [TextContent(type="text", text=_json(result))]

            if action == "wait_for":
                result = await session.wait_for(
                    arguments.get("selector"),
                    state=arguments.get("state", "visible"),
                    url=arguments.get("url"),
                    timeout=arguments.get("timeout", 30000),
                )
                return [TextContent(type="text", text=_json(result))]

            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        # ------ hutch_observe
        if name == "hutch_observe":
            session = await _get_session(arguments["session"])
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
                return [TextContent(type="text", text=_json(result))]

            if action == "fill":
                value = arguments.get("value", "")
                result = await session.fill(
                    selector, value,
                    press_enter=arguments.get("press_enter", False),
                )
                return [TextContent(type="text", text=_json(result))]

            if action == "type":
                value = arguments.get("value", "")
                result = await session.type_text(
                    selector, value,
                    delay=arguments.get("delay", 50),
                )
                return [TextContent(type="text", text=_json(result))]

            if action == "press":
                key = arguments.get("value", "Enter")
                result = await session.press(selector, key)
                return [TextContent(type="text", text=_json(result))]

            if action == "select":
                value = arguments.get("value", "")
                result = await session.select_option(selector, value)
                return [TextContent(type="text", text=_json(result))]

            if action == "hover":
                result = await session.hover(selector)
                return [TextContent(type="text", text=_json(result))]

            if action == "scroll":
                result = await session.scroll(
                    direction=arguments.get("direction", "down"),
                    amount=arguments.get("amount", 500),
                    selector=selector if selector else None,
                )
                return [TextContent(type="text", text=_json(result))]

            if action == "dblclick":
                result = await session.dblclick(
                    selector,
                    wait_after=arguments.get("wait_after", "networkidle"),
                    timeout=arguments.get("timeout", 5000),
                )
                return [TextContent(type="text", text=_json(result))]

            if action == "check":
                result = await session.check(selector)
                return [TextContent(type="text", text=_json(result))]

            if action == "uncheck":
                result = await session.uncheck(selector)
                return [TextContent(type="text", text=_json(result))]

            if action == "right_click":
                result = await session.right_click(selector)
                return [TextContent(type="text", text=_json(result))]

            if action == "tap":
                result = await session.tap(selector)
                return [TextContent(type="text", text=_json(result))]

            if action == "focus":
                await session.focus(selector)
                state = await _page_state(session)
                return [TextContent(type="text", text=_json({"focused": selector, **state}))]

            return [TextContent(type="text", text=f"error: unknown action '{action}'")]

        # ------ hutch_screenshot
        if name == "hutch_screenshot":
            session = await _get_session(arguments["session"])
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
                        result["dom"] = await page.accessibility.snapshot()
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
                headers = arguments.get("headers", {})
                async def _modify(route, _h=headers):
                    merged = {**route.request.headers, **_h}
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
                return [TextContent(type="text", text=_json(result))]

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
    import mcp.server.stdio
    server = create_mcp_server()
    asyncio.run(_run_mcp(server))


async def _run_mcp(server):
    import mcp.server.stdio
    await mcp.server.stdio.run_async(server)


if __name__ == "__main__":
    main()
