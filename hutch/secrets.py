"""Secrets agents can type but never read: {{name}} placeholders, bound to domains, redacted from output."""

import json
import os
import re
from urllib.parse import urlparse

_PLACEHOLDER = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")


class SecretError(Exception):
    pass


def path():
    return os.environ.get("HUTCH_SECRETS") or os.path.expanduser("~/.hutch/secrets.json")


def _load():
    try:
        with open(path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(data):
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)


def set_secret(name, value, domains=None):
    if not re.fullmatch(r"[\w.-]+", name):
        raise SecretError(f"invalid secret name '{name}', use letters, digits, '-', '_' or '.'")
    data = _load()
    data[name] = {"value": value, "domains": sorted({d.lower().lstrip(".") for d in domains or []})}
    _save(data)


def remove(name):
    data = _load()
    if data.pop(name, None) is None:
        raise SecretError(f"no secret named '{name}'")
    _save(data)


def listing():
    return [{"name": k, "domains": v.get("domains", [])} for k, v in sorted(_load().items())]


def _allowed(domains, url):
    if not domains:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def resolve(text, url):
    """Replace {{name}} with secret values; refuses on a page outside the secret's domains."""
    if not isinstance(text, str) or "{{" not in text:
        return text
    data = _load()

    def sub(m):
        entry = data.get(m.group(1))
        if entry is None:
            raise SecretError(f"secret '{m.group(1)}' is not set, run: hutch secret set {m.group(1)}")
        if not _allowed(entry.get("domains"), url):
            host = urlparse(url).hostname or url
            raise SecretError(f"secret '{m.group(1)}' is not allowed on {host}")
        return entry["value"]
    return _PLACEHOLDER.sub(sub, text)


def placeholder_for(value):
    for name, entry in _load().items():
        if entry["value"] == value:
            return "{{" + name + "}}"
    return None


def redact(text):
    """Put placeholders back wherever a secret value appears in output."""
    if not isinstance(text, str):
        return text
    pairs = {entry["value"]: "{{" + name + "}}" for name, entry in _load().items()}
    return scrub(text, pairs)


def scrub(text, pairs):
    """Replace each value (raw, JSON-escaped or URL-encoded) with its placeholder."""
    from urllib.parse import quote, quote_plus
    for value, placeholder in sorted(pairs.items(), key=lambda kv: -len(kv[0])):
        if len(value) < 4:
            continue
        for form in {value, json.dumps(value)[1:-1], quote(value, safe=""), quote_plus(value)}:
            text = text.replace(form, placeholder)
    return text
