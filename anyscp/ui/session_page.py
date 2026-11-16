# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""A session tab: connection state, the two commander panes and all file
operations (copy, move, rename, delete, edit, …)."""

from __future__ import annotations

import hashlib
import os
import posixpath
import shlex

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .. import compat
from ..backends.base import (CAP_CHMOD, CAP_EXEC, CAP_SHELL, CAP_SYMLINK, AlreadyExists,
                             FileInfo, FSError)
from ..backends.local import LocalFileSystem
from ..config import CACHE_DIR, SSH_PROTOCOLS, site_subtitle, site_title
from ..session import LocalEndpoint, Session, SessionEndpoint
from ..transfer import DONE, FAILED, TransferJob
from ..utils import looks_like_text
from .file_pane import FilePane, LocalBackend
from .prompts import UIPrompter, alert, ask_text_dialog, make_spinner, show_error

FKEYS = [
    ("F2", "Rename", "rename"),
    ("F4", "Edit", "edit"),
    ("F5", "Copy", "transfer"),
    ("F6", "Move", "move"),
    ("F7", "New Folder", "mkdir"),
    ("F8", "Delete", "delete"),
    ("F9", "Properties", "properties"),
]


class SessionPage(Gtk.Box):
    __gsignals__ = {
        "title-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, window, site: dict):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.settings = window.settings
        self.site = site
        self.prompter = UIPrompter(lambda: self.window, site)
        self.prompter.before_prompt = self._select_own_tab
        self.session = Session(site, self.prompter)
        self.session.connect("state-changed", self._on_session_state)
        self.local_backend = LocalBackend()
        self.local_pane: FilePane | None = None
        self.remote_pane: FilePane | None = None
        self.active_pane: FilePane | None = None
        self.sync_anchor: tuple[str, str] | None = None
        self._syncing = False
        self._external_edits: dict[str, tuple] = {}
        self.tab_page = None
        self._shut_down = False

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.append(self.stack)
        self._build_connecting_page()
        self._build_error_page()
        self.commander = None
        self.connect_session()

    # ------------------------------------------------------------------ titles
    @property
    def title(self) -> str:
        return site_title(self.site)

    @property
    def subtitle(self) -> str:
        if self.active_pane is not None and self.active_pane.path:
            path = self.active_pane.path
            if self.active_pane.is_local:
                return f"Local: {compat.tilde(path)}"
            return f"{site_subtitle(self.site).split(' · ')[-1]}: {path}"
        return site_subtitle(self.site)

    def _select_own_tab(self):
        if self.tab_page is not None and not self._shut_down:
            self.window.tab_view.set_selected_page(self.tab_page)

    # ------------------------------------------------------------------ connection
    def _build_connecting_page(self):
        page = Adw.StatusPage(title=f"Connecting to {self.title}",
                              description=site_subtitle(self.site))
        page.set_paintable(None)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, halign=Gtk.Align.CENTER)
        spinner = make_spinner(width_request=48, height_request=48)
        box.append(spinner)
        cancel = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        cancel.add_css_class("pill")
        cancel.connect("clicked", lambda *_a: self.window.close_page(self, force=True))
        box.append(cancel)
        page.set_child(box)
        self.stack.add_named(page, "connecting")

    def _build_error_page(self):
        self.error_page = Adw.StatusPage(icon_name="network-error-symbolic", title="Connection Failed")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.CENTER)
        retry = Gtk.Button(label="_Reconnect", use_underline=True)
        retry.add_css_class("pill")
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda *_a: self.connect_session())
        edit = Gtk.Button(label="_Edit Site…", use_underline=True)
        edit.add_css_class("pill")
        edit.connect("clicked", lambda *_a: self.window.edit_site(self.site, from_page=self))
        log = Gtk.Button(label="Show _Log", use_underline=True)
        log.add_css_class("pill")
        log.connect("clicked", lambda *_a: self.show_log())
        for b in (retry, edit, log):
            box.append(b)
        self.error_page.set_child(box)
        self.stack.add_named(self.error_page, "error")

    def connect_session(self):
        self.stack.set_visible_child_name("connecting")
        if self.commander is not None:
            self.session.reconnect_async(self._on_reconnected, self._on_connect_failed)
        else:
            self.session.connect_async(self._on_connected, self._on_connect_failed)

    def _on_connect_failed(self, exc):
        from ..backends.base import Cancelled
        if self._shut_down:
            return
        if isinstance(exc, Cancelled):
            if self.commander is None:
                self.window.close_page(self, force=True)
                return
            self.error_page.set_description("Login was cancelled.")
        else:
            self.error_page.set_description(GLib.markup_escape_text(str(exc)))
        self.stack.set_visible_child_name("error" if self.commander is None else "main")
        if self.commander is not None:
            self.banner.set_title(f"Reconnect failed: {exc}")
            self.banner.set_revealed(True)
        self.emit("state-changed")

    def _on_connected(self):
        if self._shut_down:
            return
        self._build_commander()
        self.stack.set_visible_child_name("main")
        home = self.session.fs.home()
        self.remote_pane.set_home(home)
        remote_dir = self._start_folder(home)
        # A site's folder that cannot be opened (renamed, mistyped): the home
        # folder instead, as WinSCP does, with a note why.
        self.remote_pane.load(remote_dir, fallback=home if remote_dir != home else None)
        local_dir = compat.local_path(self.site.get("local_dir") or self.settings["last_local_dir"]
                                      or compat.home_dir())
        if local_dir != "/" and not os.path.isdir(compat.to_native(local_dir)):
            local_dir = compat.home_dir()
        self.local_pane.set_home(compat.home_dir())
        self.local_pane.load(local_dir)
        self.set_active_pane(self.remote_pane)
        GLib.idle_add(lambda: (self.remote_pane.focus_list(), False)[1])
        self.emit("state-changed")
        self.emit("title-changed")

    def _start_folder(self, home: str) -> str:
        """The site's remote directory; relative to the home folder (the
        login folder) unless it starts with "/" (WebDAV: the site's
        directory is its home already)."""
        remote_dir = (self.site.get("remote_dir") or "").strip()
        if not remote_dir:
            return home
        if self.session.fs.protocol == "webdav" or remote_dir.startswith("/"):
            return remote_dir
        if remote_dir == "~" or remote_dir.startswith("~/"):
            remote_dir = remote_dir[1:].lstrip("/")
        return posixpath.normpath(posixpath.join(home, remote_dir)) if remote_dir else home

    def log(self, message: str):
        """A line in the session log."""
        self.session.log(message)

    def _on_reconnected(self):
        if self._shut_down:
            return
        self.stack.set_visible_child_name("main")
        self.banner.set_revealed(False)
        self.remote_pane.refresh()
        self.emit("state-changed")
        self.window.toast("Reconnected")

    def _on_session_state(self, _session, state):
        if state == "disconnected" and self.commander is not None:
            self.banner.set_title("Connection lost")
            self.banner.set_revealed(True)
        if self.commander is not None:
            for pane in (self.local_pane, self.remote_pane):
                self.update_actions(pane)
        self.emit("state-changed")

    # ------------------------------------------------------------------ layout
    def _build_commander(self):
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.banner = Adw.Banner(title="Connection lost", button_label="_Reconnect")
        self.banner.connect("button-clicked", lambda *_a: self.connect_session())
        main.append(self.banner)

        self.local_pane = FilePane(self, self.local_backend, is_local=True,
                                   settings=self.settings, title="Local")
        self.remote_pane = FilePane(self, self.session, is_local=False,
                                    settings=self.settings, title=site_subtitle(self.site).split(" · ")[0])
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True,
                               shrink_start_child=False, shrink_end_child=False,
                               resize_start_child=True, resize_end_child=True, vexpand=True)
        self.paned.set_start_child(self.local_pane)
        self.paned.set_end_child(self.remote_pane)
        self.paned.add_css_class("commander")
        self.local_pane.set_visible(self.settings["show_local_pane"])
        main.append(self.paned)
        GLib.idle_add(self._center_paned)

        self.fkeys = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        self.fkeys.add_css_class("fkey-bar")
        for key, label, action in FKEYS:
            btn = Gtk.Button()
            btn.add_css_class("flat")
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.CENTER)
            k = Gtk.Label(label=key)
            k.add_css_class("fkey")
            inner.append(k)
            inner.append(Gtk.Label(label=label, ellipsize=3))
            btn.set_child(inner)
            btn.set_action_name(f"win.{action}")
            self.fkeys.append(btn)
        self.fkeys.set_visible(self.settings["show_fkeys_bar"])
        main.append(self.fkeys)

        self.settings.connect(self._on_setting)
        self.stack.add_named(main, "main")
        self.commander = main

    def _center_paned(self):
        width = self.paned.get_width()
        if width <= 0:
            return True
        saved = self.settings["pane_position"]
        self.paned.set_position(int(width * saved) if 0.15 < (saved or 0) < 0.85 else width // 2)
        self.paned.connect("notify::position", self._on_paned_moved)
        return False

    def _on_paned_moved(self, paned, _pspec):
        width = paned.get_width()
        if width > 0:
            frac = round(paned.get_position() / width, 3)
            if abs(frac - (self.settings["pane_position"] or 0)) > 0.01:
                self.settings["pane_position"] = frac

    def _on_setting(self, key, value):
        if key == "show_fkeys_bar":
            self.fkeys.set_visible(value)
        elif key == "show_local_pane":
            self.local_pane.set_visible(value)
            if not value and self.active_pane is self.local_pane:
                self.set_active_pane(self.remote_pane)

    # ------------------------------------------------------------------ pane callbacks
    def set_active_pane(self, pane: FilePane):
        if self.active_pane is pane:
            return
        self.active_pane = pane
        for p in (self.local_pane, self.remote_pane):
            if p is not None:
                if p is pane:
                    p.add_css_class("active-pane")
                else:
                    p.remove_css_class("active-pane")
        self.update_actions(pane)
        self.emit("title-changed")

    def other_pane(self, pane: FilePane) -> FilePane:
        return self.remote_pane if pane is self.local_pane else self.local_pane

    def pane_loaded(self, pane: FilePane):
        if pane.is_local and pane.path:
            self.settings["last_local_dir"] = pane.path
        if pane is self.active_pane:
            self.emit("title-changed")
        self.update_actions(pane)
        self._sync_browse(pane)

    def selection_changed(self, pane: FilePane):
        # (also for the other pane: its copy button follows its selection)
        self.update_actions(pane)

    def toast(self, text):
        self.window.toast(text)

    def update_actions(self, pane: FilePane | None = None):
        pane = pane or self.active_pane
        if pane is None:
            return
        infos = pane.get_selected_infos()
        fs = self._fs_for(pane)
        caps = fs.capabilities if fs is not None else frozenset()
        connected = self.session.state == "connected"
        remote_ok = pane.is_local or connected
        other_ok = (not pane.is_local or connected) and self.local_pane.get_visible()
        enable = {
            "open": len(infos) == 1,
            "edit": len(infos) == 1 and not infos[0].is_dir and remote_ok,
            "open-external": len(infos) == 1 and not infos[0].is_dir and remote_ok,
            "transfer": bool(infos) and connected and other_ok,
            "move": bool(infos) and connected and other_ok,
            "rename": len(infos) == 1 and remote_ok,
            "duplicate": len(infos) == 1 and remote_ok,
            "delete": bool(infos) and remote_ok,
            "mkdir": remote_ok and pane.path is not None,
            "newfile": remote_ok and pane.path is not None,
            "symlink": len(infos) == 1 and CAP_SYMLINK in caps and remote_ok,
            "copy-path": True,
            "reveal": pane.is_local,
            "bookmark": pane.path is not None,
            "properties": remote_ok and (bool(infos) or pane.path is not None),
        }
        for name, ok in enable.items():
            action = pane.actions.lookup_action(name)
            if action is not None:
                action.set_enabled(ok)
        if pane is self.active_pane:
            self.window.update_actions()

    def action_enabled(self, name: str) -> bool:
        pane = self.active_pane
        if pane is None or self.commander is None:
            return False
        action = pane.actions.lookup_action(name)
        return action.get_enabled() if action is not None else True

    def _fs_for(self, pane):
        return self.local_backend.fs if pane.is_local else self.session.fs

    def _backend_for(self, pane):
        return self.local_backend if pane.is_local else self.session

    def _endpoint(self, pane):
        return LocalEndpoint() if pane.is_local else SessionEndpoint(self.session)

    # ------------------------------------------------------------------ actions
    def do_action(self, name: str):
        """Window-level action targeting the active pane."""
        pane = self.active_pane
        if self.commander is None:
            if name == "reconnect":
                self.connect_session()
            return
        simple = {
            "refresh": lambda: (self.local_pane.refresh(), self.remote_pane.refresh()),
            "filter": lambda: pane and pane.toggle_filter(),
            "focus-path": lambda: pane and pane.focus_path_entry(),
            "sync": self.show_sync,
            "find": self.show_find,
            "terminal": self.open_terminal,
            "info": self.show_info,
            "log": self.show_log,
            "reconnect": self.connect_session,
            "compare": self.compare_directories,
            "swap-focus": lambda: self.other_pane(pane).focus_list() if pane else None,
            "sync-browse": self.toggle_sync_browsing,
            "select-all": lambda: pane and pane.selection.select_all(),
        }
        if name in simple:
            simple[name]()
            return
        if pane is not None:
            self.pane_action(pane, name)

    def pane_action(self, pane: FilePane, name: str):
        self.set_active_pane(pane)
        action = pane.actions.lookup_action(name)
        if action is not None and not action.get_enabled():
            return
        infos = pane.get_selected_infos()
        handler = {
            "open": lambda: self.open_item(pane, infos[0]) if infos else None,
            "edit": lambda: self.edit_file(pane, infos[0]) if infos else None,
            "open-external": lambda: self.open_external(pane, infos[0]) if infos else None,
            "transfer": lambda: self.confirm_transfer(pane, infos, move=False),
            "move": lambda: self.confirm_transfer(pane, infos, move=True),
            "rename": lambda: self.rename(pane, infos[0]) if infos else None,
            "duplicate": lambda: self.duplicate(pane, infos[0]) if infos else None,
            "delete": lambda: self.delete(pane, infos),
            "delete-permanently": lambda: self.delete(pane, infos, permanent=True),
            "mkdir": lambda: self.mkdir(pane),
            "newfile": lambda: self.new_file(pane),
            "symlink": lambda: self.new_symlink(pane, infos[0]) if infos else None,
            "copy-path": lambda: self.copy_path(pane, infos),
            "reveal": lambda: self.reveal(pane, infos),
            "bookmark": lambda: self.add_bookmark(
                pane, infos[0].path if len(infos) == 1 and infos[0].is_dir else pane.path),
            "properties": lambda: self.show_properties(pane, infos),
        }.get(name)
        if handler:
            handler()

    # -- open / edit ---------------------------------------------------------
    def activate_file(self, pane, info: FileInfo):
        if pane.is_local:
            self.open_item(pane, info)
            return
        mode = self.settings["double_click_remote"]
        if mode == "download":
            self.start_transfer(pane, [info], self.local_pane, self.local_pane.path)
        elif mode == "open":
            self.open_external(pane, info)
        else:
            self.edit_file(pane, info)

    def open_item(self, pane, info: FileInfo):
        if info.is_dir:
            pane.load(info.path)
        elif pane.is_local:
            self._launch(compat.to_native(info.path))
        else:
            self.activate_file(pane, info)

    def _launch(self, native_path: str, reveal: bool = False):
        """Open with the default app; bundled builds use a clean host
        environment (see compat.open_path)."""
        try:
            if compat.open_path(native_path, reveal):
                return
        except OSError as exc:
            self.toast(f"Could not open file: {exc}")
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(native_path))
        if reveal:
            launcher.open_containing_folder(self.window, None, None)
        else:
            launcher.launch(self.window, None, self._launch_done)

    def _launch_done(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error as err:
            if not err.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self.toast(f"Could not open file: {err.message}")

    def edit_file(self, pane, info: FileInfo):
        if info.is_dir:
            pane.load(info.path)
            return
        if self.settings["editor"] == "external" or (not looks_like_text(info.name) and info.size > 0):
            if pane.is_local:
                self.open_item(pane, info)
            else:
                self.open_external(pane, info)
            return
        from .editor import open_editor
        open_editor(self, pane, info)

    def open_external(self, pane, info: FileInfo):
        """Download to a temporary folder, open with the default app and
        upload again whenever the file is saved (like WinSCP)."""
        if pane.is_local:
            self.open_item(pane, info)
            return
        key = hashlib.sha1(f"{self.site.get('id')}:{info.path}".encode()).hexdigest()[:12]
        local_dir = CACHE_DIR / "edit" / key
        local_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        local_path = str(local_dir / info.name)

        def job(fs):
            reader = fs.open_read(info.path)
            try:
                with open(local_path, "wb") as fh:
                    while chunk := reader.read(256 * 1024):
                        fh.write(chunk)
            finally:
                reader.close()
            if info.mtime:
                os.utime(local_path, (info.mtime, info.mtime))
            return local_path

        def done(path):
            self._watch_external(path, info)
            self._launch(path)
            self.toast(f"Opened {info.name}. Saved changes will be uploaded automatically.")
        self.toast(f"Downloading {info.name}…")
        self.session.run(job, done, lambda exc: show_error(self.window, "Cannot Open File", exc))

    def _watch_external(self, local_path, info):
        old = self._external_edits.pop(local_path, None)
        if old:
            old[0].cancel()
        monitor = Gio.File.new_for_path(local_path).monitor_file(Gio.FileMonitorFlags.WATCH_HARD_LINKS, None)
        state = {"timeout": 0, "mtime": os.path.getmtime(local_path)}

        def upload():
            state["timeout"] = 0
            try:
                mtime = os.path.getmtime(local_path)
            except OSError:
                return False
            if mtime == state["mtime"]:
                return False
            state["mtime"] = mtime
            local_info = LocalFileSystem().stat(compat.to_internal(local_path))
            job = TransferJob(f"Uploading {info.name}", LocalEndpoint(), SessionEndpoint(self.session),
                              [(local_info, info.path)], settings=self.settings, overwrite="overwrite",
                              direction="upload", subtitle=f"Edited file → {info.path}")

            def finished(j):
                if j.state == DONE:
                    self.toast(f"Uploaded changes to {info.name}")
                    if self.remote_pane.path == posixpath.dirname(info.path):
                        self.remote_pane.refresh(quiet=True)
                else:
                    self.toast(f"Upload of {info.name} failed: {j.error}")
            job.on_finished = finished
            self.window.transfers.add(job)
            return False

        def changed(_m, _f, _o, event):
            if event in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CHANGED,
                         Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.RENAMED,
                         Gio.FileMonitorEvent.MOVED_IN):
                if state["timeout"]:
                    GLib.source_remove(state["timeout"])
                state["timeout"] = GLib.timeout_add(700, upload)
        monitor.connect("changed", changed)
        self._external_edits[local_path] = (monitor, state)

    # -- transfers -------------------------------------------------------------
    def confirm_transfer(self, pane, infos, move=False):
        if not infos:
            return
        target_pane = self.other_pane(pane)
        target_dir = target_pane.path
        if target_dir is None:
            return
        if not self.settings["confirm_transfer"]:
            self.start_transfer(pane, infos, target_pane, target_dir, move)
            return
        verb = ("Move" if move else "Upload" if pane.is_local else "Download")
        what = f"“{infos[0].name}”" if len(infos) == 1 else f"{len(infos)} items"
        entry = Adw.EntryRow(title="Destination folder", text=target_dir)
        entry.set_activates_default(True)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        listbox.append(entry)
        dont_ask = Gtk.CheckButton(label="_Don't ask again", use_underline=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(listbox)
        box.append(dont_ask)
        where = "to the server" if pane.is_local else "to this computer"

        def response(rid):
            if rid != "ok":
                return
            if dont_ask.get_active():
                self.settings["confirm_transfer"] = False
            dest = entry.get_text().strip() or target_dir
            self.start_transfer(pane, infos, target_pane, dest, move)
        alert(self.window, f"{verb} {what}?", f"Copy {what} {where}." if not move else
              f"Move {what} {where}. The originals will be deleted after copying.",
              responses=(("cancel", "_Cancel"), ("ok", f"_{verb}")), default="ok",
              close="cancel", suggested="ok", extra=box, callback=response)

    def start_transfer(self, src_pane, infos, dst_pane, dst_dir, move=False):
        if not infos or dst_dir is None:
            return
        if not src_pane.is_local or not dst_pane.is_local:
            if self.session.state != "connected":
                self.toast("Not connected")
                return
        same_side = src_pane.is_local == dst_pane.is_local
        pairs = [(i, posixpath.join(dst_dir, i.name)) for i in infos]
        if same_side:
            pairs = [(i, d) for i, d in pairs
                     if i.path != d and not d.startswith(i.path.rstrip("/") + "/")]
            if not pairs:
                self.toast("Source and destination are the same")
                return
        if src_pane.is_local and not dst_pane.is_local:
            direction, verb = "upload", "Uploading"
        elif not src_pane.is_local and dst_pane.is_local:
            direction, verb = "download", "Downloading"
        else:
            direction, verb = "copy", "Copying"
        if move:
            verb = "Moving"
        what = infos[0].name if len(infos) == 1 else f"{len(infos)} items"
        where = compat.tilde(dst_dir) if dst_pane.is_local else f"{self.title}:{dst_dir}"
        job = TransferJob(f"{verb} {what}", self._endpoint(src_pane), self._endpoint(dst_pane), pairs,
                          move=move, settings=self.settings, direction=direction,
                          subtitle=f"to {where}")
        job.on_finished = lambda j: self._transfer_finished(j, src_pane, dst_pane)
        self.window.add_transfer(job)

    def _transfer_finished(self, job, src_pane, dst_pane):
        if dst_pane.path in job.dst_dirs or any(
                d.startswith((dst_pane.path or "").rstrip("/") + "/") for d in job.dst_dirs):
            dst_pane.refresh(quiet=True)
        if job.move and src_pane.path in job.src_dirs:
            src_pane.refresh(quiet=True)
        if job.state == FAILED:
            self.toast(f"{job.title} failed: {job.error}")
        elif job.state == DONE and job.warnings:
            self.toast(f"{job.title}: finished with {len(job.warnings)} problem(s)")

    def drop_from_pane(self, src_pane, infos, dst_pane, target_dir, move):
        if src_pane not in (self.local_pane, self.remote_pane):
            # Dragged from another session tab: only local → here makes sense.
            if src_pane.is_local:
                self.drop_local_paths([i.path for i in infos], dst_pane, target_dir, move)
            else:
                self.toast("Drag files between a session and the local pane")
            return
        self.start_transfer(src_pane, infos, dst_pane, target_dir, move)

    def drop_local_paths(self, paths, dst_pane, target_dir, move):
        local = LocalFileSystem()
        infos = []
        for p in paths:
            try:
                infos.append(local.stat(compat.to_internal(p)))
            except FSError as exc:
                self.toast(str(exc))
        if not infos:
            return
        self.start_transfer(self.local_pane, infos, dst_pane, target_dir, move)

    def move_within(self, pane, infos, target_dir):
        infos = [i for i in infos if i.path != target_dir and not target_dir.startswith(i.path + "/")]
        if not infos:
            return
        what = f"“{infos[0].name}”" if len(infos) == 1 else f"{len(infos)} items"

        def response(rid):
            if rid != "move":
                return

            def job(fs):
                for i in infos:
                    fs.rename(i.path, posixpath.join(target_dir, i.name))
            self._backend_for(pane).run(job, lambda _r: pane.refresh(),
                                        lambda exc: (show_error(self.window, "Move Failed", exc), pane.refresh()))
        alert(self.window, f"Move {what}?", f"Move into {target_dir}",
              responses=(("cancel", "_Cancel"), ("move", "_Move")), default="move",
              close="cancel", suggested="move", callback=response)

    # -- file operations ---------------------------------------------------------
    def rename(self, pane, info: FileInfo):
        def done(name):
            if not name or name == info.name:
                return
            new_path = posixpath.join(posixpath.dirname(info.path), name)
            self._backend_for(pane).run(
                lambda fs: fs.rename(info.path, new_path),
                lambda _r: pane.load(pane.path, select=name, history=False),
                lambda exc: show_error(self.window, "Rename Failed", exc))
        ask_text_dialog(self.window, "Rename", f"Rename “{info.name}” to:", info.name, "_Rename", done,
                        select_stem=not info.is_dir, validate=lambda t: "/" not in t)

    def duplicate(self, pane, info: FileInfo):
        stem, ext = os.path.splitext(info.name)
        suggestion = f"{stem} (copy){ext}" if not info.is_dir else f"{info.name} (copy)"

        def done(name):
            if not name:
                return
            dst = posixpath.join(posixpath.dirname(info.path), name)
            fs = self._fs_for(pane)
            if fs is not None and fs.has(CAP_EXEC) and not pane.is_local:
                def dup(f):
                    if f.exists(dst):
                        raise AlreadyExists(f"“{name}” already exists")
                    return _check(f.exec(f"cp -a {shlex.quote(info.path)} {shlex.quote(dst)}"))
                self.session.run(dup,
                                 lambda _r: pane.load(pane.path, select=name, history=False),
                                 lambda exc: show_error(self.window, "Duplicate Failed", exc))
                return
            ep = self._endpoint(pane)
            job = TransferJob(f"Duplicating {info.name}", ep, ep, [(info, dst)], settings=self.settings,
                              direction="copy", subtitle=f"to {name}")
            job.on_finished = lambda j: pane.refresh(quiet=True)
            self.window.add_transfer(job)
        ask_text_dialog(self.window, "Duplicate", f"Name for the copy of “{info.name}”:", suggestion,
                        "_Duplicate", done, select_stem=not info.is_dir, validate=lambda t: "/" not in t)

    def delete(self, pane, infos, permanent=False, confirmed=False):
        if not infos:
            return
        trash = pane.is_local and not permanent
        what = f"“{infos[0].name}”" if len(infos) == 1 else f"{len(infos)} items"

        def run():
            backend = self._backend_for(pane)

            def job(fs):
                errors, untrashable = [], []
                for info in infos:
                    try:
                        if trash:
                            try:
                                Gio.File.new_for_path(compat.to_native(info.path)).trash(None)
                            except GLib.Error as err:
                                if err.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
                                    untrashable.append(info)
                                else:
                                    raise FSError(err.message) from None
                            continue
                        fs.remove_tree(info.path)
                    except FSError as exc:
                        errors.append(f"{info.name}: {exc}")
                return errors, untrashable

            def done(result):
                errors, untrashable = result
                pane.refresh(quiet=True)
                if errors:
                    show_error(self.window, "Some Items Could Not Be Deleted", "\n".join(errors[:10]))
                elif untrashable:
                    names = ", ".join(i.name for i in untrashable[:5])
                    more = f" and {len(untrashable) - 5} more" if len(untrashable) > 5 else ""
                    alert(self.window, "Delete Permanently?",
                          f"{names}{more} cannot be moved to the trash on this file system. "
                          "Delete permanently instead?",
                          responses=(("cancel", "_Cancel"), ("delete", "_Delete Permanently")),
                          default="cancel", close="cancel", destructive="delete",
                          callback=lambda rid: self.delete(pane, untrashable, permanent=True, confirmed=True)
                          if rid == "delete" else None)
                elif trash:
                    self.toast(f"Moved {what} to the trash")
                else:
                    self.toast(f"Deleted {what}")
            backend.run(job, done, lambda exc: (show_error(self.window, "Delete Failed", exc),
                                                pane.refresh(quiet=True)))

        if confirmed or (not self.settings["confirm_delete"] and trash):
            run()
            return
        if trash:
            heading, body, label = f"Move {what} to the Trash?", "", "_Move to Trash"
        else:
            heading = f"Permanently Delete {what}?"
            body = "Folders are deleted with all their contents. This cannot be undone."
            label = "_Delete"
        if not self.settings["confirm_delete"]:
            run()
            return
        alert(self.window, heading, body, responses=(("cancel", "_Cancel"), ("delete", label)),
              default="delete" if trash else "cancel", close="cancel", destructive="delete",
              callback=lambda rid: run() if rid == "delete" else None)

    def mkdir(self, pane):
        existing = {e.name for e in pane.entries}

        def done(name):
            if not name:
                return
            path = posixpath.join(pane.path, name)
            self._backend_for(pane).run(lambda fs: fs.mkdir(path),
                                        lambda _r: pane.load(pane.path, select=name, history=False),
                                        lambda exc: show_error(self.window, "Cannot Create Folder", exc))
        ask_text_dialog(self.window, "New Folder", f"Create a folder in {pane.path}", "", "_Create", done,
                        title="Folder name", validate=lambda t: "/" not in t and t not in existing)

    def new_file(self, pane):
        existing = {e.name for e in pane.entries}

        def done(name):
            if not name:
                return
            path = posixpath.join(pane.path, name)

            def job(fs):
                if fs.exists(path):
                    raise AlreadyExists(f"“{name}” already exists")
                fs.open_write(path, 0, 0).close()
                return fs.stat(path)

            def created(info):
                pane.load(pane.path, select=name, history=False)
                from .editor import open_editor
                open_editor(self, pane, info)
            self._backend_for(pane).run(job, created,
                                        lambda exc: show_error(self.window, "Cannot Create File", exc))
        ask_text_dialog(self.window, "New File", f"Create an empty file in {pane.path}", "", "_Create",
                        done, title="File name", validate=lambda t: "/" not in t and t not in existing)

    def new_symlink(self, pane, info):
        def done(name):
            if not name:
                return
            link = posixpath.join(pane.path, name)
            self._backend_for(pane).run(lambda fs: fs.symlink(info.path, link),
                                        lambda _r: pane.load(pane.path, select=name, history=False),
                                        lambda exc: show_error(self.window, "Cannot Create Link", exc))
        ask_text_dialog(self.window, "New Symbolic Link", f"Link pointing to {info.path}",
                        f"{info.name}.link", "_Create", done, title="Link name",
                        validate=lambda t: "/" not in t)

    def copy_path(self, pane, infos):
        paths = [i.path for i in infos] or [pane.path]
        self.window.get_clipboard().set("\n".join(paths))
        self.toast("Path copied" if len(paths) == 1 else f"{len(paths)} paths copied")

    def reveal(self, pane, infos):
        target = infos[0].path if infos else pane.path
        self._launch(compat.to_native(target), reveal=bool(infos))

    def show_properties(self, pane, infos):
        from .properties import PropertiesDialog
        if not infos:
            def got(info):
                PropertiesDialog(self, pane, [info]).present(self.window)
            self._backend_for(pane).run(lambda f: f.stat(pane.path), got,
                                        lambda exc: show_error(self.window, "Properties", exc))
            return
        PropertiesDialog(self, pane, infos).present(self.window)

    # -- bookmarks -----------------------------------------------------------
    def get_bookmarks(self, pane) -> list[str]:
        if pane.is_local:
            return list(self.settings["local_bookmarks"] or [])
        return list(self.site.get("bookmarks") or [])

    def add_bookmark(self, pane, path):
        if not path:
            return
        marks = self.get_bookmarks(pane)
        if path in marks:
            self.toast("Already bookmarked")
            return
        marks.append(path)
        self._save_bookmarks(pane, marks)
        self.toast(f"Bookmarked {path}")

    def remove_bookmark(self, pane, path):
        marks = [m for m in self.get_bookmarks(pane) if m != path]
        self._save_bookmarks(pane, marks)

    def _save_bookmarks(self, pane, marks):
        if pane.is_local:
            self.settings["local_bookmarks"] = marks
        else:
            self.site["bookmarks"] = marks
            if self.window.sites.get(self.site.get("id", "")) is not None:
                self.window.sites.update_field(self.site["id"], "bookmarks", marks)

    # -- synchronized browsing ---------------------------------------------------
    def toggle_sync_browsing(self):
        self._syncing = False
        if self.sync_anchor is not None:
            self.sync_anchor = None
            self.toast("Synchronized browsing off")
        elif self.local_pane.path and self.remote_pane.path:
            self.sync_anchor = (self.local_pane.path, self.remote_pane.path)
            self.toast("Synchronized browsing on: folder changes are mirrored on the other side")
        self.window.update_actions()

    def _sync_browse(self, pane):
        if self.sync_anchor is None or self._syncing or not pane.path:
            return
        local_root, remote_root = self.sync_anchor
        root, other_root = (local_root, remote_root) if pane.is_local else (remote_root, local_root)
        if pane.path != root and not pane.path.startswith(root.rstrip("/") + "/"):
            self.sync_anchor = None
            self.toast("Synchronized browsing turned off (left the synchronized folder)")
            self.window.update_actions()
            return
        rel = posixpath.relpath(pane.path, root)
        target = other_root if rel == "." else posixpath.join(other_root, rel)
        other = self.other_pane(pane)
        if other.path == target:
            return
        self._syncing = True

        def loaded(ok):
            self._syncing = False
            if ok is False:
                self.sync_anchor = None
                self.toast(f"Synchronized browsing turned off: {target} does not exist")
                self.window.update_actions()
        other.load(target, on_loaded=loaded, quiet=True)

    # -- tools -----------------------------------------------------------------
    def show_sync(self):
        from .sync_dialog import SyncDialog
        SyncDialog(self).present(self.window)

    def compare_directories(self):
        """Select files that differ between the two panes (like WinSCP's
        Compare Directories)."""
        lp, rp = self.local_pane, self.remote_pane
        local = {e.name: e for e in lp.entries if not e.is_dir}
        remote = {e.name: e for e in rp.entries if not e.is_dir}
        newer_local = {n for n, e in local.items()
                       if n not in remote or (e.mtime or 0) > (remote[n].mtime or 0) + 2}
        newer_remote = {n for n, e in remote.items()
                        if n not in local or (e.mtime or 0) > (local[n].mtime or 0) + 2}
        lp.select_names(newer_local)
        rp.select_names(newer_remote)
        self.toast(f"{len(newer_local)} newer or missing locally selected, "
                   f"{len(newer_remote)} on the server")

    def show_find(self):
        from .find_dialog import FindDialog
        FindDialog(self).present(self.window)

    def open_terminal(self):
        if self.site.get("protocol") not in SSH_PROTOCOLS or self.session.fs is None \
                or not self.session.fs.has(CAP_SHELL):
            self.toast("A terminal is only available for SFTP and SCP sessions")
            return
        from .terminal import Vte, open_terminal
        if Vte is None:
            # No embedded terminal on this platform (e.g. Windows): use the
            # system terminal with OpenSSH instead.
            from ..backends.sshconfig import resolve
            r = resolve(self.site)
            error = compat.open_external_terminal(r.hostname, r.port, r.user, self.site.get("key_file", ""))
            if error:
                show_error(self.window, "Cannot Open Terminal", error)
            return
        open_terminal(self, self.remote_pane.path if self.remote_pane else None)

    def show_info(self):
        from .session_info import SessionInfoDialog
        SessionInfoDialog(self).present(self.window)

    def show_log(self):
        from .session_info import LogDialog
        LogDialog(self.session).present(self.window)

    def chmod_capable(self, pane) -> bool:
        fs = self._fs_for(pane)
        return fs is not None and (fs.is_local or fs.has(CAP_CHMOD))

    # -- shutdown ----------------------------------------------------------------
    def _job_uses_session(self, job) -> bool:
        endpoints = (getattr(job, "src", None), getattr(job, "dst", None), getattr(job, "endpoint", None))
        return any(getattr(ep, "session", None) is self.session for ep in endpoints)

    def busy_transfers(self) -> int:
        return sum(1 for j in self.window.transfers.active if self._job_uses_session(j))

    def shutdown(self):
        for monitor, _state in self._external_edits.values():
            monitor.cancel()
        self._external_edits.clear()
        self._shut_down = True
        self.prompter.closed = True
        for job in list(self.window.transfers.active):
            if self._job_uses_session(job):
                self.window.transfers.cancel(job)
        for pane in (self.local_pane, self.remote_pane):
            if pane is not None:
                pane.shutdown()
        self.settings.disconnect(self._on_setting)
        self.local_backend.stop()
        self.session.close()


def _check(result):
    status, _out, err = result
    if status != 0:
        raise FSError(err.strip() or f"Command failed with status {status}")
    return result
