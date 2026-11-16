# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Move and click the X11 pointer (XTest), and place windows, with ctypes.

For GUI tests under Xvfb, where there is neither a user nor a window manager.
"""

import ctypes


class XPointer:
    def __init__(self):
        self.x11 = ctypes.CDLL("libX11.so.6")
        self.xtst = ctypes.CDLL("libXtst.so.6")
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        for fn in (self.x11.XFlush, self.x11.XCloseDisplay):
            fn.argtypes = [ctypes.c_void_p]
        self.x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
        self.x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong)]
        self.xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                                   ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                                   ctypes.c_ulong]
        self.dpy = self.x11.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("cannot open the X display")
        self.root = self.x11.XDefaultRootWindow(self.dpy)

    def move(self, x, y):
        self.xtst.XTestFakeMotionEvent(self.dpy, -1, int(x), int(y), 0)
        self.x11.XSync(self.dpy, 0)

    def button(self, pressed: bool, button: int = 1):
        self.xtst.XTestFakeButtonEvent(self.dpy, button, 1 if pressed else 0, 0)
        self.x11.XSync(self.dpy, 0)

    def place(self, xid: int, x: int, y: int):
        self.x11.XMoveWindow(self.dpy, xid, x, y)
        self.x11.XRaiseWindow(self.dpy, xid)
        self.x11.XSync(self.dpy, 0)

    def origin(self, xid: int):
        """Position of a window's top-left corner on the screen."""
        x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        self.x11.XTranslateCoordinates(self.dpy, xid, self.root, 0, 0,
                                       ctypes.byref(x), ctypes.byref(y), ctypes.byref(child))
        return x.value, y.value
