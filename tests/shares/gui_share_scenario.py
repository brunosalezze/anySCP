# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Functional GUI test executed *inside* anySCP's packaged runtime.

Started by run_share_tests.py with the Windows runtime under Wine or with the
AppImage's Python.  It drives the real application:

  1. connect an SFTP session to the test server,
  2. browse the network share in the local pane,
  3. upload a file from the share to the server,
  4. download it back into a new folder on the share (expected to fail
     cleanly on read-only shares), rename and delete it,
  5. change a setting (stored in the portable folder on the share),
  6. take a screenshot.

Parameters come as JSON in ANYSCP_SHARE_TEST; the result is written as JSON
to the "out" path.  Verification of file contents happens in the harness.
"""

import json
import os
import sys
import time
import traceback

P = json.loads(os.environ["ANYSCP_SHARE_TEST"])
if os.environ.get("ANYSCP_SHARE_TEST_DEBUG"):
    import faulthandler
    faulthandler.dump_traceback_later(int(os.environ["ANYSCP_SHARE_TEST_DEBUG"]), exit=False)

if sys.platform == "win32" and "GI_TYPELIB_PATH" not in os.environ:
    # same environment the native launcher sets up
    rt = sys.prefix
    os.environ.update({
        "GI_TYPELIB_PATH": os.path.join(rt, "Library", "lib", "girepository-1.0"),
        "XDG_DATA_DIRS": os.path.join(rt, "Library", "share"),
        "GSETTINGS_SCHEMA_DIR": os.path.join(rt, "Library", "share", "glib-2.0", "schemas"),
        "GDK_PIXBUF_MODULE_FILE": os.path.join(rt, "Library", "lib", "gdk-pixbuf-2.0", "2.10.0", "loaders.cache"),
        "GSETTINGS_BACKEND": "memory",
    })

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Gio, GLib, Graphene, Gtk  # noqa: E402

from anyscp import compat  # noqa: E402
from anyscp.app import AnySCPApplication  # noqa: E402
from anyscp.backends.local import LocalFileSystem  # noqa: E402
from anyscp.config import CONFIG_DIR, PORTABLE  # noqa: E402

result = {"ok": False, "steps": [], "portable": PORTABLE, "config_dir": str(CONFIG_DIR)}
T0 = time.monotonic()


def step(name, ok, detail=""):
    t = round(time.monotonic() - T0, 1)
    result["steps"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:500], "t": t})
    print(f"{t:7.1f}s " + ("OK   " if ok else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)
    return ok


def spin(cond, timeout):
    """Run the main loop until cond() is true (or the timeout passes)."""
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    tick = GLib.timeout_add(50, lambda: True)   # wake up regularly to re-check cond()
    try:
        while time.monotonic() < end:
            if cond():
                return True
            ctx.iteration(True)                    # block like a real main loop
        return cond()
    finally:
        GLib.source_remove(tick)


def screenshot(widget, path):
    w, h = widget.get_width(), widget.get_height()
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(widget).snapshot(snap, w, h)
    node = snap.to_node()
    if node is not None:
        tex = widget.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
        tex.save_to_png(path)


def latest_job(app, before):
    jobs = app.transfers.jobs
    return jobs[-1] if len(jobs) > before else None


def run_scenario(app):
    win = app.window
    share = compat.local_path(P["share_dir"])
    site = {"id": "share-test", "name": "Share test", "protocol": "sftp", "host": P["ssh"]["host"],
            "port": P["ssh"]["port"], "user": P["ssh"]["user"], "auth": "key",
            "key_file": P["ssh"]["key"], "remote_dir": P["remote_dir"], "local_dir": share,
            "save_password": False}
    win.open_session(site)
    page = win.current_page()
    if not step("connect", spin(lambda: page.session.state == "connected"
                                and page.remote_pane.path and page.local_pane.path, 90),
                page.session.error or page.session.state):
        return

    lp, rp = page.local_pane, page.remote_pane
    names = lambda pane: {e.name for e in pane.entries}  # noqa: E731
    step("browse share in local pane",
         spin(lambda: lp.path == share and "payload.bin" in names(lp), 30),
         f"{lp.path}: {sorted(names(lp))[:8]}")
    step("unicode names on the share", spin(lambda: "Ünïcode folder" in names(lp), 5))

    # upload share -> server
    before = len(app.transfers.jobs)
    info = next(e for e in lp.entries if e.name == "payload.bin")
    page.start_transfer(lp, [info], rp, rp.path)
    job = None
    spin(lambda: (latest_job(app, before) is not None
                  and latest_job(app, before).state in ("done", "failed", "cancelled")), 120)
    job = latest_job(app, before)
    step("upload from share", job is not None and job.state == "done", job and (job.error or job.state))

    # download server -> new folder on the share
    fs = LocalFileSystem()
    roundtrip = share.rstrip("/") + "/roundtrip"
    try:
        if fs.exists(roundtrip):
            fs.remove_tree(roundtrip)
        fs.mkdir(roundtrip)
        made = True
    except Exception as exc:  # noqa: BLE001
        made = False
        mkdir_error = f"{type(exc).__name__}: {exc}"
    if P["writable"]:
        step("create folder on share", made, "" if made else mkdir_error)
    else:
        step("read-only share refuses new folder", not made, "" if not made else "folder was created!")
    if made:
        rp.load(rp.path)
        spin(lambda: "payload.bin" in names(rp), 20)
        rinfo = next(e for e in rp.entries if e.name == "payload.bin")
        before = len(app.transfers.jobs)
        page.start_transfer(rp, [rinfo], lp, roundtrip)
        spin(lambda: (latest_job(app, before) is not None
                      and latest_job(app, before).state in ("done", "failed", "cancelled")), 120)
        job = latest_job(app, before)
        step("download to share", job is not None and job.state == "done", job and (job.error or job.state))
        try:
            fs.rename(roundtrip + "/payload.bin", roundtrip + "/payload-renamed.bin")
            step("rename on share", fs.exists(roundtrip + "/payload-renamed.bin"))
            result["roundtrip_file"] = roundtrip + "/payload-renamed.bin"
        except Exception as exc:  # noqa: BLE001
            step("rename on share", False, exc)
        lp.load(roundtrip)
        spin(lambda: lp.path == roundtrip, 20)

    # settings go to the portable folder on the share (or the per-user folder)
    try:
        app.settings["show_hidden"] = not app.settings["show_hidden"]
        app.settings["show_hidden"] = not app.settings["show_hidden"]
        step("save settings", (CONFIG_DIR / "settings.json").exists(), CONFIG_DIR)
    except Exception as exc:  # noqa: BLE001
        step("save settings", False, exc)

    spin(lambda: False, 1.0)
    try:
        screenshot(win, P["shot"])
    except Exception as exc:  # noqa: BLE001
        step("screenshot", False, exc)

    if made:
        try:
            fs.remove_tree(roundtrip)
            step("delete folder on share", not fs.exists(roundtrip))
        except Exception as exc:  # noqa: BLE001
            step("delete folder on share", False, exc)


class App(AnySCPApplication):
    def __init__(self):
        super().__init__()
        self.set_flags(Gio.ApplicationFlags.NON_UNIQUE)


def main():
    app = App()

    def go():
        print(f"{time.monotonic() - T0:7.1f}s activated", flush=True)
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
