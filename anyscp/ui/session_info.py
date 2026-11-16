# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Server/protocol information and the session log."""

from __future__ import annotations

import time

from gi.repository import Adw, GLib, Gtk

from ..config import site_subtitle
from .prompts import make_spinner


class SessionInfoDialog(Adw.Dialog):
    def __init__(self, page):
        super().__init__(title="Server & Protocol Information", content_width=600, content_height=560)
        self.page = page
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.prefs = Adw.PreferencesPage()
        view.set_content(self.prefs)
        self.set_child(view)

        group = Adw.PreferencesGroup(title="Session")
        site = page.site
        self._row(group, "Site", page.title)
        self._row(group, "Address", site_subtitle(site))
        if page.session.connected_at:
            self._row(group, "Connected since", time.strftime("%Y-%m-%d %H:%M:%S",
                                                              time.localtime(page.session.connected_at)))
        self.prefs.add(group)

        self.server_group = Adw.PreferencesGroup(title="Server")
        spinner = make_spinner(halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=12)
        self.server_group.add(spinner)
        self._spinner = spinner
        self.prefs.add(self.server_group)
        page.session.run(lambda fs: (fs.info(), sorted(fs.capabilities)), self._got_info,
                         lambda exc: self._got_info(([("Error", str(exc))], [])))

    def _row(self, group, title, value):
        row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(str(value)))
        row.add_css_class("property")
        row.set_subtitle_selectable(True)
        group.add(row)
        return row

    def _got_info(self, result):
        rows, caps = result
        self.server_group.remove(self._spinner)
        for title, value in rows:
            row = self._row(self.server_group, title, value)
            if "fingerprint" in title.lower() or title == "Host key":
                copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text="Copy")
                copy.add_css_class("flat")
                copy.connect("clicked", lambda _b, v=value: (self.get_clipboard().set(v),))
                row.add_suffix(copy)
        if caps:
            group = Adw.PreferencesGroup(title="Capabilities")
            labels = {"chmod": "Change permissions", "chown": "Change owner", "utime": "Set timestamps",
                      "symlink": "Symbolic links", "exec": "Run commands", "shell": "Terminal",
                      "resume": "Resume transfers"}
            for cap in caps:
                row = Adw.ActionRow(title=labels.get(cap, cap))
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
                group.add(row)
            self.prefs.add(group)


class LogDialog(Adw.Dialog):
    def __init__(self, session):
        super().__init__(title="Session Log", content_width=760, content_height=520)
        self.session = session
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy log")
        copy.connect("clicked", lambda *_a: self.get_clipboard().set("\n".join(session.log_lines)))
        header.pack_start(copy)
        view.add_top_bar(header)
        self.text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 left_margin=12, right_margin=12, top_margin=8, bottom_margin=8)
        self.text.add_css_class("log-view")
        self.text.get_buffer().set_text("\n".join(session.log_lines))
        self.scroller = Gtk.ScrolledWindow(child=self.text, vexpand=True)
        view.set_content(self.scroller)
        self.set_child(view)
        self._handler = session.connect("log", self._on_log)
        self.connect("closed", lambda *_a: session.disconnect(self._handler))
        GLib.idle_add(self._scroll_end)

    def _on_log(self, _session, line):
        buf = self.text.get_buffer()
        buf.insert(buf.get_end_iter(), ("\n" if buf.get_char_count() else "") + line)
        GLib.idle_add(self._scroll_end)

    def _scroll_end(self):
        adj = self.scroller.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False
