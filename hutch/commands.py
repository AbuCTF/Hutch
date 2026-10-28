"""Every session command in one place, read from Session itself; the daemon, client, MCP and CLI
dispatch through it so a new Session method is reachable everywhere without more wiring."""

import base64
import dataclasses
import enum
import inspect

from .session import Session

ALIASES = {"type": "type_text"}

_HIDDEN = {
    "launch", "close", "detach", "set_headless", "spawn_options", "cdp_endpoint", "target_id",
    "expose_function", "intercept", "on_close", "on_pause",
}


def _method(name):
    name = ALIASES.get(name, name)
    if name.startswith("_") or name in _HIDDEN:
        return None
    attr = inspect.getattr_static(Session, name, None)
    if attr is None or isinstance(attr, (property, staticmethod, classmethod)) or not callable(attr):
        return None
    return name


def exists(name):
    return _method(name) is not None


def names():
    found = [n for n in dir(Session) if _method(n)]
    return sorted(found + [a for a in ALIASES if _method(a)])


def parameters(name):
    target = _method(name)
    if not target:
        raise KeyError(f"unknown command '{name}'")
    return [p for p in inspect.signature(getattr(Session, target)).parameters if p != "self"]


def describe(name=None):
    """[{name, signature, doc}] for one command or all of them."""
    out = []
    for n in [name] if name else names():
        target = _method(n)
        if not target:
            raise KeyError(f"unknown command '{n}'")
        fn = getattr(Session, target)
        sig = str(inspect.signature(fn)).replace("(self, ", "(").replace("(self)", "()")
        doc = (inspect.getdoc(fn) or "").split("\n\n")[0].replace("\n", " ")
        out.append({"name": n, "signature": f"{n}{sig}", "doc": doc})
    return out


def plain(value):
    """A JSON-safe copy of a command result."""
    if isinstance(value, (bytes, bytearray)):
        return {"base64": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(v) for v in value]
    return value


async def call(session, name, args=None, positional=()):
    target = _method(name)
    if not target:
        raise KeyError(f"unknown command '{name}'")
    method = getattr(session, target)
    try:
        inspect.signature(method).bind(*positional, **(args or {}))
    except TypeError as exc:
        raise ValueError(f"{describe(name)[0]['signature']}: {exc}") from None
    result = method(*positional, **(args or {}))
    if inspect.isawaitable(result):
        result = await result
    return plain(result)
