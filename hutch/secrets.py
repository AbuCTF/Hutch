"""Secrets agents can type but never read: {{name}} placeholders, bound to domains, redacted from output."""

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time
from urllib.parse import parse_qs, unquote, urlparse

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


def set_totp(name, secret, domains=None):
    """Store an authenticator secret: base32, or an otpauth://totp/ URI from the QR code."""
    params = {"digits": 6, "period": 30, "algorithm": "SHA1"}
    if secret.startswith("otpauth://"):
        u = urlparse(secret)
        if u.netloc != "totp":
            raise SecretError("only otpauth://totp/ is supported")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        secret = q.get("secret", "")
        params.update({"digits": int(q.get("digits", 6)), "period": int(q.get("period", 30)),
                       "algorithm": q.get("algorithm", "SHA1").upper(), "label": unquote(u.path.lstrip("/"))})
    secret = secret.replace(" ", "").upper()
    try:
        _b32(secret)
    except ValueError:
        raise SecretError("not a base32 secret or otpauth:// uri") from None
    if params["algorithm"] not in ("SHA1", "SHA256", "SHA512"):
        raise SecretError(f"unsupported algorithm {params['algorithm']}")
    set_secret(name, secret, domains)
    data = _load()
    data[name].update(kind="totp", **params)
    _save(data)


def _b32(secret):
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def totp(entry, at=None):
    """The current code for a totp entry (RFC 6238)."""
    period = entry.get("period", 30)
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(_b32(entry["value"]), struct.pack(">Q", counter),
                      getattr(hashlib, entry.get("algorithm", "SHA1").lower())).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** entry.get("digits", 6)).zfill(entry.get("digits", 6))


def code(name):
    """(code, seconds left) for a totp secret."""
    entry = _load().get(name)
    if not entry or entry.get("kind") != "totp":
        raise SecretError(f"no totp secret named '{name}'")
    period = entry.get("period", 30)
    return totp(entry), period - int(time.time()) % period


def _value(entry):
    if entry.get("kind") != "totp":
        return entry["value"]
    period = entry.get("period", 30)
    left = period - time.time() % period
    if left < 2:
        # a code about to roll over would be stale by the time the form is submitted
        time.sleep(left + 0.1)
    return totp(entry)


def remove(name):
    data = _load()
    if data.pop(name, None) is None:
        raise SecretError(f"no secret named '{name}'")
    _save(data)


def listing():
    return [{"name": k, "domains": v.get("domains", []), "kind": v.get("kind", "secret")}
            for k, v in sorted(_load().items())]


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
        return _value(entry)
    return _PLACEHOLDER.sub(sub, text)


def placeholder_for(value):
    """The placeholder for a typed value: a stored secret, or the current code of a totp secret."""
    now = time.time()
    for name, entry in _load().items():
        if entry.get("kind") == "totp":
            period = entry.get("period", 30)
            if value and value in (totp(entry, now), totp(entry, now - period)):
                return "{{" + name + "}}"
        elif entry["value"] == value:
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
