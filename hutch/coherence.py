"""Refuse to launch a persona whose layers tell different stories.

A warmed identity clears a risk engine because every layer agrees: a Windows UA carries a Win32
navigator.platform, a Chrome-major that matches its own client hints, a US timezone under a US locale,
and an exit IP in that same region. The moment one layer drifts — a Mac platform under a Windows UA, a
client-hint major that lags the UA, a European locale on an America/* clock — the set stops being one
story and the score re-challenges, which burns a profile that behaviour alone would have kept trusted.

This reads the Fingerprint it is handed and names those contradictions before the browser opens. No
network and no new dependency: the hard checks (``error``) are the near-impossible ones a real browser
never shows and so mark an automation build bug; the soft ones (``warn``) are the human-plausible drifts
(a traveller's clock) that are worth surfacing but not worth refusing a launch over.
"""

import os
import re
from collections import namedtuple

Issue = namedtuple("Issue", ("level", "layer", "message"))


class IncoherentIdentity(RuntimeError):
    """A persona's layers contradict each other badly enough to fingerprint it as automation."""


# navigator.platform for Android is "Linux armv8l" and for ChromeOS is "Linux x86_64", so the UA's OS
# maps to the SET of navigator.platform families that are coherent with it, not a single value.
_COMPATIBLE = {
    "windows": {"windows"},
    "mac": {"mac"},
    "linux": {"linux"},
    "android": {"linux", "android"},
    "chromeos": {"linux", "chromeos"},
}

# locale country -> continent, and timezone prefix -> continent, for the soft region check. Only the
# locales/timezones the generator actually emits need an entry; an unknown one simply skips the check.
_COUNTRY_CONTINENT = {
    "US": "america", "CA": "america", "BR": "america", "MX": "america",
    "GB": "europe", "DE": "europe", "FR": "europe", "ES": "europe", "IT": "europe", "NL": "europe",
    "JP": "asia", "CN": "asia", "KR": "asia", "IN": "asia",
    "AU": "australia", "NZ": "pacific",
}
_TZ_CONTINENT = {
    "America": "america", "Europe": "europe", "Asia": "asia",
    "Australia": "australia", "Pacific": "pacific", "Africa": "africa",
}


def _os_from_ua(ua):
    ua = ua or ""
    if "Windows NT" in ua:
        return "windows"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "mac"
    if "Android" in ua:
        return "android"
    if "CrOS" in ua:
        return "chromeos"
    if "X11" in ua or "Linux" in ua:
        return "linux"
    return None


def _os_from_platform(plat):
    p = (plat or "").lower()
    if p.startswith("win"):
        return "windows"
    if "mac" in p:
        return "mac"
    if "cros" in p:
        return "chromeos"
    if "linux" in p:
        return "linux"
    return None


def _ua_major(ua):
    m = re.search(r"Chrome/(\d+)", ua or "")
    return m.group(1) if m else None


def _client_hint_major(sec_ch_ua):
    for brand in ("Google Chrome", "Chromium", "Microsoft Edge", "Brave"):
        m = re.search(rf'"{re.escape(brand)}";v="(\d+)"', sec_ch_ua or "")
        if m:
            return m.group(1)
    return None


def _continent_of_locale(locale):
    parts = (locale or "").replace("_", "-").split("-")
    if len(parts) < 2:
        return None
    return _COUNTRY_CONTINENT.get(parts[1].upper())


def _continent_of_tz(timezone):
    return _TZ_CONTINENT.get((timezone or "").split("/")[0])


def inspect(fp, *, proxy_region=None):
    """Return every coherence Issue in a Fingerprint, most severe first. ``proxy_region`` is the exit
    IP's continent when known (from the registry / a geo lookup); None skips the proxy cross-check."""
    issues = []
    ua = getattr(fp, "user_agent", None)
    platform = getattr(fp, "platform", None)

    ua_os = _os_from_ua(ua)
    plat_os = _os_from_platform(platform)
    if ua_os and plat_os and plat_os not in _COMPATIBLE.get(ua_os, {ua_os}):
        issues.append(Issue("error", "platform",
                            f"UA claims {ua_os} but navigator.platform {platform!r} reads as {plat_os}"))

    ua_major = _ua_major(ua)
    ch_major = _client_hint_major(getattr(fp, "sec_ch_ua", None))
    if ua_major and ch_major and ua_major != ch_major:
        issues.append(Issue("error", "client-hints",
                            f"UA is Chrome {ua_major} but sec-ch-ua says {ch_major}"))

    loc_cont = _continent_of_locale(getattr(fp, "locale", None))
    tz_cont = _continent_of_tz(getattr(fp, "timezone", None))
    lang = (getattr(fp, "locale", "") or "").split("-")[0].lower()
    # English is spoken on every continent, so an en-* locale never contradicts a timezone; only a
    # region-bound language (de, ja, ...) on a foreign clock is the drift worth flagging.
    if lang and lang != "en" and loc_cont and tz_cont and loc_cont != tz_cont:
        issues.append(Issue("warn", "locale",
                            f"locale {fp.locale} ({loc_cont}) on a {tz_cont} timezone {fp.timezone}"))

    if proxy_region and tz_cont and proxy_region.lower() != tz_cont:
        issues.append(Issue("warn", "proxy",
                            f"timezone {fp.timezone} ({tz_cont}) behind a {proxy_region} exit IP"))

    issues.sort(key=lambda i: 0 if i.level == "error" else 1)
    return issues


def enforce(fp, *, name=None, proxy_region=None, mode=None):
    """Raise IncoherentIdentity on any error-level contradiction, else return the (warn-level) issues to
    log. ``mode`` defaults to $HUTCH_COHERENCE: 'enforce' (default), 'warn' (never raise), 'off' (skip)."""
    mode = (mode or os.environ.get("HUTCH_COHERENCE") or "enforce").lower()
    if mode == "off":
        return []
    issues = inspect(fp, proxy_region=proxy_region)
    errors = [i for i in issues if i.level == "error"]
    if errors and mode != "warn":
        who = f"session {name!r}: " if name else ""
        raise IncoherentIdentity(who + "incoherent identity — " + "; ".join(i.message for i in errors))
    return issues
