"""hutch/stealth.py — playwright-stealth integration.

playwright-stealth patches Playwright contexts to defeat common bot
detection: navigator.webdriver=false, WebGL noise, canvas noise,
plugin spoofing, font shimming. It covers ~80% of detection vectors
with one wrapper.

This module is optional — hutch works without it, just with less
stealth. Install with: pip install playwright-stealth

What it does NOT cover (and nothing in JS-land can):
- TLS fingerprint (JA3/JA4) — Chromium's handshake is well-known
- HTTP/2 settings fingerprint — same issue
For those, you need a different engine (Camoufox = Firefox-based)
"""

import importlib
from typing import Optional


_stealth_mod = None
_available = None


def _check_available():
    global _stealth_mod, _available
    if _available is not None:
        return _available
    try:
        _stealth_mod = importlib.import_module("playwright_stealth")
        _available = True
    except ImportError:
        _available = False
    return _available


def is_available():
    """Check if playwright-stealth is installed."""
    return _check_available()


async def apply_stealth(context):
    """Apply stealth patches to a Playwright browser context.

    Call this right after launch_persistent_context() and before
    navigating to any page. It injects scripts that run on every
    new page/frame in the context.

    Returns True if applied, False if playwright-stealth not installed.
    """
    if not _check_available():
        return False

    if hasattr(_stealth_mod, "Stealth"):
        stealth = _stealth_mod.Stealth()
        await stealth.apply_stealth_async(context)
    elif hasattr(_stealth_mod, "stealth_async"):
        await _stealth_mod.stealth_async(context)
    else:
        for page in context.pages:
            await _stealth_mod.stealth_async(page)
    return True
