# Hutch

Isolated Playwright session orchestrator. Create, manage, and run multiple browser sessions with persistent state, per-session proxy routing, and coherent fingerprint profiles — zero leakage between sessions.

Think of it as an open-source antidetect browser engine, but designed for automation and security testing instead of ad fraud.

## Architecture

```
                        ┌──────────────────────────────────────────────┐
                        │              Pool (one per process)          │
                        │                                              │
                        │  ┌─── async_playwright() ───────────────┐   │
                        │  │     single Playwright instance        │   │
                        │  │     manages all browser processes     │   │
                        │  └──────────────────────────────────────-┘   │
                        │                                              │
  ┌─────────────────────┼──────────────────────────────────────────────┼─────────────────────┐
  │                     │                                              │                     │
  ▼                     ▼                                              ▼                     ▼
┌───────────────┐ ┌───────────────┐                          ┌───────────────┐ ┌───────────────┐
│  Session A    │ │  Session B    │           ...             │  Session N    │ │  Session N+1  │
│               │ │               │                          │               │ │               │
│  profile dir  │ │  profile dir  │                          │  profile dir  │ │  profile dir  │
│  ~/.hutch/    │ │  ~/.hutch/    │                          │  ~/.hutch/    │ │  ~/.hutch/    │
│  profiles/a/  │ │  profiles/b/  │                          │  profiles/n/  │ │  profiles/n1/ │
│               │ │               │                          │               │ │               │
│  cookies ✓    │ │  cookies ✓    │                          │  cookies ✓    │ │  cookies ✓    │
│  storage ✓    │ │  storage ✓    │                          │  storage ✓    │ │  storage ✓    │
│  cache   ✓    │ │  cache   ✓    │                          │  cache   ✓    │ │  cache   ✓    │
│               │ │               │                          │               │ │               │
│  fingerprint: │ │  fingerprint: │                          │  fingerprint: │ │  fingerprint: │
│  Win/1080p    │ │  Mac/Retina   │                          │  Linux/1080p  │ │  Win/768p     │
│               │ │               │                          │               │ │               │
│  proxy:       │ │  proxy:       │                          │  proxy:       │ │  proxy:       │
│  direct       │ │  Caido:8081   │                          │  socks5://... │ │  direct       │
└───────────────┘ └───────────────┘                          └───────────────┘ └───────────────┘
       │                 │                                          │                 │
       │                 ▼                                          │                 │
       │          ┌─────────────┐                                   │                 │
       │          │  Caido SDK  │  (optional per-session)           │                 │
       │          │  localhost   │                                   │                 │
       │          │  :8081      │                                   │                 │
       │          └─────────────┘                                   │                 │
       ▼                                                            ▼                 ▼
   internet ─────────────────────────────────────────────────── internet ──────── internet
```

Each session gets its own Playwright persistent context — completely separate cookies, localStorage, IndexedDB, cache, and service workers. Sessions survive restarts (profile directories persist on disk). Log in once, reuse forever.

## Install

```bash
pip install playwright
playwright install chromium

# clone and install
git clone https://github.com/AbuCTF/Hutch.git
cd Hutch
pip install -e .

# optional: stealth patches (defeats navigator.webdriver, WebGL/canvas fingerprinting)
pip install playwright-stealth
```

## Quick start

### CLI

```bash
# create a session with a fingerprint preset
hutch create my-target --preset win-desktop-1080p

# create with proxy (e.g., Caido for traffic inspection)
hutch create my-target --proxy http://127.0.0.1:8081 --ignore-https-errors

# create with deterministic fingerprint tied to a program name
hutch create yandex --program yandex --headed

# list all sessions
hutch list

# open a headed browser for manual login, save auth state
hutch auth my-target --url https://target.com/login

# take a screenshot
hutch screenshot my-target evidence.png --full-page

# close browser (profile survives)
hutch close my-target

# nuke session and profile
hutch destroy my-target

# show available fingerprint presets
hutch presets
```

### Python API

```python
import asyncio
from hutch import Pool, generate, generate_for_program

async def main():
    async with Pool() as pool:
        # create with a preset fingerprint
        s = await pool.create(
            "target-a",
            fingerprint=generate(preset="win-desktop-1080p"),
            proxy="http://127.0.0.1:8081",
            ignore_https_errors=True,
        )

        page = await s.new_page()
        await page.goto("https://target.com")

        # do your thing...

        await s.save_state()   # checkpoint auth state
        await s.close()        # close browser, profile survives

        # later — reopen with full auth state restored
        s = await pool.get("target-a", launch=True)
        page = await s.new_page()
        # cookies, localStorage, everything is back

asyncio.run(main())
```

### Multi-session isolation

```python
async with Pool() as pool:
    # different programs get different fingerprints, fully isolated
    s1 = await pool.create("prog-a", fingerprint=generate_for_program("alpha"))
    s2 = await pool.create("prog-b", fingerprint=generate_for_program("beta"))

    # each session has its own cookies, proxy, fingerprint
    # zero leakage between them
```

## How it works

**The core idea:** Playwright's `launch_persistent_context(user_data_dir)` creates a Chromium instance that saves ALL state to a directory. Same directory = same cookies/storage. Different directory = total isolation. This is exactly how antidetect browsers work internally — separate profile directories per "browser profile."

Hutch adds:
- **Pool manager** — one Playwright process, multiple persistent contexts, concurrency limits
- **Fingerprint presets** — coherent profiles where UA matches platform matches screen matches timezone (mismatches are the #1 detection signal)
- **Deterministic fingerprints** — same program name = same fingerprint every time, different programs = different profiles
- **Session discovery** — pool scans disk on startup, finds previously created sessions, ready to relaunch
- **WebRTC protection** — blocks STUN IP leak behind proxy
- **Stealth patches** — optional playwright-stealth integration (navigator.webdriver, WebGL/canvas noise)
- **CLI** — create/list/auth/screenshot/close/destroy from the terminal

## Fingerprint presets

| Preset | Viewport | Platform | Notes |
|--------|----------|----------|-------|
| `win-desktop-1080p` | 1920x1080 | Win32 | most common desktop |
| `win-desktop-1440p` | 2560x1440 | Win32 | high-res desktop |
| `mac-desktop-retina` | 1440x900 | MacIntel | 2x scale factor |
| `linux-desktop` | 1920x1080 | Linux x86_64 | dev workstation |
| `win-laptop-768p` | 1366x768 | Win32 | laptop screen |

Each preset includes a matching UA, platform, screen dimensions, locale, and timezone — all internally consistent.

## What stealth covers (and doesn't)

With `playwright-stealth` installed (~80% of detection vectors):
- `navigator.webdriver` → false
- WebGL renderer noise
- Canvas fingerprint noise
- Plugin array spoofing
- Font list shimming

What it **cannot** cover (Chromium limitation):
- TLS fingerprint (JA3/JA4) — Chromium's handshake is well-known
- HTTP/2 settings fingerprint

For hardened WAF bypass, use [Camoufox](https://github.com/nickspaargaren/camoufox) (Firefox engine, different TLS fingerprint entirely).

## License

MIT
