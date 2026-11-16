# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Stand-in for a file manager: a window that accepts dropped files and
reports what it got (as a file manager would, it takes them from where the
dragging program says they are).

usage: drop_target.py <out.json>

Writes {"xid": ...} once the window is up, then adds "files": [{path, size,
sha256}] after a drop.  Runs with the same GTK as anySCP.
"""

import hashlib
import json
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkX11", "4.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk  # noqa: E402

out = sys.argv[1]
state = {}


def save():
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, out)


def on_drop(_target, value, _x, _y):
    files = []
    for gfile in value.get_files():
        path = gfile.get_path()
        entry = {"path": path, "exists": bool(path) and os.path.exists(path)}
        if entry["exists"] and os.path.isfile(path):
            data = open(path, "rb").read()
            entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        files.append(entry)
    state["files"] = files
    save()
    return True


def on_activate(app):
    win = Gtk.ApplicationWindow(application=app, title="Drop target", default_width=380, default_height=300)
    win.set_decorated(False)
    label = Gtk.Label(label="Drop files here")
    target = Gtk.DropTarget(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
    target.set_gtypes([Gdk.FileList])
    target.connect("drop", on_drop)
    label.add_controller(target)
    win.set_child(label)
    win.present()

    def ready():
        state["xid"] = GdkX11.X11Surface.get_xid(win.get_surface())
        save()
        return False
    GLib.timeout_add(500, ready)


app = Gtk.Application(application_id="io.github.anyscp.DropTargetTest")
app.connect("activate", on_activate)
app.run([])
