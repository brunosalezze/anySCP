# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Stand-in for a file manager: a window that accepts dropped files and
reports what it got (as a file manager would, it takes them from where the
dragging program says they are).  Given files, it also offers them to be
dragged out, as a file manager's file list does.

usage: drop_target.py <out.json> [file ...]

Writes {"xid": ...} once the window is up (and, with files, "source": [x, y],
the point in the window to start dragging them from), then adds "files":
[{path, size, sha256}] after a drop, and "drag_ended": true after its own
drag.  Runs with the same GTK as anySCP.
"""

import hashlib
import json
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkX11", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GdkX11, Gio, GLib, GObject, Graphene, Gtk  # noqa: E402

out = sys.argv[1]
offered = [os.path.abspath(f) for f in sys.argv[2:]]
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
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    label = Gtk.Label(label="Drop files here", vexpand=True)
    target = Gtk.DropTarget(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
    target.set_gtypes([Gdk.FileList])
    target.connect("drop", on_drop)
    label.add_controller(target)
    box.append(label)
    source_label = None
    if offered:
        # the files to drag out, like a file manager's list (a file list,
        # which other programs receive as text/uri-list)
        source_label = Gtk.Label(label="Drag me: " + ", ".join(os.path.basename(f) for f in offered),
                                 height_request=80)
        source = Gtk.DragSource(actions=Gdk.DragAction.COPY)

        def prepare(_source, _x, _y):
            files = Gdk.FileList.new_from_list([Gio.File.new_for_path(f) for f in offered])
            return Gdk.ContentProvider.new_for_value(GObject.Value(Gdk.FileList, files))

        def ended(*_a):
            state["drag_ended"] = True
            save()
        source.connect("prepare", prepare)
        source.connect("drag-end", ended)
        source_label.add_controller(source)
        box.append(source_label)
    win.set_child(box)
    win.present()

    def ready():
        state["xid"] = GdkX11.X11Surface.get_xid(win.get_surface())
        if source_label is not None:
            ok, pt = source_label.compute_point(win, Graphene.Point().init(
                source_label.get_width() / 2, source_label.get_height() / 2))
            state["source"] = [pt.x, pt.y] if ok else None
        save()
        return False
    GLib.timeout_add(500, ready)


app = Gtk.Application(application_id="io.github.anyscp.DropTargetTest")
app.connect("activate", on_activate)
app.run([])
