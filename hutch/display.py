"""A virtual screen for headed browsers.

A headed browser asks the window manager for its size. Tiling managers (Hyprland, i3, sway) ignore
``--window-size`` and squeeze the window into a tile, so a page that looks at ``innerWidth``,
``outerWidth`` and ``screen`` sees a small window on a big screen, one of the signals bot checks
weigh. Headless chromium has its own tells, so the answer is neither: run the *headed* browser on a
private X display (Xvfb) that is exactly the size of a monitor and has no window manager. The browser
gets the whole screen, a real X11 window, real GPU-less rendering and nothing tiles it.

Sessions that a person watches keep ``display="real"``: the window is on their desktop.

Display numbers are chosen from a high range and checked against X's lock files and sockets by
hand. Xvfb's own ``-displayfd`` search starts at :0 and can replace a running server's socket, so it
is never used.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

SIZE = (1920, 1080)
FIRST, LAST = 90, 199
_SOCKETS = "/tmp/.X11-unix"
INSTALL = "install Xvfb: pacman -S xorg-server-xvfb | apt install xvfb | dnf install xorg-x11-server-Xvfb"


class DisplayError(RuntimeError):
    pass


def find_xvfb():
    """The Xvfb binary: HUTCH_XVFB, then PATH, then ~/.local/bin."""
    for cand in (os.environ.get("HUTCH_XVFB"), shutil.which("Xvfb"),
                 os.path.expanduser("~/.local/bin/Xvfb")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def available():
    return sys.platform.startswith("linux") and find_xvfb() is not None


def has_screen():
    """True when this process can open a window on a real display."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or sys.platform in ("win32", "darwin")


PANEL = 32  # height of the top bar a desktop reserves, which a window never covers


def _reserve_panel(display, size, panel=PANEL):
    """Tell the browser a top bar uses the first ``panel`` rows, as every desktop's does.

    ``screen.availHeight`` equal to ``screen.height`` means no taskbar or menu bar, a trait of
    headless browsers and bare X servers that pages check. Window managers publish the free area as
    the root window's ``_NET_WORKAREA`` property; with none running, this publishes it.
    Returns a callable that closes the connection it used: keep that open while the screen is in
    use, because Xvfb resets, and forgets the property, when its last client leaves.
    """
    import ctypes
    import ctypes.util
    lib = ctypes.util.find_library("X11")
    if not lib:
        return lambda: None
    x = ctypes.CDLL(lib)
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    dpy = x.XOpenDisplay(display.encode())
    if not dpy:
        return lambda: None
    area = (ctypes.c_long * 4)(0, panel, size[0], size[1] - panel)
    x.XChangeProperty(dpy, x.XDefaultRootWindow(dpy), x.XInternAtom(dpy, b"_NET_WORKAREA", 0),
                      6, 32, 0, area, 4)  # XA_CARDINAL, 32-bit items, PropModeReplace
    x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XSync(dpy, 0)
    return lambda: x.XCloseDisplay(dpy)


def work_area(size=SIZE, panel=PANEL):
    """(x, y, width, height) of a maximised window on a screen with the top bar from _reserve_panel."""
    return (0, panel, size[0], size[1] - panel)


def _in_use(n):
    # any trace of a server on :n, live or stale, rules the number out
    return any(os.path.lexists(p) for p in (
        f"/tmp/.X{n}-lock", f"{_SOCKETS}/X{n}", f"{_SOCKETS}/X{n}_"))


class VirtualDisplay:
    """One Xvfb server. ``-terminate`` makes it exit when the last client leaves, so a browser that
    outlives its launcher takes its screen with it."""

    def __init__(self, size=SIZE, depth=24):
        self.size = tuple(size)
        self.depth = depth
        self.display = None
        self.pid = None
        self._proc = None
        self._release = None

    def apply(self, env):
        """``env`` pointed at this screen and away from Wayland, so the browser opens an X11 window here."""
        out = {k: v for k, v in env.items() if k != "WAYLAND_DISPLAY"}
        out["DISPLAY"] = self.display
        return out

    def start(self, timeout=10.0):
        binary = find_xvfb()
        if not binary:
            raise DisplayError(f"Xvfb not found, {INSTALL}")
        if self._proc and self._proc.poll() is None:
            return self
        last = ""
        for n in range(FIRST, LAST + 1):
            if _in_use(n):
                continue
            w, h = self.size
            log = tempfile.TemporaryFile()
            proc = subprocess.Popen(
                [binary, f":{n}", "-screen", "0", f"{w}x{h}x{self.depth}", "-nolisten", "tcp",
                 "-noreset", "-terminate", "-dpi", "96"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                start_new_session=True)
            deadline = time.time() + timeout
            while time.time() < deadline:
                if os.path.exists(f"{_SOCKETS}/X{n}") and os.path.exists(f"/tmp/.X{n}-lock"):
                    self._proc, self.pid, self.display = proc, proc.pid, f":{n}"
                    log.close()
                    self._release = _reserve_panel(self.display, self.size)
                    return self
                if proc.poll() is not None:
                    log.seek(0)
                    last = log.read().decode(errors="replace")[-300:]
                    log.close()
                    break  # raced another starter for this number; try the next
                time.sleep(0.02)
            else:
                proc.kill()
                log.close()
                raise DisplayError(f"Xvfb :{n} did not come up within {timeout:.0f}s")
        raise DisplayError(f"no free X display in :{FIRST}-:{LAST}. {last}".strip())

    def stop(self):
        release, self._release = self._release, None
        if release:
            release()
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.display = self.pid = None

    @staticmethod
    def stop_pid(pid):
        """Stop an Xvfb a previous process started, if that pid is still an Xvfb."""
        try:
            with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
                if not os.path.basename(f.read().split(b"\0")[0]).startswith(b"Xvfb"):
                    return
            os.kill(int(pid), 15)
        except (OSError, ValueError):
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
