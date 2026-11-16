# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Open a saved site the way the start page does, and report what the
server pane then shows (for a bug that only shows with someone's own site).

    <runtime>\\bin\\python.exe -s site_diag.py <config folder> <site name> <work folder>

The config folder (sites.json, settings.json, known_hosts) is copied into
the work folder first, so the original is not changed.  Passwords come from
the system's password store, as in the app.  Prints a report and saves
<work>/window.png.
"""

import json
import os
import shutil
import sys
import time
import traceback

SOURCE, SITE_NAME, WORK = sys.argv[1], sys.argv[2], sys.argv[3]
shutil.rmtree(WORK, ignore_errors=True)
shutil.copytree(SOURCE, os.path.join(WORK, "config"))
os.makedirs(os.path.join(WORK, "cache"), exist_ok=True)
os.environ.update({"ANYSCP_CONFIG_DIR": os.path.join(WORK, "config"),
                   "ANYSCP_CACHE_DIR": os.path.join(WORK, "cache")})
if "GI_TYPELIB_PATH" not in os.environ:
    rt = sys.prefix
    lib = os.path.join(rt, "Library") if os.path.isdir(os.path.join(rt, "Library")) else rt
    os.environ.update({
        "GI_TYPELIB_PATH": os.path.join(lib, "lib", "girepository-1.0"),
        "XDG_DATA_DIRS": os.path.join(lib, "share"),
        "GSETTINGS_SCHEMA_DIR": os.path.join(lib, "share", "glib-2.0", "schemas"),
        "GDK_PIXBUF_MODULE_FILE": os.path.join(lib, "lib", "gdk-pixbuf-2.0", "2.10.0", "loaders.cache"),
        "GSETTINGS_BACKEND": "memory",
    })

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, Gtk  # noqa: E402,F401

from anyscp.app import AnySCPApplication  # noqa: E402

T0 = time.monotonic()


def say(text):
    print(f"{time.monotonic() - T0:6.1f}s {text}", flush=True)


def spin(cond, timeout):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    tick = GLib.timeout_add(20, lambda: True)
    try:
        while time.monotonic() < end:
            if cond():
                return True
            ctx.iteration(True)
        return cond()
    finally:
        GLib.source_remove(tick)


def run(app):
    win = app.window
    site = next((s for s in app.sites.sites if s.get("name") == SITE_NAME), None)
    if site is None:
        say(f"no site named {SITE_NAME!r}: {[s.get('name') for s in app.sites.sites]}")
        return
    say("site: " + json.dumps({k: site.get(k) for k in ("protocol", "host", "port", "user", "auth",
                                                         "remote_dir", "save_password")}))
    win.open_session(dict(site))                    # what clicking it on the start page does
    page = win.current_page()
    connected = spin(lambda: page.session.state in ("connected", "failed") or win.get_visible_dialog(), 60)
    dialog = win.get_visible_dialog()
    if dialog is not None:
        say(f"a dialog is asking: {dialog.get_heading() if hasattr(dialog, 'get_heading') else dialog}")
        return
    say(f"session: {page.session.state} {page.session.error!r} (waited: {connected})")
    if page.session.state != "connected":
        return
    rp = page.remote_pane
    spin(lambda: rp._load_serial > 0 and rp._loaded_serial == rp._load_serial, 30)
    time.sleep(0.2)
    spin(lambda: False, 1.5)
    say(f"server pane: path={rp.path!r} entries={len(rp.entries)} rows={rp.sort_model.get_n_items()}")
    say(f"  status page shown={rp.status_page.get_visible()} title={rp.status_page.get_title()!r} "
        f"text={rp.status_page.get_description()!r}; list shown={rp.scroller.get_visible()}")
    say(f"  overlay height={rp.overlay.get_height()} status page height={rp.status_page.get_height()} "
        f"pane height={rp.get_height()}")
    fs = page.session.fs
    say(f"  home={fs.home()!r} protocol={fs.protocol}")
    for path in (site.get("remote_dir") or "", fs.home()):
        if not path:
            continue
        try:
            entries = fs.listdir(path)
            say(f"  listing {path!r} directly: {len(entries)} entries")
        except Exception as exc:  # noqa: BLE001
            say(f"  listing {path!r} directly: {type(exc).__name__}: {exc}")
    for line in page.session.log_lines[-12:]:
        say("  log: " + line)
    try:
        gi.require_version("GdkWin32", "4.0")
        from gi.repository import GdkWin32 as _w
        say(f"GdkWin32.Win32Surface handle functions: {[a for a in dir(_w.Win32Surface) if 'handle' in a]}")
    except Exception as exc:  # noqa: BLE001
        say(f"GdkWin32: {exc}")
    try:
        import win_probe
        surface = win.get_surface()
        gi.require_version("GdkWin32", "4.0")
        from gi.repository import GdkWin32
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        user32 = ctypes.WinDLL("user32")
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        user32.GetWindowRect(wintypes.HWND(GdkWin32.Win32Surface.get_handle(surface)), ctypes.byref(r))
        user32.SetThreadDpiAwarenessContext(old)
        win_probe.screenshot(os.path.join(WORK, "window.png"), step=1,
                             region=(r.left, r.top, r.right - r.left, r.bottom - r.top))
        say("screenshot: window.png")
    except Exception:  # noqa: BLE001
        say("screenshot failed: " + traceback.format_exc(limit=2))


class App(AnySCPApplication):
    def __init__(self):
        super().__init__()
        self.set_flags(Gio.ApplicationFlags.NON_UNIQUE)


def main():
    app = App()

    def go():
        try:
            run(app)
        except Exception:  # noqa: BLE001
            say("failed: " + traceback.format_exc())
        for page in app.window.pages():
            page.shutdown()
        app.quit()
        return False
    app.connect("activate", lambda *_a: GLib.timeout_add(1500, go))
    app.run([sys.argv[0]])


main()
