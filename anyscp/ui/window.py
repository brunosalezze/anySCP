# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Main window: header bar, session tabs, transfer queue along the bottom."""

from __future__ import annotations

import time

import gi
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk

from .. import compat
from ..config import site_subtitle
from ..transfer import FAILED
from ..utils import color_dot_icon
from .prompts import alert
from . import dragout
from .file_pane import FilePane
from .queue_panel import ProgressIcon, QueuePanel
from .session_page import SessionPage
from .site_dialog import PROTOCOL_ICONS, SiteDialog
from .welcome import WelcomePage

QUEUE_COLLAPSE_DELAY_MS = 2000  # the list of transfers closes this long after the last one finished

PAGE_ACTIONS = {
    # name: accelerators
    "rename": ["F2"],
    "edit": ["F4"],
    "transfer": ["F5"],
    "move": ["F6"],
    "mkdir": ["F7"],
    "delete": ["F8"],
    "properties": ["F9"],
    "newfile": ["<Shift>F4"],
    "refresh": ["<Primary>r"],
    "filter": ["<Primary>f"],
    "focus-path": ["<Primary>l"],
    "sync": ["<Primary>s"],
    "compare": ["<Primary>equal"],
    "find": ["<Primary><Shift>f"],
    "terminal": ["<Primary><Shift>t"],
    "info": [],
    "log": [],
    "reconnect": [],
    "sync-browse": [],
    "duplicate": [],
    "copy-path": ["<Primary><Shift>c"],
}


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="anySCP")
        self.app = app
        self.settings = app.settings
        self.sites = app.sites
        self.transfers = app.transfers
        self._force_close: set = set()
        self.set_default_size(self.settings["window_width"], self.settings["window_height"])
        if self.settings["window_maximized"]:
            self.maximize()
        self.set_size_request(640, 420)

        self.toasts = Adw.ToastOverlay()
        # The transfers are a bar over the full width of the bottom of the
        # window, as in WinSCP.  The bar is always there; the list of
        # transfers below it opens when a transfer starts and closes by itself
        # shortly after the last one has finished (and on a click on the bar).
        view = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.RAISED,
                               bottom_bar_style=Adw.ToolbarStyle.RAISED_BORDER)
        self.queue_panel = QueuePanel(self.transfers)
        view.add_bottom_bar(self.queue_panel)
        self._queue_busy = False            # transfers are queued or running
        self._queue_busy_since = 0.0
        self._queue_autohide = False        # close the list once the queue has drained
        self._queue_hide_timer = 0
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="anySCP")
        header.set_title_widget(self.window_title)

        new_btn = Gtk.Button(icon_name="tab-new-symbolic", tooltip_text=compat.shortcut_label("New Session (Ctrl+N)"),
                             action_name="win.new-session")
        header.pack_start(new_btn)

        self.queue_btn = Gtk.ToggleButton(tooltip_text=compat.shortcut_label("Transfers (Ctrl+J)"))
        self.queue_btn.add_css_class("image-button")
        self.queue_btn.set_child(ProgressIcon(self.transfers))
        self.queue_btn.bind_property("active", self.queue_panel, "expanded",
                                     GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.queue_btn.connect("notify::active", self._on_queue_toggled)
        header.pack_end(self._main_menu_button())
        header.pack_end(self.queue_btn)
        self.session_menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="Session",
                                               menu_model=self._session_menu())
        header.pack_end(self.session_menu_btn)
        view.add_top_bar(header)

        self.tab_view = Adw.TabView()
        self.tab_view.connect("close-page", self._on_close_page)
        self.tab_view.connect("notify::selected-page", lambda *_a: self._on_page_switched())
        self.tab_view.connect("page-detached", lambda *_a: self._update_stack())
        self.tab_bar = Adw.TabBar(view=self.tab_view, autohide=False)
        view.add_top_bar(self.tab_bar)

        self.welcome = WelcomePage(self)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.welcome, "welcome")
        self.stack.add_named(self.tab_view, "tabs")
        # Toasts belong to the content, so they never cover the queue.
        self.toasts.set_child(self.stack)
        view.set_content(self.toasts)
        self.set_content(view)

        # A drag that leaves the window is heading for another program
        # (see dragout.py).
        drag_watch = Gtk.DropControllerMotion()
        drag_watch.connect("enter", lambda *_a: FilePane.drag_entered_window())
        drag_watch.connect("leave", lambda *_a: FilePane.drag_left_window())
        self.add_controller(drag_watch)
        self._setup_drops()

        self._install_actions()
        self.connect("close-request", self._on_close_request)
        self.transfers.connect("job-finished", self._on_job_finished)
        self.transfers.connect("changed", self._on_transfers_changed)
        self._update_stack()
        GLib.idle_add(lambda: (self.welcome.focus(), False)[1])

    # ------------------------------------------------------------------ menus
    def _main_menu_button(self):
        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append("New Session…", "win.new-session")
        s1.append("New Site…", "win.new-site")
        s1.append("Import Sites from WinSCP…", "win.import-winscp")
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append("Show Hidden Files", "win.show-hidden")
        s2.append("Show Local Pane", "win.show-local")
        s2.append("Function Key Bar", "win.show-fkeys")
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append("Preferences", "app.preferences")
        s3.append("Keyboard Shortcuts", "win.shortcuts")
        s3.append("About anySCP", "app.about")
        menu.append_section(None, s3)
        btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu",
                             primary=True)
        return btn

    def _session_menu(self):
        menu = Gio.Menu()
        a = Gio.Menu()
        a.append("Synchronize…", "win.sync")
        a.append("Compare Directories", "win.compare")
        a.append("Synchronized Browsing", "win.sync-browse")
        a.append("Find Files…", "win.find")
        menu.append_section(None, a)
        b = Gio.Menu()
        b.append("Open Terminal", "win.terminal")
        b.append("Server & Protocol Information", "win.info")
        b.append("Session Log", "win.log")
        menu.append_section(None, b)
        c = Gio.Menu()
        c.append("Reconnect", "win.reconnect")
        c.append("Disconnect", "win.close-tab")
        menu.append_section(None, c)
        return menu

    # ------------------------------------------------------------------ actions
    def _install_actions(self):
        app = self.app

        def add(name, callback, accels=()):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            self.add_action(action)
            if accels:
                app.set_accels_for_action(f"win.{name}", list(accels))
            return action

        add("new-session", self.show_sites, ["<Primary>n", "<Primary>t"])
        add("new-site", lambda: SiteDialog(self, select_new=True).present(self), [])
        add("close-tab", self.close_current, ["<Primary>w"])
        add("import-winscp", lambda: self.import_winscp(self))
        add("toggle-queue", lambda: self.queue_btn.set_active(not self.queue_btn.get_active()),
            ["<Primary>j"])
        add("shortcuts", self.show_shortcuts, ["<Primary>question", "<Primary>F1"])
        for name, accels in PAGE_ACTIONS.items():
            add(name, lambda n=name: self._page_action(n), accels)

        for key, name in (("show_hidden", "show-hidden"), ("show_local_pane", "show-local"),
                          ("show_fkeys_bar", "show-fkeys")):
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(bool(self.settings[key])))
            action.connect("change-state", self._on_toggle_setting, key)
            self.add_action(action)
        app.set_accels_for_action("win.show-hidden", ["<Primary>h"])
        self.settings.connect(self._on_setting)
        self.update_actions()

    def _on_toggle_setting(self, action, value, key):
        action.set_state(value)
        self.settings[key] = value.get_boolean()

    def _on_setting(self, key, value):
        names = {"show_hidden": "show-hidden", "show_local_pane": "show-local", "show_fkeys_bar": "show-fkeys"}
        if key in names:
            action = self.lookup_action(names[key])
            if action.get_state().get_boolean() != bool(value):
                action.set_state(GLib.Variant.new_boolean(bool(value)))
        elif key == "parallel_transfers":
            self.transfers.set_parallel(value)

    def _page_action(self, name):
        page = self.current_page()
        if page is not None:
            page.do_action(name)

    def update_actions(self):
        page = self.current_page()
        has_page = page is not None
        ready = has_page and page.commander is not None
        connected = ready and page.session.state == "connected"
        for name in PAGE_ACTIONS:
            action = self.lookup_action(name)
            if action is None:
                continue
            if name in ("rename", "edit", "transfer", "move", "mkdir", "delete", "properties",
                        "newfile", "duplicate", "copy-path"):
                enabled = ready and page.action_enabled(name)
            elif name in ("sync", "compare", "find", "terminal", "info", "sync-browse"):
                enabled = connected
            elif name == "reconnect":
                enabled = has_page and page.session.state in ("disconnected", "failed")
            elif name == "log":
                enabled = has_page
            else:
                enabled = ready
            action.set_enabled(enabled)
        self.lookup_action("close-tab").set_enabled(has_page)
        self.session_menu_btn.set_sensitive(has_page)

    # ------------------------------------------------------------------ pages
    def current_page(self) -> SessionPage | None:
        tp = self.tab_view.get_selected_page()
        return tp.get_child() if tp is not None else None

    def pages(self) -> list[SessionPage]:
        return [self.tab_view.get_nth_page(i).get_child() for i in range(self.tab_view.get_n_pages())]

    def open_session(self, site: dict):
        # Replace a failed tab for the same site rather than piling them up.
        for page in self.pages():
            if site.get("id") and page.site.get("id") == site.get("id") and page.commander is None \
                    and page.session.state == "failed":
                self.close_page(page, force=True)
        page = SessionPage(self, site)
        tp = self.tab_view.append(page)
        page.tab_page = tp
        self._update_tab(page)
        page.connect("title-changed", lambda p: self._update_tab(p))
        page.connect("state-changed", lambda p: self._update_tab(p))
        self.tab_view.set_selected_page(tp)
        self._update_stack()

    def _update_tab(self, page: SessionPage):
        tp = page.tab_page
        if tp is None:
            return
        tp.set_title(page.title)
        tp.set_tooltip(GLib.markup_escape_text(site_subtitle(page.site)))
        tp.set_loading(page.session.state == "connecting")
        color = page.site.get("color")
        if color:
            tp.set_icon(color_dot_icon(color))
        else:
            tp.set_icon(Gio.ThemedIcon.new(PROTOCOL_ICONS.get(page.site.get("protocol"),
                                                               "network-server-symbolic")))
        tp.set_needs_attention(page.session.state in ("disconnected", "failed") and page.commander is not None)
        if page is self.current_page():
            self._update_title()
            self.update_actions()

    def _update_title(self):
        page = self.current_page()
        if page is None:
            self.window_title.set_title("anySCP")
            self.window_title.set_subtitle("")
            self.set_title("anySCP")
        else:
            self.window_title.set_title(page.title)
            self.window_title.set_subtitle(page.subtitle)
            self.set_title(f"{page.title} – anySCP")

    def _on_page_switched(self):
        self._update_title()
        self.update_actions()
        page = self.current_page()
        if page is not None and page.active_pane is not None:
            GLib.idle_add(lambda: (page.active_pane.focus_list(), False)[1])

    def _update_stack(self):
        has = self.tab_view.get_n_pages() > 0
        self.stack.set_visible_child_name("tabs" if has else "welcome")
        self.tab_bar.set_visible(has)
        if not has:
            self.welcome.refresh()
        self._update_title()
        self.update_actions()

    def close_current(self):
        tp = self.tab_view.get_selected_page()
        if tp is not None:
            self.tab_view.close_page(tp)

    def close_page(self, page: SessionPage, force=False):
        if page.tab_page is None:
            return
        if force:
            self._force_close.add(page)
        self.tab_view.close_page(page.tab_page)

    def _on_close_page(self, view, tp):
        page: SessionPage = tp.get_child()
        busy = page.busy_transfers()
        if page in self._force_close or (not busy and (not self.settings["confirm_close_session"]
                                                       or page.commander is None)):
            self._force_close.discard(page)
            page.shutdown()
            view.close_page_finish(tp, True)
            GLib.idle_add(lambda: (self._update_stack(), False)[1])
            return True

        def response(rid):
            if rid == "close":
                page.shutdown()
                view.close_page_finish(tp, True)
                self._update_stack()
            else:
                view.close_page_finish(tp, False)
        body = (f"{busy} transfer(s) for this session will be cancelled." if busy
                else "The connection will be closed.")
        alert(self, f"Disconnect from {page.title}?", body,
              responses=(("cancel", "_Cancel"), ("close", "_Disconnect")), default="close",
              close="cancel", destructive="close" if busy else None,
              suggested=None if busy else "close", callback=response)
        return True

    # ------------------------------------------------------------------ sites
    def show_sites(self):
        SiteDialog(self).present(self)

    def edit_site(self, site, from_page=None):
        SiteDialog(self, site).present(self)

    def sites_changed(self):
        self.welcome.refresh()
        for page in self.pages():
            saved = self.sites.get(page.site.get("id", ""))
            if saved is not None:
                for key in ("name", "color", "folder"):
                    page.site[key] = saved.get(key)
                self._update_tab(page)

    def import_winscp(self, parent):
        from .. import winscp_import
        dialog = Gtk.FileDialog(title="Import WinSCP Configuration", modal=True)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        ini = Gtk.FileFilter(name="WinSCP configuration (WinSCP.ini)")
        ini.add_pattern("*.ini")
        ini.add_pattern("*.INI")
        filters.append(ini)
        allf = Gtk.FileFilter(name="All files")
        allf.add_pattern("*")
        filters.append(allf)
        dialog.set_filters(filters)

        def chosen(dlg, result):
            try:
                gfile = dlg.open_finish(result)
            except GLib.Error:
                return
            path = gfile.get_path() if gfile else None
            if not path:
                return
            try:
                sites, warnings = winscp_import.parse_file(path)
            except (OSError, ValueError) as exc:
                alert(parent, "Import Failed", str(exc))
                return
            if not sites:
                alert(parent, "Nothing to Import", "No stored sessions were found in this file.")
                return
            with_pw = sum(1 for s in sites if s.get("_password"))
            check = Gtk.CheckButton(label=f"Also import {with_pw} saved password(s)",
                                    active=True, visible=with_pw > 0)
            body = f"Found {len(sites)} site(s)."
            if warnings:
                body += "\n\n" + "\n".join(warnings[:6])

            def response(rid):
                if rid != "import":
                    return
                count = winscp_import.store_sites(self.sites, sites, check.get_active())
                self.sites_changed()
                if isinstance(parent, SiteDialog):
                    parent.refresh_sites()
                self.toast(f"Imported {count} site(s) from WinSCP")
            alert(parent, "Import Sites?", body, responses=(("cancel", "_Cancel"), ("import", "_Import")),
                  default="import", close="cancel", suggested="import", extra=check, callback=response)
        dialog.open(self, None, chosen)

    # ------------------------------------------------------------------ misc
    def toast(self, text: str, timeout: int = 4):
        toast = Adw.Toast(title=GLib.markup_escape_text(str(text)), timeout=timeout)
        self.toasts.add_toast(toast)

    def add_transfer(self, job):
        self.transfers.add(job)
        if self.settings["auto_show_queue"]:
            self.queue_btn.set_active(True)

    # ------------------------------------------------------------------ drops onto the panes
    def _setup_drops(self):
        """Drops are taken for the whole window and handed to the pane under
        the pointer.  On Windows GTK converts a drop's position with the main
        monitor's scale (gdk/win32/gdkdrop-win32.c), which is wrong when the
        window is on a monitor with another scale: the drop would land on
        the wrong widget, or none.  So the pane and the row are found from
        where Windows says the pointer is."""
        self._drop_pane = None
        actions = Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        # From anySCP's own panes: recognised by their marker format; no data
        # is read (for a server pane that would mean a temporary download).
        internal = Gtk.DropTargetAsync(formats=Gdk.ContentFormats.new([dragout.PANE_MIME]), actions=actions)
        internal.connect("accept", lambda _t, _drop: FilePane._active_drag is not None)
        internal.connect("drag-enter", lambda _t, _drop, x, y: self._drop_hover(x, y))
        internal.connect("drag-motion", lambda _t, _drop, x, y: self._drop_hover(x, y))
        internal.connect("drag-leave", lambda *_a: self._set_drop_pane(None))
        internal.connect("drop", self._on_pane_drop)
        self.add_controller(internal)
        # Files of other programs
        external = Gtk.DropTarget(actions=actions)
        external.set_gtypes([Gdk.FileList, GObject.TYPE_STRING])
        external.connect("accept", FilePane.accept_external)
        external.connect("enter", lambda _t, x, y: self._drop_hover(x, y, Gdk.DragAction.COPY))
        external.connect("motion", lambda _t, x, y: self._drop_hover(x, y, Gdk.DragAction.COPY))
        external.connect("leave", lambda *_a: self._set_drop_pane(None))
        external.connect("drop", self._on_files_drop)
        self.add_controller(external)

    def _real_pointer(self, x, y):
        """x, y (window coordinates of a drop event) – on Windows, where the
        pointer really is."""
        if not compat.IS_WINDOWS:
            return x, y
        try:
            import ctypes
            from ctypes import wintypes
            gi.require_version("GdkWin32", "4.0")
            from gi.repository import GdkWin32
            surface = self.get_surface()
            user32 = ctypes.windll.user32
            user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
            point = wintypes.POINT()
            if not (user32.GetCursorPos(ctypes.byref(point))
                    and user32.ScreenToClient(GdkWin32.Win32Surface.get_handle(surface), ctypes.byref(point))):
                return x, y
            scale = surface.get_scale() or 1
            sx, sy = self.get_surface_transform()
            return point.x / scale - sx, point.y / scale - sy
        except (OSError, ValueError, ImportError, AttributeError):
            return x, y

    def _pane_under_pointer(self, x, y):
        """(pane, x, y in its file list) for a drop event at x, y, or (None, 0, 0)."""
        x, y = self._real_pointer(x, y)
        widget = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and not isinstance(widget, FilePane):
            widget = widget.get_parent()
        if widget is None:
            return None, 0, 0
        ok, point = self.compute_point(widget.column_view, Graphene.Point().init(x, y))
        return widget, (point.x if ok else -1), (point.y if ok else -1)

    def _set_drop_pane(self, pane):
        if pane is not self._drop_pane:
            if self._drop_pane is not None:
                self._drop_pane.clear_drop()
            self._drop_pane = pane

    def _drop_hover(self, x, y, action=None):
        """Files dragged over x, y: highlight where they would go, and say
        what a drop there would do (action: what other programs' files do)."""
        pane, px, py = self._pane_under_pointer(x, y)
        self._set_drop_pane(pane)
        if pane is None:
            return Gdk.DragAction(0)
        possible = pane.show_drop(px, py)
        return action if action is not None and possible else possible

    def _on_pane_drop(self, _target, drop, x, y):
        action = Gdk.DragAction(0)
        try:
            pane, px, py = self._pane_under_pointer(x, y)
            if pane is not None:
                action = pane.drop_internal(px, py)
        finally:
            self._set_drop_pane(None)
            FilePane._active_drag = None
            drop.finish(action)         # always: on Windows GTK waits for it, in a busy loop
        return True

    def _on_files_drop(self, _target, value, x, y):
        self._set_drop_pane(None)
        pane, px, py = self._pane_under_pointer(x, y)
        return pane is not None and pane.drop_external(value, px, py)

    # ------------------------------------------------------------------ transfer queue
    def _on_queue_toggled(self, *_a):
        # Opened or closed by hand (or by us): any pending automatic close is off.
        self._queue_autohide = False
        self._cancel_queue_hide()

    def _cancel_queue_hide(self):
        if self._queue_hide_timer:
            GLib.source_remove(self._queue_hide_timer)
            self._queue_hide_timer = 0

    def _on_transfers_changed(self, *_a):
        """Close the list QUEUE_COLLAPSE_DELAY_MS after the last transfer finished."""
        if self.transfers.active:
            if not self._queue_busy:
                self._queue_busy = True
                self._queue_busy_since = time.time()
            self._queue_autohide = False
            self._cancel_queue_hide()
            return
        if self._queue_busy:                # the queue has just drained
            self._queue_busy = False
            self._queue_autohide = self.queue_btn.get_active() and bool(self.settings["auto_show_queue"])
        if not self._queue_autohide or self._queue_hide_timer:
            return
        if any(j.state == FAILED and (j.finished_at or 0) >= self._queue_busy_since
               for j in self.transfers.jobs):
            return                          # keep the error and its Retry button in view
        self._queue_hide_timer = GLib.timeout_add(QUEUE_COLLAPSE_DELAY_MS, self._hide_drained_queue)

    def _hide_drained_queue(self):
        self._queue_hide_timer = 0
        if self._queue_autohide and not self.transfers.active:
            self.queue_btn.set_active(False)
        self._queue_autohide = False
        return False

    def _on_job_finished(self, _manager, job):
        import sys
        if sys.platform == "win32":
            return                      # GNotification needs a desktop notification service
        if not self.is_active() and job.state == "done":
            note = Gio.Notification.new("Transfer complete")
            note.set_body(job.title)
            self.app.send_notification(f"job-{job.id}", note)

    def show_shortcuts(self):
        from .shortcuts import ShortcutsDialog
        ShortcutsDialog().present(self)

    def _on_close_request(self, _win):
        busy = len(self.transfers.active)
        if busy and not getattr(self, "_closing_confirmed", False):
            def response(rid):
                if rid == "quit":
                    self._closing_confirmed = True
                    self.close()
            alert(self, "Quit anySCP?", f"{busy} transfer(s) are still running and will be cancelled.",
                  responses=(("cancel", "_Cancel"), ("quit", "_Quit")), default="cancel",
                  close="cancel", destructive="quit", callback=response)
            return True
        if not self.is_maximized():
            width, height = self.get_default_size()
            self.settings["window_width"] = width
            self.settings["window_height"] = height
        self.settings["window_maximized"] = self.is_maximized()
        self._cancel_queue_hide()
        for page in self.pages():
            page.shutdown()
        return False

