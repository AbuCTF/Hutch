# Hutch

A stealth browser orchestrator built on real Google Chrome. Hutch launches and manages persistent
browser sessions, clears Cloudflare's silent challenges and interactive checkboxes (Turnstile,
reCAPTCHA v2), and exposes them through an MCP server, a CLI, a daemon and a Python API.
It works with Playwright or Patchright, and is meant to be used by people and by AI agents
at the same time.

- **Real Chrome, not Chrome for Testing.** The installed Google Chrome is the default. Hutch never
  uses Playwright's bundled build unless you ask for it, and never launches on your everyday profile.
- **Batteries included.** Profiles and logins that persist, per-session proxies, headed/headless
  switching without losing tabs, a virtual display, humanized input, secrets an agent can type but never read,
  record and replay, multi-account replay, and a hand-off to a person when a check needs one.
- **Driver-agnostic.** `playwright` by default, `patchright` for no `Runtime.enable` traces. Same API.
- **Guarded.** Image grids are never guessed, `forbids_bypass` sessions are never clicked, secrets are bound
  to domains and redacted from output, and a private-network block and a deny-list keep sessions off hosts you name.

### Install

Requires Python 3.10+ and Google Chrome (Linux, macOS or Windows).

```bash
uv tool install "hutch[mcp] @ git+https://github.com/AbuCTF/Hutch"
hutch doctor        # checks Chrome, fonts, display, Xvfb and attachable browsers
```

Or the install script, which uses a private venv in `~/.hutch/venv` and links `hutch` and `hutch-mcp`
into `~/.local/bin`:

```bash
curl -fsSL hutch.abu.rocks | sh
```

It uses your Google Chrome when it finds one and otherwise fetches Playwright's chromium as a fallback,
asks for sudo only if the browser's system libraries are missing, then runs `hutch doctor`. Re-run to
update; `HUTCH_REF=<tag or commit>` pins a version. Uninstall with
`rm -rf ~/.hutch/venv ~/.local/bin/hutch ~/.local/bin/hutch-mcp` (profiles in `~/.hutch/profiles` are kept).

Optional extras: `hutch[patchright]` for the stealth driver, `hutch[stealth]` for persona patches.

**Virtual display (Linux).** `--display virtual` runs headed Chrome on a private 1920x1080 Xvfb screen,
which tiling window managers cannot resize and where `clear_challenge` clicks through the X server.
It needs Xvfb:

```bash
sudo pacman -S xorg-server-xvfb     # Arch
sudo apt install xvfb               # Debian, Ubuntu
sudo dnf install xorg-x11-server-Xvfb   # Fedora
```

On native Windows (PowerShell):

```powershell
pip install "hutch[mcp] @ https://github.com/AbuCTF/Hutch/archive/master.tar.gz"
hutch doctor
```

The daemon listens on loopback TCP with a token in `~/.hutch/hutch.endpoint` instead of a unix
socket, and an agent's headless browser is tied to it with a job object. `--browser edge` works
without Chrome; headless edge never exits on its own, so hutch ends it once it has written its session.

From a checkout, for development: see [CONTRIBUTING.md](CONTRIBUTING.md).

### Hello world

```bash
hutch create hello --url https://example.com    # a persistent session, headless
hutch screenshot hello hello.png
hutch call hello clear_challenge                # no challenge here: {"status": "clear"}
hutch destroy hello                             # stop it and delete its profile
```

The same in Python (`examples/hello.py`):

```python
import asyncio
from hutch import Pool

async def main():
    async with Pool() as pool:
        s = await pool.create("hello", headless=True)
        await s.goto("https://example.com")
        print((await s.page_state())["title"])        # Example Domain
        await s.screenshot(path="hello.png")
        await pool.destroy("hello")

asyncio.run(main())
```

### Clearing a challenge

```python
result = await s.clear_challenge(timeout=60)
# {"status": "passed", "kind": "turnstile", "seconds": 21.4, "clicks": 1, "clicked": True}
```

`clear_challenge` clicks a Cloudflare Turnstile or reCAPTCHA v2 checkbox the way a person does, once per
appearance, and waits for the answer. `status` is one of `clear` (nothing was shown), `passed`,
`grid-required` (an image grid opened; nothing is guessed, hand it to a person with `await_human`),
`hitl` (the session is tagged `forbids_bypass`, nothing was touched) or `timeout`. Cloudflare's
silent checks pass without a click when the browser is real and nothing scripts the page while the check
is up; for those, wait for the page.
For the best results use real Chrome, `driver="patchright"`, headed on `display="virtual"`
(`examples/clear_challenge.py`). Details and measurements are under [Stealth](#stealth).

Use it on sites and accounts you are allowed to use it on. See the note under [License](#license).

### MCP

```bash
claude mcp add -s user hutch -- hutch-mcp      # Claude Code; any MCP client runs hutch-mcp over stdio
```

### Everyday use

#### Browser

```bash
hutch login google                     # plain window, sign in to your Google accounts, close it
hutch clone google acme             # new profile that already has those accounts
hutch browse acme https://acme.com --caido   # persistent window through a Caido proxy
hutch tabs acme                     # what is open
hutch view acme                     # watch and drive it from a web page, no relaunch
hutch headless acme                 # off screen, tabs + session restored (~1.5s)
hutch headed acme                   # back on screen
hutch ps                               # running browsers and their memory
hutch stop acme                     # or just close the window
hutch profile export acme acme.tar.gz   # the profile and its logins, to carry elsewhere
hutch profile import acme.tar.gz           # back as a session (give a name to rename it)
hutch doctor                           # libraries, fonts, display, attachable browsers
hutch gc --dry-run --caches            # what old artifacts, downloads and caches would free
```

- closing the window ends the process; nothing keeps running and the profile stays in `~/.hutch/profiles/<name>`
- `browse` again restores the last tabs, including session cookies and sessionStorage
- agents (MCP, daemon, python) join a running session over CDP and work in their own tab
- an agent that starts a stopped profile starts it headless; `view` shows what it is doing
- a tab an agent drives is leased to it, so a second agent works in another tab instead of fighting
  over it; in a visible browser those tabs carry a small "agent" badge (not in snapshots or screenshots)
- `--browser chrome|brave|edge|/path` per profile; a profile stays on the browser it was made with
- `HUTCH_BASE_DIR` switches the profile root
- with a proxy, nothing leaves except through it: measured in an empty network namespace (no DNS
  lookups, no direct connections, WebSocket and WebRTC included). Loopback goes through the proxy too,
  so a page cannot reach local services around it, and chromium's own background calls are switched
  off (`strict_proxy=False` restores chrome's defaults). A proxy that is down stops the launch instead
  of falling back to direct, `require_proxy` refuses a session without one, and a browser already
  running without the session's proxy is not attached to. `session.hosts()` lists every host contacted
- captured response bodies are capped at 64 MB per session (`HUTCH_CAPTURE_MB`; the oldest bodies go
  first, request metadata stays), artifacts keep the newest 200 per kind (`HUTCH_ARTIFACTS_KEEP`) and
  chromium's disk cache is capped at 256 MB per profile (`HUTCH_DISK_CACHE_MB`)

#### Agents

`hutch_sessions` lists, creates and attaches sessions; `action: "view"` returns a live view URL
for the human. `hutch_observe` returns an accessibility snapshot whose `[ref=e12]` refs every
action accepts as a selector; `filter: "interactive"` keeps only what can be acted on and `diff`
only what changed. Actions return their changes, not the whole page, and the full tree only after
a navigation. `hutch_screenshot` with `annotate` boxes each actionable element with its ref.
`hutch_call` runs any session command by name (`command: "help"` lists them with signatures).

Every public `Session` method is a command: the daemon, the python client (`await s.mouse_down()`),
MCP `hutch_call` and the CLI dispatch through one registry read from `Session` itself.

```bash
hutch call help                                  # every command with its signature
hutch call acme click e12
hutch call acme evaluate "document.cookie"
hutch call acme set_cookies cookies:='[{"name": "a", "value": "1", "url": "https://acme.com"}]'
```

#### Several accounts

One session per account, each logged in once. `authz` replays a request as the owner, as every
other account and as nobody, and says which ones got the owner's answer.

```bash
hutch authz acme-alice acme-bob --url https://api.acme.com/orders/1042
hutch authz acme-alice acme-bob --caido 5123        # any request from caido history
```

- `bypassed`: another account got the owner's answer, likely IDOR / broken access control
- `enforced`: denied, redirected away, not found, or a 200 that is really a login page or a
  "session expired" / "not authorized" body; `public`: logged out gets it too
- `check`: answered differently, only a human can judge (often per-user data on a shared endpoint)
- each identity sends its own cookies and the auth headers it last used on that host, through the
  session's proxy so every replay lands in caido; only GET/HEAD/OPTIONS unless `--unsafe`
- agents: `hutch_authz` replays one request or scans everything the owner captured
- answers are compared after stripping what changes without meaning anything: CSRF fields, trace
  and request ids, timestamps, cursors, cache busters, Cloudflare / NetScaler / WEBDEV / Kiteworks
  tokens (`hutch/noise.py`). Add per target with `hutch call <name> set_noise keys:='["rid"]'`,
  per call with `noise`, or for everything in the file named by `HUTCH_NOISE`

```bash
hutch auth acme-alice --probe https://api.acme.com/me --login alice-login.json
hutch auth                                          # every identity: in / out, from one request
hutch auth acme-alice --keep                        # logged out: replay the login, TOTP included
hutch auth acme-alice --find                        # pick a probe from captured traffic: 200 with, 401 without
```

#### Secrets, record and replay

```bash
hutch secret set acme-pass --domain acme.com     # prompted, stored 0600 in ~/.hutch/secrets.json
hutch call acme fill "#password" "{{acme-pass}}"
hutch secret set acme-otp --totp --domain acme.com   # otpauth:// uri or base32 from the 2FA setup
hutch secret code acme-otp                       # current code; agents type {{acme-otp}}
hutch secret import creds.json --key acme --domain acme.com \
  --map username=acme-email --map password=acme-pass --map totp_seed=acme-otp:totp   # nothing is printed
hutch replay acme examples/login-totp.json      # password page, waits for the code page, types a fresh code
hutch record acme -o login.json               # do it in the window, Ctrl+C to stop
hutch replay acme login.json
hutch export login.json > login.py               # standalone playwright, secrets from HUTCH_SECRET_<NAME>
```

- agents type `{{name}}` in fill/type; the value never appears in MCP or daemon output, even via `evaluate`
- a TOTP secret types its current code (waiting out the last two seconds of a window); a code typed
  by hand while recording is saved as the placeholder, so a recorded 2FA login replays by itself
- a secret bound to domains is refused on any other site, so a prompt-injected agent cannot type it elsewhere
- recordings come from real DOM events, so a human in the window and an agent are recorded the same way;
  passwords become `{{password}}` (or the vault name) and are scrubbed from recorded URLs
- recordings can wait for an element (`{"action": "wait", "selector": ...}`) or for the url to leave a
  path (`{"action": "wait_url", "not": "/auth/"}`), which is what two-step logins need
- screenshots are not redacted: a secret typed into a visible plain-text field shows in pixels.

Dialogs, downloads, file choosers, popups and crashes land in a
per-session event log; new events are appended to every tool result, and `hutch_events` waits for
them, answers the next dialog or sets files on an open chooser. Agent tabs accept alerts and
dismiss confirm/prompt unless told otherwise; dialogs on the human's tabs are left to the human.
Downloads from a headless browser go to `<profile>/downloads`, a visible one keeps the usual folder.

#### Hand it to a person

For a CAPTCHA or bot check, an emailed code, a payment: anything a person should do. Hutch never
answers a challenge itself; it waits for the person and notices when they are done.

```python
result = await s.await_human("captcha on signup", until="challenge", timeout=600)
# {"outcome": "cleared", "waited_s": 41.2, "screenshot": "~/.hutch/artifacts/...-handoff.png", ...}
```

```bash
hutch view acme       # headless? see and drive it from a web page
hutch ps                 # lists sessions waiting for a person and why
hutch resume acme     # done: from any shell, the waiting session carries on
```

- the session pauses (agent actions raise until it carries on), saves a screenshot and emits a
  `handoff` event; `on_pause` callbacks fire, which is where you send your own alert
- it returns on `hutch resume`, `resume()` (daemon or python), the `until` condition, or the
  timeout, and the session is active again in every case; `outcome` says which:
  `resumed`, `cleared`, `timeout` or `closed`
- `until`: `"challenge"` (no challenge page and no unanswered Turnstile, reCAPTCHA or hCaptcha
  widget left on the tab), `{"gone": selector}`, `{"visible": selector}`, `{"url": substring}`,
  or any callable in python
- the clearance a person earns stays in the profile, so one pass usually covers that site
  for a while; `profile export` carries it to another machine with the logins
- agents: `hutch_handoff` (await, status, resume); over the daemon the call holds its
  connection, so resume from another client or with `hutch resume`

#### Google sign-in

Google refuses sign-in from browsers it considers automated. `hutch login` opens the profile
with no debugging port and no automation switches — a normal browser — so sign in there once.
Later `browse` sessions (and agents) reuse the cookies, and "Sign in with Google" lists those
accounts. Accounts in another browser (Brave, Chrome) cannot be copied in: their cookies are
encrypted to that install and Google binds sessions to it. Either sign in once in a hutch
profile and `clone` it, or let agents use that browser directly with `hutch attach`.

#### Attach to your own browser

```bash
# in Chrome/Brave/Edge 144+: open <browser>://inspect/#remote-debugging and enable it
hutch attach brave --name mybrave      # agents can now use the session 'mybrave'
hutch tabs brave
```

Under WSL the Windows browser listens on Windows' 127.0.0.1, which WSL only reaches with
`networkingMode=mirrored` in `%USERPROFILE%\.wslconfig`. Browsers started inside WSL need nothing.

#### CLI

```bash
hutch create target-a --preset win-desktop-1080p    # persona: fake but coherent fingerprint
hutch create target-a --proxy http://127.0.0.1:8081 --ignore-https-errors
hutch create reader --block-private                  # for untrusted URLs: no localhost, LAN or metadata
hutch create quiet --driver patchright --browser chrome  # fewest automation traces
hutch create hand --humanize                         # move, click, type and scroll like a person
hutch list
hutch screenshot target-a evidence.png --full-page
hutch destroy target-a
hutch presets

# start the daemon
hutch serve
hutch serve --idle-timeout 0
hutch serve --exit-when-idle
```

#### Direct Client

```python
from hutch import Pool, generate

async with Pool() as pool:
    s = await pool.create(
        "target-a",
        fingerprint=generate(preset="win-desktop-1080p"),
        proxy="http://127.0.0.1:8081",
        ignore_https_errors=True,
    )

    # action methods with smart waits
    await s.goto("https://target.com")
    await s.fill("#username", "user@test.com")
    await s.fill("#password", "pass")
    await s.click("#login-btn")  # waits for networkidle

    # observe all interactive elements
    elements = await s.observe()
    for el in elements:
        print(f"[{el['idx']}] {el['tag']} — {el['text']} → {el['selector']}")

    # page state (url + title + a11y tree) after every action
    state = await s.page_state()

    # context capture — network, console, errors
    snap = await s.snapshot(screenshot=True, storage=True)
    diff = await s.diff()  # only what changed since last check

    # cookie manipulation
    cookies = await s.cookies()
    await s.set_cookie(name="session", value="abc", domain=".target.com", path="/")
    await s.delete_cookies()

    # localStorage / sessionStorage
    await s.set_storage("token", "eyJ...")
    store = await s.storage()

    # request interception
    async def swap_auth(route):
        headers = {**route.request.headers, "Authorization": "Bearer other-user"}
        await route.continue_(headers=headers)
    await s.intercept("**/api/*", swap_auth)

    await s.save_state()
    await s.close()

    # reopen later — full auth state restored
    s = await pool.get("target-a", launch=True)
```

#### Daemon Client

```python
from hutch import HutchClient

async with HutchClient() as client:
    s = await client.create("target-a", preset="win-desktop-1080p")

    await s.goto("https://target.com/login")
    await s.fill("#user", "admin")
    await s.fill("#pass", "test")
    state = await s.click("#submit")

    elements = await s.observe()
    snap = await s.snapshot(screenshot=True)
    network = await s.network(pattern="/api/")

    # request modification
    await s.modify_headers({"X-Forwarded-For": "127.0.0.1"}, pattern="**/api/*")
    await s.block_urls(["**/analytics*", "**/tracking*"])

    # a person takes over: blocks until they are done, see "Hand it to a person"
    result = await s.await_human("captcha on signup", until="challenge")

    # snapshot refs work as selectors everywhere
    print(await s.aria_snapshot())   # - button "Log in" [ref=e6]
    await s.click("e6")

    # join a browser you are using yourself
    mine = await client.attach("mybrave", "brave")
```

### Features

| Feature | Description |
|---------|-------------|
| **Real browser** | Hutch launches chromium itself; no `webdriver` flag, no HeadlessChrome UA, nothing spoofed unless asked |
| **Shared sessions** | CLI, daemon, MCP and the human's window drive the same browser over CDP |
| **Headed ↔ headless** | Relaunch on the same profile with tabs, session cookies and sessionStorage restored |
| **Attach** | Join your own Chrome/Brave/Edge (144+ remote-debugging toggle) |
| **Session isolation** | Separate profile dirs, cookies, storage, proxy per session |
| **Personas** | Optional coherent fingerprints, version-matched to the real engine |
| **Persistent state** | Cookies/storage/tabs survive close+relaunch |
| **Context capture** | Network, console, errors, navigations in ring buffers |
| **Cursor-based diffs** | Only get what changed since last check |
| **Snapshot refs** | Accessibility tree with `[ref=e12]`; refs are valid selectors in every action |
| **Page state** | URL + title + ref snapshot returned after every action |
| **Smart waits** | Click waits for networkidle; configurable strategies |
| **Request interception** | Modify headers, block URLs, swap auth tokens |
| **Deny hosts** | `session.deny_hosts(check)`: hosts never to contact, refused by the request guard and, with the private-network block, by the proxy every connection goes through (service workers and WebSockets included) |
| **Private network block** | Opt-in `block_private`: refuse loopback, LAN, link-local and metadata addresses, for sessions that open untrusted URLs. Checked per request, then again by a local proxy that connects only to the address it checked (no DNS rebinding, service workers covered) |
| **Stealth driver** | Opt-in `driver: patchright` per session: no `Runtime.enable` or `Console.enable` traces, scripts evaluated out of the page's sight |
| **Cookie/storage API** | Get/set/delete cookies and localStorage |
| **Health monitoring** | Auto-detect captcha, auth expiry, rate limiting |
| **HITL handoff** | Auto-pause on challenges; `await_human` pauses, screenshots, alerts and waits until a person is done (`hutch resume`, a condition, or a timeout) |
| **Parallel execution** | Same action across multiple sessions simultaneously |
| **Compare** | Diff responses across sessions for authz testing |
| **Daemon mode** | Unix socket JSON-RPC server for agent integration |
| **Artifacts** | HAR exports, screenshots, snapshots, agent notes |
| **Human input** | Opt-in `humanize`: curved, Fitts's-law-timed mouse paths with overshoot, off-centre clicks with a real hold, varied typing rhythm, wheel-notch scrolling |
| **Headless that looks headed** | Real screen size with a taskbar, a mouse pointer, full client hints; set inside the browser, no page script patched |

### Fingerprint presets (personas)

Only applied when asked for (`--preset`, `--program NAME` for a persona seeded from a name). Without one, sessions use the browser's
real identity — spoofing a Windows UA on a Linux engine is easier to detect than not spoofing.


| Preset | Viewport | Platform |
|--------|----------|----------|
| `win-desktop-1080p` | 1920x1080 | Win32 |
| `win-desktop-1440p` | 2560x1440 | Win32 |
| `mac-desktop-retina` | 1440x900 | MacIntel |
| `linux-desktop` | 1920x1080 | Linux x86_64 |
| `win-laptop-768p` | 1366x768 | Win32 |

each preset bundles a matching UA, platform, screen, locale, and timezone.

### Stealth

Without a persona, a session is the real browser with nothing spoofed. The tells that remain come
from automation itself, and three opt-ins remove them:

- **`driver: patchright`** (`--driver patchright`, `pip install 'hutch[patchright]'`) drives the
  browser without `Runtime.enable` and `Console.enable`, the CDP calls detectors probe for, and runs
  Hutch's own scripts in an isolated world. Costs: no console messages in `observe`, and
  `evaluate` reaches page globals only because Hutch asks for the main world explicitly.
- **Google Chrome instead of the bundled Chromium** (`--browser chrome`, the default): detectors treat a
  Chromium without the `Google Chrome` brand as a test build.
- **Headless display** is automatic: `--screen-info` gives headless a real screen with a taskbar,
  `--blink-settings` gives it a mouse, and pages get back the full client hints (version list,
  architecture, bitness) that the user-agent switch would otherwise blank.

- **`humanize`** (`--humanize`) replaces teleporting clicks and constant-rate typing with a
  hand's movement: curved paths timed by Fitts's law, an overshoot and correction on long moves,
  clicks near but not on the centre with a 55-130 ms hold, log-normal gaps between keys with
  pauses between words, and scrolling in 100 px wheel notches. Every event is a real, trusted
  input event. It hedges against simple behavioural rules; it is not cover against vendors that
  model a whole session, so hand those steps to a person.

- **`display: virtual`** (`--display virtual`, with `--headed`) runs the real, headed Google Chrome on a
  private [Xvfb](https://www.x.org/wiki/) screen of exactly 1920x1080 and no window manager. Tiling
  window managers (Hyprland, i3, sway) ignore `--window-size` and squeeze a headed window into a tile,
  a viewport of 771x326 on a 2560x1440 screen in the test that prompted this; headless chrome has
  tells of its own. On a virtual screen the browser gets the whole display, a real X11 window and the
  host GPU through its render node for WebGL (software rendering when there is none), and nothing can
  resize it. `real` (the default where there is a desktop) opens on your desktop, for a person to
  watch or to take over; `auto` picks virtual only on a machine with no display at all. Needs
  `Xvfb` (`pacman -S xorg-server-xvfb`, `apt install xvfb`) and only on Linux.
- **Real Chrome, always.** `--browser` defaults to `chrome`: the installed Google Chrome, never
  Playwright's bundled chromium or Chrome for Testing (ask for `--browser chromium` by name), and a
  browser is never launched on the everyday profile in `~/.config/google-chrome`, where your logins
  and sync live. Headed chrome gets `--disable-blink-features=AutomationControlled` with
  `--test-type`, which keeps `navigator.webdriver` false without the "unsupported flag" bar that
  would otherwise eat 56 px of the viewport.
- **`clear_challenge`** clicks a Cloudflare Turnstile checkbox or a reCAPTCHA v2 box the way a person
  does, for accounts a site's own rules let you create. On a virtual screen the pointer is
  moved and pressed through the X server's XTEST extension, so the click reaches the page by the same
  path a mouse's does; elsewhere it is sent over DevTools. The pointer arrives from elsewhere over a
  curved path, hovers on the box, presses for a human-length hold, and nothing is clicked while a
  managed widget is still "Verifying". An image grid is never attempted: the status is
  `grid-required`. A session tagged `forbids_bypass` (`--tag forbids_bypass=true`) is never clicked:
  the status is `hitl`, for a person. Works with every driver.

      await session.clear_challenge(timeout=60)
      # {"status": "passed", "kind": "turnstile", "seconds": 21.4, "clicks": 1, "clicked": true}

`hutch.cdp` is a small DevTools client of Hutch's own for callers that want a browser with no
automation library attached at all. It sends only what it is asked to, never `Runtime.enable` or
`Console.enable`, and `challenge.RawSurface` lets `clear_challenge` run on it. `hutch.xinput`
sends mouse and key events through the X server on a virtual screen.

Measured on 2026-11-20 with `--driver patchright --browser chrome --headed --display virtual` (Google
Chrome 156, a fresh profile per run, a residential connection), public test pages only:

| Page | Result |
|------|--------|
| [peet.ws Turnstile, non-interactive](https://peet.ws/turnstile-test/non-interactive.html) | real token, 9 of 10 runs (the miss was while the machine was busy), 14-43 s |
| [scrapingcourse Cloudflare challenge](https://www.scrapingcourse.com/cloudflare-challenge) | cleared, 5 of 5, 21-38 s |
| [peet.ws Turnstile, managed](https://peet.ws/turnstile-test/managed.html) | `clear_challenge` clicked the checkbox once, passed 5 of 5, 20-42 s |
| [nopecha Cloudflare interstitial](https://nopecha.com/demo/cloudflare) | `clear_challenge` clicked once, passed 5 of 5, 18-45 s |
| reCAPTCHA v2 ([Google's demo](https://www.google.com/recaptcha/api2/demo)) | box clicked, image grid opened, status `grid-required` in 4 s, nothing guessed |
| [sannysoft](https://bot.sannysoft.com/) | 31 of 31 |
| [CreepJS](https://abrahamjuliot.github.io/creepjs/) | headless 0%, stealth 0%, like-headless 31% (38% on the same machine with a tiled window) |
| viewport | screen 1920x1080, window 1920x1048, page 1920x961, `navigator.webdriver` false |

The interstitial row came after a finding worth knowing: a Cloudflare interstitial passes when the
page is only watched through DevTools DOM reads and fails every time when a script is run in it on a
poll (0 of 16 attempts before the change, 8 of 8 after), so `clear_challenge` never scripts a page that
shows one. If you poll such a page yourself, expect to be trusted less.

Measured on 2026-11-11, headless, Linux (WSL2), no persona:

| Check | playwright + bundled Chromium | patchright + Google Chrome 157 |
|-------|-------------------------------|--------------------------------|
| [rebrowser bot-detector](https://bot-detector.rebrowser.net/) | 2 red: `runtimeEnableLeak`, `sourceUrlLeak` | all green |
| [deviceandbrowserinfo](https://deviceandbrowserinfo.com/are_you_a_bot) | bot (CDP detected) | human |
| [BrowserScan](https://www.browserscan.net/bot-detection) | Normal | Normal |
| [sannysoft](https://bot.sannysoft.com/) | 0 failed | 0 failed |
| [CreepJS](https://abrahamjuliot.github.io/creepjs/) headless / stealth | 0% / 0% | 0% / 0% |

What these pages cannot see, and what still decides real sites: IP reputation, the time zone and
language against the IP's location, a working GPU (WSL2 has no WebGL in headless), and how the
session behaves. No test page stands in for a real protected site.

The time zone matters more than it looks. On 2026-11-11 two Cloudflare-protected sites blocked a
headless session from an Indian IP every time it reported UTC, and let it in on most tries once it
reported India time. A desktop's browser inherits the right zone; WSL often runs on UTC, so under
WSL Hutch asks Windows for its zone and gives browsers that one. `TZ` in the environment and a
persona's time zone always win.

Personas add a coherent foreign identity on top; with `playwright-stealth` installed they also
patch `navigator` properties in page scripts, which detectors can notice. Client hints
(`sec-ch-ua`, `-mobile`, `-platform`) match the persona from a tab's first request, popups
included; they are also sent to plain-http sites, where Chrome itself sends none. TLS and HTTP/2
fingerprints are always the real browser's, which is why personas are version-matched to it.

### License

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Dependencies are Apache-2.0 or MIT.

Hutch makes a browser behave like a browser. It does not defeat paywalls, rate limits or access controls,
and `clear_challenge` is for sites and accounts you are allowed to use. You are responsible for staying
within a site's terms and the law where you live.
