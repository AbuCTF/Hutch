# Hutch Upgrade Roadmap

> Research date: 2026-10-28
> Scope: audit of v0.10.5, landscape survey, engine/platform choice, target architecture, phased plan

---

## 0. Checklist

### v0.11.0 — done

- [x] hutch launches chromium itself (`browser.py`) and attaches over CDP; any client (CLI, daemon,
      MCP, python) and the human's window share one browser per profile
- [x] real identity by default: no `navigator.webdriver`, no `HeadlessChrome` UA, no init-script
      spoofing; fingerprints only for explicit personas (verified)
- [x] closing the window ends the process (0.15 s) and keeps the profile; nothing idles in the background
- [x] `browse`, `login`, `headed`, `headless`, `tabs`, `ps` (with memory), `stop`, `clone`, `attach`, `doctor`
- [x] headless ↔ headed via chromium's own session restore: tabs, session cookies, sessionStorage
      (verified, 1.7 s); graceful `Browser.close` — SIGTERM loses tabs
- [x] attach waits for session restore to settle (attaching early misses restored tabs)
- [x] agent attached to a human window works in its own tab and never follows tabs the human opens
- [x] owned headless browsers die with their owner (PDEATHSIG); headed windows outlive agents
- [x] `aria_snapshot(mode="ai")` replaces the removed `page.accessibility`; refs (`e12`, `f1e3`) accepted
      by every action; MCP observe returns refs
- [x] persona UA/client-hints pinned to the real engine version; `sec_ch_ua` persisted
- [x] site isolation back on; canvas noise stable per session; window size faked only headless
- [x] daemon never idle-closes headed/attached browsers; `--exit-when-idle`
- [x] version from package metadata; extras fixed; `setup.sh` fails loudly; `--no-shell` (−273 MB)
- [x] `HUTCH_BASE_DIR`

### v0.11.1 – v0.12.0 — done

- [x] `curl -fsSL hutch.abu.rocks | sh` installer (Cloudflare redirect to the raw GitHub file)
- [x] terse CLI and doctor output
- [x] Google sign-in verified: 2 accounts, still signed in with the debugging port on, webdriver false
- [x] WSL mirrored networking: not now, agents run inside WSL
- [x] live view: `hutch view`, MCP `view`, daemon `view`; screencast + mouse, keyboard, paste, copy,
      tabs, URL bar; follows popups from the viewed tab; reachable from Windows under NAT
- [x] agents start a stopped profile headless (a remembered headed mode no longer pops a window)

### v0.13.0 — done

- [x] event log per session: dialogs, downloads, file choosers, popups, crashes; MCP appends new
      events to every tool result; `hutch_events` list / wait / dialog / policy / files; daemon + client
- [x] dialogs: a listener on every tab stops playwright auto-dismissing the human's dialogs; agent tabs
      answered by one-shot plan, policy, or default (accept alert/beforeunload, dismiss confirm/prompt)
- [x] downloads over CDP (`Browser.setDownloadBehavior` events): headless saves to `<profile>/downloads`,
      headed keeps the user's folder; `expect_download` works on native sessions again
- [x] file choosers intercepted only on agent tabs; MCP `upload` action; live view shows and answers dialogs

### v0.14.0 — done

- [x] actions return only what changed on the tab (line diff of the aria snapshot, refs are stable);
      full tree after a navigation or first look
- [x] `filter: interactive` snapshot: actionable elements + headings, link targets, nearby label for
      unnamed inputs
- [x] annotated screenshots from `aria_snapshot(boxes=True)` (one call, iframe offsets applied), with legend

### v0.15.0 — done

- [x] command registry read from `Session` (signatures + docstrings): daemon falls back to it, 33
      pass-through RPC wrappers deleted, client `__getattr__` fallback with positionals, MCP `hutch_call`,
      `hutch call <name> <cmd> ...` (httpie-style `key=value` / `key:=json`); 106 commands everywhere
- [x] curated MCP tools stay (grouped actions are easier for models than 106 tools); `hutch_call` covers the rest

### v0.16.0 — done

- [x] secrets vault (`hutch secret`, 0600 file), `{{name}}` resolved at type time, domain binding,
      redaction of every MCP / daemon response (raw, JSON-escaped and URL-encoded forms)
- [x] recorder from real DOM events (human or agent alike), robust selectors, password scrubbing incl. URLs;
      `hutch record`, `replay`, `export` (standalone playwright); session methods so every surface has them
- [x] a client joining a browser acts on the tab in front, not the newest-listed (restored sessions)
- [x] `hutch call` treats `key=value` as a keyword only for real parameters (selectors contain `=`)

### v0.17.0 — done

- [x] capture byte budget (64 MB of bodies per session, oldest bodies evicted first, metadata kept);
      bounded in-flight request table, console text, websocket connection count
- [x] artifacts keep the newest N per kind and session; chromium disk cache capped per profile
- [x] `hutch gc`: old artifacts and downloads, stale state of stopped profiles, `--caches`, `--dry-run`;
      running browsers and site data (cookies, storage) are never touched

### Next

- [ ] Windows transport (loopback TCP + token), PDEATHSIG equivalent (job objects)
- [ ] tab leases / visual indicator on agent-driven tabs

---

## 1. Where v0.10.5 stands

Baseline on WSL2 Debian 13, Playwright 1.64.1 (Chromium 156), Python 3.13:

- fresh install: 101 pass / 84 fail / 44 error — every failure is `libglib-2.0.so.0` missing.
  `setup.sh` runs `install-deps ... || true`, so the real error is swallowed.
- with Chromium's libs side-loaded (no fonts, no sudo): 189 pass / 40 fail — the 40 are renderer
  crashes that reproduce with bare Playwright, i.e. environment, not Hutch.

### Strengths worth keeping

- per-session isolation (own `user_data_dir`, proxy, fingerprint), coherent presets
- ring-buffered network/console/error/navigation capture with cursor diffs
- interception, mocking, HAR, WebSocket frame capture, Caido integration
- disconnect recovery decorator + watchdog
- broad action surface (~110 RPC methods)

### Defects found

| # | Where | Problem | Impact |
|---|-------|---------|--------|
| 1 | `session.py:739,966`, `mcp.py:713` | `page.accessibility` was removed in Playwright 1.64; the bare `except` hides it | `page_state()["dom"]` is always `None` — the "a11y tree after every action" feature is silently gone |
| 2 | `session.py:208-268` | `sec_ch_ua` is never written to / read from `hutch_meta.json` | after any relaunch the CDP client-hint override is skipped → `HeadlessChrome` leak returns |
| 3 | `fingerprint.py` presets | UA pins `Chrome/137` while the engine is 156 | version/feature mismatch is a cheap detection signal |
| 4 | `cli.py:111` (`open`, `create --url`, `screenshot --url`) | each CLI call opens its own `Pool`, which closes the browser on exit | `hutch open x url` flashes a browser and closes it |
| 5 | `cli.py`, `server.py`, `mcp.py` | three independent `Pool`s | same profile in two processes hits Chromium's `SingletonLock`; MCP cannot see daemon sessions; an agent cannot join a browser the human opened |
| 6 | `server.py:83` | idle loop closes *any* alive session after 15 min | would close a human's headed window |
| 7 | `server.py:576` | `handoff` only reports state | a headless session paused on a captcha has no window for the human to solve it in |
| 8 | `session.py:1029`, `mcp.py:603` | observe selectors are non-unique (`tag.cls:has-text(...)`), and `idx` is re-resolved by re-running observe | misclicks when the page shifts or two elements share text |
| 9 | `stealth.py` | canvas noise keyed on `Date.now()`; `outerWidth/outerHeight` forced; WebGL claims NVIDIA on SwiftShader | repeat-read canvas hashes differ (detectable); headed geometry lies; GPU claims contradict the real renderer |
| 10 | `session.py:290` | `--disable-features=IsolateOrigins,site-per-process` | turns off site isolation — a security downgrade for a daily browser, and itself a fingerprint |
| 11 | `server.py:108` | requests on one connection run serially; no push events | one long `wait_for` blocks the client; dialogs/downloads/popups cannot be surfaced as they happen |
| 12 | `server.py`, `session.py:341` | Unix socket + `hyprctl` | no native Windows; Hyprland-only geometry code in the core |
| 13 | `__init__.py`, `server.py:1039` | `__version__ = "0.7.0"`, HAR creator `0.5.0` | stale metadata |
| 14 | `pyproject.toml` | `all = hutch[stealth,caido,mcp]` but no `caido` extra | pip warning; `aiohttp` is a hard dep only Caido needs |
| 16 | `session.py:434` | persistent profiles default to `chromium-headless-shell`, which takes no profile lock | agent + human can open and corrupt the same profile; `HeadlessChrome` brand; no Widevine |
| 15 | `server.py` / `client.py` / `mcp.py` | three hand-written mirrors of the same API | every new action is written 3×, and they already drift (MCP exposes ~15% of the surface) |

---

## 2. Landscape — what to take from whom

| Tool | Idea to adopt (and how we'd upgrade it) |
|------|------------------------------------------|
| playwright-mcp / playwright-cli | ARIA snapshot with `[ref=eN]` (Playwright 1.64 ships it: `page.aria_snapshot(mode="ai", boxes=True)` + `aria-ref=eN` selector). `show` dashboard: click a viewport to take input, Esc to hand back. `detach` leaves the user's browser running. |
| chrome-devtools-mcp | consent-based attach to the user's real Chrome (Chrome 144+ `chrome://inspect/#remote-debugging`, read `DevToolsActivePort`). `--slim` toolset. |
| Vercel agent-browser | daemon + thin CLI, `snapshot -i` (interactive only), `screenshot --annotate` (ref labels drawn on the image), `diff snapshot`, auth vault, live stream. |
| dev-browser | attached browsers belong to the user — never idle-closed. Named pages that persist across calls. |
| Stagehand v3 | action cache keyed on instruction + URL → replay without the LLM. |
| Notte | secrets as `{{placeholders}}` substituted at execution; the model never sees them. |
| Claude in Chrome | tab group as the permission boundary — the agent sees only tabs the human shares. |
| Playwriter | one `execute` tool exposing the full Playwright API as an escape hatch instead of 100 schemas. |
| Browserbase / Steel / Kernel | per-tab live view, read-only vs interactive; profiles opt-in to saving changes. |
| browser-use harness | agent writes missing helpers into a workspace file; core stays fixed. |
| Lightpanda | cheap no-render tier for read-only fetches. |

Recurring user complaints these address: token cost of page dumps, ref staleness, uploads/downloads/iframes/dialogs, logged-in access vs prompt-injection risk, no replay, clumsy human takeover, tools closing the user's browser.

---

## 3. Engine, platform and footprint

Measured on WSL2 Debian 13, 12 vCPU, Playwright 1.64.1 (bundled build is now Chrome for Testing 156).
Proportional set size (PSS), whole process tree, ±15%.

| | headless shell (current default) | full build, new headless (`channel="chromium"`) | headed |
|---|---|---|---|
| launch | 88–172 ms | 190–380 ms | 230–540 ms |
| idle, 1 blank tab | **106 MB** | **~330 MB** | ~331 MB |
| each extra tab | ~95 MB | ~93 MB | ~94 MB |
| each extra context + tab | ~85 MB | ~142 MB | ~136 MB |
| Widevine | no | yes | yes |
| profile lock | **none** | yes | yes |

Playwright's Node driver costs 114–178 MB once per daemon; the Python daemon itself 21–28 MB.
Memory flags (`--renderer-process-limit`, `--process-per-site`, disabling isolation) saved ~0–7% —
the real levers are fewer tabs, closing/restoring idle tabs, and sharing one browser for throwaway contexts.

**Profile corruption risk (measured):** the headless shell takes no `SingletonLock` and will open a
profile a headed Chrome already holds. Hutch defaults to headless shell on persistent profiles, so an
agent and a human can write one profile at once. Hutch needs its own lock (`flock` / `msvcrt.locking`).

**Handoff (measured):** headless → headed → headless on one profile keeps cookies + localStorage,
relaunch ≈ 0.5 s, provided the binary version never goes *down* (bundled 156 vs stable Chrome 155
is a downgrade). One channel per profile, recorded in meta, enforced.

### Engine verdict

| Engine | Verdict |
|---|---|
| bundled Chrome for Testing | **default** — pinned, same binary headless & headed, H.264/AAC, CDP |
| branded Chrome / Edge | opt-in per profile for daily use: Sync, Web Store extensions, full DRM |
| Firefox / Camoufox | no CDP (Firefox removed it in 141) → nothing else can attach; optional persona backend at most |
| WebKit | no |
| Lightpanda | optional no-render fetch tier (AGPL, glibc-only) |
| Brave | only for people who need MV2 uBlock Origin; via `executable_path` |

Language: stay on Python + Playwright. A Rust/Go rewrite saves ~150 MB once per machine against
~330 MB per profile — not worth losing Playwright's auto-wait and maturity.

### Platforms

| Platform | Status |
|---|---|
| Debian 12/13, Ubuntu 22.04–26.04 (x64, arm64) | supported; `install-deps` needs sudo |
| Arch, Fedora | works with distro packages / system Chrome; `install-deps` (apt) fails |
| NixOS | nixpkgs browsers + matching Playwright version |
| Alpine (musl) | unsupported |
| macOS 14+ | works; mock keychain makes profiles non-portable to real Chrome |
| Windows 11 | Playwright fine; **no `start_unix_server`** → need loopback TCP + token or named pipe |
| WSL2 | headed via WSLg works; can drive the Windows host's Chrome over CDP with mirrored networking |
| Docker | `--ipc=host --init`, non-root |

---

## 3a. Disk and memory footprint

| Consumer | Size | Notes |
|----------|------|-------|
| `~/.cache/ms-playwright` | ~680 MB per Playwright version | two Chromiums (full 403 MB + headless shell 273 MB) + ffmpeg; every venv/project pinning a different Playwright keeps its own set |
| `.venv` | ~225 MB | Playwright's bundled Node driver alone is 135 MB |
| each profile | 1–15 MB empty, unbounded in use | HTTP `Cache`, `Code Cache`, GPU/Dawn caches, Service Worker CacheStorage and IndexedDB (per-origin quota is a share of free disk), on-device model downloads in branded Chrome. Hutch sets no `--disk-cache-size` and never prunes |
| `~/.hutch/artifacts` | unbounded | HARs, screenshots, PDFs, snapshots never rotated; `trace.zip` lands in the profile dir |
| RAM: capture buffers | up to ~1 GB per session | `network_cap=1000` × response bodies up to 1 MB of text (JS bundles count as text) + 200 × 4 KB per WebSocket |
| RAM: browsers | one full Chrome process tree per session | persistent profiles cannot share a browser process |

Fixes: `hutch gc` (prune caches, artifacts, stale Playwright builds), `--disk-cache-size` default for
agent profiles, byte-budgeted capture buffers (e.g. 32 MB per session, bodies off-heap to disk),
artifact retention, only install the headless shell when needed.

## 4. Target architecture

**Invert ownership.** Today Playwright launches and owns each browser over a pipe, so only the
process that launched it can drive it. Every goal in this upgrade — human daily browser, agent
joining the human's tabs, headless↔headed, CLI + MCP + Python sharing one browser — needs the
opposite: the browser is a long-lived process, and everything (Playwright, MCP, CLI, the human)
*attaches* to it.

```
                       ┌──────────────── hutchd (one per user) ───────────────┐
 hutch CLI ─┐          │ registry ─ profiles ─ leases ─ event bus ─ vault     │
 MCP stdio ─┼─ RPC ───►│                                                      │
 Python    ─┘ (unix    │  profile "main" ── Chrome (headed)  ◄── human        │
              socket / │  profile "agent-1" ─ Chrome (new headless)           │
              named    │  attach "my-chrome" ─ user's own Chrome (144+)       │
              pipe)    │        ▲ connect_over_cdp per browser                 │
                       └────────┼─────────────────────────────────────────────┘
                                └─ live view (screencast + input) on localhost
```

- **hutchd** is the only process that touches browsers. CLI, MCP and Python client become thin
  clients; the MCP server stops owning a `Pool`. Auto-start on first call, like agent-browser.
- **Launch real Chrome ourselves** (`subprocess`, `--user-data-dir=<profile>`, `--remote-debugging-port=0`),
  read `DevToolsActivePort`, then `connect_over_cdp`. Closing a Hutch client never kills the
  browser; the human keeps their window. Same code path attaches to the user's own Chrome.
- **One command registry** (name, params schema, handler, doc) generates the RPC dispatch, CLI
  subcommands, MCP tools and the Python client — fixes defect 15.
- **Leases**: a tab is owned by `human`, `agent:<id>`, or shared. Agents create their own tabs by
  default and see the human's tabs read-only until shared. Per-client diff cursors.
- **Event bus**: dialogs, downloads, file choosers, popups, auth/captcha alerts pushed to
  subscribers instead of pre-registered handlers.

### Headless ↔ headed

Chrome cannot switch modes in-process, so offer two paths:

1. **Live view (instant, no relaunch)** — `hutch show <session>` serves a local page streaming
   `Page.startScreencast` frames and forwarding mouse/keyboard via `Input.dispatch*`. Works for
   headless sessions, remote boxes, WSL without WSLg. Human solves the captcha, clicks "hand back".
2. **Promote (full fidelity)** — `hutch promote <session>` snapshots cookies (incl. session cookies
   via `Network.getAllCookies`), open tabs + sessionStorage, closes, relaunches headed on the same
   profile, restores. `demote` is the reverse. Lossy for in-memory page state (unsaved form input,
   JS state) — that is a hard Chrome limit.

### Human mode vs persona mode

| | human / daily profile | persona (recon, authz testing) |
|---|---|---|
| binary | bundled Chrome for Testing (or `--browser chrome/brave`) | same |
| fingerprint | real — no UA/platform spoofing (default) | coherent preset, version-matched to the engine |
| extensions | allowed (password manager, uBlock) | off |
| password store | OS keyring | `basic` |
| site isolation | on | on |
| tabs | restored on relaunch | per run |
| idle close | never | yes |

Spoofing a Windows UA on a Linux engine is *more* detectable, and a spoofed UA was one of the flags
in reported Google sign-in refusals. Fingerprint presets stay, but only for personas.

### Google sign-in

Google blocks sign-in it considers automated. Most reliable path from community reports:
`hutch login main` launches plain Chrome on the profile with **no debugging port**, the human signs
in, closes it; subsequent launches add the port and cookies keep working. Alternative: attach to the
user's own Chrome (144+ toggle) where they are already signed in. Copying an existing Chrome
profile does not work (app-bound / keyring-bound cookie encryption).

---

## 5. Agent ergonomics

- `snapshot` → ARIA tree with refs; `-i` interactive only; `--diff` against previous; written to a
  file the agent can grep when large. Actions take `e12`, a CSS/role locator, or `x,y`.
- action results: compact delta (url/title change, new tab, dialog, download, console errors), not
  a full tree dump.
- `screenshot --annotate` with ref labels.
- waits: DOM-quiet (MutationObserver) + in-flight request count, instead of `networkidle` which
  stalls on polling SPAs.
- first-class uploads (local path), downloads (to session dir), dialogs (pending-event queue),
  iframes and shadow DOM (refs cross them).
- secrets: `hutch secret set github.password`; actions accept `{{github.password}}`.
- record → replay: every action logged with the ref's resolved locator; `hutch replay` re-runs
  without the LLM, `hutch export` emits a Playwright Python script.
- CLI-first for coding agents (`hutch -s main click e12`) plus a `SKILL.md`; MCP kept, slimmed to
  ~8 tools + one `run` escape hatch.

---

## 6. Phased plan

1. **v0.11 — fix the floor**: defects 1, 2, 3, 6, 10, 13, 14, 16 (Hutch profile lock, `channel="chromium"`); `hutch doctor` (missing libs,
   fonts, display, Chrome found); setup.sh stops swallowing errors.
2. **v0.12 — snapshot & refs**: `aria_snapshot(mode="ai")`, ref-based actions, snapshot diff,
   annotated screenshots; remove the hand-rolled observe walker.
3. **v0.13 — one brain**: command registry; CLI/MCP/Python become hutchd clients; auto-start.
4. **v0.14 — browser as a service**: self-launched Chrome + `connect_over_cdp`; `hutch browse`
   (human), `hutch attach chrome`, leases, never-close for human/attached browsers.
5. **v0.15 — collaboration**: live view, promote/demote, event bus, agent-tab indicator.
6. **v0.16 — productivity**: vault, record/replay/export, action cache.
7. **v0.17 — portability**: loopback-TCP/named-pipe transport for Windows, Hyprland sync moved behind an
   optional window-manager hook, packaging (`uv tool install`).
