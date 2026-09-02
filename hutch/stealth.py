import importlib


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
    return _check_available()


async def apply_stealth(context):
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
