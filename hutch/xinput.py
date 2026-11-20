"""Operating-system input for a browser on an X display.

A click sent through the DevTools protocol reaches the page as a synthetic event: it is flagged
trusted, but its screen coordinates equal its viewport coordinates, as if the window sat at the
corner of the screen with no frame, and the browser's own input pipeline never saw a pointer. Pages
that look for automation check exactly that. Input through the X server's XTEST extension takes the
route a physical mouse does: the X server delivers it to the window under the pointer and the
browser handles it as it would a hand's.

For a browser on a ``display.VirtualDisplay`` (or any X display this process can open). Plain Xlib
and XTEST through ctypes, no extra packages.
"""

import ctypes
import ctypes.util

BUTTONS = {"left": 1, "middle": 2, "right": 3}


class XInputError(RuntimeError):
    pass


class XInput:
    """XTEST pointer on one X display. ``origin`` is where the page's viewport starts on the screen."""

    def __init__(self, display):
        x11 = ctypes.util.find_library("X11")
        xtst = ctypes.util.find_library("Xtst")
        if not x11 or not xtst:
            raise XInputError("libX11 and libXtst are needed for operating-system input")
        self._x = ctypes.CDLL(x11)
        self._t = ctypes.CDLL(xtst)
        self._x.XOpenDisplay.restype = ctypes.c_void_p
        self._x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x.XFlush.argtypes = [ctypes.c_void_p]
        self._x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._t.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                                 ctypes.c_ulong]
        self._t.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self._t.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self._x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self._x.XStringToKeysym.restype = ctypes.c_ulong
        self._dpy = self._x.XOpenDisplay(display.encode())
        if not self._dpy:
            raise XInputError(f"cannot open X display {display}")
        self.display = display
        self.origin = (0.0, 0.0)

    def close(self):
        if self._dpy:
            self._x.XCloseDisplay(self._dpy)
            self._dpy = None

    def _flush(self):
        self._x.XFlush(self._dpy)

    def move(self, x, y):
        """Move the pointer to screen coordinates."""
        self._t.XTestFakeMotionEvent(self._dpy, -1, int(round(x)), int(round(y)), 0)
        self._flush()

    def move_page(self, x, y):
        """Move the pointer to page (viewport) coordinates."""
        self.move(x + self.origin[0], y + self.origin[1])

    def button(self, down, button="left"):
        self._t.XTestFakeButtonEvent(self._dpy, BUTTONS[button], 1 if down else 0, 0)
        self._flush()

    def key(self, name, down):
        """Press or release a key by X keysym name ("a", "Return", "BackSpace", "Tab")."""
        sym = self._x.XStringToKeysym(name.encode())
        code = self._x.XKeysymToKeycode(self._dpy, sym) if sym else 0
        if not code:
            raise XInputError(f"no key for {name!r}")
        self._t.XTestFakeKeyEvent(self._dpy, code, 1 if down else 0, 0)
        self._flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


async def viewport_origin(send):
    """Where the page's top-left corner is on the screen, for a window without a window manager.

    ``send`` is a DevTools ``send(method, params)``. The browser's own chrome (tabs, address bar)
    sits above the page: that height is outerHeight - innerHeight, and the window's left and top are
    ``screenX`` and ``screenY``.
    """
    out = await send("Runtime.evaluate", {
        "expression": "[screenX, screenY, outerWidth - innerWidth, outerHeight - innerHeight]",
        "returnByValue": True})
    sx, sy, dw, dh = out["result"]["value"]
    return (sx + dw / 2.0, sy + dh - dw / 2.0)
