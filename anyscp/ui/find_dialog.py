# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Find files on the server by name pattern."""

from __future__ import annotations

import fnmatch
import posixpath
import threading

from gi.repository import Adw, GLib, Gtk

from ..backends.base import Cancelled, FSError
from ..utils import format_size, format_time, icon_for


class FindDialog(Adw.Dialog):
    def __init__(self, page):
        super().__init__(title="Find Files", content_width=680, content_height=600)
        self.page = page
        self.cancel = threading.Event()
        self.results = []

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        prefs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                        margin_start=12, margin_end=12)
        form = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        form.add_css_class("boxed-list")
        self.mask_row = Adw.EntryRow(title="File name pattern (e.g. *.log; *.conf)", text="*")
        self.mask_row.connect("entry-activated", lambda *_a: self._start())
        self.dir_row = Adw.EntryRow(title="Search in", text=page.remote_pane.path or "/")
        self.dir_row.connect("entry-activated", lambda *_a: self._start())
        self.text_row = Adw.SwitchRow(title="Match folders too", active=False)
        form.append(self.mask_row)
        form.append(self.dir_row)
        form.append(self.text_row)
        prefs.append(form)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=1)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        buttons.append(self.status)
        self.stop_btn = Gtk.Button(label="_Stop", use_underline=True, sensitive=False)
        self.stop_btn.connect("clicked", lambda *_a: self.cancel.set())
        buttons.append(self.stop_btn)
        self.find_btn = Gtk.Button(label="_Find", use_underline=True)
        self.find_btn.add_css_class("suggested-action")
        self.find_btn.connect("clicked", lambda *_a: self._start())
        buttons.append(self.find_btn)
        prefs.append(buttons)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.connect("row-activated", self._on_activated)
        self.listbox.set_placeholder(self._placeholder("Enter a pattern and press Find"))
        prefs.append(self.listbox)
        view.set_content(Gtk.ScrolledWindow(child=Adw.Clamp(child=prefs, maximum_size=760), vexpand=True))
        self.set_child(view)
        self.connect("closed", lambda *_a: self.cancel.set())
        GLib.idle_add(lambda: (self.mask_row.grab_focus(), False)[1])

    def _placeholder(self, text):
        label = Gtk.Label(label=text, margin_top=24, margin_bottom=24)
        label.add_css_class("dim-label")
        return label

    def _start(self):
        masks = [m.strip() for m in self.mask_row.get_text().replace(",", ";").split(";") if m.strip()] or ["*"]
        root = self.dir_row.get_text().strip() or "/"
        dirs_too = self.text_row.get_active()
        self.listbox.remove_all()
        self.results = []
        self.cancel = threading.Event()
        cancel = self.cancel
        self.find_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.status.set_text("Searching…")
        found = []

        def match(name):
            return any(fnmatch.fnmatch(name.lower(), m.lower()) for m in masks)

        def job(fs):
            stack = [root]
            scanned = 0
            while stack:
                if cancel.is_set():
                    raise Cancelled()
                path = stack.pop()
                try:
                    entries = fs.listdir(path)
                except FSError:
                    continue
                scanned += 1
                if scanned % 10 == 0:
                    GLib.idle_add(self.status.set_text, f"Searching {path}")
                batch = []
                for e in sorted(entries, key=lambda x: x.name):
                    if e.is_dir and not e.is_link:
                        stack.append(e.path)
                    if (not e.is_dir or dirs_too) and match(e.name):
                        batch.append(e)
                if batch:
                    found.extend(batch)
                    GLib.idle_add(self._add_results, batch)
                if len(found) >= 2000:
                    break
            return scanned

        def done(scanned):
            self._finish(f"{len(found)} found in {scanned} folders")

        def failed(exc):
            self._finish("Stopped" if isinstance(exc, Cancelled) else f"Error: {exc}")
        self.page.session.run(job, done, failed)

    def _finish(self, text):
        self.find_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        self.status.set_text(text)
        if not self.results:
            self.listbox.set_placeholder(self._placeholder("No matching files"))

    def _add_results(self, batch):
        for e in batch:
            self.results.append(e)
            row = Adw.ActionRow(title=GLib.markup_escape_text(e.name),
                                subtitle=GLib.markup_escape_text(
                                    f"{posixpath.dirname(e.path)} · "
                                    f"{'' if e.is_dir else format_size(e.size) + ' · '}{format_time(e.mtime)}"),
                                activatable=True)
            row.add_prefix(Gtk.Image.new_from_gicon(icon_for(e.name, e.is_dir)))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.info = e
            self.listbox.append(row)
        return False

    def _on_activated(self, _lb, row):
        info = row.info
        pane = self.page.remote_pane
        pane.load(posixpath.dirname(info.path), select=info.name)
        self.page.set_active_pane(pane)
        self.close()
