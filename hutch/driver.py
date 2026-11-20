"""Automation drivers, chosen per session.

``playwright`` is the default. ``patchright`` is a Playwright build that leaves out the traces
bot detectors read from a page: it never sends ``Runtime.enable`` or ``Console.enable``, and it
evaluates scripts in an isolated world, so page scripts cannot see them in error stacks.

``camoufox`` (experimental) is a patched Firefox behind the same Playwright page API
(``hutch.camoufoxsession``); it needs ``camoufox`` installed apart from Hutch.

``cdp`` is Hutch's own: no automation library at all, only ``hutch.cdp`` speaking DevTools to a
Chrome Hutch launched itself (``hutch.cdpsession.CdpSession``). It sends only the commands a call
needs, never ``Runtime.enable``, and installs no init scripts, routes or emulation overrides. It
covers navigation, evaluate, content, screenshots, CSS-selector input, cookies, tabs and
``clear_challenge``; the rest of the Playwright-shaped surface (routing, mocking, network capture,
locators, tracing) raises a clear error instead.

What patchright gives up:
  - no console events (``Console.enable`` is the leak), so ``observe`` shows no console lines;
  - ``evaluate`` runs in an isolated world unless asked otherwise. Hutch asks for the page's
    main world wherever a caller expects page globals, see ``main_world``.
"""

import importlib

PLAYWRIGHT_DRIVERS = ("playwright", "patchright")
DRIVERS = PLAYWRIGHT_DRIVERS + ("cdp", "camoufox")
CHROME_DRIVERS = PLAYWRIGHT_DRIVERS + ("cdp",)  # the ones that drive Chrome over DevTools
OWN_DRIVERS = ("cdp", "camoufox")  # drivers that are a Session class of their own, not a Playwright factory
INSTALL = {
    "cdp": "built in",
    "camoufox": "pip install camoufox && python -m camoufox fetch",
    "playwright": "pip install playwright",
    "patchright": "pip install 'hutch[patchright]'",
}


def check(name):
    if name not in DRIVERS:
        raise ValueError(f"unknown driver '{name}', expected one of: {', '.join(DRIVERS)}")
    return name


def available(name):
    if check(name) == "cdp":
        return True
    try:
        importlib.import_module(f"{name}.async_api" if name != "camoufox" else name)
    except ImportError:
        return False
    return True


def async_playwright(name):
    """The driver's ``async_playwright()`` factory, imported on first use."""
    if check(name) in OWN_DRIVERS:
        raise ValueError(f"the {name} driver does not use a Playwright factory")
    try:
        module = importlib.import_module(f"{name}.async_api")
    except ImportError as exc:
        raise RuntimeError(f"driver '{name}' is not installed: {INSTALL[name]}") from exc
    return module.async_playwright


def _error_classes():
    errors, closed, timeouts = [], [], []
    for name in PLAYWRIGHT_DRIVERS:
        try:
            module = importlib.import_module(f"{name}._impl._errors")
        except ImportError:
            continue
        errors.append(module.Error)
        closed.append(module.TargetClosedError)
        timeouts.append(module.TimeoutError)
    return tuple(errors), tuple(closed), tuple(timeouts)


# every installed driver's classes, usable in isinstance() and except clauses
ERRORS, TARGET_CLOSED, _PW_TIMEOUTS = _error_classes()
# a navigation or wait that ran out of time, under any driver
TIMEOUTS = _PW_TIMEOUTS + (TimeoutError,)


def name_of(obj):
    """The driver a page, frame or context object belongs to."""
    if type(obj).__module__.startswith("hutch.cdp"):
        return "cdp"
    return "patchright" if type(obj).__module__.startswith("patchright") else "playwright"


def main_world(obj):
    """Keyword arguments that make evaluate() run in the page's own world under either driver."""
    return {"isolated_context": False} if name_of(obj) == "patchright" else {}
