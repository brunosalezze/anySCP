# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Preferences dialog."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk, Pango

from ..compat import shortcut_label
from ..config import CONFIG_DIR, PORTABLE, secrets


def _combo(title, subtitle, options, settings, key):
    values = [v for v, _l in options]
    row = Adw.ComboRow(title=title, subtitle=subtitle or "",
                       model=Gtk.StringList.new([label for _v, label in options]))
    current = settings[key]
    row.set_selected(values.index(current) if current in values else 0)
    row.connect("notify::selected", lambda r, _p: settings.__setitem__(key, values[r.get_selected()]))
    return row


def _switch(title, subtitle, settings, key):
    row = Adw.SwitchRow(title=title, subtitle=subtitle or "", active=bool(settings[key]))
    row.connect("notify::active", lambda r, _p: settings.__setitem__(key, r.get_active()))
    return row


def _spin(title, subtitle, lo, hi, settings, key):
    row = Adw.SpinRow.new_with_range(lo, hi, 1)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_value(int(settings[key]))
    row.connect("notify::value", lambda r, _p: settings.__setitem__(key, int(r.get_value())))
    return row


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, settings):
        super().__init__(title="Preferences", search_enabled=True)
        s = settings

        general = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        look = Adw.PreferencesGroup(title="Appearance")
        look.add(_combo("Style", None, [("system", "Follow system"), ("light", "Light"), ("dark", "Dark")],
                        s, "color_scheme"))
        look.add(_switch("Function key bar", "Show F2–F9 buttons below the panes", s, "show_fkeys_bar"))
        look.add(_switch("Show local pane", "Turn off for a single remote pane (Explorer style)",
                         s, "show_local_pane"))
        general.add(look)

        panes = Adw.PreferencesGroup(title="File Lists")
        panes.add(_switch("Show hidden files", shortcut_label("Files starting with a dot (Ctrl+H)"), s, "show_hidden"))
        panes.add(_switch("Folders first", None, s, "dirs_first"))
        panes.add(_combo("File sizes", None, [("human", "Human readable (4.2 MB)"), ("bytes", "Bytes")],
                         s, "size_format"))
        panes.add(_combo("Dates", None, [("short", "Short (Today 14:05)"), ("iso", "ISO 8601"),
                                         ("relative", "Relative (5 min ago)")], s, "date_format"))
        panes.add(_combo("Double-click on a remote file", None,
                         [("edit", "Edit"), ("open", "Open with default app"), ("download", "Download")],
                         s, "double_click_remote"))
        general.add(panes)

        storage = Adw.PreferencesGroup(title="Storage")
        where = Adw.ActionRow(title="Portable settings folder" if PORTABLE else "Settings folder",
                              subtitle=GLib.markup_escape_text(str(CONFIG_DIR)))
        where.set_subtitle_selectable(True)
        where.add_css_class("property")
        storage.add(where)
        pw = Adw.ActionRow(title="Saved passwords",
                           subtitle=GLib.markup_escape_text(
                               secrets.backend_name if secrets.available else "Not saved (no keyring available)"))
        pw.add_css_class("property")
        storage.add(pw)
        general.add(storage)

        confirm = Adw.PreferencesGroup(title="Confirmations")
        confirm.add(_switch("Confirm copy/move", "Show the destination dialog for F5/F6", s, "confirm_transfer"))
        confirm.add(_switch("Confirm deleting", None, s, "confirm_delete"))
        confirm.add(_switch("Confirm overwriting", "Ask before replacing existing files", s, "confirm_overwrite"))
        confirm.add(_switch("Confirm closing sessions", None, s, "confirm_close_session"))
        general.add(confirm)
        self.add(general)

        transfer = Adw.PreferencesPage(title="Transfers", icon_name="folder-download-symbolic")
        tg = Adw.PreferencesGroup(title="Queue")
        tg.add(_spin("Parallel transfers", "How many queue entries run at the same time", 1, 10,
                     s, "parallel_transfers"))
        tg.add(_switch("Open the transfer list automatically",
                       "Open the list of transfers at the bottom of the window when a transfer starts, "
                       "and close it 2 seconds after the last one has finished",
                       s, "auto_show_queue"))
        transfer.add(tg)
        pg = Adw.PreferencesGroup(title="Preserve")
        pg.add(_switch("Modification time", "Keep the original timestamps", s, "preserve_mtime"))
        pg.add(_switch("Permissions", "Copy the permission bits to the destination", s, "preserve_permissions"))
        transfer.add(pg)
        self.add(transfer)

        editor = Adw.PreferencesPage(title="Editor", icon_name="accessories-text-editor-symbolic")
        eg = Adw.PreferencesGroup(title="Editing")
        eg.add(_combo("Editor", "Used by F4 and double-click",
                      [("internal", "Built-in editor"), ("external", "Default application")], s, "editor"))
        font_row = Adw.ActionRow(title="Font")
        font_btn = Gtk.FontDialogButton(dialog=Gtk.FontDialog(), valign=Gtk.Align.CENTER,
                                        use_font=True, use_size=True)
        font_btn.set_font_desc(Pango.FontDescription.from_string(s["editor_font"]))
        font_btn.connect("notify::font-desc",
                         lambda b, _p: s.__setitem__("editor_font", b.get_font_desc().to_string()))
        font_row.add_suffix(font_btn)
        eg.add(font_row)
        eg.add(_spin("Tab width", None, 1, 16, s, "editor_tab_width"))
        eg.add(_switch("Wrap long lines", None, s, "editor_wrap"))
        editor.add(eg)
        self.add(editor)
