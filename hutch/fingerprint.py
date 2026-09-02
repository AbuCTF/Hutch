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

_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Phoenix", "America/Anchorage",
    "Europe/London", "Europe/Berlin", "Europe/Paris",
    "Asia/Tokyo", "Asia/Shanghai", "Asia/Kolkata",
    "Australia/Sydney", "Pacific/Auckland",
]

_LOCALES = [
    "en-US", "en-GB", "en-AU", "en-CA",
    "de-DE", "fr-FR", "ja-JP", "zh-CN",
    "pt-BR", "es-ES", "ko-KR", "it-IT",
]


def generate(*, preset=None, seed=None, locale=None, timezone=None,
             geolocation=None) -> Fingerprint:
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

    if not locale and not preset:
        locale = rng.choice(_LOCALES)
    if not timezone and not preset:
        timezone = rng.choice(_TIMEZONES)

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
    seed = int(hashlib.sha256(program_name.encode()).hexdigest()[:8], 16)
    return generate(seed=seed, **kwargs)


def list_presets():
    return [
        {"name": p["name"], "viewport": f"{p['viewport'][0]}x{p['viewport'][1]}",
         "platform": p.get("platform", "?"), "locale": p.get("locale", "en-US")}
        for p in _PRESETS
    ]
