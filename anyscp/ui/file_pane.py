# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""One side of the commander: path bar, sortable file list, status line."""

from __future__ import annotations

import posixpath
import time

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk

from .. import compat
from ..backends.base import FileInfo
from . import dragout, win32drag
from ..utils import (PARENT_ICON, format_mode, format_size, format_time, icon_for,
                     type_description)
from .pathbar import PathBar
from .prompts import make_spinner

COLUMNS = [
    # key, title, expand, default visible
    ("name", "Name", True, True),
    ("size", "Size", False, True),
    ("type", "Type", False, False),
    ("mtime", "Modified", False, True),
    ("perms", "Permissions", False, True),
    ("owner", "Owner", False, False),
]


def display_name(name: str) -> str:
    """Names from servers with non-UTF-8 encodings carry lone surrogates
    (surrogateescape); GTK needs valid UTF-8."""
    try:
        name.encode("utf-8")
        return name
    except UnicodeEncodeError:
        return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


class FileItem(GObject.Object):
    __gtype_name__ = "AnyscpFileItem"

    name = GObject.Property(type=str, default="")
    size = GObject.Property(type=GObject.TYPE_INT64, default=0)
    mtime = GObject.Property(type=GObject.TYPE_DOUBLE, default=0)
    rank = GObject.Property(type=int, default=2)
    kind = GObject.Property(type=str, default="")
    perms = GObject.Property(type=str, default="")
    owner = GObject.Property(type=str, default="")

    def __init__(self, info: FileInfo | None, parent_dir: str | None = None, dirs_first=True):
        if info is None:   # ".." entry
            super().__init__(name="..", rank=0)
            self.info = FileInfo(name="..", path=parent_dir or "/", is_dir=True)
            self.is_parent = True
            return
        super().__init__(
            name=display_name(info.name), size=info.size or 0, mtime=info.mtime or 0,
            rank=(1 if info.is_dir else 2) if dirs_first else 1,
            perms=format_mode(info.mode, info.is_dir, info.is_link),
            owner=f"{info.owner}:{info.group}" if info.group else info.owner)
        self.info = info
        self.is_parent = False
        self._kind = None

    def get_kind(self) -> str:
        if self._kind is None:
            self._kind = type_description(self.info.name, self.info.is_dir, self.info.is_link)
        return self._kind


class LocalBackend:
    """Adapter giving the local pane the same ``run()`` API as a session."""

    def __init__(self):
        from ..backends.local import LocalFileSystem
        from ..session import SerialWorker
        self.fs = LocalFileSystem()
        self.worker = SerialWorker("local")
        self.state = "connected"

    def run(self, fn, on_done=None, on_error=None):
        self.worker.submit(lambda: fn(self.fs), on_done, on_error)

    def stop(self):
        self.worker.stop()


class FilePane(Gtk.Box):
    """A directory view bound to a backend (local disk or a session)."""

    _registry: dict[int, "FilePane"] = {}
    _next_id = 1
    # (source pane, infos) of a drag started inside anySCP.  The drop side
    # may receive the local pane's Gdk.FileList instead of our marker string,
    # so internal drags are recognised by this, not by the payload type.
    _active_drag: tuple | None = None

    def __init__(self, controller, backend, *, is_local: bool, settings, title: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.pane_id = FilePane._next_id
        FilePane._next_id += 1
        FilePane._registry[self.pane_id] = self
        self.add_css_class("file-pane")

        self.controller = controller
        self.backend = backend
        self.is_local = is_local
        self._dragout = None            # the download behind the last drag out of a server pane
        self._drag_download = None      # ... while that drag lasts
        self._drag_provider = None      # ... and what it offers other programs
        self._drag_cancelled = False
        self._drop_row = None           # the row highlighted as where a drag would go
        self.settings = settings
        self.title = title
        self.path: str | None = None
        self.home_path: str | None = None
        self.back_stack: list[str] = []
        self.forward_stack: list[str] = []
        self._load_serial = 0
        self._loaded_serial = 0
        self._monitor = None
        self._monitor_timeout = 0
        self._typeahead = ""
        self._typeahead_time = 0.0
        self._free_space: int | None = None
        self.entries: list[FileInfo] = []
        self._filter_text = ""

        self._build_header()
        self._build_list()
        self._build_footer()
        self._install_actions()

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_a: self.controller.set_active_pane(self))
        self.add_controller(focus)
        click = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_a: self.controller.set_active_pane(self))
        self.add_controller(click)

        settings.connect(self._on_setting)

    # ------------------------------------------------------------------ UI
    def _build_header(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.add_css_class("pane-toolbar")

        # Copy the selection to the other pane, from the side next to it: the
        # local pane (left) at its end, the server pane (right) at its start.
        if self.is_local:
            icon, tip = "anyscp-copy-to-right-symbolic", "Upload: copy the selected files to the right (F5)"
        else:
            icon, tip = "anyscp-copy-to-left-symbolic", "Download: copy the selected files to the left (F5)"
        self.copy_btn = Gtk.Button(icon_name=icon, tooltip_text=tip, action_name="pane.transfer")
        self.copy_btn.add_css_class("flat")
        self.copy_btn.add_css_class("copy-across")
        if not self.is_local:
            bar.append(self.copy_btn)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        nav.add_css_class("linked")
        self.back_btn = self._tool_button("go-previous-symbolic", "Back (Alt+Left)", self.go_back)
        self.fwd_btn = self._tool_button("go-next-symbolic", "Forward (Alt+Right)", self.go_forward)
        self.up_btn = self._tool_button("go-up-symbolic", "Parent folder (Backspace)", self.go_up)
        for b in (self.back_btn, self.fwd_btn, self.up_btn):
            nav.append(b)
        bar.append(nav)

        self.pathbar = PathBar("computer-symbolic" if self.is_local else "network-server-symbolic")
        self.pathbar.connect("navigate", lambda _p, path: self.load(path))
        bar.append(self.pathbar)

        self.home_btn = self._tool_button("go-home-symbolic", "Home folder", self.go_home)
        bar.append(self.home_btn)
        self.refresh_btn = self._tool_button("view-refresh-symbolic", compat.shortcut_label("Refresh (Ctrl+R)"), self.refresh)
        bar.append(self.refresh_btn)

        self.bookmark_btn = Gtk.MenuButton(icon_name="starred-symbolic", tooltip_text="Bookmarks")
        self.bookmark_btn.add_css_class("flat")
        self.bookmark_popover = Gtk.Popover()
        self.bookmark_popover.add_css_class("menu")
        self.bookmark_btn.set_popover(self.bookmark_popover)
        self.bookmark_popover.connect("show", lambda *_a: self._fill_bookmarks())
        bar.append(self.bookmark_btn)

        self.filter_btn = Gtk.ToggleButton(icon_name="edit-find-symbolic", tooltip_text=compat.shortcut_label("Filter (Ctrl+F)"))
        self.filter_btn.add_css_class("flat")
        bar.append(self.filter_btn)
        if self.is_local:
            bar.append(self.copy_btn)

        self.append(bar)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Filter by name…", hexpand=True)
        self.search_entry.connect("search-changed", self._on_filter_changed)
        self.search_entry.connect("stop-search", lambda *_a: self.filter_btn.set_active(False))
        self.search_bar = Gtk.SearchBar(child=self.search_entry, show_close_button=False)
        self.search_bar.connect_entry(self.search_entry)
        self.filter_btn.bind_property("active", self.search_bar, "search-mode-enabled",
                                      GObject.BindingFlags.BIDIRECTIONAL)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)
        self.append(self.search_bar)

    def _tool_button(self, icon, tooltip, callback):
        btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
        btn.add_css_class("flat")
        btn.connect("clicked", lambda *_a: callback())
        return btn

    def _build_list(self):
        self.store = Gio.ListStore.new(FileItem)
        self.filter = Gtk.CustomFilter.new(self._filter_func)
        self.filter_model = Gtk.FilterListModel(model=self.store, filter=self.filter)

        # Dragging from a file drags the files (see _setup_dnd); dragging
        # from anywhere else in the list draws a rubber band that selects.
        self.column_view = Gtk.ColumnView(show_row_separators=False, show_column_separators=False,
                                          reorderable=True, hexpand=True, vexpand=True,
                                          enable_rubberband=True)
        self.column_view.add_css_class("file-list")
        self.columns: dict[str, Gtk.ColumnViewColumn] = {}

        rank_sorter = Gtk.NumericSorter(expression=Gtk.PropertyExpression.new(FileItem, None, "rank"))
        self.multi_sorter = Gtk.MultiSorter()
        self.multi_sorter.append(rank_sorter)
        self.multi_sorter.append(self.column_view.get_sorter())
        self.sort_model = Gtk.SortListModel(model=self.filter_model, sorter=self.multi_sorter)
        self.selection = Gtk.MultiSelection(model=self.sort_model)
        self.selection.connect("selection-changed", lambda *_a: self._update_status())
        self.column_view.set_model(self.selection)

        visible = self.settings.get("columns_local" if self.is_local else "columns_remote")
        for key, title, expand, default in COLUMNS:
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._setup_name if key == "name" else self._setup_cell, key)
            factory.connect("bind", self._bind_name if key == "name" else self._bind_cell, key)
            col = Gtk.ColumnViewColumn(title=title, factory=factory, expand=expand, resizable=True)
            col.set_sorter(self._sorter_for(key))
            if key == "name":
                col.set_fixed_width(-1)
            show = default if visible is None else key in visible or key == "name"
            col.set_visible(show)
            self.columns[key] = col
            self.column_view.append_column(col)
        self.column_view.sort_by_column(self.columns["name"], Gtk.SortType.ASCENDING)
        self._install_column_menu()

        self.column_view.connect("activate", self._on_activate)

        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("pressed", self._on_right_click)
        self.column_view.add_controller(right)
        long_press = Gtk.GestureLongPress(touch_only=True)
        long_press.connect("pressed", lambda g, x, y: self._on_right_click(g, 1, x, y))
        self.column_view.add_controller(long_press)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.column_view.add_controller(keys)

        self._setup_dnd()

        self.context_menu = Gtk.PopoverMenu.new_from_model(self._build_context_menu())
        self.context_menu.set_parent(self.column_view)
        self.context_menu.set_has_arrow(False)
        self.context_menu.set_halign(Gtk.Align.START)

        self.scroller = Gtk.ScrolledWindow(child=self.column_view, vexpand=True)
        # (expands on its own: while an error page replaces the hidden list, an
        # overlay sized by its child alone would shrink to nothing)
        self.overlay = Gtk.Overlay(child=self.scroller, vexpand=True, hexpand=True)
        self.spinner = make_spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                   width_request=32, height_request=32, visible=False)
        self.overlay.add_overlay(self.spinner)
        self.status_page = Adw.StatusPage(visible=False, icon_name="dialog-warning-symbolic")
        self.status_page.add_css_class("compact")
        retry = Gtk.Button(label="Try Again", halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_a: self.refresh())
        self.status_page.set_child(retry)
        self.overlay.add_overlay(self.status_page)
        self.append(self.overlay)

    def _sorter_for(self, key):
        if key in ("name", "type", "perms", "owner"):
            prop = {"name": "name", "type": "kind", "perms": "perms", "owner": "owner"}[key]
            sorter = Gtk.StringSorter(expression=Gtk.PropertyExpression.new(FileItem, None, prop),
                                      ignore_case=True)
            if key == "name":
                sorter.set_collation(Gtk.Collation.FILENAME)
            if key == "type":
                # "kind" is computed lazily; sort with a Python key instead.
                return Gtk.CustomSorter.new(self._sort_by_type)
            return sorter
        prop = "size" if key == "size" else "mtime"
        return Gtk.NumericSorter(expression=Gtk.PropertyExpression.new(FileItem, None, prop))

    @staticmethod
    def _sort_by_type(a, b, *_args):
        ka, kb = a.get_kind().lower(), b.get_kind().lower()
        return Gtk.Ordering.SMALLER if ka < kb else Gtk.Ordering.LARGER if ka > kb else Gtk.Ordering.EQUAL

    def _install_column_menu(self):
        group = Gio.SimpleActionGroup()
        menu = Gio.Menu()
        for key, title, _expand, _default in COLUMNS:
            if key == "name":
                continue
            action = Gio.SimpleAction.new_stateful(
                f"col-{key}", None, GLib.Variant.new_boolean(self.columns[key].get_visible()))
            action.connect("change-state", self._on_toggle_column, key)
            group.add_action(action)
            menu.append(title, f"columns.col-{key}")
        self.insert_action_group("columns", group)
        for col in self.columns.values():
            col.set_header_menu(menu)

    def _on_toggle_column(self, action, value, key):
        action.set_state(value)
        self.columns[key].set_visible(value.get_boolean())
        visible = [k for k, c in self.columns.items() if c.get_visible()]
        self.settings["columns_local" if self.is_local else "columns_remote"] = visible

    # cell factories -------------------------------------------------------
    def _setup_name(self, _factory, cell, _key):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        image = Gtk.Image(pixel_size=16)
        label = Gtk.Label(xalign=0, ellipsize=3, hexpand=True)  # Pango.EllipsizeMode.END
        emblem = Gtk.Image(icon_name="emblem-symbolic-link", pixel_size=12, visible=False)
        emblem.add_css_class("dim-label")
        box.append(image)
        box.append(label)
        box.append(emblem)
        box.add_css_class("file-cell")
        box._cell = cell
        box._parts = (image, label, emblem)
        cell.set_child(box)

    def _bind_name(self, _factory, cell, _key):
        item = cell.get_item()
        box = cell.get_child()
        image, label, emblem = box._parts
        info = item.info
        label.set_text(item.name)
        if item.is_parent:
            image.set_from_gicon(PARENT_ICON)
            emblem.set_visible(False)
        else:
            image.set_from_gicon(icon_for(info.name, info.is_dir, info.is_link))
            emblem.set_visible(info.is_link)
        if info.hidden and not item.is_parent:
            label.add_css_class("dim-label")
        else:
            label.remove_css_class("dim-label")
        tip = item.name
        if info.link_target:
            tip += f" → {info.link_target}"
        box.set_tooltip_text(tip if not item.is_parent else "Parent folder")

    def _setup_cell(self, _factory, cell, key):
        label = Gtk.Label(xalign=1 if key == "size" else 0, ellipsize=3)
        label.add_css_class("dim-label" if key != "size" else "numeric")
        if key in ("mtime", "perms"):
            label.add_css_class("numeric")
        label.add_css_class("file-cell")
        label._cell = cell
        cell.set_child(label)

    def _bind_cell(self, _factory, cell, key):
        item = cell.get_item()
        label = cell.get_child()
        info = item.info
        if item.is_parent:
            label.set_text("")
            return
        if key == "size":
            label.set_text("" if info.is_dir else format_size(info.size, self.settings["size_format"]))
        elif key == "mtime":
            label.set_text(format_time(info.mtime, self.settings["date_format"]))
        elif key == "type":
            label.set_text(item.get_kind())
        elif key == "perms":
            label.set_text(item.perms)
        elif key == "owner":
            label.set_text(item.owner)

    def _build_footer(self):
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.add_css_class("pane-footer")
        self.title_label = Gtk.Label(label=self.title, xalign=0)
        self.title_label.add_css_class("pane-title")
        self.status_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.status_label.add_css_class("dim-label")
        self.free_label = Gtk.Label(xalign=1)
        self.free_label.add_css_class("dim-label")
        footer.append(self.title_label)
        footer.append(self.status_label)
        footer.append(self.free_label)
        self.append(footer)

    def _build_context_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        open_section = Gio.Menu()
        open_section.append("Open", "pane.open")
        open_section.append("Edit", "pane.edit")
        if not self.is_local:
            open_section.append("Open With Default App", "pane.open-external")
        menu.append_section(None, open_section)
        xfer = Gio.Menu()
        xfer.append("Download" if not self.is_local else "Upload", "pane.transfer")
        xfer.append("Move to Other Side", "pane.move")
        menu.append_section(None, xfer)
        edit = Gio.Menu()
        edit.append("Rename…", "pane.rename")
        edit.append("Duplicate…", "pane.duplicate")
        edit.append("Delete", "pane.delete")
        menu.append_section(None, edit)
        new = Gio.Menu()
        new.append("New Folder…", "pane.mkdir")
        new.append("New File…", "pane.newfile")
        if not self.is_local:
            new.append("New Symbolic Link…", "pane.symlink")
        menu.append_section(None, new)
        misc = Gio.Menu()
        misc.append("Copy Path", "pane.copy-path")
        if self.is_local:
            misc.append("Show in File Manager", "pane.reveal")
        misc.append("Add Bookmark", "pane.bookmark")
        misc.append("Properties", "pane.properties")
        menu.append_section(None, misc)
        return menu

    def _install_actions(self):
        group = Gio.SimpleActionGroup()
        names = ["open", "edit", "open-external", "transfer", "move", "rename", "duplicate",
                 "delete", "mkdir", "newfile", "symlink", "copy-path", "reveal", "bookmark",
                 "properties"]
        for name in names:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, n=name: self.controller.pane_action(self, n))
            group.add_action(action)
        self.actions = group
        self.insert_action_group("pane", group)

    # ------------------------------------------------------------------ data
    def _filter_func(self, item, *_args):
        if item.is_parent:
            return True
        if not self.settings["show_hidden"] and item.info.hidden:
            return False
        if self._filter_text and self._filter_text not in item.name.lower():
            return False
        return True

    def _on_filter_changed(self, entry):
        self._filter_text = entry.get_text().strip().lower()
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_status()

    def _on_search_mode(self, bar, _pspec):
        if not bar.get_search_mode() and self._filter_text:
            self.search_entry.set_text("")
        if bar.get_search_mode():
            self.search_entry.grab_focus()
        else:
            self.focus_list()

    def _on_setting(self, key, _value):
        if key == "show_hidden":
            self.filter.changed(Gtk.FilterChange.DIFFERENT)
            self._update_status()
        elif key in ("size_format", "date_format"):
            self._repopulate()
        elif key == "dirs_first":
            self._repopulate()

    def _repopulate(self):
        if self.path is not None:
            self._populate(self.entries, keep_selection=True)

    def load(self, path: str, select: str | None = None, history: bool = True,
             on_loaded=None, quiet: bool = False, fallback: str | None = None):
        """List ``path`` in the background and show it.  If it cannot be
        listed and ``fallback`` is given, show that folder instead (and say why)."""
        self._load_serial += 1
        serial = self._load_serial
        GLib.timeout_add(250, lambda: (self._show_spinner(serial), False)[1])

        def job(fs):
            entries = fs.listdir(path)
            free = fs.disk_free(path) if self.is_local or fs.protocol == "scp" else None
            return entries, free

        def done(result):
            if serial != self._load_serial:
                if on_loaded:
                    on_loaded(None)        # superseded by a newer load
                return
            entries, free = result
            self.spinner.set_visible(False)
            self.status_page.set_visible(False)
            self.scroller.set_visible(True)
            if history and self.path is not None and self.path != path:
                self.back_stack.append(self.path)
                self.forward_stack.clear()
            previous = self.path
            self.path = path
            self._free_space = free
            self.pathbar.set_path(path)
            select_name = select
            if select is None and previous and previous != "/" and \
                    self._parent(previous) == path:
                select_name = posixpath.basename(previous.rstrip("/"))   # came up from there
            self._populate(entries, select_name=select_name, keep_selection=previous == path)
            self._update_nav()
            if self.is_local:
                self._watch(path)
            self.controller.pane_loaded(self)
            if on_loaded:
                on_loaded(True)

        def failed(exc):
            if serial != self._load_serial:
                if on_loaded:
                    on_loaded(None)
                return
            if fallback and fallback != path:
                reason = str(exc) if path in str(exc) else f"Cannot open {path}: {exc}"
                self.controller.toast(f"{reason}. Showing {fallback} instead.")
                note = getattr(self.controller, "log", None)
                if note is not None:
                    note(f"{reason}; showing {fallback} instead")
                self.load(fallback, select=select, history=history, on_loaded=on_loaded, quiet=quiet)
                return
            self._loaded_serial = serial
            self.spinner.set_visible(False)
            if self.path is None:
                self.status_page.set_title("Cannot Open Folder")
                self.status_page.set_description(str(exc))
                self.status_page.set_visible(True)
                self.scroller.set_visible(False)
                self.pathbar.set_path(path)
            elif not quiet:
                self.controller.toast(f"Cannot open {path}: {exc}")
            if on_loaded:
                on_loaded(False)

        self.backend.run(job, done, failed)

    def _show_spinner(self, serial):
        # Still loading after 250 ms: show progress.
        if serial == self._load_serial and self._loaded_serial != serial:
            self.spinner.set_visible(True)

    def _populate(self, entries: list[FileInfo], select_name: str | None = None, keep_selection=False):
        self._loaded_serial = self._load_serial
        selected_names = {i.name for i in self.get_selected_infos()} if keep_selection else set()
        focus_name = self.focused_name() if keep_selection else None
        self.entries = entries
        dirs_first = self.settings["dirs_first"]
        items = []
        if self.path and self.path != "/":
            items.append(FileItem(None, self._parent(self.path)))
        items.extend(FileItem(e, dirs_first=dirs_first) for e in entries)
        self.store.splice(0, self.store.get_n_items(), items)

        target = select_name or focus_name
        if selected_names or target:
            self.selection.unselect_all()
            n = self.sort_model.get_n_items()
            focus_pos = None
            for pos in range(n):
                item = self.sort_model.get_item(pos)
                if item.name in selected_names:
                    self.selection.select_item(pos, False)
                if target and item.name == target:
                    focus_pos = pos
            if focus_pos is not None:
                if not selected_names:
                    self.selection.select_item(focus_pos, True)
                self._scroll_to(focus_pos, focus=self.column_view.has_focus() or
                                self.controller.active_pane is self)
        elif self.sort_model.get_n_items():
            self._scroll_to(0, focus=False)
        self._update_status()

    def _scroll_to(self, pos, focus=True):
        flags = Gtk.ListScrollFlags.FOCUS if focus else Gtk.ListScrollFlags.NONE
        try:
            self.column_view.scroll_to(pos, None, flags, None)
        except (TypeError, GLib.Error):
            pass

    def _update_nav(self):
        self.back_btn.set_sensitive(bool(self.back_stack))
        self.fwd_btn.set_sensitive(bool(self.forward_stack))
        self.up_btn.set_sensitive(self.path not in (None, "/"))

    def _update_status(self):
        total = self.sort_model.get_n_items()
        if total and self.path != "/":
            total -= 1
        hidden = len(self.entries) - total if not self.settings["show_hidden"] or self._filter_text else 0
        infos = self.get_selected_infos()
        parts = [f"{total} item{'s' if total != 1 else ''}"]
        if hidden > 0:
            parts[0] += f" ({hidden} hidden)"
        if infos:
            size = sum(i.size for i in infos if not i.is_dir)
            text = f"{len(infos)} selected"
            if size:
                text += f" ({format_size(size, self.settings['size_format'])})"
            parts.append(text)
        self.status_label.set_text("  ·  ".join(parts))
        if self._free_space is not None:
            self.free_label.set_text(f"{format_size(self._free_space)} free")
            self.free_label.set_visible(True)
        else:
            self.free_label.set_visible(False)
        self.controller.selection_changed(self)

    # ------------------------------------------------------------------ navigation
    def refresh(self, quiet=False):
        if self.path is not None:
            self.load(self.path, history=False, quiet=quiet)
        elif self.home_path:
            self.load(self.home_path, history=False)

    def go_up(self):
        if self.path and self.path != "/":
            self.load(self._parent(self.path))

    def go_back(self):
        if self.back_stack:
            target = self.back_stack.pop()
            if self.path:
                self.forward_stack.append(self.path)
            self.load(target, history=False)
            self._update_nav()

    def go_forward(self):
        if self.forward_stack:
            target = self.forward_stack.pop()
            if self.path:
                self.back_stack.append(self.path)
            self.load(target, history=False)
            self._update_nav()

    def go_home(self):
        if self.home_path:
            self.load(self.home_path)

    def _parent(self, path: str) -> str:
        if self.is_local:
            return compat.parent_dir(path)          # knows about \\server\share roots
        return posixpath.dirname(path.rstrip("/")) or "/"

    def set_home(self, path: str):
        self.home_path = path
        self.pathbar.home = path

    def focus_list(self):
        self.column_view.grab_focus()

    def focus_path_entry(self):
        self.pathbar.start_editing()

    def toggle_filter(self):
        self.filter_btn.set_active(not self.filter_btn.get_active())

    # ------------------------------------------------------------------ selection
    def get_selected_infos(self) -> list[FileInfo]:
        out = []
        n = self.sort_model.get_n_items()
        sel = self.selection
        bitset = sel.get_selection()
        if bitset.get_size() == 0:
            return out
        for pos in range(n):
            if sel.is_selected(pos):
                item = self.sort_model.get_item(pos)
                if not item.is_parent:
                    out.append(item.info)
        return out

    def focused_name(self) -> str | None:
        infos = self.get_selected_infos()
        return infos[0].name if infos else None

    def select_names(self, names: set[str]):
        self.selection.unselect_all()
        first = None
        for pos in range(self.sort_model.get_n_items()):
            if self.sort_model.get_item(pos).name in names:
                self.selection.select_item(pos, False)
                first = pos if first is None else first
        if first is not None:
            self._scroll_to(first)

    def _item_at(self, x, y):
        widget = self.column_view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self.column_view:
            cell = getattr(widget, "_cell", None)
            if cell is not None:
                pos = cell.get_position()
                if pos != Gtk.INVALID_LIST_POSITION:
                    return pos, cell.get_item()
                return None, None
            widget = widget.get_parent()
        return None, None

    # ------------------------------------------------------------------ events
    def _on_activate(self, _view, position):
        item = self.sort_model.get_item(position)
        if item is None:
            return
        if item.is_parent:
            self.go_up()
        elif item.info.is_dir:
            self.load(item.info.path)
        else:
            self.controller.activate_file(self, item.info)

    def _on_right_click(self, gesture, _n, x, y):
        self.controller.set_active_pane(self)
        pos, item = self._item_at(x, y)
        if pos is None:
            self.selection.unselect_all()
        elif not self.selection.is_selected(pos):
            self.selection.select_item(pos, True)
        self.controller.update_actions(self)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.context_menu.set_pointing_to(rect)
        self.context_menu.popup()
        if isinstance(gesture, Gtk.GestureClick):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_key(self, _ctrl, keyval, _keycode, state):
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if keyval == Gdk.KEY_BackSpace and not mods:
            self.go_up()
            return True
        if keyval in (Gdk.KEY_Left,) and mods == Gdk.ModifierType.ALT_MASK:
            self.go_back()
            return True
        if keyval in (Gdk.KEY_Right,) and mods == Gdk.ModifierType.ALT_MASK:
            self.go_forward()
            return True
        if keyval == Gdk.KEY_Up and mods == Gdk.ModifierType.ALT_MASK:
            self.go_up()
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and not mods:
            self.controller.pane_action(self, "delete")
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and mods == Gdk.ModifierType.SHIFT_MASK:
            self.controller.pane_action(self, "delete-permanently")
            return True
        if keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and mods == Gdk.ModifierType.SHIFT_MASK):
            self._open_menu_at_focus()
            return True
        if keyval == Gdk.KEY_Escape and self._filter_text:
            self.filter_btn.set_active(False)
            return True
        # type-ahead find
        if mods & ~Gdk.ModifierType.SHIFT_MASK:
            return False
        ch = Gdk.keyval_to_unicode(keyval)
        if ch and chr(ch).isprintable() and not chr(ch).isspace():
            now = time.monotonic()
            if now - self._typeahead_time > 1.0:
                self._typeahead = ""
            self._typeahead_time = now
            self._typeahead += chr(ch).lower()
            for pos in range(self.sort_model.get_n_items()):
                item = self.sort_model.get_item(pos)
                if not item.is_parent and item.name.lower().startswith(self._typeahead):
                    self.selection.select_item(pos, True)
                    self._scroll_to(pos)
                    break
            return True
        return False

    def _open_menu_at_focus(self):
        infos = self.get_selected_infos()
        if not infos:
            return
        self.controller.update_actions(self)
        rect = Gdk.Rectangle()
        w = self.column_view.get_width()
        rect.x, rect.y, rect.width, rect.height = w // 3, 40, 1, 1
        self.context_menu.set_pointing_to(rect)
        self.context_menu.popup()

    # ------------------------------------------------------------------ bookmarks
    def _fill_bookmarks(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        def add_button(label, path, icon=None, removable=False):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            btn = Gtk.Button(hexpand=True)
            btn.add_css_class("flat")
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            if icon:
                inner.append(Gtk.Image(icon_name=icon))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            title = Gtk.Label(label=label, xalign=0, ellipsize=3, max_width_chars=36)
            text.append(title)
            if label != path:
                sub = Gtk.Label(label=path, xalign=0, ellipsize=1, max_width_chars=36)
                sub.add_css_class("caption")
                sub.add_css_class("dim-label")
                text.append(sub)
            inner.append(text)
            btn.set_child(inner)

            def go(*_a):
                self.bookmark_popover.popdown()
                self.load(path)
            btn.connect("clicked", go)
            row.append(btn)
            if removable:
                rm = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove bookmark",
                                valign=Gtk.Align.CENTER)
                rm.add_css_class("flat")
                rm.add_css_class("circular")

                def remove(*_a):
                    self.controller.remove_bookmark(self, path)
                    self._fill_bookmarks()
                rm.connect("clicked", remove)
                row.append(rm)
            box.append(row)

        add = Gtk.Button(label="Bookmark This Folder")
        add.add_css_class("flat")
        add.connect("clicked", lambda *_a: (self.controller.add_bookmark(self, self.path),
                                             self.bookmark_popover.popdown()))
        box.append(add)
        bookmarks = self.controller.get_bookmarks(self)
        if bookmarks:
            box.append(Gtk.Separator())
            for path in bookmarks:
                add_button(posixpath.basename(path.rstrip("/")) or "/", path, "folder-symbolic", True)
        if self.is_local:
            box.append(Gtk.Separator())
            home = compat.home_dir()
            places = [("Home", home, "user-home-symbolic")]
            for special, icon in ((GLib.UserDirectory.DIRECTORY_DESKTOP, "user-desktop-symbolic"),
                                  (GLib.UserDirectory.DIRECTORY_DOCUMENTS, "folder-documents-symbolic"),
                                  (GLib.UserDirectory.DIRECTORY_DOWNLOAD, "folder-download-symbolic")):
                p = GLib.get_user_special_dir(special)
                if p and compat.to_internal(p) != home:
                    p = compat.to_internal(p)
                    places.append((posixpath.basename(p), p, icon))
            places.append(("This PC" if compat.IS_WINDOWS else "File System", "/", "drive-harddisk-symbolic"))
            for label, path, icon in places:
                add_button(label, path, icon)
        elif self.home_path:
            box.append(Gtk.Separator())
            add_button("Home", self.home_path, "user-home-symbolic")
            add_button("Root", "/", "drive-harddisk-symbolic")
        scroller = Gtk.ScrolledWindow(child=box, propagate_natural_height=True,
                                      max_content_height=480, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.bookmark_popover.set_child(scroller)

    # ------------------------------------------------------------------ local monitoring
    def _watch(self, path):
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        native = compat.to_native(path)
        if not native:
            return
        try:
            self._monitor = Gio.File.new_for_path(native).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.set_rate_limit(500)
            self._monitor.connect("changed", self._on_monitor_changed)
        except GLib.Error:
            self._monitor = None

    def _on_monitor_changed(self, *_args):
        if self._monitor_timeout:
            return
        def fire():
            self._monitor_timeout = 0
            self.refresh(quiet=True)
            return False
        self._monitor_timeout = GLib.timeout_add(400, fire)

    def shutdown(self):
        if self._dragout is not None:
            self._dragout.abort()
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        FilePane._registry.pop(self.pane_id, None)
        self.settings.disconnect(self._on_setting)
        self.context_menu.unparent()

    # ------------------------------------------------------------------ drag and drop
    def _setup_dnd(self):
        actions = Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        source = Gtk.DragSource(actions=actions)
        source.connect("prepare", self._on_drag_prepare)
        source.connect("drag-begin", self._on_drag_begin)
        source.connect("drag-cancel", self._on_drag_cancel)
        source.connect("drag-end", self._on_drag_end)
        self.column_view.add_controller(source)
        # A press on a file drags files; GTK's rubber band (the list's own
        # gesture, which would take the drag) is switched off before the
        # list sees such a press, and back on when it is over.  So only a
        # press on empty space starts a rubber band.
        press = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        press.connect("pressed", self._on_list_press)
        press.connect("end", lambda *_a: self._restore_rubberband())
        self.column_view.add_controller(press)
        # Drops are taken by the window, which hands them to drop_internal()
        # or drop_external() of the pane under the pointer (see window.py).

    def _on_list_press(self, _gesture, _n_press, x, y):
        if self._item_at(x, y)[1] is not None:
            self.column_view.set_enable_rubberband(False)

    def _restore_rubberband(self):
        def restore():
            self.column_view.set_enable_rubberband(True)
            return False
        if not self.column_view.get_enable_rubberband():
            GLib.idle_add(restore)

    def drop_action(self):
        seat = self.get_display().get_default_seat()
        keyboard = seat.get_keyboard() if seat else None
        shift = bool(keyboard and keyboard.get_modifier_state() & Gdk.ModifierType.SHIFT_MASK)
        return Gdk.DragAction.MOVE if shift else Gdk.DragAction.COPY

    @staticmethod
    def accept_external(target, drop):
        """Files of another program (a drag from anySCP's own panes is not)."""
        if FilePane._active_drag is not None:
            return False
        return bool(drop.get_actions() & target.get_actions()) and \
            target.get_formats().match_gtype(drop.get_formats()) != GObject.TYPE_INVALID

    def _on_drag_prepare(self, _source, x, y):
        pos, item = self._item_at(x, y)
        if pos is None or item is None or item.is_parent:
            return None
        if not self.selection.is_selected(pos):
            self.selection.select_item(pos, True)
        infos = self.get_selected_infos()
        if not infos:
            return None
        self._drag_infos = infos
        self._drag_cancelled = False
        self._drag_download = None
        FilePane._active_drag = (self, infos)
        # For our own panes: a marker.  For other programs: the files.
        providers = [dragout.pane_marker(self.pane_id)]
        if self.is_local:
            paths = [p for p in (compat.to_native(i.path) for i in infos) if p]
            files = [Gio.File.new_for_path(p) for p in paths]
            providers.append(Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list(files)))
            if compat.IS_WINDOWS and paths:         # Explorer does not take GTK's file list
                explorer = dragout.provider_for_paths(paths)
                if explorer is not None:
                    providers.append(explorer)
        else:
            key = tuple((i.path, i.size, i.mtime, i.is_dir) for i in infos)
            download = self._dragout
            if download is None or download.key != key or not download.usable():
                download = dragout.RemoteDownload(self.controller, infos)
            self._dragout = self._drag_download = download
            self._drag_provider = dragout.provider_for_download(download)
            providers.append(self._drag_provider)
        return Gdk.ContentProvider.new_union(providers)

    def _on_drag_cancel(self, _source, _drag, _reason):
        """Escape, a drop where nothing takes it, or dropped back where it came
        from.  Handled here: nothing to undo – and GTK's own handling on
        Windows, an animation of the drag icon sliding back, crashes."""
        self._drag_cancelled = True
        return True

    def _on_drag_end(self, _source=None, drag=None, *_a):
        FilePane._active_drag = None
        self._restore_rubberband()
        win32drag.release_drag_later(drag)
        FilePane._cancel_leave_timer()
        provider, self._drag_provider = self._drag_provider, None
        if hasattr(provider, "forget"):
            provider.forget()
        download, self._drag_download = self._drag_download, None
        if download is not None:
            download.drag_finished(self._drag_cancelled)

    # A drag whose pointer leaves the window is on its way to another
    # program: start fetching a server pane's files right away.
    _leave_timer = 0

    @classmethod
    def _cancel_leave_timer(cls):
        if cls._leave_timer:
            GLib.source_remove(cls._leave_timer)
            cls._leave_timer = 0

    @classmethod
    def drag_entered_window(cls):
        cls._cancel_leave_timer()

    @classmethod
    def drag_left_window(cls):
        def start():
            cls._leave_timer = 0
            active = cls._active_drag       # None by now if it was a drop inside the window
            if active is not None and active[0]._drag_download is not None:
                active[0]._drag_download.start()
            return False
        cls._cancel_leave_timer()
        if cls._active_drag is not None:
            cls._leave_timer = GLib.timeout_add(150, start)

    def _on_drag_begin(self, source, drag):
        infos = getattr(self, "_drag_infos", [])
        if not infos:
            return
        # Windows, monitors left of or above the main one: GTK gets the
        # pointer's position wrong there (see win32drag)
        win32drag.fix_drag_positions()
        win32drag.hold_drag(drag)               # (GTK on Windows releases it once too often)
        if dragout.drag_icon_misplaced(self.get_display()) and not win32drag.icon_follows_pointer(drag):
            source.set_icon(dragout.blank_icon(), 0, 0)     # (Windows still shows its drag cursor)
            return
        theme = Gtk.IconTheme.get_for_display(self.get_display())
        gicon = icon_for(infos[0].name, infos[0].is_dir) if len(infos) == 1 else \
            Gio.ThemedIcon.new("edit-copy")
        paintable = theme.lookup_by_gicon(gicon, 32, self.get_scale_factor(),
                                          Gtk.TextDirection.NONE, 0)
        source.set_icon(paintable, 0, 0)

    def _drop_target(self, x, y):
        """Where files dropped at x, y of the file list go: (folder, the row
        showing that folder, or None for the folder the pane shows).
        (None, None) if a drop there does nothing: a drag from this pane
        dropped back on the folder it came from."""
        _pos, item = self._item_at(x, y)
        active = FilePane._active_drag
        own = active is not None and active[0] is self
        dragged = {i.path for i in active[1]} if own else set()
        target_dir, row = self.path, None
        if item is not None and (item.is_parent or item.info.is_dir) and item.info.path not in dragged:
            target_dir, row = item.info.path, self._row_at(x, y)
        if own and target_dir == self.path:
            return None, None
        return target_dir, row

    def _row_at(self, x, y):
        widget = self.column_view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self.column_view:
            if widget.get_css_name() == "row":
                return widget
            widget = widget.get_parent()
        return None

    def show_drop(self, x, y) -> Gdk.DragAction:
        """Feedback while files are dragged over x, y of the file list: the
        folder they would go to is highlighted (a folder's row, or the whole
        list for the folder shown).  Returns what a drop there would do."""
        target_dir, row = self._drop_target(x, y)
        if row is not self._drop_row:
            if self._drop_row is not None:
                self._drop_row.remove_css_class("drop-target")
            if row is not None:
                row.add_css_class("drop-target")
            self._drop_row = row
        if target_dir is not None and row is None:
            self.add_css_class("drop-hover")
        else:
            self.remove_css_class("drop-hover")
        if target_dir is None:
            return Gdk.DragAction(0)
        active = FilePane._active_drag
        if active is not None and active[0] is self:
            return Gdk.DragAction.MOVE          # into a folder of this pane: moved there
        return self.drop_action()

    def clear_drop(self):
        self.remove_css_class("drop-hover")
        if self._drop_row is not None:
            self._drop_row.remove_css_class("drop-target")
            self._drop_row = None

    def _drop_destination(self, x, y):
        """(folder the drop goes to or None, move instead of copy)"""
        self.clear_drop()
        target_dir, _row = self._drop_target(x, y)
        seat = self.get_display().get_default_seat()
        keyboard = seat.get_keyboard() if seat else None
        move = bool(keyboard and keyboard.get_modifier_state() & Gdk.ModifierType.SHIFT_MASK)
        return target_dir, move

    def drop_internal(self, x, y) -> Gdk.DragAction:
        """A drag from one of anySCP's panes was dropped at x, y of this pane's
        file list.  Returns what the drag did: COPY when anySCP copies or moves
        the files (it does that itself), nothing if it was dropped back where
        it came from."""
        target_dir, move = self._drop_destination(x, y)
        internal, FilePane._active_drag = FilePane._active_drag, None
        if internal is None:
            return Gdk.DragAction(0)
        source, infos = internal
        if source._drag_download is not None:
            source._drag_download.abort()       # not going to another program after all
        if target_dir is None or FilePane._registry.get(source.pane_id) is not source or not infos:
            return Gdk.DragAction(0)            # (None: dropped back where it came from)
        if source is self:
            if target_dir == self.path:
                return Gdk.DragAction(0)
            self.controller.move_within(self, infos, target_dir)
        else:
            self.controller.drop_from_pane(source, infos, self, target_dir, move)
        return Gdk.DragAction.COPY

    def drop_external(self, value, x, y) -> bool:
        """Files of another program were dropped at x, y of the file list."""
        target_dir, move = self._drop_destination(x, y)
        if target_dir is None:
            return False
        if isinstance(value, str) and value.startswith("anyscp-pane:"):
            return False                    # stale marker from a finished drag
        if isinstance(value, Gdk.FileList):
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            if paths:
                self.controller.drop_local_paths(paths, self, target_dir, move)
                return True
        if isinstance(value, str) and value.startswith("file://"):
            paths = [Gio.File.new_for_uri(u.strip()).get_path() for u in value.splitlines() if u.strip()]
            paths = [p for p in paths if p]
            if paths:
                self.controller.drop_local_paths(paths, self, target_dir, move)
                return True
        return False
