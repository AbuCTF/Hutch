import os
import subprocess

import pytest

from hutch import browser, display
from hutch.session import Session

needs_xvfb = pytest.mark.skipif(not display.available(), reason="Xvfb is not installed")


def test_numbers_below_the_range_are_never_used(monkeypatch):
    seen = []

    def fake_popen(argv, **kw):
        seen.append(argv[1])
        return type("P", (), {"poll": lambda s: 1, "pid": 1})()

    monkeypatch.setattr(display, "find_xvfb", lambda: "/usr/bin/Xvfb")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(display, "_in_use", lambda n: False)
    monkeypatch.setattr(display.os.path, "exists", lambda p: False)
    with pytest.raises(display.DisplayError):
        display.VirtualDisplay().start(timeout=0.2)
    assert seen[0] == f":{display.FIRST}" and all(int(s[1:]) >= display.FIRST for s in seen)


def test_a_display_with_any_lock_or_socket_is_in_use(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "lexists", lambda p: p.endswith("X91_"))
    assert display._in_use(91) and not display._in_use(92)


def test_missing_xvfb_says_how_to_install(monkeypatch):
    monkeypatch.setattr(display, "find_xvfb", lambda: None)
    with pytest.raises(display.DisplayError, match="install Xvfb"):
        display.VirtualDisplay().start()


@needs_xvfb
def test_xvfb_starts_at_the_size_asked_and_stops():
    with display.VirtualDisplay((1280, 720)) as vd:
        assert vd.display and int(vd.display[1:]) >= display.FIRST
        assert os.path.exists(f"/tmp/.X11-unix/X{vd.display[1:]}")
        env = vd.apply({"WAYLAND_DISPLAY": "wayland-1", "PATH": "/bin"})
        assert env["DISPLAY"] == vd.display and "WAYLAND_DISPLAY" not in env
        pid = vd.pid
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split(")")[1].split()[0] == "Z"


def test_virtual_screen_args_size_the_window_to_the_work_area():
    args = browser.build_args("/p", virtual=(1920, 1080), window_class="hutch-browser-x")
    assert "--window-size=1920,1048" in args and "--window-position=0,32" in args
    assert "--ozone-platform=x11" in args and "--class=hutch-browser-x" in args
    assert "--start-maximized" not in args and "--headless=new" not in args
    assert any(a.startswith("--use-angle=") for a in args)


def test_no_launch_carries_an_automation_tell():
    for headless in (False, True):
        args = browser.build_args("/p", headless=headless)
        assert not any("AutomationControlled" in a or a in ("--test-type", "--enable-automation") for a in args)
        assert "--remote-debugging-port=0" not in args


def test_display_sticks_to_the_profile(tmp_path):
    s = Session("v", str(tmp_path / "v"), display="virtual", headless=False)
    os.makedirs(s.profile_dir, exist_ok=True)
    s._save_meta()
    assert Session.from_profile_dir(s.profile_dir).display == "virtual"
    with pytest.raises(ValueError):
        Session("bad", str(tmp_path / "bad"), display="mirror")


def test_auto_display_is_virtual_only_without_a_desktop(tmp_path, monkeypatch):
    s = Session("a", str(tmp_path / "a"), headless=False)
    monkeypatch.setattr(display, "available", lambda: True)
    monkeypatch.setattr(display, "has_screen", lambda: True)
    assert s._screen_kind() == "real"
    monkeypatch.setattr(display, "has_screen", lambda: False)
    assert s._screen_kind() == "virtual"
    assert Session("h", str(tmp_path / "h"), headless=True)._screen_kind() is None


def test_virtual_without_xvfb_is_an_error_not_a_silent_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(display, "available", lambda: False)
    s = Session("x", str(tmp_path / "x"), headless=False, display="virtual")
    with pytest.raises(RuntimeError, match="Xvfb"):
        s._screen_kind()


@needs_xvfb
def test_xtest_pointer_and_keys_work_on_a_virtual_screen():
    from hutch import xinput
    with display.VirtualDisplay() as vd, xinput.XInput(vd.display) as x:
        x.origin = (0, 119)
        x.move_page(500, 400)
        x.button(True)
        x.button(False)
        x.key("a", True)
        x.key("a", False)
        with pytest.raises(xinput.XInputError):
            x.key("NoSuchKeysymName", True)


def test_xinput_refuses_a_display_it_cannot_open():
    from hutch import xinput
    with pytest.raises(xinput.XInputError):
        xinput.XInput(":199")


@needs_xvfb
def test_the_virtual_screen_publishes_a_top_bar():
    import ctypes
    import ctypes.util
    x = ctypes.CDLL(ctypes.util.find_library("X11"))
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
                                     ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                     ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
                                     ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
    with display.VirtualDisplay((1920, 1080)) as vd:
        dpy = x.XOpenDisplay(vd.display.encode())
        atom, fmt = ctypes.c_ulong(), ctypes.c_int()
        n, left, data = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        x.XGetWindowProperty(dpy, x.XDefaultRootWindow(dpy), x.XInternAtom(dpy, b"_NET_WORKAREA", 0), 0, 4, 0, 6,
                             ctypes.byref(atom), ctypes.byref(fmt), ctypes.byref(n), ctypes.byref(left),
                             ctypes.byref(data))
        area = ctypes.cast(data, ctypes.POINTER(ctypes.c_long))[:4]
        x.XCloseDisplay(dpy)
    assert area == [0, display.PANEL, 1920, 1080 - display.PANEL]
    assert display.work_area((1920, 1080)) == (0, display.PANEL, 1920, 1080 - display.PANEL)
