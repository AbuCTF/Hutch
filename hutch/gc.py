"""Reclaim disk: old artifacts and downloads, stale browser state, and caches of stopped profiles."""

import os
import shutil
import time

from . import browser

CACHE_DIRS = {
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache",
    "GraphiteDawnCache", "ShaderCache", "component_crx_cache", "optimization_guide_model_store",
    "OptGuideOnDeviceModel", "Crashpad", "BrowserMetrics",
}
_STATE = ("DevToolsActivePort", "hutch_browser.json", "SingletonLock", "SingletonSocket", "SingletonCookie")


def _size(path):
    if os.path.islink(path) or os.path.isfile(path):
        return os.lstat(path).st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _old_files(root, cutoff):
    if not os.path.isdir(root):
        return
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                if os.lstat(p).st_mtime < cutoff:
                    yield p
            except OSError:
                pass


def plan(base_dir, artifacts_dir, *, days=30, caches=False):
    """[(kind, path, bytes)] that gc would remove; running browsers are never touched."""
    cutoff = time.time() - days * 86400
    out = [("artifacts", p, _size(p)) for p in _old_files(artifacts_dir, cutoff)
           if os.path.basename(p) != "notes.json"]
    if not os.path.isdir(base_dir):
        return out
    for name in sorted(os.listdir(base_dir)):
        profile = os.path.join(base_dir, name)
        if not os.path.isdir(profile):
            continue
        out += [("downloads", p, _size(p)) for p in _old_files(os.path.join(profile, "downloads"), cutoff)]
        if browser.status(profile):
            continue
        out += [("stale state", os.path.join(profile, f), 0) for f in _STATE
                if os.path.lexists(os.path.join(profile, f))]
        if caches:
            for parent in [profile] + [os.path.join(profile, d) for d in os.listdir(profile)
                                       if d == "Default" or d.startswith("Profile ")]:
                for d in sorted(CACHE_DIRS & set(os.listdir(parent) if os.path.isdir(parent) else ())):
                    p = os.path.join(parent, d)
                    out.append(("caches", p, _size(p)))
    return out


def remove(items):
    for _kind, path, _size_ in items:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError:
            pass
        parent = os.path.dirname(path)
        while parent and os.path.basename(parent) in ("har", "screenshots", "snapshots"):
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)


def run(base_dir, artifacts_dir, *, days=30, caches=False, dry_run=False):
    items = plan(base_dir, artifacts_dir, days=days, caches=caches)
    if not dry_run:
        remove(items)
    kinds = {}
    for kind, _path, size in items:
        n, b = kinds.get(kind, (0, 0))
        kinds[kind] = (n + 1, b + size)
    total = sum(b for _n, b in kinds.values())
    verb = "would free" if dry_run else "freed"
    print(f"hutch gc: {len(items)} items, {total / 1048576:.1f} MB {verb}")
    for kind, (n, b) in kinds.items():
        print(f"  {kind:<12} {n:>5}  {b / 1048576:8.1f} MB")
    return items
