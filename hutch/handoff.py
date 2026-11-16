"""Handing a browser to a person and noticing when they are done.

``Session.await_human`` uses this. Nothing here solves or skips a check: a person does it in the
window (or ``hutch view NAME``), and Hutch only waits, looking for one of three signals:

- someone calls ``session.resume()``, or runs ``hutch resume NAME`` from any shell, which drops a
  file in the profile so the waiter can see it even from another process;
- the ``until`` condition holds, for example ``until="challenge"``: no challenge page and no
  unanswered challenge widget left on the active tab;
- the timeout passes.
"""

import json
import os
import re
import time

REQUEST_FILE = "hutch_handoff.json"
RESUME_FILE = "hutch_resume"

# a title or element of an interstitial page that holds the whole site back until it is passed
_INTERSTITIAL_TITLE = re.compile(
    r"just a moment|attention required|verify you are (a )?human|checking your browser|"
    r"one more step|ddos-guard|are you a (robot|human)|human verification|security check",
    re.I,
)
_INTERSTITIAL_JS = """() => !!document.querySelector(
    '#challenge-form, #challenge-stage, #cf-challenge-running, form[action*="__cf_chl"]')"""

# challenge widgets: (kind, frame url pattern, response field the page fills once it is answered)
_WIDGETS = (
    ("turnstile", re.compile(r"challenges\.cloudflare\.com/"), "cf-turnstile-response"),
    ("recaptcha", re.compile(r"/recaptcha/(api2|enterprise)/anchor"), "g-recaptcha-response"),
    ("hcaptcha", re.compile(r"hcaptcha\.com/.*frame=checkbox"), "h-captcha-response"),
    ("datadome", re.compile(r"captcha-delivery\.com/"), None),
    ("arkose", re.compile(r"(arkoselabs\.com|funcaptcha\.com)/"), None),
)
_ANSWERED_JS = """(name) => Array.from(document.querySelectorAll(
    `[name="${name}"], #${name}`)).some(e => (e.value || "").length > 20)"""


async def challenge_state(page):
    """What, if anything, is still asking for a person on this tab.

    {"present": bool, "kind": "interstitial" | "turnstile" | ... | None, "detail": str}. A widget
    that already carries its response token counts as answered.
    """
    try:
        title = await page.title()
    except Exception:
        title = ""
    if _INTERSTITIAL_TITLE.search(title or ""):
        return {"present": True, "kind": "interstitial", "detail": f"title: {title[:80]}"}
    try:
        if await page.evaluate(_INTERSTITIAL_JS):
            return {"present": True, "kind": "interstitial", "detail": "challenge form on the page"}
    except Exception:
        pass  # mid-navigation: look again on the next poll
    for frame in page.frames:
        url = frame.url or ""
        for kind, pattern, field in _WIDGETS:
            if not pattern.search(url) or "size=invisible" in url:
                continue
            if field is None:
                return {"present": True, "kind": kind, "detail": url[:120]}
            parent = frame.parent_frame
            try:
                answered = bool(parent and await parent.evaluate(_ANSWERED_JS, field))
            except Exception:
                answered = False
            if not answered:
                return {"present": True, "kind": kind, "detail": f"{field} is empty"}
    return {"present": False, "kind": None, "detail": ""}


def condition(until):
    """An async check(session) -> bool for ``until``, and how many polls in a row it must hold.

    ``until`` is a callable(session) returning a bool (or an awaitable of one), "challenge", or a
    dict with one of: {"gone": selector}, {"visible": selector}, {"url": substring}.
    """
    if until is None:
        return None, 1
    if callable(until):
        async def check(session):
            got = until(session)
            if hasattr(got, "__await__"):
                got = await got
            return bool(got)
        return check, 1
    if until == "challenge":
        async def check(session):
            page = session._active_page()
            return bool(page) and not (await challenge_state(page))["present"]
        # widgets render late and interstitials redirect: want two quiet polls, not one
        return check, 2
    if isinstance(until, dict) and len(until) == 1:
        (key, value), = until.items()
        if key in ("gone", "visible") and isinstance(value, str) and value:
            async def check(session):
                page = session._active_page()
                if not page:
                    return False
                try:
                    shown = await page.locator(value).first.is_visible()
                except Exception:
                    return False
                return shown if key == "visible" else not shown
            return check, 1
        if key == "url" and isinstance(value, str) and value:
            async def check(session):
                page = session._active_page()
                return bool(page) and value in (page.url or "")
            return check, 1
    raise ValueError(
        "until is a callable, \"challenge\", or one of {\"gone\": selector}, "
        "{\"visible\": selector}, {\"url\": substring}")


def write_request(profile_dir, info):
    if not profile_dir:
        return
    try:
        path = os.path.join(profile_dir, REQUEST_FILE)
        with open(path + ".tmp", "w") as f:
            json.dump(info, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def read_request(profile_dir):
    try:
        with open(os.path.join(profile_dir, REQUEST_FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def live_request(profile_dir):
    """The request a waiting process left in the profile, if that process is still running.

    A waiter that was killed cannot clean up after itself; its file is removed here instead of
    reporting a person being waited on forever.
    """
    info = read_request(profile_dir)
    if not info:
        return None
    pid = info.get("pid")
    if isinstance(pid, int) and pid > 0 and not _pid_alive(pid):
        clear_request(profile_dir)
        return None
    return info


def _pid_alive(pid):
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists but not ours, or a platform without signal 0: assume alive
    return True


def clear_request(profile_dir):
    for name in (REQUEST_FILE, RESUME_FILE):
        try:
            os.remove(os.path.join(profile_dir, name))
        except OSError:
            pass


def request_resume(profile_dir):
    """What ``hutch resume NAME`` does: tell a waiting session, in any process, to carry on."""
    path = os.path.join(profile_dir, RESUME_FILE)
    with open(path, "w") as f:
        f.write(str(time.time()))
    return path


def resume_requested(profile_dir, since):
    try:
        return os.path.getmtime(os.path.join(profile_dir, RESUME_FILE)) >= since - 1
    except OSError:
        return False
