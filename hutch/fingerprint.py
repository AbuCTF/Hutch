"""hutch/fingerprint.py — coherent fingerprint profile generation.

Antidetect browsers (GoLogin, Multilogin) generate fingerprint "profiles"
where every signal is internally consistent: the UA says Windows, the
platform says Win32, the screen says 1920x1080, the timezone says EST.
A mismatch (Linux UA + Windows platform) is the #1 detection signal.

This module generates coherent profiles from a small set of presets.
It's NOT trying to defeat Cloudflare Bot Management — that's a losing
arms race. It's making sure sessions don't correlate with each other
and don't scream "I'm a bot" from obvious mismatches.

For hardened WAF bypass, use Camoufox (Firefox engine) instead of
Chromium — different TLS fingerprint (JA3), different everything.
"""

import hashlib
import random
from typing import Optional

from .session import Fingerprint


_PRESETS = [
    {
        "name": "win-desktop-1080p",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": "Win32",
        "viewport": (1920, 1080),
        "screen": (1920, 1080),
        "locale": "en-US",
        "timezone": "America/New_York",
    },
    {
        "name": "win-desktop-1440p",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": "Win32",
        "viewport": (2560, 1440),
        "screen": (2560, 1440),
        "locale": "en-US",
        "timezone": "America/Chicago",
    },
    {
        "name": "mac-desktop-retina",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": "MacIntel",
        "viewport": (1440, 900),
        "screen": (2880, 1800),
        "locale": "en-US",
        "timezone": "America/Los_Angeles",
        "device_scale_factor": 2.0,
    },
    {
        "name": "linux-desktop",
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": "Linux x86_64",
        "viewport": (1920, 1080),
        "screen": (1920, 1080),
        "locale": "en-US",
        "timezone": "America/New_York",
    },
    {
        "name": "win-laptop-768p",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": "Win32",
        "viewport": (1366, 768),
        "screen": (1366, 768),
        "locale": "en-US",
        "timezone": "America/Denver",
    },
]


def generate(*, preset=None, seed=None, locale=None, timezone=None,
             geolocation=None) -> Fingerprint:
    """Generate a coherent fingerprint profile.

    Args:
        preset: name of a preset (e.g. "win-desktop-1080p") or None for random
        seed: deterministic seed — same seed = same fingerprint every time.
              Useful for giving each program a stable identity.
        locale: override locale (e.g. "en-IN")
        timezone: override timezone (e.g. "Asia/Kolkata")
        geolocation: override geo (e.g. {"latitude": 28.6, "longitude": 77.2})
    """
    rng = random.Random(seed) if seed else random.Random()

    if preset:
        p = next((x for x in _PRESETS if x["name"] == preset), None)
        if not p:
            names = [x["name"] for x in _PRESETS]
            raise ValueError(f"unknown preset '{preset}' — available: {names}")
    else:
        p = rng.choice(_PRESETS)

    vw, vh = p["viewport"]
    sw, sh = p["screen"]

    return Fingerprint(
        viewport_width=vw,
        viewport_height=vh,
        screen_width=sw,
        screen_height=sh,
        user_agent=p["ua"],
        platform=p.get("platform"),
        locale=locale or p.get("locale", "en-US"),
        timezone=timezone or p.get("timezone", "America/New_York"),
        device_scale_factor=p.get("device_scale_factor", 1.0),
        geolocation=geolocation,
        disable_webrtc=True,
    )


def generate_for_program(program_name: str, **kwargs) -> Fingerprint:
    """Generate a deterministic fingerprint from a program name.

    Same program name always gets the same fingerprint — consistent identity
    across sessions, but different programs get different profiles so they
    can't be correlated.
    """
    seed = int(hashlib.sha256(program_name.encode()).hexdigest()[:8], 16)
    return generate(seed=seed, **kwargs)


def list_presets():
    """Return available preset names and their descriptions."""
    return [
        {"name": p["name"], "viewport": f"{p['viewport'][0]}x{p['viewport'][1]}",
         "platform": p.get("platform", "?"), "locale": p.get("locale", "en-US")}
        for p in _PRESETS
    ]
