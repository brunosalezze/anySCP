# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""File properties: info, permissions (chmod) and ownership (chown)."""

from __future__ import annotations

import posixpath

from gi.repository import Adw, GLib, GObject, Gtk

from ..backends.base import CAP_CHOWN, FSError
from ..utils import format_mode, format_size, format_time, type_description
from .prompts import show_error

BITS = [  # (row label, read, write, exec)
    ("Owner", 0o400, 0o200, 0o100),
    ("Group", 0o040, 0o020, 0o010),
    ("Others", 0o004, 0o002, 0o001),
]
SPECIAL = [("Set UID", 0o4000), ("Set GID", 0o2000), ("Sticky", 0o1000)]


class PropertiesDialog(Adw.Dialog):
    def __init__(self, page, pane, infos):
        multiple = len(infos) > 1
        title = f"{len(infos)} Items" if multiple else infos[0].name
        super().__init__(title=title, content_width=520, content_height=720)
        self.page = page
        self.pane = pane
        self.infos = infos
        self._updating = False
        self._perm_touched = False
        fs = page._fs_for(pane)
        self.can_chmod = page.chmod_capable(pane)
        self.can_chown = fs is not None and (fs.is_local or fs.has(CAP_CHOWN))

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        prefs = Adw.PreferencesPage()
        view.set_content(prefs)

        # general ------------------------------------------------------------
        general = Adw.PreferencesGroup()
        if not multiple:
            info = infos[0]
            self._prop(general, "Name", info.name)
            self._prop(general, "Location", posixpath.dirname(info.path) or "/")
            self._prop(general, "Type", type_description(info.name, info.is_dir, info.is_link))
            if info.is_link:
                self.link_row = self._prop(general, "Link target", info.link_target or "…")
                if not info.link_target:
                    page._backend_for(pane).run(lambda f: f.readlink(info.path),
                                                lambda t: self.link_row.set_subtitle(
                                                    GLib.markup_escape_text(t or "unknown")))
            if not info.is_dir:
                self._prop(general, "Size", f"{format_size(info.size)} ({info.size:,} bytes)")
            self._prop(general, "Modified", format_time(info.mtime, "iso") or "Unknown")
            if info.owner:
                self._prop(general, "Owner", f"{info.owner}" + (f":{info.group}" if info.group else ""))
        else:
            dirs = sum(1 for i in infos if i.is_dir)
            self._prop(general, "Selection", f"{len(infos) - dirs} file(s), {dirs} folder(s)")
            self._prop(general, "Location", posixpath.dirname(infos[0].path) or "/")
            size = sum(i.size for i in infos if not i.is_dir)
            self._prop(general, "Size of files", format_size(size))
        if any(i.is_dir for i in infos):
            self.size_row = Adw.ActionRow(title="Total size", subtitle="Not calculated")
            self.size_row.add_css_class("property")
            calc = Gtk.Button(label="Calculate", valign=Gtk.Align.CENTER)
            calc.add_css_class("flat")
            calc.connect("clicked", self._calculate)
            self.size_row.add_suffix(calc)
            general.add(self.size_row)
        prefs.add(general)

        # permissions ------------------------------------------------------------
        modes = [i.mode for i in infos if i.mode is not None]
        self.original_modes = {i.path: i.mode for i in infos}
        self.checks: dict[int, Gtk.CheckButton] = {}
        if self.can_chmod:
            perm = Adw.PreferencesGroup(title="Permissions",
                                        description=None if modes else "Current permissions are unknown")
            grid = Gtk.Grid(column_spacing=8, row_spacing=4, margin_top=10, margin_bottom=10,
                            margin_start=14, margin_end=14, column_homogeneous=False)
            grid.add_css_class("perm-grid")
            for col, label in enumerate(("Read", "Write", "Execute"), start=1):
                lab = Gtk.Label(label=label, hexpand=True)
                lab.add_css_class("perm-header")
                grid.attach(lab, col, 0, 1, 1)
            for row, (label, r, w, x) in enumerate(BITS, start=1):
                lab = Gtk.Label(label=label, xalign=0)
                grid.attach(lab, 0, row, 1, 1)
                for col, bit in enumerate((r, w, x), start=1):
                    cb = Gtk.CheckButton(halign=Gtk.Align.CENTER)
                    cb.connect("toggled", self._on_check_toggled, bit)
                    self.checks[bit] = cb
                    grid.attach(cb, col, row, 1, 1)
            special_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
                                  halign=Gtk.Align.CENTER, margin_top=8)
            for label, bit in SPECIAL:
                cb = Gtk.CheckButton(label=label)
                cb.connect("toggled", self._on_check_toggled, bit)
                self.checks[bit] = cb
                special_box.append(cb)
            grid.attach(special_box, 0, 4, 4, 1)
            frame = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            frame.add_css_class("boxed-list")
            frame_row = Gtk.ListBoxRow(activatable=False, child=grid)
            frame.append(frame_row)
            perm.add(frame)
            self.octal_row = Adw.EntryRow(title="Octal", input_purpose=Gtk.InputPurpose.DIGITS)
            self.octal_row.set_margin_top(12)
            self.octal_row.connect("changed", self._on_octal_changed)
            octal_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            octal_box.add_css_class("boxed-list")
            octal_box.set_margin_top(12)
            octal_box.append(self.octal_row)
            self.symbolic_row = Adw.ActionRow(title="Symbolic")
            self.symbolic_row.add_css_class("property")
            octal_box.append(self.symbolic_row)
            perm.add(octal_box)
            if any(i.is_dir for i in infos):
                rec_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
                rec_box.add_css_class("boxed-list")
                rec_box.set_margin_top(12)
                self.recursive_row = Adw.SwitchRow(title="Apply to enclosed files and folders",
                                                   subtitle="Change permissions recursively")
                rec_box.append(self.recursive_row)
                self.dir_exec_row = Adw.SwitchRow(title="Add execute for folders (X)",
                                                  subtitle="Keep folders browsable when removing x from files",
                                                  active=True)
                self.recursive_row.bind_property("active", self.dir_exec_row, "visible",
                                                GObject.BindingFlags.SYNC_CREATE)
                self.dir_exec_row.set_visible(False)
                rec_box.append(self.dir_exec_row)
                perm.add(rec_box)
            else:
                self.recursive_row = None
            prefs.add(perm)
            self._init_checks(modes)

        # ownership ------------------------------------------------------------
        self.owner_row = self.group_row = None
        if self.can_chown:
            own = Adw.PreferencesGroup(title="Ownership")
            owners = {i.owner for i in infos}
            groups = {i.group for i in infos}
            self.owner_row = Adw.EntryRow(title="Owner", text=owners.pop() if len(owners) == 1 else "")
            self.group_row = Adw.EntryRow(title="Group", text=groups.pop() if len(groups) == 1 else "")
            self.initial_owner = self.owner_row.get_text()
            self.initial_group = self.group_row.get_text()
            own.add(self.owner_row)
            own.add(self.group_row)
            prefs.add(own)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        bottom.set_margin_top(10)
        bottom.set_margin_bottom(10)
        bottom.set_margin_end(12)
        cancel = Gtk.Button(label="_Cancel", use_underline=True)
        cancel.connect("clicked", lambda *_a: self.close())
        self.apply_btn = Gtk.Button(label="_Apply", use_underline=True)
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", lambda *_a: self._apply())
        bottom.append(cancel)
        bottom.append(self.apply_btn)
        if self.can_chmod or self.can_chown:
            view.add_bottom_bar(bottom)
        self.set_child(view)

    def _prop(self, group, title, value):
        row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(str(value)))
        row.add_css_class("property")
        row.set_subtitle_selectable(True)
        group.add(row)
        return row

    # ------------------------------------------------------------------ permissions
    def _init_checks(self, modes):
        self._updating = True
        self.mixed = 0
        common = 0
        if modes:
            all_or = 0
            all_and = 0o7777
            for m in modes:
                all_or |= m
                all_and &= m
            common = all_and
            self.mixed = all_or & ~all_and
        for bit, cb in self.checks.items():
            cb.set_active(bool(common & bit))
            cb.set_inconsistent(bool(self.mixed & bit))
        self._updating = False
        self._sync_octal()

    def _current_mode(self) -> int:
        mode = 0
        for bit, cb in self.checks.items():
            if cb.get_active() and not cb.get_inconsistent():
                mode |= bit
        return mode

    def _on_check_toggled(self, cb, bit):
        if self._updating:
            return
        self._perm_touched = True
        if cb.get_inconsistent():
            self._updating = True
            cb.set_inconsistent(False)
            self.mixed &= ~bit
            self._updating = False
        self._sync_octal()

    def _sync_octal(self):
        self._updating = True
        if self.mixed:
            self.octal_row.set_text("")
            self.symbolic_row.set_subtitle("Mixed")
        else:
            mode = self._current_mode()
            self.octal_row.set_text(f"{mode:04o}")
            self.symbolic_row.set_subtitle(format_mode(mode, False)[1:])
        self._updating = False

    def _on_octal_changed(self, row):
        if self._updating:
            return
        self._perm_touched = True
        text = row.get_text().strip()
        try:
            mode = int(text, 8)
            ok = 0 <= mode <= 0o7777 and len(text) <= 4
        except ValueError:
            ok = False
        if not ok:
            row.add_css_class("error")
            return
        row.remove_css_class("error")
        self._updating = True
        self.mixed = 0
        for bit, cb in self.checks.items():
            cb.set_inconsistent(False)
            cb.set_active(bool(mode & bit))
        self.symbolic_row.set_subtitle(format_mode(mode, False)[1:])
        self._updating = False

    # ------------------------------------------------------------------ size
    def _calculate(self, button):
        button.set_sensitive(False)
        self.size_row.set_subtitle("Calculating…")
        infos = self.infos

        def job(fs):
            total = files = dirs = 0
            for info in infos:
                if not info.is_dir:
                    total += info.size
                    files += 1
                    continue
                for _p, ds, fs_ in fs.walk(info.path):
                    dirs += len(ds)
                    files += len(fs_)
                    total += sum(f.size for f in fs_)
            return total, files, dirs

        def done(result):
            total, files, dirs = result
            self.size_row.set_subtitle(f"{format_size(total)} ({total:,} bytes) in {files} files, {dirs} folders")
        self.page._backend_for(self.pane).run(job, done, lambda exc: self.size_row.set_subtitle(str(exc)))

    # ------------------------------------------------------------------ apply
    def _apply(self):
        chmod_mode = None
        keep_bits = 0
        modes_known = any(m is not None for m in self.original_modes.values())
        recursive_requested = bool(self.recursive_row and self.recursive_row.get_active())
        if self.can_chmod and (self._perm_touched or (recursive_requested and modes_known)):
            chmod_mode = self._current_mode()
            keep_bits = self.mixed     # bits left "mixed" keep each file's own value
        recursive = bool(self.recursive_row and self.recursive_row.get_active())
        dir_x = bool(recursive and getattr(self, "dir_exec_row", None) and self.dir_exec_row.get_active())
        owner = group = None
        if self.owner_row is not None:
            o, g = self.owner_row.get_text().strip(), self.group_row.get_text().strip()
            owner = o if o and o != self.initial_owner else None
            group = g if g and g != self.initial_group else None
        infos = self.infos
        originals = self.original_modes

        def new_mode(old, is_dir):
            if chmod_mode is None:
                return None
            base = (old or 0) & keep_bits
            mode = (chmod_mode & ~keep_bits) | base
            if is_dir and dir_x:
                # X: folders get execute where they get read.
                for r, x in ((0o400, 0o100), (0o040, 0o010), (0o004, 0o001)):
                    if mode & r:
                        mode |= x
            return mode

        def job(fs):
            errors = []

            def apply_one(path, old_mode, is_dir):
                try:
                    m = new_mode(old_mode, is_dir)
                    if m is not None and m != old_mode:
                        fs.chmod(path, m)
                    if owner or group:
                        fs.chown(path, owner, group)
                except FSError as exc:
                    errors.append(f"{posixpath.basename(path)}: {exc}")

            for info in infos:
                apply_one(info.path, originals.get(info.path), info.is_dir)
                if recursive and info.is_dir and not info.is_link:
                    for _p, ds, files in fs.walk(info.path):
                        for entry in ds + files:
                            if entry.is_link:
                                continue
                            apply_one(entry.path, entry.mode, entry.is_dir)
            return errors

        def done(errors):
            self.pane.refresh(quiet=True)
            if errors:
                show_error(self.page.window, "Some Changes Failed", "\n".join(errors[:12]))
            else:
                self.page.toast("Properties updated")
        self.apply_btn.set_sensitive(False)
        self.page._backend_for(self.pane).run(job, done, lambda exc: show_error(self.page.window,
                                                                                "Cannot Change Properties", exc))
        self.close()
