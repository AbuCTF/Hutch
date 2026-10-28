import asyncio
import contextlib
import glob
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

_STATE_FILE = "hutch_browser.json"
_LOG_FILE = "hutch_browser.log"
_PORT_FILE = "DevToolsActivePort"

# same values the playwright launcher uses, so a profile decrypts its cookies under both
_COMPAT_ARGS = ["--password-store=basic", "--use-mock-keychain"]

_CLONE_SKIP = {
    "SingletonLock", "SingletonSocket", "SingletonCookie", _PORT_FILE, _STATE_FILE, _LOG_FILE,
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache",
    "GraphiteDawnCache", "ShaderCache", "Crashpad", "BrowserMetrics", "component_crx_cache",
    "optimization_guide_model_store", "trace.zip",
}


class BrowserError(RuntimeError):
    pass


def is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _playwright_chromium():
    try:
        import playwright
        pkg = os.path.join(os.path.dirname(playwright.__file__), "driver", "package", "browsers.json")
        with open(pkg) as f:
            rev = next(b["revision"] for b in json.load(f)["browsers"] if b["name"] == "chromium")
    except Exception:
        return None
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or {
        "linux": "~/.cache/ms-playwright",
        "darwin": "~/Library/Caches/ms-playwright",
        "win32": os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
    }.get(sys.platform, "~/.cache/ms-playwright")
    base = os.path.join(os.path.expanduser(root), f"chromium-{rev}")
    for rel in ("chrome-linux64/chrome", "chrome-linux/chrome",
                "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                "chrome-win64/chrome.exe", "chrome-win/chrome.exe"):
        path = os.path.join(base, rel)
        if os.path.exists(path):
            return path
    return None


_CANDIDATES = {
    "chrome": {
        "linux": ["google-chrome", "google-chrome-stable"],
        "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "win32": [r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
                  r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
    },
    "brave": {
        "linux": ["brave-browser", "brave"],
        "darwin": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
        "win32": [r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
                  r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"],
    },
    "edge": {
        "linux": ["microsoft-edge", "microsoft-edge-stable"],
        "darwin": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "win32": [r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"],
    },
}

_USER_DATA = {
    "chrome": {"linux": "~/.config/google-chrome",
               "darwin": "~/Library/Application Support/Google/Chrome",
               "win32": r"%LOCALAPPDATA%\Google\Chrome\User Data"},
    "brave": {"linux": "~/.config/BraveSoftware/Brave-Browser",
              "darwin": "~/Library/Application Support/BraveSoftware/Brave-Browser",
              "win32": r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"},
    "edge": {"linux": "~/.config/microsoft-edge",
             "darwin": "~/Library/Application Support/Microsoft Edge",
             "win32": r"%LOCALAPPDATA%\Microsoft\Edge\User Data"},
}


def find_executable(browser="chromium"):
    """Resolve chromium|chrome|brave|edge or an explicit path to an executable."""
    browser = browser or "chromium"
    if os.sep in browser or browser.endswith(".exe"):
        if os.path.exists(browser):
            return browser
        raise BrowserError(f"browser executable not found: {browser}")
    if browser == "chromium":
        path = _playwright_chromium()
        if path:
            return path
        raise BrowserError("bundled chromium missing, run: playwright install chromium")
    if browser not in _CANDIDATES:
        raise BrowserError(f"unknown browser '{browser}', use chromium, chrome, brave, edge or a path")
    plat = "win32" if sys.platform == "win32" else sys.platform
    for cand in _CANDIDATES[browser].get(plat, []):
        cand = os.path.expandvars(cand)
        found = cand if os.path.isabs(cand) else shutil.which(cand)
        if found and os.path.exists(found):
            return found
    hint = " (the Windows install is not usable from inside WSL; install the Linux build or use 'hutch attach')" \
        if is_wsl() else ""
    raise BrowserError(f"{browser} not found{hint}")


def default_user_data_dir(browser):
    """The browser's everyday profile root, including the Windows side when running under WSL."""
    if browser not in _USER_DATA:
        raise BrowserError(f"unknown browser '{browser}'")
    if is_wsl():
        for home in glob.glob("/mnt/c/Users/*/AppData/Local"):
            win = _USER_DATA[browser]["win32"].replace("%LOCALAPPDATA%", home).replace("\\", "/")
            if os.path.isdir(win):
                return win
    plat = "win32" if sys.platform == "win32" else sys.platform
    return os.path.expanduser(os.path.expandvars(_USER_DATA[browser][plat]))


def browser_version(executable):
    try:
        out = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for tok in out.split():
        if tok[:1].isdigit() and "." in tok:
            return tok
    return None


def real_user_agent(version):
    major = (version or "0").split(".")[0]
    if sys.platform == "darwin":
        osinfo = "Macintosh; Intel Mac OS X 10_15_7"
    elif sys.platform == "win32":
        osinfo = "Windows NT 10.0; Win64; x64"
    else:
        osinfo = "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({osinfo}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


def _http_json(port, path, timeout=1.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def read_endpoint(user_data_dir):
    """Return (port, ws_url) from DevToolsActivePort if that browser answers, else None."""
    try:
        with open(os.path.join(user_data_dir, _PORT_FILE)) as f:
            lines = f.read().split()
        port, path = int(lines[0]), lines[1]
    except (OSError, ValueError, IndexError):
        return None
    ws = f"ws://127.0.0.1:{port}{path}"
    try:
        info = _http_json(port, "/json/version", timeout=0.7)
    except Exception:
        # chrome 144+ consent-mode attach serves no /json endpoints; trust the file
        return (port, ws) if _port_open(port) else None
    if not info.get("webSocketDebuggerUrl", "").endswith(path):
        return None
    return port, ws


def _port_open(port):
    import socket
    with contextlib.closing(socket.socket()) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pid_alive(pid):
    if not pid:
        return False
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                with contextlib.suppress(ChildProcessError, OSError):
                    os.waitpid(pid, os.WNOHANG)
                return False
    except (OSError, IndexError):
        pass
    return True


def lock_pid(user_data_dir):
    """Pid holding chromium's profile lock (SingletonLock -> host-pid), if alive."""
    try:
        target = os.readlink(os.path.join(user_data_dir, "SingletonLock"))
        pid = int(target.rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return None
    return pid if _pid_alive(pid) else None


def read_state(user_data_dir):
    try:
        with open(os.path.join(user_data_dir, _STATE_FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def status(user_data_dir):
    """Describe the browser currently holding this profile, if any."""
    ep = read_endpoint(user_data_dir)
    state = read_state(user_data_dir)
    pid = lock_pid(user_data_dir) or (state.get("pid") if _pid_alive(state.get("pid")) else None)
    if not ep and not pid:
        return None
    return {
        "pid": pid,
        "port": ep[0] if ep else None,
        "ws": ep[1] if ep else None,
        "headless": state.get("headless") if state.get("pid") == pid else None,
        "browser": state.get("browser"),
        "agent_access": bool(ep),
    }


def build_args(user_data_dir, *, headless=False, debug=True, restore=True, url=None,
               proxy=None, proxy_bypass=None, ignore_https_errors=False, window_size=None,
               lang=None, user_agent=None, window_class=None, maximize=True, extra=()):
    args = [
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        *_COMPAT_ARGS,
    ]
    if debug:
        args += ["--remote-debugging-port=0", "--disable-blink-features=AutomationControlled"]
    if restore:
        args.append("--restore-last-session")
    if headless:
        args += ["--headless=new", "--hide-scrollbars", "--mute-audio"]
    else:
        if window_class:
            args.append(f"--class={window_class}")
        if maximize:
            args.append("--start-maximized")
    if window_size:
        args.append(f"--window-size={window_size[0]},{window_size[1]}")
    if proxy:
        args.append(f"--proxy-server={proxy}")
    if proxy_bypass:
        args.append(f"--proxy-bypass-list={proxy_bypass}")
    if ignore_https_errors:
        args.append("--ignore-certificate-errors")
    if lang:
        args += [f"--lang={lang}", f"--accept-lang={lang}"]
    if user_agent:
        args.append(f"--user-agent={user_agent}")
    args += list(extra)
    if url:
        args.append(url)
    return args


def _pdeathsig():
    # owned headless browsers die with their owner instead of idling in the background
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


def spawn(user_data_dir, *, browser="chromium", headless=False, debug=True, tie_to_parent=False,
          env=None, timeout=20.0, **kw):
    """Start a detached browser on the profile. Returns status() once it is reachable.

    If the profile is already open, chromium hands the request to the running instance
    (new window / url) and the existing browser is returned instead.
    """
    os.makedirs(user_data_dir, exist_ok=True)
    running = status(user_data_dir)
    if running:
        return running
    executable = find_executable(browser)
    if not kw.get("user_agent") and headless:
        kw["user_agent"] = real_user_agent(browser_version(executable))
    with contextlib.suppress(FileNotFoundError):
        os.unlink(os.path.join(user_data_dir, _PORT_FILE))
    if not kw.get("url") and not os.path.isdir(os.path.join(user_data_dir, "Default", "Sessions")):
        # first launch: blank tab instead of the new-tab page and its google fetches
        kw["url"] = "about:blank"
    args = [executable] + build_args(user_data_dir, headless=headless, debug=debug, **kw)
    log = open(os.path.join(user_data_dir, _LOG_FILE), "wb")
    popen = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL,
             "env": {**os.environ, **(env or {})}}
    if sys.platform == "win32":
        popen["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        popen["start_new_session"] = True
        if tie_to_parent and sys.platform.startswith("linux"):
            popen["preexec_fn"] = _pdeathsig
    proc = subprocess.Popen(args, **popen)
    log.close()
    with open(os.path.join(user_data_dir, _STATE_FILE), "w") as f:
        json.dump({"pid": proc.pid, "headless": headless, "browser": browser,
                   "executable": executable, "debug": debug, "started": time.time()}, f)
    if not debug:
        return {"pid": proc.pid, "port": None, "ws": None, "headless": headless,
                "browser": browser, "agent_access": False, "process": proc}
    deadline = time.time() + timeout
    while time.time() < deadline:
        ep = read_endpoint(user_data_dir)
        if ep:
            _settle(ep[0])
            return {"pid": proc.pid, "port": ep[0], "ws": ep[1], "headless": headless,
                    "browser": browser, "agent_access": True, "process": proc}
        if proc.poll() is not None:
            other = status(user_data_dir)
            if other:
                return other
            raise BrowserError(f"browser exited with code {proc.returncode}, see "
                               f"{os.path.join(user_data_dir, _LOG_FILE)}")
        time.sleep(0.05)
    proc.terminate()
    raise BrowserError(f"browser did not expose a debugging port within {timeout:.0f}s")


def _settle(port, timeout=3.0):
    """Wait for session-restore to finish creating tabs; attaching earlier misses them."""
    last, stable, deadline = None, 0, time.time() + timeout
    while time.time() < deadline:
        try:
            count = sum(1 for t in _http_json(port, "/json/list") if t.get("type") == "page")
        except Exception:
            return
        stable = stable + 1 if count == last and count > 0 else 0
        if stable >= 3:
            return
        last = count
        time.sleep(0.1)


async def cdp_call(ws_url, method, params=None, timeout=5.0):
    return await asyncio.wait_for(_cdp_call(ws_url, method, params), timeout)


async def _cdp_call(ws_url, method, params):
    import aiohttp
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(ws_url, max_msg_size=0) as ws:
            await ws.send_json({"id": 1, "method": method, "params": params or {}})
            async for msg in ws:
                data = json.loads(msg.data)
                if data.get("id") == 1:
                    if "error" in data:
                        raise BrowserError(data["error"].get("message", "cdp error"))
                    return data.get("result", {})
    return {}


async def wait_exit(user_data_dir, pid=None, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = _pid_alive(pid) if pid else bool(status(user_data_dir))
        if not alive:
            return True
        await asyncio.sleep(0.05)
    return False


async def stop(user_data_dir, *, timeout=15.0):
    """Close gracefully (session saved for restore); kill only if it will not exit."""
    st = status(user_data_dir)
    if not st:
        return False
    if st["ws"]:
        with contextlib.suppress(Exception):
            await cdp_call(st["ws"], "Browser.close")
        if await wait_exit(user_data_dir, st["pid"], timeout):
            return True
    if st["pid"]:
        with contextlib.suppress(Exception):
            os.kill(st["pid"], signal.SIGTERM)
        if await wait_exit(user_data_dir, st["pid"], 5):
            return True
        with contextlib.suppress(Exception):
            os.kill(st["pid"], signal.SIGKILL)
    return True


def memory_mb(pid):
    """Proportional memory of a browser's whole process tree (linux), else None."""
    if not pid or not os.path.isdir("/proc"):
        return None
    children = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(d))
    total, stack = 0, [pid]
    while stack:
        p = stack.pop()
        stack.extend(children.get(p, []))
        try:
            with open(f"/proc/{p}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        total += int(line.split()[1])
                        break
        except OSError:
            continue
    return round(total / 1024)


def clone_profile(src, dst):
    """Copy a profile (logins, storage, settings) without caches or lock files."""
    if status(src):
        raise BrowserError("source profile is open, close it first")
    if os.path.exists(dst):
        raise BrowserError(f"destination exists: {dst}")

    def ignore(_dir, names):
        return [n for n in names if n in _CLONE_SKIP]

    shutil.copytree(src, dst, ignore=ignore, symlinks=True)
    for name in os.listdir(dst):
        if name.startswith("Singleton"):
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(dst, name))


def attach_hint():
    if is_wsl():
        return ("WSL needs mirrored networking: set networkingMode=mirrored under [wsl2] in "
                "%USERPROFILE%\\.wslconfig, then run wsl --shutdown")
    return ""


def open_url(url):
    """Open a URL in the user's desktop browser; on WSL that is the Windows default."""
    if is_wsl() and shutil.which("explorer.exe"):
        subprocess.Popen(["explorer.exe", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    import webbrowser
    return webbrowser.open(url)


def platform_summary():
    return {"os": sys.platform, "machine": platform.machine(), "wsl": is_wsl(),
            "display": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            or sys.platform in ("win32", "darwin")}


def resolve_endpoint(target):
    """chrome|brave|edge (their everyday profile), a port, http://host:port or ws://... -> ws url."""
    target = str(target)
    if target in _USER_DATA:
        root = default_user_data_dir(target)
        ep = read_endpoint(root)
        if ep:
            return ep[1]
        hint = attach_hint()
        raise BrowserError(
            f"{target} has remote debugging off, enable it at {target}://inspect/#remote-debugging"
            + (f". {hint}" if hint else ""))
    if target.startswith("ws://") or target.startswith("wss://"):
        return target
    if target.isdigit():
        target = f"http://127.0.0.1:{target}"
    try:
        with urllib.request.urlopen(target.rstrip("/") + "/json/version", timeout=2) as r:
            return json.loads(r.read())["webSocketDebuggerUrl"]
    except Exception as exc:
        raise BrowserError(f"no CDP endpoint at {target}: {exc}") from None
