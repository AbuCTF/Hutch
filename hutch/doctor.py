import os
import shutil
import subprocess
import sys

from . import browser

_OK, _WARN, _FAIL = "ok", "warn", "FAIL"


def _line(level, label, detail=""):
    print(f"  [{level:<4}] {label}{(' — ' + detail) if detail else ''}")


def _du_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total // (1024 * 1024)


def _wsl_networking():
    if shutil.which("wslinfo"):
        try:
            out = subprocess.run(["wslinfo", "--networking-mode"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            if out:
                return out
        except Exception:
            pass
    return "nat?"


def run(base_dir):
    failed = False
    plat = browser.platform_summary()
    print(f"hutch doctor — {plat['os']} {plat['machine']}{' (WSL2)' if plat['wsl'] else ''}")

    try:
        import importlib.metadata as md
        _line(_OK, "playwright", md.version("playwright"))
    except Exception:
        _line(_FAIL, "playwright", "not installed — pip install -e .")
        return 1

    exe = None
    try:
        exe = browser.find_executable("chromium")
    except browser.BrowserError as e:
        _line(_FAIL, "bundled chromium", str(e))
        failed = True
    if exe:
        version = browser.browser_version(exe)
        if version:
            _line(_OK, "bundled chromium", version)
        else:
            failed = True
            missing = []
            if sys.platform.startswith("linux") and shutil.which("ldd"):
                out = subprocess.run(["ldd", exe], capture_output=True, text=True).stdout
                missing = [l.split()[0] for l in out.splitlines() if "not found" in l]
            hint = f"missing {', '.join(missing[:4])}{'…' if len(missing) > 4 else ''}; " if missing else ""
            pw = os.path.join(os.path.dirname(sys.executable), "playwright")
            _line(_FAIL, "bundled chromium does not start", f"{hint}run: sudo {pw} install-deps chromium")

    if sys.platform.startswith("linux"):
        fonts = 0
        if shutil.which("fc-list"):
            fonts = len(subprocess.run(["fc-list"], capture_output=True, text=True).stdout.splitlines())
        if fonts:
            _line(_OK, "fonts", f"{fonts} installed")
        else:
            _line(_WARN, "fonts", "none found — pages render blank or crash; install-deps adds them")

    if plat["display"]:
        _line(_OK, "display", "headed windows available"
              + (" via WSLg" if plat["wsl"] and os.path.isdir("/mnt/wslg") else ""))
    else:
        _line(_WARN, "display", "no DISPLAY/WAYLAND_DISPLAY — only headless works")

    for name in ("chrome", "brave", "edge"):
        try:
            path = browser.find_executable(name)
            _line(_OK, f"{name}", path)
        except browser.BrowserError:
            _line("--", f"{name}", "not installed here (optional)")

    for name in ("chrome", "brave", "edge"):
        try:
            root = browser.default_user_data_dir(name)
        except browser.BrowserError:
            continue
        if not os.path.isdir(root):
            continue
        ep = browser.read_endpoint(root)
        if ep:
            _line(_OK, f"attach {name}", f"accepting agents on port {ep[0]}")
        elif os.path.exists(os.path.join(root, "DevToolsActivePort")):
            _line(_WARN, f"attach {name}", "remote debugging on but unreachable — " + browser.attach_hint())
        else:
            _line("--", f"attach {name}", f"profile at {root}; enable {name}://inspect/#remote-debugging to attach")

    if plat["wsl"]:
        mode = _wsl_networking()
        level = _OK if mode == "mirrored" else "--"
        _line(level, "wsl networking", f"{mode}" + ("" if mode == "mirrored" else
              " — fine for browsers inside WSL; mirrored needed to attach to Windows browsers "
              "or reach a Windows-side proxy on 127.0.0.1"))

    if os.path.isdir(base_dir):
        names = [n for n in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, n))]
        running = [n for n in names if browser.status(os.path.join(base_dir, n))]
        _line(_OK, "profiles", f"{len(names)} in {base_dir} ({_du_mb(base_dir)} MB), {len(running)} running")
    else:
        _line("--", "profiles", f"none yet ({base_dir})")

    print("\nall good" if not failed else "\nfix the FAIL items above")
    return 1 if failed else 0
