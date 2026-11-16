# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""GUI test executed *inside* anySCP's packaged runtime (started by
run_ui_tests.py, with the AppImage's Python or the Windows runtime under
Wine).  It drives the real application against a throwaway SFTP server:

  A. a new site is saved when "Log In" is pressed
  B. the transfer queue: a strip along the bottom of the window that opens
     when a transfer starts and closes 2 s after the last one has finished
     (not after a failure; not when opened by hand while nothing runs)
  C. dragging files out to another program: the data that program is given
     (file URIs on Linux, a Shell IDList Array on Windows), the download of
     server files to a temporary folder, its reuse and its cancellation,
     and the clean-up of temporary folders
  D. (X11 only) the same with a real pointer: a drag between the panes, and
     a drag out of the window onto a stand-in file manager

Parameters come as JSON in ANYSCP_UI_TEST; the result is written as JSON to
the "out" path.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import traceback

P = json.loads(os.environ["ANYSCP_UI_TEST"])
SLOW = float(P.get("slow", 1))          # > 1 under emulation
ONLY = set(P.get("only") or [])         # run only these parts (after A): queue, drag, pointer

if sys.platform == "win32" and "GI_TYPELIB_PATH" not in os.environ:
    # the environment the native launcher sets up (conda or MSYS2 layout)
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
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

from anyscp import compat  # noqa: E402
from anyscp.app import AnySCPApplication  # noqa: E402
from anyscp.config import CONFIG_DIR  # noqa: E402
from anyscp.transfer import AutoDecider  # noqa: E402
from anyscp.ui import dragout  # noqa: E402
from anyscp.ui.file_pane import FilePane  # noqa: E402
from anyscp.ui.site_dialog import AUTH_MODES, SiteDialog  # noqa: E402
from anyscp.ui.window import QUEUE_HIDE_DELAY_MS  # noqa: E402

result = {"ok": False, "steps": []}
T0 = time.monotonic()
TEMP_JOB = "to a temporary folder"      # subtitle of the downloads behind a drag

# Temporary drag folders of "earlier runs", made before anySCP starts: the
# stale one must be removed at start, the fresh one (another instance's) kept.
STALE = dragout.temp_root() / "1-1"
FRESH = dragout.temp_root() / "2-1"
for _folder, _age in ((STALE, 13 * 3600), (FRESH, 60)):
    os.makedirs(_folder / "x", exist_ok=True)
    open(_folder / "x" / "f.txt", "w").close()
    os.utime(_folder, (time.time() - _age, time.time() - _age))


def step(name, ok, detail=""):
    t = round(time.monotonic() - T0, 1)
    result["steps"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:600], "t": t})
    print(f"{t:7.1f}s " + ("OK   " if ok else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)
    return ok


def spin(cond, timeout):
    """Run the main loop until cond() is true (or the timeout passes)."""
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
    """Let the main loop run for a while (not scaled)."""
    end = time.monotonic() + seconds
    spin(lambda: time.monotonic() >= end, seconds + 1)


def screenshot(widget, name):
    try:
        w, h = widget.get_width(), widget.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(widget).snapshot(snap, w, h)
        node = snap.to_node()
        if node is not None:
            tex = widget.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(os.path.join(P["shots"], name))
    except Exception as exc:  # noqa: BLE001
        step(f"screenshot {name}", False, exc)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def names(pane):
    return {e.name for e in pane.entries}


def info_of(pane, name):
    return next(e for e in pane.entries if e.name == name)


def finished(job):
    return job is not None and job.state in ("done", "failed", "cancelled")


def job_titled(app, title):
    return next((j for j in reversed(app.transfers.jobs) if j.title == title), None)


def temp_jobs(app):
    return [j for j in app.transfers.jobs if j.subtitle.startswith(TEMP_JOB)]


def row_widget(pane, name):
    """The widget of the row showing ``name`` (its first cell)."""
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


def rows_on_screen(pane) -> int:
    """How many files the pane's list really shows: mapped row widgets with a
    file in them (not what its model holds)."""
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


def files_shown(page, name, shot=None):
    """After a connection: the server's files appear without a refresh."""
    spin(lambda: name in names(page.remote_pane), 30)
    spin(lambda: rows_on_screen(page.remote_pane) > 0, 5)
    if shot:
        screenshot(page.window, shot)
    shown = rows_on_screen(page.remote_pane)
    return shown > 0, f"{shown} rows on screen for {len(page.remote_pane.entries)} files"


def point_in(widget, relative_to, fx=0.5, fy=0.5):
    ok, pt = widget.compute_point(relative_to, Graphene.Point().init(widget.get_width() * fx,
                                                                      widget.get_height() * fy))
    assert ok
    return pt.x, pt.y


def select(pane, name):
    pane.select_names({name})


def ask_like_another_program(provider, timeout):
    """Request the drag's files the way a drop target of another program
    does: as a stream of dragout.MIME_TYPE.  → (bytes | None, error text)"""
    clipboard = Gdk.Display.get_default().get_clipboard()
    clipboard.set_content(provider)
    got = {}

    def chunk(stream, res, acc):
        try:
            data = stream.read_bytes_finish(res).get_data() or b""
        except GLib.Error as exc:
            got["error"] = exc.message
            return
        if data:
            acc.append(data)
            stream.read_bytes_async(65536, 0, None, chunk, acc)
        else:
            got["data"] = b"".join(acc)

    def opened(clip, res):
        try:
            stream, _mime = clip.read_finish(res)
        except GLib.Error as exc:
            got["error"] = exc.message
            return
        stream.read_bytes_async(65536, 0, None, chunk, [])

    clipboard.read_async([dragout.MIME_TYPE], 0, None, opened)
    spin(lambda: got, timeout)
    clipboard.set_content(None)
    data = got.get("data")
    return (data if data else None), got.get("error", "" if data else "nothing was sent")


def paths_in(data: bytes) -> list[str]:
    """Decode what ask_like_another_program() received into native paths."""
    if dragout.MIME_TYPE == dragout.URI_LIST:
        return [Gio.File.new_for_uri(line).get_path() for line in data.decode().split("\r\n") if line]
    import ctypes
    import struct
    shell32 = ctypes.windll.shell32
    shell32.ILCombine.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    shell32.ILCombine.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    count = struct.unpack_from("<I", data)[0]
    offsets = struct.unpack_from(f"<{count + 1}I", data, 4)
    found = []
    for off in offsets[1:]:
        pidl = shell32.ILCombine(data[offsets[0]:], data[off:])
        buf = ctypes.create_unicode_buffer(1024)
        shell32.SHGetPathFromIDListW(pidl, buf)
        shell32.ILFree(pidl)
        found.append(buf.value)
    return found


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# ---------------------------------------------------------------------------- A

def test_login_saves(app):
    win = app.window
    step("a stale temporary drag folder of an earlier run is removed at start", not STALE.exists())
    step("... a fresh one (of another instance) is kept", FRESH.exists())
    dlg = SiteDialog(win, select_new=True)
    dlg.present(win)
    wait(0.6)
    dlg.host_row.set_text(P["ssh"]["host"])
    dlg.port_row.set_value(P["ssh"]["port"])
    dlg.user_row.set_text(P["ssh"]["user"])
    dlg.auth_row.set_selected(AUTH_MODES.index("key"))
    dlg._set_key(P["ssh"]["key"])
    dlg.remote_dir_row.set_text(P["remote_dir"])
    dlg._set_local_dir(P["local_dir"])
    step("the site is new and unsaved", dlg.is_new and not app.sites.sites, app.sites.sites)
    dlg.login_btn.emit("clicked")
    saved = [s for s in app.sites.sites if s.get("host") == P["ssh"]["host"] and s.get("port") == P["ssh"]["port"]]
    step("Log In saves the new site", len(saved) == 1 and saved[0].get("name"), app.sites.sites)
    path = CONFIG_DIR / "sites.json"
    on_disk = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    step("... into sites.json", any(s.get("host") == P["ssh"]["host"] for s in on_disk.get("sites", [])), path)
    page = win.current_page()
    ok = page is not None and spin(lambda: page.session.state == "connected"
                                   and page.remote_pane.path and page.local_pane.path, 90)
    step("the session of the saved site connects", ok, page and (page.session.error or page.session.state))
    if ok:
        step("... and is that site's session", saved and page.site.get("id") == saved[0]["id"])
        step("... and shows the server's files without a refresh", *files_shown(page, "big.bin", "login.png"))
    return page if ok else None


def describe(page):
    rp = page.remote_pane
    return (f"state={page.session.state} error={page.session.error!r} remote={rp.path if rp else None} "
            f"entries={len(rp.entries) if rp else None} log={page.session.log_lines[-4:]}")


def test_saved_password_site(app):
    """A site with a saved password, opened again from the saved sites: the
    password then comes from the system's password store."""
    from anyscp.config import secrets
    win = app.window
    pw = P["pw_ssh"]
    dlg = SiteDialog(win, select_new=True)
    dlg.present(win)
    wait(0.6)
    dlg.name_row.set_text("Password site")
    dlg.host_row.set_text(pw["host"])
    dlg.port_row.set_value(pw["port"])
    dlg.user_row.set_text(pw["user"])
    dlg.auth_row.set_selected(AUTH_MODES.index("password"))
    dlg.password_row.set_text(pw["password"])
    dlg.save_pw_row.set_active(True)
    dlg.login_btn.emit("clicked")
    page = win.current_page()

    def listed(pg):
        return pg.session.state == "connected" and pg.remote_pane is not None and \
            "small.txt" in names(pg.remote_pane)
    step("password site: Log In connects and lists the server", spin(lambda: listed(page), 60), describe(page))
    step("password site: ... and shows the files without a refresh", *files_shown(page, "small.txt"))
    site_id = page.site.get("id")
    step(f"password site: the password is in the password store ({secrets.backend_name})",
         secrets.lookup(site_id) == pw["password"])
    win.close_page(page, force=True)
    spin(lambda: page not in win.pages(), 10)
    secrets._memory.pop(site_id, None)             # as after a restart: read from the store
    win.open_session(dict(app.sites.get(site_id)))  # what clicking it on the start page does
    again = win.current_page()
    step("password site: opening it again from the saved sites lists the server",
         spin(lambda: listed(again), 60), describe(again))
    step("password site: ... and shows the files without a refresh", *files_shown(again, "small.txt", "reopened.png"))
    win.close_page(again, force=True)
    spin(lambda: again not in win.pages(), 10)


def test_relative_remote_dir(app):
    """A saved site whose remote directory is relative to the home folder
    ("southserve" instead of "/home/me/southserve"), opened from the start
    page: the server pane shows that folder's files without a refresh."""
    from anyscp import config as cfg
    win = app.window
    ssh = P["ssh"]
    import posixpath
    relative = posixpath.relpath(P["remote_dir"], P["ssh_home"])
    site = cfg.new_site(name="Relative folder", protocol="sftp", host=ssh["host"], port=ssh["port"],
                        user=ssh["user"], auth="key", key_file=ssh["key"], remote_dir=relative)
    saved = app.sites.upsert(site)
    win.sites_changed()
    win.open_session(dict(saved))
    page = win.current_page()
    ok = spin(lambda: page.session.state == "connected" and page.remote_pane is not None
              and page.remote_pane.path is not None, 30)
    shown = ok and spin(lambda: "small.txt" in names(page.remote_pane) and rows_on_screen(page.remote_pane) > 0, 10)
    screenshot(win, "relative-remote-dir.png")
    rp = page.remote_pane
    step(f"a site with a relative remote directory ({relative}) shows its files without a refresh", shown,
         describe(page) + f" path={rp and rp.path!r} rows={rp and rows_on_screen(rp)}")
    win.close_page(page, force=True)
    spin(lambda: page not in win.pages(), 10)

    # a remote directory that does not exist
    missing = dict(saved, remote_dir="southserve-missing")
    win.open_session(missing)
    page = win.current_page()
    spin(lambda: page.session.state == "connected" and page.remote_pane is not None, 30)
    rp = page.remote_pane
    home = P["ssh_home"]
    shown = spin(lambda: rp.path == home and rp.entries and rows_on_screen(rp) > 0, 20)
    screenshot(win, "missing-remote-dir.png")
    step("a site whose remote directory does not exist opens the home folder instead", shown,
         f"path={rp.path!r} entries={len(rp.entries)} rows={rows_on_screen(rp)}")
    step("... and says why", any("southserve-missing" in line and "instead" in line
                                 for line in page.session.log_lines), page.session.log_lines[-4:])

    # a pane that cannot show any folder at all: the error is on the screen
    rp.path = None
    rp.load("/surely/not/there")
    spin(lambda: rp.status_page.get_visible(), 15)
    wait(0.8)
    screenshot(win, "folder-error.png")
    step("a folder error is shown in full, not squeezed out of sight",
         rp.status_page.get_visible() and rp.status_page.get_height() > 150 and rp.overlay.get_height() > 150,
         f"status page {rp.status_page.get_height()} px, overlay {rp.overlay.get_height()} px, "
         f"text {rp.status_page.get_description()!r}")
    win.close_page(page, force=True)
    spin(lambda: page not in win.pages(), 10)


def test_proxy_and_jump_site(app):
    """A site with a SOCKS5 proxy and a jump host, made in the site dialog:
    Log In goes through both, and both are saved with the site."""
    from anyscp.backends.proxy import PROXY_TYPES
    win = app.window
    ssh, proxy = P["ssh"], P["proxy"]
    jump = f"{ssh['user']}@127.0.0.1:{ssh['port']}"
    dlg = SiteDialog(win, select_new=True)
    dlg.present(win)
    wait(0.6)
    dlg.name_row.set_text("Through a jump host")
    dlg.host_row.set_text(ssh["host"])
    dlg.port_row.set_value(ssh["port"])
    dlg.user_row.set_text(ssh["user"])
    dlg.auth_row.set_selected(AUTH_MODES.index("key"))
    dlg._set_key(ssh["key"])
    dlg.remote_dir_row.set_text(P["remote_dir"])
    dlg.proxy_type_row.set_selected(PROXY_TYPES.index("socks5"))
    dlg.proxy_host_row.set_text("127.0.0.1")
    dlg.proxy_port_row.set_value(proxy["port"])
    dlg.jump_row.set_text(jump)
    dlg.proxy_expander.set_expanded(True)
    dlg.jump_expander.set_expanded(True)
    wait(0.5)
    scroller = next((w for w in all_widgets(dlg.page) if isinstance(w, Gtk.ScrolledWindow)), None)
    if scroller is not None:
        adj = scroller.get_vadjustment()
        adj.set_value(adj.get_upper())
        wait(0.4)
    screenshot(dlg, "site-dialog-proxy-jump.png")
    step("proxy + jump: the dialog sums the settings up",
         f"SOCKS5 · 127.0.0.1:{proxy['port']}" in dlg.proxy_expander.get_subtitle()
         and jump in dlg.jump_expander.get_subtitle(),
         (dlg.proxy_expander.get_subtitle(), dlg.jump_expander.get_subtitle()))
    dlg.login_btn.emit("clicked")
    page = win.current_page()
    ok = page is not None and spin(lambda: page.session.state == "connected" and page.remote_pane is not None
                                   and "small.txt" in names(page.remote_pane), 60)
    step("proxy + jump: Log In connects and lists the server", ok, page and describe(page))
    if page is None:
        return
    with open(proxy["targets"], encoding="utf-8") as fh:
        targets = fh.read().split()
    step("proxy + jump: the proxy carried the connection to the jump host",
         f"127.0.0.1:{ssh['port']}" in targets, targets)
    step("proxy + jump: ... and the jump host the one to the site",
         any(f"Opening a tunnel through 127.0.0.1 to {ssh['host']}:{ssh['port']}" in line
             for line in page.session.log_lines), page.session.log_lines[-8:])
    saved = app.sites.get(page.site["id"]) or {}
    step("proxy + jump: both are saved with the site",
         (saved.get("proxy_type"), saved.get("proxy_host"), saved.get("proxy_port"), saved.get("jump_hosts"))
         == ("socks5", "127.0.0.1", proxy["port"], jump),
         {k: saved.get(k) for k in ("proxy_type", "proxy_host", "proxy_port", "jump_hosts")})
    win.close_page(page, force=True)
    spin(lambda: page not in win.pages(), 10)


# ---------------------------------------------------------------------------- B

def test_queue(app, page):
    win, lp, rp = app.window, page.local_pane, page.remote_pane
    delay = QUEUE_HIDE_DELAY_MS / 1000
    step("the queue is closed while nothing is transferred", not win.queue_btn.get_active())
    # When the app learnt that a job had finished, and when the queue opened or closed
    seen_finished, toggled = {}, []
    app.transfers.connect("job-finished", lambda _m, j: seen_finished.setdefault(j.id, time.time()))
    win.queue_btn.connect("notify::active", lambda b, _p: toggled.append((b.get_active(), time.time())))

    def closed_after(job):
        closes = [t for active, t in toggled if not active and t >= seen_finished.get(job.id, 1e18)]
        return closes[0] - seen_finished[job.id] if closes else None

    page.start_transfer(rp, [info_of(rp, "big.bin")], lp, lp.path)
    job = job_titled(app, "Downloading big.bin")
    step("the queue opens when a transfer starts", spin(lambda: win.queue_btn.get_active(), 2))

    def laid_out():                             # (after the reveal animation)
        ok, rect = win.queue_panel.compute_bounds(win)
        return ok and rect.size.height > 30 and abs(rect.origin.y + rect.size.height - win.get_height()) <= 2
    spin(laid_out, 5)
    ok, rect = win.queue_panel.compute_bounds(win)
    bottom, width, height = rect.origin.y + rect.size.height, rect.size.width, rect.size.height
    step("the queue is a strip along the bottom, over the full width",
         ok and abs(bottom - win.get_height()) <= 2 and abs(width - win.get_width()) <= 2
         and 30 < height < win.get_height() * 0.4 and rect.origin.x <= 1,
         f"x={rect.origin.x:.0f} y={rect.origin.y:.0f} {width:.0f}x{height:.0f} in "
         f"{win.get_width()}x{win.get_height()}")
    ok2, pane_rect = page.compute_bounds(win)
    step("the panes end where the queue begins",
         ok2 and pane_rect.origin.y + pane_rect.size.height <= rect.origin.y + 1,
         f"panes end at {pane_rect.origin.y + pane_rect.size.height:.0f}, queue starts at {rect.origin.y:.0f}")
    minimum = win.queue_panel.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
    step("with a transfer in it the queue still fits the smallest window (640 px)", minimum <= 640,
         f"needs {minimum} px")
    screenshot(win, "queue-running.png")
    spin(lambda: finished(job), 180)
    step("download", job.state == "done", job.error or job.state)
    step("the queue is still open right after the transfer", win.queue_btn.get_active())
    spin(lambda: not win.queue_btn.get_active(), delay + 4)
    waited = closed_after(job)
    step(f"the queue closes {delay:g} s after the last transfer finished",
         waited is not None and delay - 0.1 <= waited <= delay + 0.5 * SLOW,
         f"after {waited:.2f} s" if waited is not None else "it did not close")
    wait(0.5)
    screenshot(win, "queue-closed.png")

    # two transfers: it closes after the second one only
    page.start_transfer(rp, [info_of(rp, "small.txt")], lp, lp.path)
    first = job_titled(app, "Downloading small.txt")
    spin(lambda: finished(first), 60)
    wait(delay / 2)
    page.start_transfer(rp, [info_of(rp, "docs")], lp, lp.path)
    second = job_titled(app, "Downloading docs")
    spin(lambda: finished(second), 60)
    closed_early = spin(lambda: not win.queue_btn.get_active(), (delay - 0.5) / SLOW)
    step("a transfer started meanwhile restarts the wait", not closed_early and second.state == "done",
         f"closed {time.time() - second.finished_at:.2f} s after the second transfer" if closed_early
         else second.error or "")
    spin(lambda: not win.queue_btn.get_active(), 4)
    step("... and it closes after the second transfer", not win.queue_btn.get_active())

    # a failed transfer stays in view until it is dealt with
    decider, app.transfers.decider = app.transfers.decider, AutoDecider(on_error="abort")
    try:
        page.start_transfer(rp, [info_of(rp, "small.txt")], lp, compat.to_internal(P["ro_dir"]))
        failed = job_titled(app, "Downloading small.txt")
        spin(lambda: finished(failed), 60)
        wait(delay + 1.5)
        step("the queue stays open after a failed transfer",
             failed.state == "failed" and win.queue_btn.get_active(), failed.error or failed.state)
        screenshot(win, "queue-failed.png")
        app.transfers.remove(failed)
        spin(lambda: not win.queue_btn.get_active(), delay + 3)
        step("... and closes once the failed transfer is removed", not win.queue_btn.get_active())
    finally:
        app.transfers.decider = decider

    # opened by hand while idle: stays
    win.activate_action("win.toggle-queue", None)
    wait(delay + 1.5)
    step("the queue stays open when opened by hand", win.queue_btn.get_active())
    screenshot(win, "queue-by-hand.png")
    win.queue_panel.close_btn.emit("clicked")
    step("its close button hides it", spin(lambda: not win.queue_btn.get_active(), 2))


# ---------------------------------------------------------------------------- C

def drag_prepare(pane, name):
    select(pane, name)
    wait(0.3)
    x, y = point_in(row_widget(pane, name), pane.column_view)
    return pane._on_drag_prepare(None, x, y)


def test_drag_data(app, page):
    win, lp, rp = app.window, page.local_pane, page.remote_pane
    temp = str(dragout.temp_root())

    # a server file: downloaded to a temporary folder, then handed over
    provider = drag_prepare(rp, "small.txt")
    offered = provider.ref_formats().union_serialize_mime_types() if provider is not None else None
    step("a drag from the server pane offers files to other programs",
         offered is not None and offered.contain_mime_type(dragout.MIME_TYPE), offered and offered.to_string())
    step("nothing is downloaded while the drag stays inside the window",
         rp._drag_download is not None and rp._drag_download.state == "idle" and not temp_jobs(app))
    data, error = ask_like_another_program(provider, 60)
    got = paths_in(data) if data else []
    ok = len(got) == 1 and os.path.basename(got[0]) == "small.txt" and os.path.isfile(got[0])
    step("the other program is given the downloaded file", ok, error or got)
    if ok:
        step("... complete, in the temporary folder",
             same_path(os.path.dirname(os.path.dirname(got[0])), temp) and sha256(got[0]) == P["sha"]["small.txt"],
             got[0])
    rp._on_drag_end()
    step("the file stays after the drop (the other program copies it)", ok and os.path.isfile(got[0]))
    step("the pane is not in a drag any more", FilePane._active_drag is None)

    # dragging the same file again reuses the download
    jobs_before = len(app.transfers.jobs)
    provider = drag_prepare(rp, "small.txt")
    data, error = ask_like_another_program(provider, 10)
    again = paths_in(data) if data else []
    rp._on_drag_end()
    step("a second drag of the same file reuses the download",
         again == got and len(app.transfers.jobs) == jobs_before, error or again)

    # a folder
    provider = drag_prepare(rp, "docs")
    data, error = ask_like_another_program(provider, 60)
    got = paths_in(data) if data else []
    ok = len(got) == 1 and os.path.isdir(got[0]) and sorted(os.listdir(got[0])) == sorted(P["docs"]) and \
        all(sha256(os.path.join(got[0], n)) == P["sha"][n] for n in P["docs"])
    step("a folder is handed over with its content", ok, error or got)
    rp._on_drag_end()

    # the pointer leaves the window, the drag is cancelled: download stopped, nothing left
    provider = drag_prepare(rp, "huge.bin")
    FilePane.drag_left_window()
    download = rp._drag_download
    started = spin(lambda: download.state == "running", 3)
    step("leaving the window starts the download, shown in the queue",
         started and win.queue_btn.get_active(), download.state)
    rp._on_drag_cancel(None, None, None)
    rp._on_drag_end()
    spin(lambda: download.state == "failed", 30)
    wait(0.5)
    step("a cancelled drag stops the download and removes its files",
         download.state == "failed" and not os.path.exists(str(download.dir))
         and download.job not in app.transfers.jobs, f"{download.state}, {download.dir}")

    # the pointer leaves and comes back in time: no download
    provider = drag_prepare(rp, "notyet.txt")
    FilePane.drag_left_window()
    FilePane.drag_entered_window()
    wait(0.5)
    step("coming back into the window in time starts no download", rp._drag_download.state == "idle",
         rp._drag_download.state)
    rp._on_drag_end()

    if compat.IS_WINDOWS:
        # Explorer waits for the data after a drop; one that would wait
        # longer than GTK allows is refused, the download carries on.
        provider = drag_prepare(rp, "huge.bin")
        download = rp._drag_download
        download.start()
        spin(lambda: download.job.state == "running" and download.job.total_bytes > 0, 20)
        wait(2.0)                                           # speed samples
        t0 = time.monotonic()
        data, error = ask_like_another_program(provider, 40)
        step("Windows: a drop that would have to wait too long is refused at once",
             data is None and download.announce and download.state == "running"
             and time.monotonic() - t0 < 10, f"{error}, {download.state}, {time.monotonic() - t0:.1f} s")
        rp._on_drag_cancel(None, None, None)
        rp._on_drag_end()
        wait(0.5)
        step("... and its download carries on for the next drag", download.state == "running", download.state)
        app.transfers.cancel(download.job)
        spin(lambda: download.state == "failed", 30)

    # a local file: handed over as it is
    spin(lambda: "big.bin" in names(lp), 20)
    provider = drag_prepare(lp, "big.bin")
    formats = provider.ref_formats() if provider is not None else None
    step("a drag from the local pane offers its files to other programs",
         formats is not None and (Gdk.FileList.__gtype__ in formats.get_gtypes()),
         formats and formats.to_string())
    if compat.IS_WINDOWS:
        data, error = ask_like_another_program(provider, 10)
        got = paths_in(data) if data else []
        step("Windows: Explorer is given the local file itself",
             len(got) == 1 and same_path(got[0], compat.to_native(info_of(lp, "big.bin").path)), error or got)
    lp._on_drag_end()
    spin(lambda: not app.transfers.active, 30)
    spin(lambda: not win.queue_btn.get_active(), 6)


# ---------------------------------------------------------------------------- D

def test_pointer(app, page):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    gi.require_version("GdkX11", "4.0")
    from gi.repository import GdkX11
    from xpointer import XPointer

    win, lp, rp = app.window, page.local_pane, page.remote_pane
    pointer = XPointer()
    xid = GdkX11.X11Surface.get_xid(win.get_surface())
    pointer.place(xid, 0, 0)
    wait(0.5)
    ox, oy = pointer.origin(xid)
    sx, sy = win.get_surface_transform()

    def screen(widget, fx=0.5, fy=0.5):
        x, y = point_in(widget, win, fx, fy)
        return ox + sx + x, oy + sy + y

    def drag(start, stops, hold=0.4):
        pointer.move(*start)
        wait(0.3)
        pointer.button(True)
        wait(0.3)
        x0, y0 = start
        for x1, y1 in stops:
            for i in range(1, 16):
                pointer.move(x0 + (x1 - x0) * i / 15, y0 + (y1 - y0) * i / 15)
                wait(0.03 * SLOW)
            x0, y0 = x1, y1
            wait(hold * SLOW)
        pointer.button(False)
        wait(0.3 * SLOW)

    # 1. between the panes: a normal download, no temporary folder involved
    temp_before = len(temp_jobs(app))
    select(rp, "pointer.txt")
    wait(0.3)
    drag(screen(row_widget(rp, "pointer.txt"), 0.3), [screen(lp.column_view, 0.5, 0.85)])
    spin(lambda: job_titled(app, "Downloading pointer.txt") is not None, 5)
    job = job_titled(app, "Downloading pointer.txt")
    spin(lambda: finished(job), 60)
    target = os.path.join(P["local_dir"], "pointer.txt")
    step("pointer: dragging from the server pane onto the local pane downloads there",
         job is not None and job.state == "done" and os.path.isfile(target)
         and sha256(target) == P["sha"]["pointer.txt"], job and (job.error or job.state, job.subtitle))
    step("pointer: ... without a temporary download", len(temp_jobs(app)) == temp_before)
    spin(lambda: not win.queue_btn.get_active(), 6)

    # dropped back on the side it came from: nothing happens, dragging still works
    jobs_before = len(app.transfers.jobs)
    select(rp, "notyet.txt")
    wait(0.3)
    drag(screen(row_widget(rp, "notyet.txt"), 0.3), [screen(rp.column_view, 0.5, 0.9)])
    wait(1.0)
    step("pointer: dropping back on the same side does nothing",
         len(app.transfers.jobs) == jobs_before and FilePane._active_drag is None)

    # 2. out of the window, onto another program
    drop_out = P["drop_out"]
    proc = subprocess.Popen(P["drop_target_cmd"] + [drop_out])
    try:
        def target_state():
            try:
                with open(drop_out, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                return {}
        if not step("pointer: the stand-in file manager is up", spin(lambda: "xid" in target_state(), 60)):
            return
        txid = target_state()["xid"]
        tx, ty = ox + win.get_width() + 60, 120
        pointer.place(txid, tx, ty)
        wait(0.8)
        select(rp, "dragged.bin")
        wait(0.3)
        start = screen(row_widget(rp, "dragged.bin"), 0.3)
        edge = (ox + sx + win.get_width() - 6, start[1])
        drag(start, [edge, (tx + 190, ty + 150)], hold=0.6)
        got = spin(lambda: "files" in target_state(), 120)
        files = target_state().get("files", [])
        step("pointer: dragging a server file onto another program hands it over",
             got and len(files) == 1 and files[0].get("sha256") == P["sha"]["dragged.bin"]
             and os.path.basename(files[0]["path"]) == "dragged.bin", files or "nothing was dropped")
        temp_job = job_titled(app, "Downloading dragged.bin")
        step("pointer: ... downloaded to a temporary folder first, shown in the queue",
             temp_job is not None and temp_job.state == "done" and temp_job.subtitle.startswith(TEMP_JOB),
             temp_job and temp_job.subtitle)
        screenshot(win, "drag-out.png")
        step("pointer: the drag is over", spin(lambda: FilePane._active_drag is None, 5))
    finally:
        proc.terminate()

    class Mouse:
        move = staticmethod(pointer.move)

        @staticmethod
        def down():
            pointer.button(True)

        @staticmethod
        def up():
            pointer.button(False)
    test_pane_features(app, page, screen, Mouse, "pointer")


def all_widgets(root):
    found, stack = [], [root]
    while stack:
        widget = stack.pop()
        found.append(widget)
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


def answer_dialog(win, response, timeout=6):
    """Answer the dialog the window shows (as its button would); its heading."""
    spin(lambda: win.get_visible_dialog() is not None, timeout)
    dialog = win.get_visible_dialog()
    if dialog is None:
        return None
    heading = dialog.get_heading() if isinstance(dialog, Adw.AlertDialog) else dialog.get_title()
    dialog.emit("response", response)
    dialog.force_close()
    wait(0.3)
    return heading


def active_rows(pane):
    """Row widgets GTK marks :active (inside a rubber band while it is drawn)."""
    found, stack = [], [pane.column_view]
    while stack:
        widget = stack.pop()
        if widget.get_css_name() == "row" and widget.get_state_flags() & Gtk.StateFlags.ACTIVE:
            found.append(widget)
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


def has_rubberband(pane):
    stack = [pane.column_view]
    while stack:
        widget = stack.pop()
        if widget.get_css_name() == "rubberband":
            return True
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return False


def test_pane_features(app, page, screen, mouse, label):
    """With the real pointer: the panes' copy buttons, rubber-band selection
    from empty space, and the highlight of where dragged files would go."""
    win, lp, rp = app.window, page.local_pane, page.remote_pane
    local = compat.to_native(lp.path)

    def click(point):
        mouse.move(*point)
        wait(0.3)
        mouse.down()
        wait(0.08)
        mouse.up()
        wait(0.4)

    def drag(start, stops, hold=0.5, checks=()):
        """Press at start, move through stops, release; checks[i]() runs at stop i."""
        mouse.move(*start)
        wait(0.3)
        mouse.down()
        wait(0.3)
        x0, y0 = start
        seen = []
        for n, (x1, y1) in enumerate(stops):
            for i in range(1, 16):
                mouse.move(x0 + (x1 - x0) * i / 15, y0 + (y1 - y0) * i / 15)
                wait(0.03 * SLOW)
            x0, y0 = x1, y1
            wait(hold * SLOW)
            if n < len(checks) and checks[n] is not None:
                seen.append(checks[n]())
        mouse.up()
        wait(0.5 * SLOW)
        return seen

    def make_local(*names):
        for name in names:
            if name.endswith("/"):
                os.makedirs(os.path.join(local, name), exist_ok=True)
            else:
                with open(os.path.join(local, name), "w", encoding="utf-8") as fh:
                    fh.write(f"{name}\n" * 30)
        lp.load(lp.path)
        spin(lambda: all(n.rstrip("/") in names_of(lp) for n in names), 10)
        wait(0.4)

    def names_of(pane):
        return {e.name for e in pane.entries}

    def selected(pane):
        return {i.name for i in pane.get_selected_infos()}

    # 1. the copy buttons: from the left pane to the right one and back
    lp.selection.unselect_all()
    rp.selection.unselect_all()
    wait(0.3)
    step(f"{label}: the copy buttons are off while nothing is selected",
         not lp.copy_btn.get_sensitive() and not rp.copy_btn.get_sensitive())
    make_local("button-up.txt")
    lp.select_names({"button-up.txt"})
    wait(0.3)
    step(f"{label}: ... and on with a selection", lp.copy_btn.get_sensitive())
    click(screen(lp.copy_btn))
    heading = answer_dialog(win, "ok")
    spin(lambda: job_titled(app, "Uploading button-up.txt") is not None, 10)
    job = job_titled(app, "Uploading button-up.txt")
    spin(lambda: finished(job), 60)
    step(f"{label}: the left pane's copy button uploads the selection to the right",
         job is not None and job.state == "done", (heading, job and (job.error or job.state)))
    rp.load(rp.path)
    spin(lambda: "button-up.txt" in names_of(rp) and "button-down.txt" in names_of(rp), 10)
    rp.select_names({"button-down.txt"})
    wait(0.3)
    click(screen(rp.copy_btn))
    heading = answer_dialog(win, "ok")
    spin(lambda: job_titled(app, "Downloading button-down.txt") is not None, 10)
    job = job_titled(app, "Downloading button-down.txt")
    spin(lambda: finished(job), 60)
    step(f"{label}: the right pane's copy button downloads the selection to the left",
         job is not None and job.state == "done" and os.path.isfile(os.path.join(local, "button-down.txt")),
         (heading, job and (job.error or job.state)))
    spin(lambda: not app.transfers.active, 30)

    # 2. a rubber band from empty space below the files selects them
    make_local("rb-1.txt", "rb-2.txt", "rb-3.txt", "into/")
    lp.selection.unselect_all()
    wait(0.3)
    last = lp.sort_model.get_item(lp.sort_model.get_n_items() - 1).name
    below = point_in(row_widget(lp, last), lp.column_view, 0.5, 1.0)[1] + 24
    if below < lp.column_view.get_height() - 8:
        start = screen(lp.column_view, 0.55, below / lp.column_view.get_height())
        end = screen(row_widget(lp, "rb-1.txt"), 0.5, 0.5)
        shot = f"{label.replace(' ', '-')}-rubberband.png"
        during = drag(start, [end], checks=[lambda: (screenshot(win, shot), has_rubberband(lp),
                                                     len(active_rows(lp)), FilePane._active_drag is None)[1:]])
        got = selected(lp)
        step(f"{label}: dragging from empty space draws a rubber band over the files it covers",
             during and during[0][0] and during[0][1] >= 3 and during[0][2], during)
        step(f"{label}: ... and selects them",
             {"rb-1.txt", "rb-2.txt", "rb-3.txt"} <= got and "pointer.txt" not in got, sorted(got))
        step(f"{label}: ... without dragging any files", FilePane._active_drag is None
             and not any(j.title.endswith("rb-1.txt") for j in app.transfers.jobs))
    else:
        step(f"{label}: (no empty space below the files to start a rubber band)", True)

    # 3. where dragged files would go is highlighted: the pane, or a folder in it
    lp.selection.unselect_all()
    rp.select_names({"highlight.txt"})
    wait(0.3)
    start = screen(row_widget(rp, "highlight.txt"), 0.3)
    empty_y = (point_in(row_widget(lp, last), lp.column_view, 0.5, 1.0)[1] + 30) / lp.column_view.get_height()
    over_pane = screen(lp.column_view, 0.55, min(empty_y, 0.97))
    over_folder = screen(row_widget(lp, "into"), 0.4, 0.5)
    shots = [f"{label.replace(' ', '-')}-drop-on-pane.png", f"{label.replace(' ', '-')}-drop-on-folder.png"]
    seen = drag(start, [over_pane, over_folder], checks=[
        lambda: (screenshot(win, shots[0]), lp.has_css_class("drop-hover"), lp._drop_row is None)[1:],
        lambda: (screenshot(win, shots[1]),
                 lp._drop_row is not None and lp._drop_row.has_css_class("drop-target"),
                 not lp.has_css_class("drop-hover"))[1:]])
    step(f"{label}: over the files of the other pane, that pane is highlighted", seen and all(seen[0]), seen)
    step(f"{label}: over a folder, that folder is highlighted instead", len(seen) == 2 and all(seen[1]), seen)
    spin(lambda: job_titled(app, "Downloading highlight.txt") is not None, 10)
    job = job_titled(app, "Downloading highlight.txt")
    spin(lambda: finished(job), 60)
    step(f"{label}: ... and the files go into that folder",
         job is not None and job.state == "done" and os.path.isfile(os.path.join(local, "into", "highlight.txt")),
         job and (job.error or job.state, job.subtitle))
    step(f"{label}: nothing stays highlighted after the drop",
         not lp.has_css_class("drop-hover") and lp._drop_row is None and not rp.has_css_class("drop-hover"))
    spin(lambda: not app.transfers.active, 30)

    # in the same pane: over its own files nothing (a drop there does nothing),
    # over one of its folders that folder - and the file is moved there
    lp.selection.unselect_all()
    lp.select_names({"rb-3.txt"})
    wait(0.3)
    start = screen(row_widget(lp, "rb-3.txt"), 0.3)
    seen = drag(start, [screen(row_widget(lp, "rb-1.txt"), 0.4), screen(row_widget(lp, "into"), 0.4)], checks=[
        lambda: (not lp.has_css_class("drop-hover"), lp._drop_row is None),
        lambda: (lp._drop_row is not None and lp._drop_row.has_css_class("drop-target"),)])
    heading = answer_dialog(win, "move")
    spin(lambda: os.path.isfile(os.path.join(local, "into", "rb-3.txt")), 10)
    step(f"{label}: in the same pane, its own files are not highlighted", seen and all(seen[0]), seen)
    step(f"{label}: no rubber band is left behind by the file drags",
         not has_rubberband(lp) and not has_rubberband(rp))
    step(f"{label}: ... its folders are, and the file is moved into one",
         len(seen) == 2 and all(seen[1]) and os.path.isfile(os.path.join(local, "into", "rb-3.txt"))
         and not os.path.exists(os.path.join(local, "rb-3.txt")), (seen, heading))


def test_pointer_windows(app, page):
    """Drags with real (synthesized) mouse input through Windows' drag and
    drop: SetCursorPos and mouse_event, as a user's hand would."""
    import ctypes
    from ctypes import wintypes
    gi.require_version("GdkWin32", "4.0")
    from gi.repository import GdkWin32

    user32 = ctypes.windll.user32
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    win, lp, rp = app.window, page.local_pane, page.remote_pane
    surface = win.get_surface()
    hwnd = GdkWin32.Win32Surface.get_handle(surface)
    scale = surface.get_scale()
    sx, sy = win.get_surface_transform()

    def screen(widget, fx=0.5, fy=0.5):
        x, y = point_in(widget, win, fx, fy)
        pt = wintypes.POINT(int((x + sx) * scale), int((y + sy) * scale))
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def drag(start, stops, hold=0.4):
        user32.SetCursorPos(*start)
        wait(0.3)
        user32.mouse_event(0x0002, 0, 0, 0, 0)          # MOUSEEVENTF_LEFTDOWN
        wait(0.3)
        x0, y0 = start
        for x1, y1 in stops:
            for i in range(1, 16):
                user32.SetCursorPos(int(x0 + (x1 - x0) * i / 15), int(y0 + (y1 - y0) * i / 15))
                wait(0.04 * SLOW)
            x0, y0 = x1, y1
            wait(hold * SLOW)
        user32.mouse_event(0x0004, 0, 0, 0, 0)          # MOUSEEVENTF_LEFTUP
        wait(0.5 * SLOW)

    temp_before = len(temp_jobs(app))
    select(rp, "pointer.txt")
    wait(0.3)
    start = screen(row_widget(rp, "pointer.txt"), 0.3)
    target = screen(lp.column_view, 0.5, 0.85)
    drag(start, [target])
    spin(lambda: job_titled(app, "Downloading pointer.txt") is not None, 10)
    job = job_titled(app, "Downloading pointer.txt")
    spin(lambda: finished(job), 60)
    dest = os.path.join(compat.to_native(lp.path), "pointer.txt")
    step("Windows pointer: dragging from the server pane onto the local pane downloads there",
         job is not None and job.state == "done" and os.path.isfile(dest), job and (job.error or job.state))
    step("Windows pointer: ... without a temporary download", len(temp_jobs(app)) == temp_before)
    step("Windows pointer: the drag is over", spin(lambda: FilePane._active_drag is None, 5))
    spin(lambda: not app.transfers.active, 30)

    # dropped back on the side it came from: nothing happens, and dragging
    # still works afterwards (the next drag)
    jobs_before = len(app.transfers.jobs)
    select(rp, "notyet.txt")
    wait(0.3)
    drag(screen(row_widget(rp, "notyet.txt"), 0.3), [screen(rp.column_view, 0.5, 0.9)])
    wait(1.0)
    step("Windows pointer: dropping back on the same side does nothing",
         len(app.transfers.jobs) == jobs_before and FilePane._active_drag is None)

    # and back: a new local file -> server
    with open(os.path.join(compat.to_native(lp.path), "upload-me.txt"), "w", encoding="utf-8") as fh:
        fh.write("dragged up with the mouse\n" * 50)
    lp.load(lp.path)
    spin(lambda: "upload-me.txt" in names(lp), 10)
    select(lp, "upload-me.txt")
    wait(0.3)
    drag(screen(row_widget(lp, "upload-me.txt"), 0.3), [screen(rp.column_view, 0.5, 0.85)])
    spin(lambda: job_titled(app, "Uploading upload-me.txt") is not None, 10)
    job = job_titled(app, "Uploading upload-me.txt")
    spin(lambda: finished(job), 60)
    rp.load(rp.path)
    spin(lambda: "upload-me.txt" in names(rp), 10)
    step("Windows pointer: dragging from the local pane onto the server pane uploads",
         job is not None and job.state == "done" and "upload-me.txt" in names(rp), job and (job.error or job.state))
    spin(lambda: not app.transfers.active, 30)

    class Mouse:
        @staticmethod
        def move(x, y):
            user32.SetCursorPos(int(x), int(y))

        @staticmethod
        def down():
            user32.mouse_event(0x0002, 0, 0, 0, 0)

        @staticmethod
        def up():
            user32.mouse_event(0x0004, 0, 0, 0, 0)
    test_pane_features(app, page, screen, Mouse, "Windows pointer")
    from anyscp.ui import win32drag
    wait(6)                                     # (each drag object is checked 5 s after the drag)
    stats = {k: v for k, v in win32drag.stats.items() if k.startswith("drags_")}
    step("Windows: GTK's extra release of each drag object is made up for, and nothing leaks",
         stats.get("drags_made_up_for", 0) >= 5 and not stats.get("drags_given_back"), stats)


# ---------------------------------------------------------------------------- main

def run_scenario(app):
    page = test_login_saves(app)
    if page is None:
        return
    if P.get("pw_ssh") and (not ONLY or "saved" in ONLY):
        test_saved_password_site(app)
        win = app.window
        win.tab_view.set_selected_page(page.tab_page)
        spin(lambda: win.current_page() is page, 5)
    if not ONLY or "relative" in ONLY:
        test_relative_remote_dir(app)
        win = app.window
        win.tab_view.set_selected_page(page.tab_page)
        spin(lambda: win.current_page() is page, 5)
    if P.get("proxy") and (not ONLY or "proxy" in ONLY):
        test_proxy_and_jump_site(app)
        win = app.window
        win.tab_view.set_selected_page(page.tab_page)
        spin(lambda: win.current_page() is page, 5)
    if not ONLY or "queue" in ONLY:
        test_queue(app, page)
    if not ONLY or "drag" in ONLY or "pointer" in ONLY:
        if "big.bin" not in names(page.local_pane):     # (test_queue downloads it)
            page.start_transfer(page.remote_pane, [info_of(page.remote_pane, "big.bin")], page.local_pane,
                                page.local_pane.path)
            spin(lambda: not app.transfers.active, 120)
        page.local_pane.load(page.local_pane.path)
        spin(lambda: "big.bin" in names(page.local_pane), 20)
    if not ONLY or "drag" in ONLY:
        test_drag_data(app, page)
    if P.get("pointer") and (not ONLY or "pointer" in ONLY):
        (test_pointer_windows if compat.IS_WINDOWS else test_pointer)(app, page)
    spin(lambda: not app.transfers.active, 30)


class App(AnySCPApplication):
    def __init__(self):
        super().__init__()
        self.set_flags(Gio.ApplicationFlags.NON_UNIQUE)


def main():
    app = App()

    def go():
        try:
            run_scenario(app)
        except Exception:  # noqa: BLE001
            step("scenario", False, traceback.format_exc())
        result["ok"] = bool(result["steps"]) and all(s["ok"] for s in result["steps"])
        with open(P["out"], "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        for page in app.window.pages():
            page.shutdown()
        app.quit()
        return False

    app.connect("activate", lambda *_a: GLib.timeout_add(800, go))
    app.run([])
    sys.exit(0 if result["ok"] else 1)


main()
