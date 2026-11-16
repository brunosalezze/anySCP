# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Windows: drags that end on a monitor left of or above the main one.

Such a monitor has negative screen coordinates.  GTK runs Windows' drag loop
(DoDragDrop) on a thread of its own, "GDK Win32 DnD Thread", and tells it
where the pointer is by posting WM_MOUSEMOVE messages whose position it packs
from floating point numbers (gdk/win32/gdkdrag-win32.c,
gdk_dnd_handle_motion_event).  Turning a negative number into an unsigned one
is undefined in C, and ARM64 makes it 0: the drag loop then looks for the
drop target at the left or top edge of the main monitor instead of under the
pointer, and nothing on the other monitor gets the drop, not even Explorer.

A WH_GETMESSAGE hook on that thread sees each message before the drag loop
does, and puts in the position Windows itself reports for the pointer.

The drag icon has the same problem (move_drag_surface() takes unsigned
coordinates): it stays behind at the edge of the main monitor.  Its window
is subclassed so that every move GTK asks for goes to the pointer instead.

And GTK releases each drag object once more than it took it: when Windows'
drag loop is over, do_drag_drop_response() clears the data object's pointer
to the drag with g_clear_object(), but data_object_new() never took a
reference.  The object is freed early, and the last release hits freed
memory ("g_object_unref: assertion 'G_IS_OBJECT (object)' failed", and now
and then a crash in the next drag).  hold_drag() takes one reference to
make up for it; should a GTK without that bug leave the reference to us,
release_drag_later() gives it back.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from .. import compat

log = logging.getLogger(__name__)

DND_THREAD = "GDK Win32 DnD Thread"     # gdk/win32/gdkclipdrop-win32.c
THREAD_NAME_MAX = 15                    # GLib cuts thread names to this many characters
WH_GETMESSAGE = 3
HC_ACTION = 0
WM_WINDOWPOSCHANGING = 0x0046
WM_NCDESTROY = 0x0082
WM_MOUSEMOVE = 0x0200
SWP_NOMOVE = 0x0002
GWLP_WNDPROC = -4

_hook = None        # (hook handle, callback) once installed, False if that is impossible
stats = {"moves": 0, "last_posted": None, "last_fixed": None}     # what the hook did (for tests)
_icons: dict = {}   # drag icon window -> (original window procedure, our callback)
_retired: list = []     # callbacks of destroyed icon windows, kept a little longer


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


class WINDOWPOS(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("hwndInsertAfter", wintypes.HWND), ("x", ctypes.c_int),
                ("y", ctypes.c_int), ("cx", ctypes.c_int), ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]


if compat.IS_WINDOWS:
    HOOKPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.SetWindowsHookExW.restype = wintypes.HHOOK
    _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    _user32.CallNextHookEx.restype = wintypes.LPARAM
    _user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _user32.CallWindowProcW.restype = wintypes.LPARAM
    _user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                        wintypes.LPARAM]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.UINT]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.GetThreadDescription.restype = ctypes.c_long
    _kernel32.GetThreadDescription.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _thread_named(name: str) -> int:
    """Id of this process's thread called ``name`` (GLib names its threads,
    but only with their first 15 characters), or 0."""
    wanted = name[:THREAD_NAME_MAX]
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x4, 0)          # TH32CS_SNAPTHREAD
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return 0
    pid = _kernel32.GetCurrentProcessId()
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread = _kernel32.OpenThread(0x0800, False, entry.th32ThreadID)   # QUERY_LIMITED_INFORMATION
                if thread:
                    text = ctypes.c_void_p()
                    try:
                        if _kernel32.GetThreadDescription(thread, ctypes.byref(text)) >= 0 and text.value:
                            if ctypes.wstring_at(text.value)[:THREAD_NAME_MAX] == wanted:
                                return entry.th32ThreadID
                    finally:
                        if text.value:
                            _kernel32.LocalFree(text)
                        _kernel32.CloseHandle(thread)
            ok = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return 0


def _on_message(code, wparam, lparam):
    """Runs on GTK's drag thread for each message it takes from its queue."""
    try:
        if code == HC_ACTION and lparam:
            msg = MSG.from_address(lparam)
            if msg.message == WM_MOUSEMOVE and not msg.hwnd:      # posted by GTK (to the thread)
                pt = wintypes.POINT()
                if _user32.GetCursorPos(ctypes.byref(pt)):
                    stats["moves"] += 1
                    stats["last_posted"] = (ctypes.c_short(msg.lParam & 0xFFFF).value,
                                            ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value,
                                            msg.pt.x, msg.pt.y)
                    msg.lParam = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
                    msg.pt = pt
                    stats["last_fixed"] = (pt.x, pt.y)
    except Exception:  # noqa: BLE001 - never break the drag loop
        pass
    return _user32.CallNextHookEx(None, code, wparam, lparam)


def fix_drag_positions() -> bool:
    """Install the hook (once; Windows only).  False if it cannot be."""
    global _hook
    if not compat.IS_WINDOWS:
        return False
    if _hook is None:
        try:
            thread = _thread_named(DND_THREAD)
            if not thread:
                raise OSError(f"no thread named {DND_THREAD!r}")
            callback = HOOKPROC(_on_message)
            handle = _user32.SetWindowsHookExW(WH_GETMESSAGE, callback, None, thread)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            _hook = (handle, callback)
            log.debug("watching the drag positions of GTK's drag thread %s", thread)
        except (OSError, AttributeError) as exc:
            log.warning("cannot correct drag positions: %s", exc)
            _hook = False
    return bool(_hook)


def icon_follows_pointer(drag, hot_x: int = 0, hot_y: int = 0) -> bool:
    """Keep the drag icon of ``drag`` (a Gdk.Drag) under the pointer.  False
    if that is not possible; then the icon may be drawn in the wrong place."""
    if not compat.IS_WINDOWS:
        return False
    try:
        import gi
        gi.require_version("GdkWin32", "4.0")
        from gi.repository import GdkWin32
        surface = drag.get_drag_surface()
        if surface is None:
            return False
        hwnd = GdkWin32.Win32Surface.get_handle(surface)
        if not hwnd:
            return False
        if hwnd in _icons:
            return True
        original = _user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)
        if not original:
            return False

        def procedure(window, message, wparam, lparam):
            try:
                if message == WM_WINDOWPOSCHANGING and lparam:
                    pos = WINDOWPOS.from_address(lparam)
                    pt = wintypes.POINT()
                    if not pos.flags & SWP_NOMOVE and _user32.GetCursorPos(ctypes.byref(pt)):
                        pos.x, pos.y = pt.x - hot_x, pt.y - hot_y
                elif message == WM_NCDESTROY:
                    _user32.SetWindowLongPtrW(window, GWLP_WNDPROC, original)
                    entry = _icons.pop(window, None)
                    _retired.append(entry)          # (not freed while it runs)
            except Exception:  # noqa: BLE001
                pass
            return _user32.CallWindowProcW(original, window, message, wparam, lparam)

        callback = WNDPROC(procedure)
        _icons[hwnd] = (original, callback)
        del _retired[:-4]
        _user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(callback, ctypes.c_void_p))
        pt = wintypes.POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):       # (GTK has placed it once already)
            _user32.SetWindowPos(hwnd, None, pt.x - hot_x, pt.y - hot_y, 0, 0, 0x0001 | 0x0004 | 0x0010)
        return True
    except (OSError, ValueError, ImportError, AttributeError) as exc:
        log.warning("cannot keep the drag icon under the pointer: %s", exc)
        return False



# -- the drag object GTK releases once too often --------------------------------

_held: dict[int, tuple] = {}    # drag object's address -> (weak notify callback, state)
_gobject = None


def _gobject_library():
    global _gobject
    if _gobject is None:
        for name in ("gobject-2.0-0.dll", "libgobject-2.0-0.dll"):     # conda-forge, MSYS2
            try:
                lib = ctypes.CDLL(name)          # (already loaded: this finds it)
                break
            except OSError:
                continue
        else:
            raise OSError("GObject library not found")
        lib.g_object_ref.argtypes = [ctypes.c_void_p]
        lib.g_object_ref.restype = ctypes.c_void_p
        lib.g_object_unref.argtypes = [ctypes.c_void_p]
        lib.g_object_unref.restype = None
        lib.g_object_weak_ref.argtypes = [ctypes.c_void_p, WEAK_NOTIFY, ctypes.c_void_p]
        lib.g_object_weak_ref.restype = None
        lib.g_object_weak_unref.argtypes = [ctypes.c_void_p, WEAK_NOTIFY, ctypes.c_void_p]
        lib.g_object_weak_unref.restype = None
        _gobject = lib
    return _gobject


WEAK_NOTIFY = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)


def hold_drag(drag) -> bool:
    """One extra reference to ``drag`` (a Gdk.Drag), for the one GTK gives
    back too many (Windows only)."""
    if not compat.IS_WINDOWS or drag is None:
        return False
    try:
        lib = _gobject_library()
        address = hash(drag)                    # (PyGObject: the GObject's address)
        if address in _held:
            return True
        state = {"freed": False}

        def freed(_data, _where):
            state["freed"] = True
        callback = WEAK_NOTIFY(freed)
        lib.g_object_weak_ref(address, callback, None)
        lib.g_object_ref(address)
        _held[address] = (callback, state)
        return True
    except (OSError, AttributeError, TypeError) as exc:
        log.warning("cannot hold the drag object: %s", exc)
        return False


def release_drag_later(drag, seconds: int = 5) -> None:
    """After the drag: if GTK left our reference alone (it has no such bug),
    give it back, so the drag object is not leaked."""
    if not compat.IS_WINDOWS or drag is None:
        return
    address = hash(drag)
    if address not in _held:
        return
    from gi.repository import GLib

    def check():
        callback, state = _held.pop(address, (None, {"freed": True}))
        if not state["freed"]:
            lib = _gobject_library()
            lib.g_object_weak_unref(address, callback, None)
            lib.g_object_unref(address)
            stats["drags_given_back"] = stats.get("drags_given_back", 0) + 1
        else:
            stats["drags_made_up_for"] = stats.get("drags_made_up_for", 0) + 1
        return False
    GLib.timeout_add_seconds(seconds, check)
