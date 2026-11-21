"""Per-program warm-profile registry — which aged, coherent, proxy-pinned profile serves which program.

Returning the same trusted identity to the same target over the same exit IP is what keeps a warmed
profile from being re-challenged, so Hutch records that binding here. SQLite at ~/.hutch/registry.sqlite,
purely additive over the profile dirs in ~/.hutch/profiles: it changes nothing on disk, it only tracks
each profile's program binding, warmth state and challenge counters. No autonomous CAPTCHA solving lives
here or anywhere in Hutch — a profile that still meets an image grid is handed to a person.
"""

import hashlib
import json
import os
import sqlite3
import threading
import time

from . import warm

_DEFAULT_DB = os.environ.get("HUTCH_REGISTRY_DB") or os.path.expanduser("~/.hutch/registry.sqlite")
_PROFILES_DIR = os.environ.get("HUTCH_BASE_DIR") or os.path.expanduser("~/.hutch/profiles")
_META_FILE = "hutch_meta.json"

STATES = ("fresh", "warming", "aging", "ready", "assigned", "cooling", "retired")
ROLES = ("a", "b", "anon")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_assignments (
    profile_name     TEXT PRIMARY KEY,
    program_id       TEXT,
    target_host      TEXT,
    role             TEXT,
    proxy_id         TEXT,
    fingerprint_hash TEXT,
    state            TEXT NOT NULL DEFAULT 'fresh',
    created_at       INTEGER,
    assigned_at      INTEGER,
    last_used_at     INTEGER,
    sessions_count   INTEGER NOT NULL DEFAULT 0,
    challenge_count  INTEGER NOT NULL DEFAULT 0,
    note             TEXT
);
CREATE INDEX IF NOT EXISTS idx_pa_program_role ON profile_assignments(program_id, role);
CREATE INDEX IF NOT EXISTS idx_pa_state ON profile_assignments(state);
"""


def fingerprint_hash(fp):
    """A stable short hash over the coherence-identifying fingerprint fields; the identity's 'story'."""
    if not isinstance(fp, dict):
        return None
    key = "|".join(str(fp.get(k)) for k in
                   ("platform", "user_agent", "locale", "timezone", "screen_width", "screen_height"))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _state(v):
    v = v or "fresh"
    if v not in STATES:
        raise ValueError(f"unknown state {v!r}, expected one of {', '.join(STATES)}")
    return v


def _role(v):
    if v is None:
        return None
    if v not in ROLES:
        raise ValueError(f"unknown role {v!r}, expected one of {', '.join(ROLES)}")
    return v


class Registry:

    def __init__(self, path=None):
        self.path = path or _DEFAULT_DB
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def register(self, profile_name, *, program_id=None, target_host=None, role=None, proxy_id=None,
                 fingerprint_hash=None, state="fresh", created_at=None, note=None):
        """Upsert a profile's binding. Counters (sessions/challenge) are preserved across re-register."""
        now = int(time.time())
        params = {"profile_name": profile_name, "program_id": program_id, "target_host": target_host,
                  "role": _role(role), "proxy_id": proxy_id, "fingerprint_hash": fingerprint_hash,
                  "state": _state(state), "created_at": int(created_at or now), "note": note}
        with self._lock:
            self._db.execute(
                """INSERT INTO profile_assignments
                     (profile_name, program_id, target_host, role, proxy_id, fingerprint_hash,
                      state, created_at, note)
                   VALUES (:profile_name, :program_id, :target_host, :role, :proxy_id, :fingerprint_hash,
                           :state, :created_at, :note)
                   ON CONFLICT(profile_name) DO UPDATE SET
                     program_id=excluded.program_id, target_host=excluded.target_host, role=excluded.role,
                     proxy_id=excluded.proxy_id, fingerprint_hash=excluded.fingerprint_hash,
                     state=excluded.state, note=excluded.note""", params)
            self._db.commit()
        return self.get(profile_name)

    def get(self, profile_name):
        with self._lock:
            row = self._db.execute("SELECT * FROM profile_assignments WHERE profile_name=?",
                                   (profile_name,)).fetchone()
        return dict(row) if row else None

    def ls(self, program_id=None, state=None):
        query, where, args = "SELECT * FROM profile_assignments", [], []
        if program_id is not None:
            where.append("program_id=?")
            args.append(program_id)
        if state is not None:
            where.append("state=?")
            args.append(_state(state))
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY program_id IS NULL, program_id, role, profile_name"
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def claim(self, program_id, role="a"):
        """Return the SAME ready/assigned profile bound to (program_id, role), re-marking it assigned.

        None when no warmed identity is standing by — the caller then births and warms a fresh one.
        """
        role = _role(role) or "a"
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                """SELECT profile_name FROM profile_assignments
                   WHERE program_id=? AND role=? AND state IN ('ready', 'assigned')
                   ORDER BY (last_used_at IS NULL) DESC, last_used_at ASC LIMIT 1""",
                (program_id, role)).fetchone()
            if not row:
                return None
            self._db.execute(
                "UPDATE profile_assignments SET state='assigned', assigned_at=COALESCE(assigned_at,?), "
                "last_used_at=? WHERE profile_name=?", (now, now, row["profile_name"]))
            self._db.commit()
            return self.get(row["profile_name"])

    def assign(self, profile_name, program_id, role="a", *, target_host=None):
        """Bind a profile to (program, role). Max one live profile per role — a conflict raises."""
        role = _role(role) or "a"
        now = int(time.time())
        with self._lock:
            for r in self.ls(program_id=program_id):
                if r["role"] == role and r["state"] != "retired" and r["profile_name"] != profile_name:
                    raise ValueError(
                        f"program {program_id!r} role {role!r} already held by {r['profile_name']!r}")
            if not self.get(profile_name):
                self.register(profile_name, program_id=program_id, role=role, target_host=target_host,
                              state="assigned")
            self._db.execute(
                "UPDATE profile_assignments SET program_id=?, role=?, "
                "target_host=COALESCE(?, target_host), state='assigned', "
                "assigned_at=COALESCE(assigned_at,?), last_used_at=? WHERE profile_name=?",
                (program_id, role, target_host, now, now, profile_name))
            self._db.commit()
        return self.get(profile_name)

    def record_use(self, profile_name, *, challenged=False, advance=True):
        """One session's outcome — bumps the usage count and, if a challenge showed, the challenge count,
        then (by default) re-evaluates the profile's lifecycle state against the warm policy."""
        now = int(time.time())
        with self._lock:
            self._db.execute(
                "UPDATE profile_assignments SET sessions_count=sessions_count+1, last_used_at=?, "
                "challenge_count=challenge_count+? WHERE profile_name=?",
                (now, 1 if challenged else 0, profile_name))
            self._db.commit()
        return self.advance(profile_name, now) if advance else self.get(profile_name)

    def advance(self, profile_name, now=None):
        """Re-evaluate one profile's state against the warm policy; persist and return it if it moved."""
        now = int(now if now is not None else time.time())
        with self._lock:
            row = self.get(profile_name)
            if not row:
                return None
            target = warm.next_state(row, now)
            if target != row["state"]:
                self._db.execute("UPDATE profile_assignments SET state=? WHERE profile_name=?",
                                 (target, profile_name))
                self._db.commit()
        return self.get(profile_name)

    def advance_all(self, now=None, program_id=None):
        """Sweep every profile (optionally one program) through the policy; return each one that moved."""
        now = int(now if now is not None else time.time())
        moved = []
        for r in self.ls(program_id=program_id):
            target = warm.next_state(r, now)
            if target != r["state"]:
                self.set_state(r["profile_name"], target)
                moved.append({"profile_name": r["profile_name"], "from": r["state"], "to": target})
        return moved

    def set_state(self, profile_name, state):
        with self._lock:
            self._db.execute("UPDATE profile_assignments SET state=? WHERE profile_name=?",
                             (_state(state), profile_name))
            self._db.commit()
        return self.get(profile_name)

    def release(self, profile_name):
        return self.set_state(profile_name, "cooling")

    def retire(self, profile_name, note=None):
        with self._lock:
            self._db.execute("UPDATE profile_assignments SET state='retired', note=COALESCE(?, note) "
                             "WHERE profile_name=?", (note, profile_name))
            self._db.commit()
        return self.get(profile_name)

    def import_profiles(self, base_dir=None):
        """Seed the registry from existing profile dirs. Proxy-null profiles are quarantined (retired):
        they cannot meet the IP-pin / coherence rule, so they must not be reused for low-challenge work."""
        base = base_dir or _PROFILES_DIR
        summary = {"imported": 0, "quarantined": 0, "skipped": 0}
        if not os.path.isdir(base):
            return summary
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name, _META_FILE)
            try:
                with open(path) as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                summary["skipped"] += 1
                continue
            if not isinstance(meta, dict):
                summary["skipped"] += 1
                continue
            proxy = meta.get("proxy")
            proxy_id = proxy.get("server") if isinstance(proxy, dict) else None
            tags = meta.get("tags")
            program = tags.get("program") if isinstance(tags, dict) else None
            if proxy_id:
                state, note = "aging", "imported"
                summary["imported"] += 1
            else:
                state, note = "retired", "legacy: no pinned proxy — quarantined, do not reuse for warm work"
                summary["quarantined"] += 1
            self.register(name, program_id=program, proxy_id=proxy_id,
                          fingerprint_hash=fingerprint_hash(meta.get("fingerprint")),
                          state=state, created_at=meta.get("created_at"), note=note)
        return summary
