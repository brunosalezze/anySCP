# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Facts about the Windows desktop a GUI test is about to run on.

usage: python win_probe.py [screenshot.png]

Prints JSON: the session, whether its desktop takes input (not locked),
and the monitors in physical pixels with their DPI.  With a file name, also
saves a screenshot of all monitors (half size).  Needs only ctypes, so any
Windows Python works (the runtime of an anySCP package, for example).
"""

import ctypes
import json
import struct
import sys
import zlib
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")
shcore = ctypes.WinDLL("shcore")

user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
PER_MONITOR_V2 = ctypes.c_void_p(-4)


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
shcore.GetDpiForMonitor.argtypes = [wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(wintypes.UINT),
                                    ctypes.POINTER(wintypes.UINT)]


def rect(r):
    return [r.left, r.top, r.right, r.bottom]


def monitors():
    """[{device, rect, work, dpi, primary}] in physical pixels"""
    found = []

    def one(hmon, _hdc, _rect, _lp):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        dx, dy = wintypes.UINT(), wintypes.UINT()
        shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
        found.append({"device": info.szDevice, "rect": rect(info.rcMonitor), "work": rect(info.rcWork),
                      "dpi": dx.value, "scale_percent": round(dx.value * 100 / 96),
                      "primary": bool(info.dwFlags & 1)})
        return True

    old = user32.SetThreadDpiAwarenessContext(PER_MONITOR_V2)
    try:
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(one), 0)
    finally:
        user32.SetThreadDpiAwarenessContext(old)
    return found


def input_desktop():
    """Name of the desktop that receives input ("Default" when unlocked), or
    an error text (the secure desktop of the lock screen cannot be opened)."""
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    handle = user32.OpenInputDesktop(0, False, 0x0001)
    if not handle:
        return f"(cannot open: error {ctypes.get_last_error()})"
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        user32.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed))
        return buf.value
    finally:
        user32.CloseDesktop(handle)


def system_dpi():
    hdc = user32.GetDC(None)
    try:
        return gdi32.GetDeviceCaps(hdc, 88)        # LOGPIXELSX
    finally:
        user32.ReleaseDC(None, hdc)


user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]


def screenshot(path, step=2, region=None):
    """All monitors (or region: x, y, width, height in physical pixels),
    every step-th pixel, as PNG."""
    x, y, w, h, raw = grab(region)
    _write_png(path, w, h, raw, step)
    return {"origin": [x, y], "size": [w, h], "step": step}


def grab(region=None):
    """(x, y, width, height, BGRA bytes, top row first) of the screen: all
    monitors, or region (x, y, width, height in physical pixels)."""
    old = user32.SetThreadDpiAwarenessContext(PER_MONITOR_V2)
    try:
        if region:
            x, y, w, h = region
        else:
            x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
            w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
        bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                           ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        bmp = gdi32.CreateDIBSection(screen, ctypes.byref(bih), 0, ctypes.byref(bits), None, 0)
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject(mem, bmp)
        gdi32.BitBlt(mem, 0, 0, w, h, screen, x, y, 0x00CC0020 | 0x40000000)   # SRCCOPY | CAPTUREBLT
        raw = ctypes.string_at(bits, w * h * 4)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
    finally:
        user32.SetThreadDpiAwarenessContext(old)
    return x, y, w, h, raw


def _write_png(path, w, h, raw, step):
    rows = []
    for row in range(0, h, step):
        line = raw[row * w * 4:(row + 1) * w * 4]
        px = bytearray()
        for col in range(0, w, step):
            b, g, r = line[col * 4], line[col * 4 + 1], line[col * 4 + 2]
            px += bytes((r, g, b))
        rows.append(b"\0" + bytes(px))
    ow, oh = (w + step - 1) // step, len(rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", ow, oh, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


def main():
    session = wintypes.DWORD()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session))
    facts = {"session": session.value, "console_session": kernel32.WTSGetActiveConsoleSessionId(),
             "input_desktop": input_desktop(), "system_dpi": system_dpi(), "monitors": monitors()}
    if len(sys.argv) > 1:
        try:
            facts["screenshot"] = screenshot(sys.argv[1])
        except OSError as exc:
            facts["screenshot"] = f"failed: {exc}"
    print(json.dumps(facts, indent=1))


if __name__ == "__main__":
    main()
