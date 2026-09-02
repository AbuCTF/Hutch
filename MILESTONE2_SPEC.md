# milestone 2 — persistent daemon + context engine

hutch today: isolated browser sessions with fingerprinting. one-shot — process exits, browsers die.

hutch after this: a persistent browser intelligence layer. sessions stay alive across agent invocations. every session silently captures everything the browser sees. agents query structured context instead of scraping raw HTML.

## what ships

### 1. daemon (`hutch serve`)

long-running process that owns the Pool. sessions persist until explicitly closed/destroyed.

```bash
hutch serve                          # foreground, unix socket
hutch serve --host 100.64.x.x       # bind to tailnet IP (http)
hutch serve --daemonize              # background, pidfile
hutch serve --max-sessions 20
```

transport: unix socket at `~/.hutch/hutch.sock` (default) for same-machine agents. optional HTTP on a tailnet IP for cross-machine. JSON-RPC over both — same message format, same handler.

the CLI detects a running daemon and routes through it. `hutch create foo` with a daemon running creates a persistent session. without a daemon, current one-shot behavior preserved.

### 2. context capture

every launched session hooks playwright events and records to an in-memory ring buffer (per-session, capped, not persisted to disk unless exported).

**what gets captured:**

| channel | playwright hook | what's stored |
|---------|----------------|---------------|
| network | `page.on("request")`, `page.on("response")` | method, url, status, req/res headers, body (first 64KB), timing, size, resource type |
| console | `page.on("console")` | level (log/warn/error/info), text, timestamp, source url+line |
| errors | `page.on("pageerror")` | stack trace, message, timestamp |
| navigation | `page.on("framenavigated")` | url, timestamp, status |

**what's queryable on-demand (not continuously recorded):**

| query | how |
|-------|-----|
| dom snapshot | `page.accessibility.snapshot()` — structured tree, not raw HTML |
| full html | `page.content()` |
| cookies | `context.cookies()` |
| storage | `page.evaluate()` on localStorage/sessionStorage |
| screenshot | `page.screenshot()` |
| evaluate | `page.evaluate(js)` — run arbitrary JS, return result |
| loaded scripts | `page.evaluate()` to enumerate `<script>` srcs + inline content |

ring buffer default: 1000 network entries, 500 console entries per session. oldest evicted first. configurable.

### 3. agent API (python client)

```python
from hutch import connect

async with connect() as h:                     # unix socket
async with connect("http://100.64.x.x:7600") as h:  # remote

    # session lifecycle
    s = await h.create("hackerone", program="hackerone", proxy="...")
    s = await h.session("hackerone")           # get existing
    sessions = await h.list()
    await h.destroy("hackerone")

    # navigate + interact
    await s.goto("https://target.com/dashboard")
    await s.click("text=Settings")
    await s.fill("#search", "admin")
    result = await s.evaluate("document.title")

    # context — the whole point
    snap = await s.snapshot()
    snap.url                  # current URL
    snap.title                # page title
    snap.cookies              # list of cookie dicts
    snap.dom                  # accessibility tree (structured)
    snap.screenshot           # PNG bytes

    # query captured data
    reqs = await s.network()                         # all captured
    reqs = await s.network(pattern="*/api/*")        # glob filter
    reqs = await s.network(method="POST", status=range(400,500))
    reqs = await s.network(since=timestamp)          # since last check

    logs = await s.console()                         # all
    logs = await s.console(level="error")            # errors only
    logs = await s.console(pattern="*TypeError*")    # grep

    errs = await s.errors()                          # uncaught JS exceptions

    # export full capture for a session
    har = await s.export_har()                       # HAR 1.2 JSON
    await s.export_har("evidence/hackerone.har")     # write to file
```

### 4. context snapshot structure

what `s.snapshot()` returns — a single object with everything an agent needs to understand the current page:

```python
@dataclass
class Snapshot:
    url: str
    title: str
    timestamp: float
    cookies: list[dict]
    dom: dict                    # accessibility tree
    screenshot: bytes | None     # optional, pass screenshot=True
    network: list[NetworkEntry]  # since last snapshot (or all)
    console: list[ConsoleEntry]
    errors: list[ErrorEntry]
    storage: dict | None         # localStorage, pass storage=True

@dataclass
class NetworkEntry:
    method: str
    url: str
    status: int | None           # None if pending/failed
    request_headers: dict
    response_headers: dict | None
    request_body: str | None
    response_body: str | None    # first 64KB
    resource_type: str           # document, xhr, fetch, script, stylesheet...
    timing: float                # response time ms
    size: int
    timestamp: float

@dataclass
class ConsoleEntry:
    level: str                   # log, warn, error, info
    text: str
    source: str | None           # source URL
    line: int | None
    timestamp: float

@dataclass
class ErrorEntry:
    message: str
    stack: str | None
    timestamp: float
```

### 5. session idle/hibernate

sessions auto-hibernate after configurable inactivity (default: 15 minutes). hibernate = close the browser process, keep the profile on disk. next access auto-relaunches transparently.

```python
s = await h.session("target-x")          # auto-relaunches if hibernated
await s.goto("https://target.com")       # works seamlessly
```

pool tracks `last_activity` per session. background task checks every 60s. configurable via `hutch serve --idle-timeout 900` (seconds) or `0` to disable.

### 6. event subscriptions (push-based)

agents subscribe to filtered event streams instead of polling:

```python
async for event in s.subscribe(network="*/api/*", console="error"):
    if event.type == "network":
        # new API call captured
    elif event.type == "console":
        # new error logged
```

server pushes matching events over the socket as they happen. agents react in real-time instead of polling snapshots.

### 7. diff-based snapshots

`s.snapshot()` tracks a cursor. consecutive calls return only what changed:

```python
snap1 = await s.snapshot()               # full state
await s.goto("https://target.com/profile")
snap2 = await s.snapshot()               # only NEW network entries, cookie changes, etc.

# or explicit diff
diff = await s.diff(since=snap1.cursor)
diff.network_added                       # new requests
diff.cookies_added                       # new cookies
diff.cookies_removed                     # expired/deleted cookies
```

useful for scheduled recon: snapshot every N hours, diff shows what changed on the target.

### 8. session health monitoring

daemon watches for signs of trouble and pushes alerts:

| signal | detection | action |
|--------|-----------|--------|
| auth expired | 3+ consecutive 401/403 responses | alert: `session:auth_expired` |
| captcha wall | page content matches captcha patterns (recaptcha, hcaptcha, cloudflare challenge) | alert: `session:captcha` |
| page crash | `page.on("crash")` | alert: `session:crashed` |
| rate limited | 429 responses | alert: `session:rate_limited` |
| session idle | no activity for idle_timeout | auto-hibernate |

alerts are queryable (`await h.alerts()`) and optionally pushed to an external channel (ntfy, webhook).

```python
alerts = await h.alerts(session="target-x")
# [Alert(type="auth_expired", session="target-x", timestamp=..., detail="3x 401 on /api/")]
```

### 9. session artifacts

persistent per-session directory for evidence and intelligence. survives daemon restarts.

```
~/.hutch/artifacts/<session>/
  har/                    # exported HAR files (timestamped)
  screenshots/            # screenshots (timestamped)
  snapshots/              # serialized snapshots
  notes.json              # agent-written metadata (discovered endpoints, auth type, etc.)
```

```python
await s.export_har()                          # writes to artifacts/<session>/har/
await s.screenshot("artifacts")               # writes to artifacts/<session>/screenshots/
await s.note("auth", {"type": "jwt", "header": "Authorization"})  # agent stores learnings
notes = await s.notes()                       # retrieve
```

agents persist intelligence about a target. next time the session is used — even after a daemon restart — the learnings are there.

## file layout

```
hutch/
  __init__.py          # existing, add connect() export
  session.py           # existing, add context hooks in launch()
  pool.py              # existing, add idle tracking + hibernate
  fingerprint.py       # existing, unchanged
  stealth.py           # existing, unchanged
  cli.py               # existing, add serve command + daemon detection
  context.py           # NEW — ring buffers, Snapshot/NetworkEntry/ConsoleEntry dataclasses
  server.py            # NEW — daemon: unix socket + optional HTTP, JSON-RPC handler
  client.py            # NEW — connect(), SessionHandle, async client
  health.py            # NEW — health monitors (auth expiry, captcha, crash detection)
  artifacts.py         # NEW — artifact directory management, notes store
```

## what changes in existing code

**session.py** — `launch()` gains a `capture=True` kwarg. when true, hooks `page.on(...)` events into a `Context` object (from context.py) attached to the session. every `new_page()` auto-hooks the new page. `session.context` exposes the ring buffer. `last_activity` timestamp updated on every page interaction.

**pool.py** — gains idle tracking. background task checks `last_activity` per session, hibernates (close browser, keep profile) after `idle_timeout`. `get()` with `launch=True` transparently relaunches hibernated sessions.

**cli.py** — new `serve` subcommand. all other subcommands check for `~/.hutch/hutch.sock` — if daemon is running, route through client instead of creating a local Pool. transparent to the user.

## build order

1. `context.py` — dataclasses + ring buffer + playwright hooks. test standalone with a manual Pool.
2. wire into `session.py` — launch() hooks pages, expose session.context / session.snapshot().
3. `artifacts.py` — directory management, notes store, export helpers.
4. `health.py` — health monitors, alert dataclass, detection logic.
5. `server.py` — daemon process, JSON-RPC over unix socket. Pool.start() on boot, idle manager, health monitors, accept connections.
6. `client.py` — async client matching the agent API above. connect() → SessionHandle. subscribe() for push events.
7. `cli.py` — `hutch serve` command + daemon detection in existing commands.
8. HTTP transport — optional, for cross-machine (tailnet) access.

## non-goals for this milestone

- **no browser automation DSL** — agents use playwright's Page API directly (via evaluate/click/fill passthrough), not a new abstraction layer
- **no multi-page management** — snapshot() works on the active page. multi-tab orchestration is the agent's job
- **no auth flow automation** — `hutch auth` stays manual. agents can automate via click/fill if they want

## how agents use this — concrete example

```python
from hutch import connect

async with connect() as h:
    s = await h.session("target-x")
    await s.goto("https://target.com")

    # passive recon — see what the app does on load
    snap = await s.snapshot()
    api_calls = [r for r in snap.network if "/api/" in r.url]
    # agent now knows every API endpoint the frontend calls

    # look for errors
    for err in snap.errors:
        # JS exceptions on page load = potential attack surface

    # navigate deeper
    await s.click("text=Profile")
    snap2 = await s.snapshot()
    # new network entries since snap — what API calls does the profile page make?

    # check for IDOR
    profile_req = next(r for r in snap2.network if "/api/user/" in r.url)
    # agent sees the exact request format, can now craft IDOR test through scope gate
```

the agent never parses HTML. it reads structured context. the browser does the heavy lifting.
