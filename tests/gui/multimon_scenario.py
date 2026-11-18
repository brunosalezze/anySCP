# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Drag and drop with several monitors, on a real Windows PC.

Wine shows one screen, so this runs on real hardware, in the desktop the
monitors show (see win_runner.py), with the Python of an anySCP package:

    <package>\\runtime\\bin\\python.exe -s multimon_scenario.py <params.json>

It starts a throwaway SFTP server (sftp_server.py), connects anySCP to it
and, with synthesized mouse input (SetCursorPos and mouse_event, as a hand
would), places the anySCP window and an Explorer window on each pair of
monitors and drags:

  pane:  a file from the local pane onto the server pane (an upload)
  down:  a file from the server pane onto the local pane (a download)
  same:  a file dropped back on the pane it came from (nothing happens)
  out:   a local file out of anySCP onto the Explorer window
  rout:  a server file out of anySCP onto the Explorer window
  in:    a file from the Explorer window onto anySCP's server pane
  menu:  (asked for only) a right click opens the context menu next to the
         pointer, and a new local file shows up on the screen
  clicks: (asked for only) a click on a pane's empty space clears that
         pane's selection; a double click on a saved site in the site
         dialog logs in to it
  jumps: (asked for only) real servers through real jump hosts, set up in the
         site dialog: P["jumps"] = {"target": {host, user, key, passphrase},
         "chains": [[{host, user, password}, ...], ...]}; a chain can also be
         {"hops": [...], "proxy": {type, host, port}}, and a hop can have a
         "proxy" of its own
  sites: (asked for only) the ways of opening a session - a new site in the
         site dialog, the saved site from the start page and from the site
         dialog, quick connect - and whether the server's files then really show

params.json: {"work": folder for this run,
              "cases": [...] (default: pane, down, same, out, rout, in),
              "monitors": [indexes] (default: all),
              "pairs": [[app monitor, Explorer monitor], ...] (default: all pairs),
              "env": {extra environment, e.g. GDK_DEBUG},
              "keyring": use the system's password store,
              "server_delay": seconds the server waits before answering, "slow": 1}
Writes <work>/result.json and prints each step.
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
P = json.load(open(sys.argv[1], encoding="utf-8"))
WORK = P["work"]
SLOW = float(P.get("slow", 1))
CASES = P.get("cases") or ["pane", "down", "same", "out", "rout", "in"]
DIRS = {k: os.path.join(WORK, k) for k in ("config", "cache", "remote", "local", "explorer", "source", "shots")}
shutil.rmtree(WORK, ignore_errors=True)
for d in DIRS.values():
    os.makedirs(d)
os.environ.update({"ANYSCP_CONFIG_DIR": DIRS["config"], "ANYSCP_CACHE_DIR": DIRS["cache"]})
if not P.get("keyring"):
    os.environ["ANYSCP_NO_KEYRING"] = "1"
for key, value in (P.get("env") or {}).items():
    os.environ[key] = value
if "GI_TYPELIB_PATH" not in os.environ:          # what the launcher sets up (MSYS2 or conda layout)
    rt = sys.prefix
    lib = os.path.join(rt, "Library") if os.path.isdir(os.path.join(rt, "Library")) else rt
    os.environ.update({
        "GI_TYPELIB_PATH": os.path.join(lib, "lib", "girepository-1.0"),
        "XDG_DATA_DIRS": os.path.join(lib, "share"),
        "GSETTINGS_SCHEMA_DIR": os.path.join(lib, "share", "glib-2.0", "schemas"),
        "GDK_PIXBUF_MODULE_FILE": os.path.join(lib, "lib", "gdk-pixbuf-2.0", "2.10.0", "loaders.cache"),
        "GSETTINGS_BACKEND": "memory",
    })

# ---------------------------------------------------------------------------- the server
PASSWORD = "multi monitor"
with open(os.path.join(DIRS["remote"], "on-server.bin"), "wb") as fh:
    fh.write(os.urandom(300_000))
INFO = os.path.join(WORK, "server.json")
SERVER = subprocess.Popen([sys.executable, "-s", os.path.join(HERE, "sftp_server.py"), DIRS["remote"], "tester",
                           PASSWORD, INFO, str(P.get("server_delay", 0))],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
for _ in range(400):
    if os.path.exists(INFO):
        break
    time.sleep(0.05)
SERVER_INFO = json.load(open(INFO, encoding="utf-8"))
with open(os.path.join(DIRS["config"], "known_hosts"), "w", encoding="utf-8") as fh:
    fh.write(SERVER_INFO["known_hosts"] + "\n")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkWin32", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GdkWin32, Gio, GLib, Graphene, Gtk  # noqa: E402,F401

import win_probe  # noqa: E402
from anyscp import config  # noqa: E402
from anyscp.app import AnySCPApplication  # noqa: E402
from anyscp.ui import win32drag  # noqa: E402
from anyscp.ui.file_pane import FilePane  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")
oleaut32 = ctypes.WinDLL("oleaut32")
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.LogicalToPhysicalPointForPerMonitorDPI.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
PER_MONITOR_V2 = ctypes.c_void_p(-4)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]

result = {"ok": False, "steps": [], "monitors": win_probe.monitors(), "system_dpi": win_probe.system_dpi()}
T0 = time.monotonic()


class physical:
    """Win32 calls in this block use physical pixels (per-monitor DPI)."""

    def __enter__(self):
        self.old = user32.SetThreadDpiAwarenessContext(PER_MONITOR_V2)

    def __exit__(self, *_exc):
        user32.SetThreadDpiAwarenessContext(self.old)


def step(name, ok, detail=""):
    t = round(time.monotonic() - T0, 1)
    result["steps"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:900], "t": t})
    print(f"{t:7.1f}s " + ("OK   " if ok else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)
    return ok


def spin(cond, timeout):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout * SLOW
    tick = GLib.timeout_add(20, lambda: True)
    try:
        while time.monotonic() < end:
            if cond():
                return True
            ctx.iteration(True)
        return cond()
    finally:
        GLib.source_remove(tick)


def wait(seconds):
    end = time.monotonic() + seconds
    spin(lambda: time.monotonic() >= end, seconds + 1)


def shot(name):
    try:
        win_probe.screenshot(os.path.join(DIRS["shots"], name), step=3)
    except OSError as exc:
        print("screenshot failed:", exc)


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def window_at(x, y):
    """(class, title) of the top-level window at physical x, y"""
    with physical():
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        top = user32.GetAncestor(hwnd, 2) if hwnd else None      # GA_ROOT
    return (class_name(top), title(top)) if top else (None, None)


def place(hwnd, rect):
    """Move a top-level window to rect (physical l, t, r, b)."""
    left, top, right, bottom = (int(v) for v in rect)
    user32.ShowWindow(hwnd, 9)                                    # SW_RESTORE
    with physical():
        user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top, 0x0004 | 0x0040)


def window_rect(hwnd):
    r = wintypes.RECT()
    with physical():
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


# ---------------------------------------------------------------------------- pointer
def drag(start, stops, hold=0.5, steps=20, closeup=None):
    """Press at start, move through stops, release (physical pixels).  With
    closeup (a file name): a full-size screenshot around the pointer just
    before the button is released (is the drag icon there?)."""
    with physical():
        user32.SetCursorPos(*start)
    wait(0.3)
    user32.mouse_event(0x0002, 0, 0, 0, 0)          # MOUSEEVENTF_LEFTDOWN
    wait(0.25)
    x0, y0 = start
    for x1, y1 in stops:
        for i in range(1, steps + 1):
            with physical():
                user32.SetCursorPos(int(x0 + (x1 - x0) * i / steps), int(y0 + (y1 - y0) * i / steps))
            wait(0.03 * SLOW)
        x0, y0 = x1, y1
        wait(hold * SLOW)
    if closeup:
        try:
            win_probe.screenshot(os.path.join(DIRS["shots"], closeup), step=1,
                                 region=(x0 - 120, y0 - 80, 240, 160))
        except OSError as exc:
            print("close-up failed:", exc)
    user32.mouse_event(0x0004, 0, 0, 0, 0)          # MOUSEEVENTF_LEFTUP
    wait(0.5 * SLOW)


# ---------------------------------------------------------------------------- Explorer
def explorer_windows():
    found = []

    def one(hwnd, _lp):
        if class_name(hwnd) == "CabinetWClass":
            found.append(hwnd)
        return True
    user32.EnumWindows(WNDENUMPROC(one), 0)
    return found


def open_explorer(folder):
    before = set(explorer_windows())
    subprocess.Popen(["explorer.exe", folder])
    hwnd = None

    def appeared():
        nonlocal hwnd
        new = [h for h in explorer_windows() if h not in before and title(h)]
        hwnd = new[0] if new else None
        return hwnd is not None
    spin(appeared, 20)
    wait(1.5)
    return hwnd


def close_window(hwnd):
    if hwnd and user32.IsWindow(hwnd):
        user32.PostMessageW(hwnd, 0x0010, 0, 0)    # WM_CLOSE


class GUID(ctypes.Structure):
    _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    def __init__(self, text):
        super().__init__()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(self))


def method(obj, index, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
    return lambda *args: fn(obj, *args)


def release(obj):
    if obj:
        method(obj, 2)()


def item_rect(hwnd, name):
    """Physical rect of the item ``name`` in an Explorer window (UI Automation)."""
    ole32.CoInitializeEx(None, 2)
    automation = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(GUID("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")), None, 1,
                                ctypes.byref(GUID("{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}")),
                                ctypes.byref(automation))
    if hr:
        raise OSError(f"CoCreateInstance(CUIAutomation): {hr & 0xFFFFFFFF:#x}")
    root, cond, items = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        with physical():
            method(automation, 6, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(hwnd, ctypes.byref(root))
            method(automation, 21, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(cond))      # CreateTrueCondition
            method(root, 6, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                4, cond, ctypes.byref(items))                                                # FindAll(descendants)
            count = ctypes.c_int()
            method(items, 3, ctypes.POINTER(ctypes.c_int))(ctypes.byref(count))
            best = None
            for i in range(count.value):
                el = ctypes.c_void_p()
                method(items, 4, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(i, ctypes.byref(el))
                bstr = ctypes.c_void_p()
                method(el, 23, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(bstr))          # get_CurrentName
                text = ctypes.wstring_at(bstr.value) if bstr.value else ""
                oleaut32.SysFreeString(bstr)
                kind = ctypes.c_int()
                method(el, 21, ctypes.POINTER(ctypes.c_int))(ctypes.byref(kind))             # get_CurrentControlType
                if text in (name, os.path.splitext(name)[0]) and kind.value == 50007:      # ListItem
                    # (Explorer hides known extensions)
                    r = wintypes.RECT()
                    method(el, 43, ctypes.POINTER(wintypes.RECT))(ctypes.byref(r))         # BoundingRectangle
                    best = (r.left, r.top, r.right, r.bottom)
                release(el)
                if best:
                    break
            return best
    finally:
        for obj in (items, cond, root, automation):
            release(obj)


# ---------------------------------------------------------------------------- anySCP
def app_hwnd(win):
    return GdkWin32.Win32Surface.get_handle(win.get_surface())


def to_screen(win, widget, fx=0.5, fy=0.5):
    """Physical screen point of a spot in a widget."""
    ok, pt = widget.compute_point(win, Graphene.Point().init(widget.get_width() * fx, widget.get_height() * fy))
    assert ok
    surface = win.get_surface()
    scale = surface.get_scale()
    sx, sy = win.get_surface_transform()
    hwnd = app_hwnd(win)
    p = wintypes.POINT(int((pt.x + sx) * scale), int((pt.y + sy) * scale))
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    user32.LogicalToPhysicalPointForPerMonitorDPI(hwnd, ctypes.byref(p))
    return p.x, p.y


def row_widget(pane, name):
    model = pane.sort_model
    position = next(i for i in range(model.get_n_items())
                    if not model.get_item(i).is_parent and model.get_item(i).info.name == name)
    stack = [pane.column_view]
    while stack:
        widget = stack.pop()
        cell = getattr(widget, "_cell", None)
        if cell is not None and cell.get_position() == position and widget.get_mapped():
            return widget
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    raise LookupError(f"no row for {name}")


def names(pane):
    return {e.name for e in pane.entries}


def job_titled(app, text):
    return next((j for j in reversed(app.transfers.jobs) if j.title == text), None)


def finished(job):
    return job is not None and job.state in ("done", "failed", "cancelled")


def layout(mon_app, mon_exp):
    """Window rects (physical) for anySCP and Explorer on these monitors."""
    a = result["monitors"][mon_app]["work"]
    e = result["monitors"][mon_exp]["work"]
    aw, ah = a[2] - a[0], a[3] - a[1]
    app_rect = (a[0] + 40, a[1] + 40, a[0] + 40 + min(1500, int(aw * 0.55)), a[1] + 40 + min(950, int(ah * 0.7)))
    ew, eh = e[2] - e[0], e[3] - e[1]
    if mon_app == mon_exp:
        left = app_rect[2] + 60
        exp_rect = (left, e[1] + 80, min(e[2] - 40, left + 1000), e[1] + 80 + min(800, int(eh * 0.6)))
    else:
        exp_rect = (e[0] + int(ew * 0.3), e[1] + 80, e[0] + int(ew * 0.3) + min(1000, int(ew * 0.5)),
                    e[1] + 80 + min(800, int(eh * 0.6)))
    return app_rect, exp_rect


def describe(page):
    rp = page.remote_pane
    worker = page.session.worker
    text = f"state={page.session.state} error={page.session.error!r} worker_busy={worker.busy} " \
           f"queued={worker._queue.qsize()}"
    if rp is not None:
        text += (f" remote path={rp.path!r} entries={len(rp.entries)} load_serial={rp._load_serial} "
                 f"loaded_serial={rp._loaded_serial} spinner={rp.spinner.get_visible()} "
                 f"status_page={rp.status_page.get_visible()} ({rp.status_page.get_description()!r}) "
                 f"scroller={rp.scroller.get_visible()} "
                 f"rows={rp.sort_model.get_n_items()}")
    return text + f" log={page.session.log_lines[-6:]}"


def diagnose(page):
    """What every thread does, then: does loading again help?"""
    import faulthandler
    shot("empty-remote.png")
    print("---- threads", flush=True)
    faulthandler.dump_traceback(file=sys.stdout, all_threads=True)
    sys.stdout.flush()
    rp = page.remote_pane
    if rp is not None:
        rp.load(rp.path or "/")
        listed = spin(lambda: "on-server.bin" in names(rp), 20)
        step("diagnosis: loading the server pane again lists it", listed, describe(page))
        if not listed:
            print("---- threads after reloading", flush=True)
            faulthandler.dump_traceback(file=sys.stdout, all_threads=True)


def _all_widgets(root):
    stack, found = [root], []
    while stack:
        widget = stack.pop()
        found.append(widget)
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


def watch_drag_begin():
    """Report what FilePane._on_drag_begin does (it runs inside GTK's signal emission)."""
    original = FilePane._on_drag_begin

    def wrapped(self, source, drag):
        try:
            return original(self, source, drag)
        except Exception:  # noqa: BLE001
            print("drag-begin failed:\n" + traceback.format_exc(), flush=True)
            raise
        finally:
            print(f"drag-begin: infos={len(getattr(self, '_drag_infos', []) or [])} hook={win32drag._hook!r}",
                  flush=True)
    FilePane._on_drag_begin = wrapped


def click(point):
    with physical():
        user32.SetCursorPos(*point)
    wait(0.25)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    wait(0.08)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    wait(0.3)


def rows_on_screen(pane):
    count, stack = 0, [pane.column_view]
    while stack:
        widget = stack.pop()
        cell = getattr(widget, "_cell", None)
        if cell is not None and widget.get_mapped() and widget.get_width() > 0 and widget.get_height() > 0:
            item = cell.get_item()
            if item is not None and not item.is_parent:
                count += 1
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return count


def dark_pixels(win, pane):
    """Share of dark (text) pixels on the real screen in the top of a pane's file list."""
    x0, y0 = to_screen(win, pane.column_view, 0.0, 0.0)
    x1, y1 = to_screen(win, pane.column_view, 1.0, 1.0)
    top = y0 + 36                                   # below the column titles
    region = (x0 + 4, top, max(1, x1 - x0 - 8), max(1, min(y1 - top, 120)))
    _x, _y, w, h, raw = win_probe.grab(region)
    dark = sum(1 for i in range(0, len(raw), 4 * 3) if raw[i] + raw[i + 1] + raw[i + 2] < 330)
    return dark / max(1, len(raw) // (4 * 3))


def check_listed(app, label, page):
    win = app.window
    ok = spin(lambda: page.session.state == "connected" and page.remote_pane is not None
              and "on-server.bin" in names(page.remote_pane), 30)
    step(f"{label}: connects and lists the server (model)", ok, describe(page))
    if not ok:
        shot(f"sites-{label}-failed.png")
        return
    seen = []
    for delay in (0.2, 1.0, 3.0):
        wait(delay)
        seen.append((rows_on_screen(page.remote_pane), round(dark_pixels(win, page.remote_pane), 4)))
    shot(f"sites-{label}.png")
    rows, dark = seen[-1]
    step(f"{label}: the server's files are on the screen", rows > 0 and dark > 0.001,
         f"(rows on screen, dark pixel share) after 0.2/1.2/4.2 s: {seen}")


def close_tabs(win):
    for page in list(win.pages()):
        win.close_page(page, force=True)
    spin(lambda: not win.pages(), 10)
    wait(0.8)


def test_sites(app):
    """The ways of opening a session: a new site in the site dialog (Log In),
    the saved site from the start page and from the site dialog, quick connect."""
    from urllib.parse import quote
    from anyscp.ui.site_dialog import AUTH_MODES, SiteDialog
    win = app.window
    port = SERVER_INFO["port"]
    place(app_hwnd(win), layout(0, 0)[0])
    wait(1.0)

    dlg = SiteDialog(win, select_new=True)
    dlg.present(win)
    wait(1.0)
    dlg.name_row.set_text("Test site")
    dlg.host_row.set_text("127.0.0.1")
    dlg.port_row.set_value(port)
    dlg.user_row.set_text("tester")
    dlg.auth_row.set_selected(AUTH_MODES.index("password"))
    dlg.password_row.set_text(PASSWORD)
    dlg.save_pw_row.set_active(True)
    wait(0.5)
    click(to_screen(win, dlg.login_btn))
    page = win.current_page()
    step("new site: Log In opens a session", page is not None)
    if page is None:
        shot("sites-new-nopage.png")
        return
    check_listed(app, "new site", page)
    site_id = page.site.get("id")
    close_tabs(win)

    rows = []
    child = win.welcome.listbox.get_first_child()
    while child is not None:
        if getattr(child, "site", {}).get("id") == site_id:
            rows.append(child)
        child = child.get_next_sibling()
    if step("start page: the saved site is listed", rows):
        click(to_screen(win, rows[0], 0.3, 0.5))
        page = win.current_page()
        if step("start page: clicking the saved site opens a session", page is not None):
            check_listed(app, "saved site (start page)", page)
        close_tabs(win)

    dlg = SiteDialog(win, app.sites.get(site_id))
    dlg.present(win)
    wait(1.0)
    click(to_screen(win, dlg.login_btn))
    page = win.current_page()
    if step("site dialog: Log In on the saved site opens a session", page is not None):
        check_listed(app, "saved site (site dialog)", page)
    close_tabs(win)

    win.welcome.quick_entry.set_text(f"sftp://tester:{quote(PASSWORD)}@127.0.0.1:{port}")
    win.welcome._quick_connect()
    page = win.current_page()
    if step("quick connect opens a session", page is not None):
        check_listed(app, "quick connect", page)
    close_tabs(win)
    app.sites.delete(site_id)          # (and its password)


def test_clicks(app, page):
    """With real clicks: empty space of a pane, and a double click in the
    site dialog."""
    from anyscp.ui.site_dialog import SiteDialog
    win = app.window
    lp, rp = page.local_pane, page.remote_pane
    for name in ("click-a.txt", "click-b.txt"):
        for folder in (DIRS["local"], DIRS["remote"]):
            with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
                fh.write("click\n")
    for pane in (lp, rp):
        pane.load(pane.path)
        spin(lambda: {"click-a.txt", "click-b.txt"} <= names(pane), 10)
    wait(0.8)

    def selected(pane):
        return {i.name for i in pane.get_selected_infos()}

    for pane, other in ((lp, rp), (rp, lp)):
        side = "local" if pane is lp else "server"
        last = pane.sort_model.get_item(pane.sort_model.get_n_items() - 1).name
        ok, pt = row_widget(pane, last).compute_point(pane.column_view, Graphene.Point().init(0, 0))
        below = (pt.y + row_widget(pane, last).get_height() + 30) / pane.column_view.get_height()
        if not step(f"clicks: the {side} pane has empty space below its files", ok and below < 0.95, below):
            continue
        pane.select_names({"click-a.txt", "click-b.txt"})
        other.select_names({"click-a.txt"})
        wait(0.3)
        header = next((w for w in _all_widgets(pane.column_view) if w.get_css_name() == "header"), None)
        if header is not None:
            click(to_screen(win, header, 0.9, 0.5))
            step(f"clicks: a click on the {side} pane's column titles keeps its selection",
                 selected(pane) == {"click-a.txt", "click-b.txt"}, sorted(selected(pane)))
        click(to_screen(win, pane.column_view, 0.5, below))
        shot(f"clicks-{side}-blank.png")
        step(f"clicks: a click on the {side} pane's empty space clears its selection", not selected(pane),
             sorted(selected(pane)))
        step("clicks: ... and not the other pane's", selected(other) == {"click-a.txt"}, sorted(selected(other)))
        click(to_screen(win, row_widget(pane, "click-b.txt"), 0.6, 0.5))
        step(f"clicks: a click on a file of the {side} pane selects it", selected(pane) == {"click-b.txt"},
             sorted(selected(pane)))
        other.selection.unselect_all()
    lp.column_view.sort_by_column(lp.columns["name"], Gtk.SortType.ASCENDING)
    rp.column_view.sort_by_column(rp.columns["name"], Gtk.SortType.ASCENDING)

    site = app.sites.upsert(config.new_site(name="Double-click site", protocol="sftp", host="127.0.0.1",
                                            port=SERVER_INFO["port"], user="tester", auth="password",
                                            remote_dir="/", save_password=False))
    win.sites_changed()
    dlg = SiteDialog(win, select_new=True)
    dlg.present(win)
    wait(1.0)
    row = next(r for r in _all_widgets(dlg.listbox) if getattr(r, "site", None) and r.site["id"] == site["id"])
    pages = list(win.pages())
    click(to_screen(win, row))
    wait(0.6)
    step("clicks: a single click on a saved site only shows it",
         dlg.current.get("id") == site["id"] and list(win.pages()) == pages and dlg.get_mapped())
    with physical():
        user32.SetCursorPos(*to_screen(win, row))
    wait(0.3)
    for _ in range(2):
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        wait(0.04)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        wait(0.06)
    # (no saved password: the login asks for it)
    asked = spin(lambda: len(win.pages()) > len(pages) and win.get_visible_dialog() is not None
                 and win.get_visible_dialog() is not dlg, 10)
    shot("clicks-double-click.png")
    new = win.current_page()
    step("clicks: a double click on a saved site logs in to it",
         asked and new.site.get("id") == site["id"], (len(pages), len(win.pages()), win.get_visible_dialog()))
    prompt = win.get_visible_dialog()
    if asked and prompt is not None and prompt is not dlg:
        entry = next((w for w in _all_widgets(prompt) if isinstance(w, Gtk.Editable)
                      and isinstance(w, Gtk.Text)), None)
        if entry is not None:
            entry.set_text(PASSWORD)
        prompt.emit("response", "ok")
        prompt.force_close()
        step("clicks: ... and that session connects", spin(lambda: new.session.state == "connected"
                                                           and "click-a.txt" in names(new.remote_pane or lp), 20),
             new.session.state)
    if new is not page:
        win.close_page(new, force=True)
    app.sites.delete(site["id"])


def answer_prompts(win, page, secrets_by_title, until, timeout, screenshot=None):
    """Answer the login's prompts (host keys: once; passwords from
    secrets_by_title) until until() is true; the headings seen.  With
    screenshot: a picture of the screen at the first prompt."""
    seen = []
    end = time.monotonic() + timeout
    while time.monotonic() < end and not until():
        spin(lambda: until() or isinstance(win.get_visible_dialog(), Adw.AlertDialog)
             or page.session.state == "failed", 2)
        dialog = win.get_visible_dialog()
        if page.session.state == "failed" or not isinstance(dialog, Adw.AlertDialog):
            if page.session.state == "failed":
                break
            continue
        heading = dialog.get_heading()
        if screenshot and not seen:
            wait(0.4)
            shot(screenshot)
        seen.append(heading)
        if heading in ("Unknown Server", "Host Key Has Changed!"):
            dialog.emit("response", "once")
        else:
            text = secrets_by_title.get(heading)
            entry = next((w for w in _all_widgets(dialog) if isinstance(w, Gtk.Text)), None)
            if text is None or entry is None:
                dialog.emit("response", "cancel")
            else:
                entry.set_text(text)
                dialog.emit("response", "ok")
        dialog.force_close()
        wait(0.3)
    return seen


def test_jumps(app):
    """Real servers through real jump hosts, set up in the site dialog."""
    from anyscp.ui.site_dialog import AUTH_MODES, SiteDialog
    win = app.window
    J = P["jumps"]
    target = J["target"]
    key = next((k for k in target["keys"] if os.path.isfile(k)), None)
    if not step("jumps: the key file is reachable", key, target["keys"]):
        return
    place(app_hwnd(win), layout(0, 0)[0])
    wait(1.0)
    from anyscp.backends.proxy import PROXY_TYPES

    def set_proxy(rows, proxy):
        type_row, host_row, port_row = rows
        type_row.set_selected(PROXY_TYPES.index(proxy["type"]))
        host_row.set_text(proxy["host"])
        port_row.set_value(proxy["port"])

    def via(proxy):
        return f"[{proxy['type']} proxy {proxy['host']}:{proxy['port']}] -> " if proxy else ""

    for n, chain in enumerate(J["chains"], 1):
        site_proxy = chain.get("proxy") if isinstance(chain, dict) else None
        chain = chain["hops"] if isinstance(chain, dict) else chain
        label = f"jumps {n}: PC -> " + "".join(via(h.get("proxy")) + h["host"] + " -> " for h in chain) \
            + via(site_proxy) + target["host"]
        dlg = SiteDialog(win, select_new=True)
        dlg.present(win)
        wait(1.0)
        dlg.name_row.set_text(f"Through {len(chain)} jump host(s) {n}")
        dlg.host_row.set_text(target["host"])
        dlg.user_row.set_text(target["user"])
        dlg.auth_row.set_selected(AUTH_MODES.index("key"))
        dlg._set_key(key)                       # (its passphrase is asked at the first login)
        dlg.save_pw_row.set_active(True)
        scroller = next(w for w in _all_widgets(dlg.page) if isinstance(w, Gtk.ScrolledWindow))
        for hop in chain:
            adj = scroller.get_vadjustment()
            adj.set_value(adj.get_upper())
            wait(0.5)
            click(to_screen(win, dlg.add_jump_btn, 0.3, 0.5))      # (the button's main part)
            if not step(f"{label}: a click on Add adds a jump host", len(dlg.jump_rows) == chain.index(hop) + 1):
                dlg.add_jump()
            row = dlg.jump_rows[-1]
            row.host_row.set_text(hop["host"])
            row.user_row.set_text(hop["user"])
            row.auth_row.set_selected(AUTH_MODES.index("password"))
            row.password_row.set_text(hop["password"])
            row.save_pw_row.set_active(True)
            if hop.get("key"):
                row.auth_row.set_selected(AUTH_MODES.index("key"))
                row.set_key(hop["key"])
                row.passphrase_row.set_text(hop["passphrase"])
            if hop.get("proxy"):
                set_proxy((row.proxy_type_row, row.proxy_host_row, row.proxy_port_row), hop["proxy"])
            row.set_expanded(False)
        if site_proxy:
            set_proxy((dlg.proxy_type_row, dlg.proxy_host_row, dlg.proxy_port_row), site_proxy)
        wait(0.5)
        step(f"{label}: Add made {len(chain)} jump host(s)", len(dlg.jump_rows) == len(chain),
             [r.get_subtitle() for r in dlg.jump_rows])
        shot(f"jumps-{n}-dialog.png")
        click(to_screen(win, dlg.login_btn))
        page = win.current_page()
        if not step(f"{label}: Log In opens a session", page is not None):
            continue
        prompts = {"Key passphrase": target["passphrase"]}

        def listed():
            return page.session.state == "connected" and page.remote_pane is not None \
                and page.remote_pane.path and page.remote_pane.entries
        seen = answer_prompts(win, page, prompts, listed, 90, screenshot=f"jumps-{n}-connecting.png")
        ok = spin(listed, 30)
        log = page.session.log_lines
        stages = page.session.stages
        step(f"{label}: the connection screen listed each machine, all ticked off",
             len(stages) == len(chain) + 1 and all(st["state"] == "done" for st in stages),
             [(st["title"], st["role"], st["state"]) for st in stages])
        tunnels = [line[9:] for line in log if "Opening a tunnel" in line]
        step(f"{label}: connects and lists the server's home folder", ok,
             f"prompts {seen}; path {page.remote_pane and page.remote_pane.path}; "
             f"entries {page.remote_pane and len(page.remote_pane.entries)}; error {page.session.error!r}; "
             f"log {log[-6:]}")
        step(f"{label}: ... through each jump host in turn", len(tunnels) == len(chain), tunnels)
        proxied = [line[9:] for line in log if " proxy " in line and ("Connecting" in line or "tunnel" in line)]
        wanted = sum(1 for h in chain if h.get("proxy")) + (1 if site_proxy else 0)
        step(f"{label}: ... and through each proxy", len(proxied) == wanted, proxied)
        if ok:
            shot(f"jumps-{n}-session.png")
            status, out, _err = page.session.fs.exec("hostname")
            step(f"{label}: ... to the right machine", status == 0 and out.strip(), out.strip())
        site_id = page.site.get("id")
        win.close_page(page, force=True)
        spin(lambda: page not in win.pages(), 10)
        if not ok:
            continue

        # again from the saved site, as after a restart (nothing in memory but
        # the password store): its passwords and passphrases apply by themselves
        saved = {k: v for k, v in app.sites.get(site_id).items() if not k.startswith("_")}
        saved["jumps"] = [{k: v for k, v in h.items() if not k.startswith("_")} for h in saved["jumps"]]
        config.secrets._memory.clear()
        win.open_session(saved)
        page = win.current_page()
        seen = answer_prompts(win, page, {}, listed, 90)
        ok = spin(listed, 30)
        step(f"{label}: the saved site connects again without asking for any password or passphrase",
             ok and set(seen) <= {"Unknown Server"}, f"prompts {seen}; error {page.session.error!r}")
        win.close_page(page, force=True)
        spin(lambda: page not in win.pages(), 10)
        app.sites.delete(site_id)


def run(app):
    win = app.window
    watch_drag_begin()
    if "jumps" in CASES:
        test_jumps(app)
        if CASES == ["jumps"]:
            return
    if "sites" in CASES:
        test_sites(app)
        if CASES == ["sites"]:
            return
    site = config.new_site(name="multimon", protocol="sftp", host="127.0.0.1", port=SERVER_INFO["port"],
                           user="tester", auth="password", remote_dir="/", local_dir=DIRS["local"],
                           save_password=False)
    site["_password"] = PASSWORD
    win.open_session(site)
    page = win.current_page()
    ok = spin(lambda: page.session.state == "connected" and page.remote_pane is not None
              and page.remote_pane.path and "on-server.bin" in names(page.remote_pane), 30)
    if not step("the session connects and lists the server", ok, describe(page)):
        diagnose(page)
        return
    lp, rp = page.local_pane, page.remote_pane
    if "clicks" in CASES:
        place(app_hwnd(win), layout(0, 0)[0])
        wait(1.0)
        test_clicks(app, page)
        if CASES == ["clicks"]:
            return
    monitors = P.get("monitors") or list(range(len(result["monitors"])))
    explorer = open_explorer(DIRS["explorer"])
    source = open_explorer(DIRS["source"]) if "in" in CASES else None
    step("Explorer windows opened", explorer and (source or "in" not in CASES), (explorer, source))
    n = 0
    try:
        pairs = [tuple(p) for p in P["pairs"]] if P.get("pairs") else [(a, e) for a in monitors for e in monitors]
        for ma, me in pairs:
            if True:
                n += 1
                tag = f"anySCP on monitor {ma + 1}, Explorer on monitor {me + 1}"
                app_rect, exp_rect = layout(ma, me)
                place(app_hwnd(win), app_rect)
                place(explorer, exp_rect)
                if source:
                    sx0 = exp_rect[0]
                    place(source, (sx0, exp_rect[3] + 20 if exp_rect[3] + 420 < result["monitors"][me]["work"][3]
                                   else exp_rect[1] - 420, exp_rect[2], (exp_rect[3] + 420)
                                   if exp_rect[3] + 420 < result["monitors"][me]["work"][3] else exp_rect[1] - 20))
                wait(1.2)
                user32.SetForegroundWindow(app_hwnd(win))
                wait(0.4)
                exp_drop = (int(exp_rect[0] + (exp_rect[2] - exp_rect[0]) * 0.62),
                            int(exp_rect[1] + (exp_rect[3] - exp_rect[1]) * 0.62))

                if "pane" in CASES and me == monitors[0]:
                    fname = f"up-{n}.txt"
                    with open(os.path.join(DIRS["local"], fname), "w", encoding="utf-8") as fh:
                        fh.write("from the local pane\n" * 100)
                    lp.load(lp.path)
                    spin(lambda: fname in names(lp), 10)
                    lp.select_names({fname})
                    wait(0.3)
                    start = to_screen(win, row_widget(lp, fname), 0.3)
                    target = to_screen(win, rp.column_view, 0.5, 0.8)
                    drag(start, [target])
                    job = None
                    spin(lambda: job_titled(app, f"Uploading {fname}") is not None, 8)
                    job = job_titled(app, f"Uploading {fname}")
                    spin(lambda: finished(job), 30)
                    ok = job is not None and job.state == "done" and \
                        os.path.isfile(os.path.join(DIRS["remote"], fname))
                    if not ok:
                        shot(f"fail-pane-{n}.png")
                    step(f"pane: local -> server pane ({tag.split(',')[0]}; start {start}, drop {target})",
                         ok, job and (job.error or job.state) or "no upload started")
                    spin(lambda: FilePane._active_drag is None, 5)

                if "down" in CASES and me == monitors[0]:
                    fname = f"down-{n}.bin"
                    with open(os.path.join(DIRS["remote"], fname), "wb") as fh:
                        fh.write(os.urandom(150_000))
                    rp.load(rp.path)
                    spin(lambda: fname in names(rp), 10)
                    rp.select_names({fname})
                    wait(0.3)
                    start = to_screen(win, row_widget(rp, fname), 0.3)
                    target = to_screen(win, lp.column_view, 0.5, 0.8)
                    drag(start, [target])
                    spin(lambda: job_titled(app, f"Downloading {fname}") is not None, 8)
                    job = job_titled(app, f"Downloading {fname}")
                    spin(lambda: finished(job), 30)
                    ok = job is not None and job.state == "done" and \
                        os.path.isfile(os.path.join(DIRS["local"], fname)) and not job.subtitle.startswith(
                            "to a temporary folder")
                    if not ok:
                        shot(f"fail-down-{n}.png")
                    step(f"down: server -> local pane ({tag.split(',')[0]})", ok,
                         job and (job.error or job.state, job.subtitle) or "no download started")
                    spin(lambda: FilePane._active_drag is None, 5)

                if "same" in CASES and me == monitors[0]:
                    jobs_before = len(app.transfers.jobs)
                    fname = next(iter(sorted(names(lp))))
                    lp.select_names({fname})
                    wait(0.3)
                    start = to_screen(win, row_widget(lp, fname), 0.3)
                    drag(start, [to_screen(win, lp.column_view, 0.5, 0.9)])
                    wait(1.0)
                    ok = len(app.transfers.jobs) == jobs_before and FilePane._active_drag is None
                    step(f"same: dropping back on the same pane does nothing ({tag.split(',')[0]})", ok,
                         f"{len(app.transfers.jobs) - jobs_before} new jobs")

                if "menu" in CASES and me == monitors[0]:
                    # a context menu opens next to the pointer on this monitor too
                    fname = f"menu-{n}.txt"
                    seen = [("before", rows_on_screen(lp), round(dark_pixels(win, lp), 4))]
                    win_probe.screenshot(os.path.join(DIRS["shots"], f"window-{n}-before.png"), step=1,
                                         region=tuple(int(v) for v in (app_rect[0], app_rect[1],
                                                                       app_rect[2] - app_rect[0],
                                                                       app_rect[3] - app_rect[1])))
                    with open(os.path.join(DIRS["local"], fname), "w", encoding="utf-8") as fh:
                        fh.write("right-clicked\n")
                    lp.load(lp.path)
                    spin(lambda: fname in names(lp), 10)
                    for delay in (0.1, 0.5, 2.0):
                        wait(delay)
                        seen.append((f"+{delay}", rows_on_screen(lp), round(dark_pixels(win, lp), 4)))
                    win_probe.screenshot(os.path.join(DIRS["shots"], f"window-{n}-after.png"), step=1,
                                         region=tuple(int(v) for v in (app_rect[0], app_rect[1],
                                                                       app_rect[2] - app_rect[0],
                                                                       app_rect[3] - app_rect[1])))
                    step(f"menu: the new local file is on the screen ({tag.split(',')[0]})",
                         seen[-1][1] > 0 and seen[-1][2] > 0.001,
                         f"(when, rows on screen, dark pixel share): {seen}")
                    point = to_screen(win, row_widget(lp, fname), 0.3)
                    with physical():
                        user32.SetCursorPos(*point)
                    wait(0.3)
                    user32.mouse_event(0x0008, 0, 0, 0, 0)          # right button down
                    user32.mouse_event(0x0010, 0, 0, 0, 0)          # up
                    wait(1.0)
                    win_probe.screenshot(os.path.join(DIRS["shots"], f"menu-{n}.png"), step=1,
                                         region=(point[0] - 60, point[1] - 60, 520, 560))
                    popovers = [w for w in _all_widgets(win) if isinstance(w, Gtk.Popover) and w.get_visible()]
                    step(f"menu: a right click opens the context menu ({tag.split(',')[0]})", popovers,
                         f"see menu-{n}.png")
                    user32.keybd_event(0x1B, 0, 0, 0)                 # Escape
                    user32.keybd_event(0x1B, 0, 2, 0)
                    wait(0.5)

                if "out" in CASES:
                    fname = f"out-{n}.txt"
                    with open(os.path.join(DIRS["local"], fname), "w", encoding="utf-8") as fh:
                        fh.write("dragged out of anySCP\n" * 100)
                    lp.load(lp.path)
                    spin(lambda: fname in names(lp), 10)
                    lp.select_names({fname})
                    wait(0.3)
                    start = to_screen(win, row_widget(lp, fname), 0.3)
                    edge = (app_rect[2] - 8, start[1])
                    under = window_at(*exp_drop)
                    drag(start, [edge, exp_drop], hold=0.6, closeup=f"icon-{n}.png")
                    dest = os.path.join(DIRS["explorer"], fname)
                    ok = spin(lambda: os.path.isfile(dest), 15)
                    if not ok:
                        shot(f"fail-out-{n}.png")
                    step(f"out: local file -> Explorer ({tag}; drop at {exp_drop} on {under})", ok,
                         "copied" if ok else "nothing arrived in the Explorer folder"
                         + f" (drag thread hook: {bool(win32drag._hook)}, {win32drag.stats})")
                    spin(lambda: FilePane._active_drag is None, 5)

                if "rout" in CASES:
                    fname = f"rout-{n}.bin"
                    with open(os.path.join(DIRS["remote"], fname), "wb") as fh:
                        fh.write(os.urandom(200_000))
                    rp.load(rp.path)
                    spin(lambda: fname in names(rp), 10)
                    rp.select_names({fname})
                    wait(0.3)
                    start = to_screen(win, row_widget(rp, fname), 0.3)
                    edge = (app_rect[2] - 8, start[1])
                    drag(start, [edge, exp_drop], hold=0.8)
                    dest = os.path.join(DIRS["explorer"], fname)
                    ok = spin(lambda: os.path.isfile(dest) and os.path.getsize(dest) == 200_000, 30)
                    if not ok:
                        shot(f"fail-rout-{n}.png")
                    step(f"rout: server file -> Explorer ({tag})", ok,
                         "downloaded and copied" if ok else "nothing arrived in the Explorer folder")
                    spin(lambda: FilePane._active_drag is None, 5)

                if "in" in CASES and source:
                    fname = f"in-{n}.txt"
                    with open(os.path.join(DIRS["source"], fname), "w", encoding="utf-8") as fh:
                        fh.write("dragged in from Explorer\n" * 100)
                    rect = None

                    def found():
                        nonlocal rect
                        rect = item_rect(source, fname)
                        return rect is not None
                    spin(found, 15)
                    if not step(f"in: Explorer shows {fname}", rect, rect):
                        shot(f"fail-in-find-{n}.png")
                        continue
                    start = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
                    target = to_screen(win, rp.column_view, 0.5, 0.8)
                    drag(start, [(start[0] + 30, start[1] + 10), target], hold=0.8)
                    spin(lambda: job_titled(app, f"Uploading {fname}") is not None, 10)
                    job = job_titled(app, f"Uploading {fname}")
                    spin(lambda: finished(job), 30)
                    ok = job is not None and job.state == "done" and \
                        os.path.isfile(os.path.join(DIRS["remote"], fname))
                    if not ok:
                        shot(f"fail-in-{n}.png")
                    step(f"in: Explorer file -> server pane ({tag})", ok,
                         job and (job.error or job.state) or "no upload started")
                spin(lambda: not app.transfers.active, 30)
    finally:
        close_window(explorer)
        close_window(source)


class App(AnySCPApplication):
    def __init__(self):
        super().__init__()
        self.set_flags(Gio.ApplicationFlags.NON_UNIQUE)


def main():
    print(json.dumps({"monitors": result["monitors"], "system_dpi": result["system_dpi"]}), flush=True)
    app = App()

    def go():
        try:
            run(app)
        except Exception:  # noqa: BLE001
            step("scenario", False, traceback.format_exc())
        result["ok"] = bool(result["steps"]) and all(s["ok"] for s in result["steps"])
        with open(os.path.join(WORK, "result.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1)
        for page in app.window.pages():
            page.shutdown()
        app.quit()
        return False

    app.connect("activate", lambda *_a: GLib.timeout_add(1500, go))
    try:
        app.run([sys.argv[0]])
    finally:
        SERVER.terminate()


main()
