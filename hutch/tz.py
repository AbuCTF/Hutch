"""The time zone a session's browser should report when no persona says otherwise.

Detectors compare the browser's time zone with where its IP address is. A browser that says UTC
from an Indian or American home connection stands out; in measurements on 2026-11-11, two
Cloudflare-protected sites blocked a headless session on UTC and let the same session in once it
reported the machine's real zone.

On an ordinary desktop the system zone is already right and the browser inherits it. Under WSL
the Linux side often runs on UTC while Windows knows the real zone, so Hutch asks Windows and
passes the matching IANA zone to the browsers it starts. ``TZ`` in the environment always wins.
"""

import functools
import os
import subprocess
import time

from . import browser as _browser

# Windows time zone ids -> IANA (CLDR windowsZones, territory 001), the common ones
WINDOWS_ZONES = {
    "UTC": "Etc/UTC", "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin", "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw", "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau", "FLE Standard Time": "Europe/Kyiv",
    "GTB Standard Time": "Europe/Bucharest", "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul", "Israel Standard Time": "Asia/Jerusalem",
    "Egypt Standard Time": "Africa/Cairo", "South Africa Standard Time": "Africa/Johannesburg",
    "W. Central Africa Standard Time": "Africa/Lagos", "E. Africa Standard Time": "Africa/Nairobi",
    "Morocco Standard Time": "Africa/Casablanca", "Arabic Standard Time": "Asia/Baghdad",
    "Arab Standard Time": "Asia/Riyadh", "Arabian Standard Time": "Asia/Dubai", "Iran Standard Time": "Asia/Tehran",
    "Afghanistan Standard Time": "Asia/Kabul", "Pakistan Standard Time": "Asia/Karachi",
    "West Asia Standard Time": "Asia/Tashkent", "India Standard Time": "Asia/Kolkata",
    "Sri Lanka Standard Time": "Asia/Colombo", "Nepal Standard Time": "Asia/Kathmandu",
    "Bangladesh Standard Time": "Asia/Dhaka", "Myanmar Standard Time": "Asia/Yangon",
    "SE Asia Standard Time": "Asia/Bangkok", "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Singapore Standard Time": "Asia/Singapore", "China Standard Time": "Asia/Shanghai",
    "Taipei Standard Time": "Asia/Taipei", "W. Australia Standard Time": "Australia/Perth",
    "Tokyo Standard Time": "Asia/Tokyo", "Korea Standard Time": "Asia/Seoul",
    "Cen. Australia Standard Time": "Australia/Adelaide", "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane", "AUS Eastern Standard Time": "Australia/Sydney",
    "Tasmania Standard Time": "Australia/Hobart", "New Zealand Standard Time": "Pacific/Auckland",
    "Hawaiian Standard Time": "Pacific/Honolulu", "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time": "America/Los_Angeles", "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time": "America/Denver", "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City", "Canada Central Standard Time": "America/Regina",
    "Eastern Standard Time": "America/New_York", "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "SA Pacific Standard Time": "America/Bogota", "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns", "Pacific SA Standard Time": "America/Santiago",
    "Venezuela Standard Time": "America/Caracas", "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "E. South America Standard Time": "America/Sao_Paulo",
}
_TZUTIL = "/mnt/c/Windows/System32/tzutil.exe"


def windows_zone(zone_id):
    """IANA name for a Windows zone id, from the table above or, if installed, tzlocal's full one."""
    if zone_id in WINDOWS_ZONES:
        return WINDOWS_ZONES[zone_id]
    try:
        from tzlocal.windows_tz import win_tz
    except ImportError:
        return None
    return win_tz.get(zone_id)


@functools.lru_cache(maxsize=1)
def _windows_zone_id():
    try:
        out = subprocess.run([_TZUTIL, "/g"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def host_timezone():
    """IANA zone to give browsers, or None to let them inherit the system's (already right)."""
    if os.environ.get("TZ") or not _browser.is_wsl():
        return None
    if time.localtime().tm_gmtoff != 0:
        return None  # the Linux side already has a real zone
    zone = windows_zone(_windows_zone_id() or "")
    return zone if zone and zone != "Etc/UTC" else None
