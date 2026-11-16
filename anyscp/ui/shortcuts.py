# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Keyboard shortcuts overview."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from .. import compat

GROUPS = [
    ("Sessions", [
        ("New session / site manager", "Ctrl+N"),
        ("Close session tab", "Ctrl+W"),
        ("Next / previous tab", "Ctrl+PgDn / Ctrl+PgUp"),
        ("Transfer queue", "Ctrl+J"),
        ("Synchronize folders", "Ctrl+S"),
        ("Compare directories", "Ctrl+="),
        ("Find files on the server", "Ctrl+Shift+F"),
        ("Open terminal", "Ctrl+Shift+T"),
        ("Preferences", "Ctrl+,"),
        ("Quit", "Ctrl+Q"),
    ]),
    ("Files", [
        ("Rename", "F2"),
        ("Edit", "F4"),
        ("New file", "Shift+F4"),
        ("Copy to other side (upload/download)", "F5"),
        ("Move to other side", "F6"),
        ("New folder", "F7"),
        ("Delete (local: to trash)", "F8 / Delete"),
        ("Delete permanently", "Shift+Delete"),
        ("Properties & permissions", "F9"),
        ("Copy path", "Ctrl+Shift+C"),
        ("Context menu", "Menu / Shift+F10"),
    ]),
    ("Navigation", [
        ("Open folder / file", "Enter / double-click"),
        ("Parent folder", "Backspace / Alt+Up"),
        ("Back / forward", "Alt+Left / Alt+Right"),
        ("Type a path", "Ctrl+L"),
        ("Filter the list", "Ctrl+F"),
        ("Jump to a file", "start typing its name"),
        ("Refresh", "Ctrl+R"),
        ("Show hidden files", "Ctrl+H"),
        ("Select all", "Ctrl+A"),
        ("Switch pane", "Tab"),
    ]),
    ("Drag and drop", [
        ("Copy between panes", "drag"),
        ("Move between panes", "Shift+drag"),
        ("Upload from the file manager", "drop on the remote pane"),
        ("Download to the file manager", "drag out of the window"),
        ("Move into a folder", "drop on the folder"),
    ]),
]


class ShortcutsDialog(Adw.Dialog):
    def __init__(self):
        super().__init__(title="Keyboard Shortcuts", content_width=560, content_height=640)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        for title, items in GROUPS:
            group = Adw.PreferencesGroup(title=title)
            for label, keys in items:
                row = Adw.ActionRow(title=label, use_markup=False)
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, valign=Gtk.Align.CENTER)
                for i, alt in enumerate(keys.split(" / ")):
                    if i:
                        sep = Gtk.Label(label="or")
                        sep.add_css_class("dim-label")
                        sep.add_css_class("caption")
                        box.append(sep)
                    if alt[:1].islower():
                        lab = Gtk.Label(label=alt)
                        lab.add_css_class("dim-label")
                        box.append(lab)
                        continue
                    if compat.IS_MACOS:         # one cap, written the Mac way: ⇧⌘F
                        cap = Gtk.Label(label=compat.shortcut_label(alt))
                        cap.add_css_class("keycap")
                        box.append(cap)
                        continue
                    for j, key in enumerate(alt.split("+") if alt != "Ctrl+=" else ["Ctrl", "="]):
                        if j:
                            plus = Gtk.Label(label="+")
                            plus.add_css_class("dim-label")
                            box.append(plus)
                        cap = Gtk.Label(label=key)
                        cap.add_css_class("keycap")
                        box.append(cap)
                row.add_suffix(box)
                group.add(row)
            page.add(group)
        view.set_content(page)
        self.set_child(view)
