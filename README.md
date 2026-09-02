# Hutch

isolated playwright session orchestrator.

multiple browser sessions, persistent state, per-session proxy, coherent fingerprints, zero leakage.

## Architecture

![architecture](docs/architecture.png)

## Install

```bash
pip install playwright
playwright install chromium

git clone https://github.com/AbuCTF/Hutch.git
cd Hutch
pip install -e .

# optional stealth patches
pip install playwright-stealth
```

## Usage

### CLI

```bash
hutch create my-target --preset win-desktop-1080p
hutch create my-target --proxy http://127.0.0.1:8081 --ignore-https-errors
hutch create yandex --program yandex --headed
hutch list
hutch auth my-target --url https://target.com/login
hutch screenshot my-target evidence.png --full-page
hutch close my-target
hutch destroy my-target
hutch presets
```

### Python

```python
from hutch import Pool, generate, generate_for_program

async with Pool() as pool:
    s = await pool.create(
        "target-a",
        fingerprint=generate(preset="win-desktop-1080p"),
        proxy="http://127.0.0.1:8081",
        ignore_https_errors=True,
    )

    page = await s.new_page()
    await page.goto("https://target.com")

    await s.save_state()
    await s.close()

    # reopen later — full auth state restored
    s = await pool.get("target-a", launch=True)
    page = await s.new_page()
```

### Multi-session isolation

```python
async with Pool() as pool:
    s1 = await pool.create("prog-a", fingerprint=generate_for_program("alpha"))
    s2 = await pool.create("prog-b", fingerprint=generate_for_program("beta"))
    # separate cookies, proxy, fingerprint per session
```

## Fingerprint presets

| Preset | Viewport | Platform |
|--------|----------|----------|
| `win-desktop-1080p` | 1920x1080 | Win32 |
| `win-desktop-1440p` | 2560x1440 | Win32 |
| `mac-desktop-retina` | 1440x900 | MacIntel |
| `linux-desktop` | 1920x1080 | Linux x86_64 |
| `win-laptop-768p` | 1366x768 | Win32 |

each preset bundles a matching UA, platform, screen, locale, and timezone.

## Stealth

with `playwright-stealth` installed:
- `navigator.webdriver` → false
- WebGL / canvas noise
- plugin array spoofing
- font list shimming
- WebRTC IP leak blocked

not covered (chromium limitation): TLS fingerprint (JA3/JA4), HTTP/2 settings fingerprint.

## License

MIT
