import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

_DEFAULT_ARTIFACTS_DIR = os.path.expanduser("~/.hutch/artifacts")


@dataclass
class ArtifactMeta:
    session: str
    type: str
    path: str
    timestamp: float
    detail: Optional[str] = None


class ArtifactStore:

    def __init__(self, base_dir=None):
        self.base_dir = base_dir or _DEFAULT_ARTIFACTS_DIR

    def _session_dir(self, session_name, subdir=None):
        parts = [self.base_dir, session_name]
        if subdir:
            parts.append(subdir)
        d = os.path.join(*parts)
        os.makedirs(d, exist_ok=True)
        return d

    def save_har(self, session_name, har_data, label=None):
        d = self._session_dir(session_name, "har")
        ts = int(time.time())
        suffix = f"-{label}" if label else ""
        path = os.path.join(d, f"{ts}{suffix}.har")
        with open(path, "w") as f:
            json.dump(har_data, f, indent=2)
        return ArtifactMeta(
            session=session_name, type="har", path=path, timestamp=ts,
        )

    def save_screenshot(self, session_name, png_bytes, label=None):
        d = self._session_dir(session_name, "screenshots")
        ts = int(time.time())
        suffix = f"-{label}" if label else ""
        path = os.path.join(d, f"{ts}{suffix}.png")
        with open(path, "wb") as f:
            f.write(png_bytes)
        return ArtifactMeta(
            session=session_name, type="screenshot", path=path, timestamp=ts,
        )

    def save_snapshot(self, session_name, snapshot_data, label=None):
        d = self._session_dir(session_name, "snapshots")
        ts = int(time.time())
        suffix = f"-{label}" if label else ""
        path = os.path.join(d, f"{ts}{suffix}.json")
        with open(path, "w") as f:
            json.dump(snapshot_data, f, indent=2, default=_serialize)
        return ArtifactMeta(
            session=session_name, type="snapshot", path=path, timestamp=ts,
        )

    def note(self, session_name, key, value):
        d = self._session_dir(session_name)
        path = os.path.join(d, "notes.json")
        notes = {}
        if os.path.exists(path):
            with open(path) as f:
                notes = json.load(f)
        notes[key] = {"value": value, "updated_at": time.time()}
        with open(path, "w") as f:
            json.dump(notes, f, indent=2)

    def notes(self, session_name):
        d = self._session_dir(session_name)
        path = os.path.join(d, "notes.json")
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            raw = json.load(f)
        return {k: v["value"] for k, v in raw.items()}

    def delete_note(self, session_name, key):
        d = self._session_dir(session_name)
        path = os.path.join(d, "notes.json")
        if not os.path.exists(path):
            return
        with open(path) as f:
            notes = json.load(f)
        notes.pop(key, None)
        with open(path, "w") as f:
            json.dump(notes, f, indent=2)

    def list_artifacts(self, session_name, type_filter=None):
        base = self._session_dir(session_name)
        results = []
        for subdir in ("har", "screenshots", "snapshots"):
            d = os.path.join(base, subdir)
            if not os.path.isdir(d):
                continue
            if type_filter and subdir.rstrip("s") != type_filter and subdir != type_filter:
                continue
            for fname in sorted(os.listdir(d)):
                path = os.path.join(d, fname)
                if os.path.isfile(path):
                    results.append(ArtifactMeta(
                        session=session_name,
                        type=subdir.rstrip("s") if subdir != "har" else "har",
                        path=path,
                        timestamp=os.path.getmtime(path),
                    ))
        return results

    def purge(self, session_name):
        import shutil
        d = os.path.join(self.base_dir, session_name)
        if os.path.isdir(d):
            shutil.rmtree(d)

    def sessions_with_artifacts(self):
        if not os.path.isdir(self.base_dir):
            return []
        return [
            name for name in sorted(os.listdir(self.base_dir))
            if os.path.isdir(os.path.join(self.base_dir, name))
        ]


def _serialize(obj):
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes>"
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(obj)
    raise TypeError(f"not serializable: {type(obj)}")
