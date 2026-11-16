# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Start page: quick connect and saved sites."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..compat import shortcut_label
from ..config import APP_ID, site_from_url, site_subtitle, site_title
from ..utils import color_dot_icon
from .site_dialog import PROTOCOL_ICONS


class WelcomePage(Gtk.ScrolledWindow):
    def __init__(self, window):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.window = window

        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=480)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(36)
        box.set_margin_bottom(36)
        box.set_margin_start(18)
        box.set_margin_end(18)
        clamp.set_child(box)
        self.set_child(clamp)

        logo = Gtk.Image(icon_name=APP_ID, pixel_size=112)
        logo.add_css_class("icon-dropshadow")
        box.append(logo)
        title = Gtk.Label(label="anySCP")
        title.add_css_class("title-1")
        box.append(title)
        tagline = Gtk.Label(label="Secure file transfer with SFTP, SCP, FTP(S) and WebDAV")
        tagline.add_css_class("dim-label")
        tagline.set_wrap(True)
        tagline.set_justify(Gtk.Justification.CENTER)
        box.append(tagline)

        quick = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, margin_top=12)
        quick.add_css_class("linked")
        quick.add_css_class("quick-connect")
        self.quick_entry = Gtk.Entry(hexpand=True,
                                     placeholder_text="Quick connect: sftp://user@host:port/path")
        self.quick_entry.set_input_purpose(Gtk.InputPurpose.URL)
        self.quick_entry.connect("activate", lambda *_a: self._quick_connect())
        self.quick_entry.connect("changed", lambda e: e.remove_css_class("error"))
        go = Gtk.Button(label="Connect")
        go.add_css_class("suggested-action")
        go.connect("clicked", lambda *_a: self._quick_connect())
        quick.append(self.quick_entry)
        quick.append(go)
        box.append(quick)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=18)
        label = Gtk.Label(label="Saved Sites", xalign=0, hexpand=True)
        label.add_css_class("heading")
        header.append(label)
        self.search = Gtk.SearchEntry(placeholder_text="Search", width_chars=14)
        self.search.connect("search-changed", lambda *_a: self.listbox.invalidate_filter())
        header.append(self.search)
        new = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=shortcut_label("New site (Ctrl+N)"))
        new.add_css_class("flat")
        new.set_action_name("win.new-session")
        header.append(new)
        box.append(header)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_filter_func(self._filter)
        self.listbox.connect("row-activated", self._on_row_activated)
        box.append(self.listbox)

        self.empty = Adw.StatusPage(icon_name="network-server-symbolic", title="No Saved Sites",
                                    description="Create a site to store connection details, or import "
                                                "your sites from WinSCP.")
        self.empty.add_css_class("compact")
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.CENTER)
        b1 = Gtk.Button(label="_New Site…", use_underline=True)
        b1.add_css_class("pill")
        b1.add_css_class("suggested-action")
        b1.set_action_name("win.new-session")
        b2 = Gtk.Button(label="_Import from WinSCP…", use_underline=True)
        b2.add_css_class("pill")
        b2.set_action_name("win.import-winscp")
        buttons.append(b1)
        buttons.append(b2)
        self.empty.set_child(buttons)
        box.append(self.empty)
        self.refresh()

    def refresh(self):
        self.listbox.remove_all()
        sites = self.window.sites.sorted()
        for site in sites:
            self.listbox.append(self._make_row(site))
        self.listbox.set_visible(bool(sites))
        self.search.set_visible(bool(sites))
        self.empty.set_visible(not sites)

    def _make_row(self, site):
        row = Adw.ActionRow(title=GLib.markup_escape_text(site_title(site)),
                            subtitle=GLib.markup_escape_text(site_subtitle(site)),
                            activatable=True)
        row.site = site
        row.search_text = f"{site_title(site)} {site_subtitle(site)} {site.get('folder', '')}".lower()
        icon = Gtk.Image(icon_name=PROTOCOL_ICONS.get(site.get("protocol"), "network-server-symbolic"))
        icon.add_css_class("site-icon")
        row.add_prefix(icon)
        if site.get("folder"):
            tag = Gtk.Label(label=site["folder"], valign=Gtk.Align.CENTER)
            tag.add_css_class("tag")
            row.add_suffix(tag)
        if site.get("color"):
            row.add_suffix(Gtk.Image.new_from_gicon(color_dot_icon(site["color"])))
        edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Edit")
        edit.add_css_class("flat")
        edit.connect("clicked", lambda *_a: self.window.edit_site(site))
        row.add_suffix(edit)
        connect = Gtk.Image(icon_name="go-next-symbolic")
        row.add_suffix(connect)
        return row

    def _filter(self, row):
        text = self.search.get_text().strip().lower()
        return not text or text in row.search_text

    def _on_row_activated(self, _lb, row):
        self.window.open_session(dict(row.site))

    def _quick_connect(self):
        site = site_from_url(self.quick_entry.get_text())
        if site is None:
            self.quick_entry.add_css_class("error")
            self.window.toast("Enter an address like sftp://user@example.com")
            return
        site["_quick"] = True
        self.window.open_session(site)

    def focus(self):
        self.quick_entry.grab_focus()

