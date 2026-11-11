"""Automation drivers, chosen per session.

``playwright`` is the default. ``patchright`` is a Playwright build that leaves out the traces
bot detectors read from a page: it never sends ``Runtime.enable`` or ``Console.enable``, and it
evaluates scripts in an isolated world, so page scripts cannot see them in error stacks.

What patchright gives up:
  - no console events (``Console.enable`` is the leak), so ``observe`` shows no console lines;
  - ``evaluate`` runs in an isolated world unless asked otherwise. Hutch asks for the page's
    main world wherever a caller expects page globals, see ``main_world``.
"""

import importlib

DRIVERS = ("playwright", "patchright")
INSTALL = {
    "playwright": "pip install playwright",
    "patchright": "pip install 'hutch[patchright]'",
}


def check(name):
    if name not in DRIVERS:
        raise ValueError(f"unknown driver '{name}', expected one of: {', '.join(DRIVERS)}")
    return name


def available(name):
    try:
        importlib.import_module(f"{check(name)}.async_api")
    except ImportError:
        return False
    return True


def async_playwright(name):
    """The driver's ``async_playwright()`` factory, imported on first use."""
    try:
        module = importlib.import_module(f"{check(name)}.async_api")
    except ImportError as exc:
        raise RuntimeError(f"driver '{name}' is not installed: {INSTALL[name]}") from exc
    return module.async_playwright


def _error_classes():
    errors, closed = [], []
    for name in DRIVERS:
        try:
            module = importlib.import_module(f"{name}._impl._errors")
        except ImportError:
            continue
        errors.append(module.Error)
        closed.append(module.TargetClosedError)
    return tuple(errors), tuple(closed)


# every installed driver's classes, usable in isinstance() and except clauses
ERRORS, TARGET_CLOSED = _error_classes()


def name_of(obj):
    """The driver a page, frame or context object belongs to."""
    return "patchright" if type(obj).__module__.startswith("patchright") else "playwright"


def main_world(obj):
    """Keyword arguments that make evaluate() run in the page's own world under either driver."""
    return {"isolated_context": False} if name_of(obj) == "patchright" else {}
