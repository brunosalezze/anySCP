# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Synchronize directories: options → preview → queue jobs."""

from __future__ import annotations

import posixpath
import threading

from gi.repository import Adw, GLib, Gtk

from ..backends.local import LocalFileSystem
from ..session import LocalEndpoint, SessionEndpoint
from ..sync import DELETE_LOCAL, DELETE_REMOTE, DOWNLOAD, UPLOAD, SyncOptions, compare
from ..transfer import DeleteJob, TransferJob
from ..utils import format_size, format_time
from .prompts import ChoiceGroup

KIND_INFO = {
    UPLOAD: ("go-up-symbolic", "Upload"),
    DOWNLOAD: ("go-down-symbolic", "Download"),
    DELETE_LOCAL: ("user-trash-symbolic", "Delete local"),
    DELETE_REMOTE: ("user-trash-symbolic", "Delete on server"),
}


class SyncDialog(Adw.Dialog):
    def __init__(self, page):
        super().__init__(title="Synchronize", content_width=720, content_height=640)
        self.page = page
        self.actions = []
        self.cancel_event = threading.Event()
        self.nav = Adw.NavigationView()
        self.nav.add(self._options_page())
        self.set_child(self.nav)
        self.connect("closed", lambda *_a: self.cancel_event.set())

    # ------------------------------------------------------------------ options
    def _options_page(self):
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        prefs = Adw.PreferencesPage()

        dirs = Adw.PreferencesGroup(title="Folders")
        self.local_row = Adw.EntryRow(title="Local folder", text=self.page.local_pane.path or "")
        self.remote_row = Adw.EntryRow(title="Remote folder", text=self.page.remote_pane.path or "")
        dirs.add(self.local_row)
        dirs.add(self.remote_row)
        prefs.add(dirs)

        direction = Adw.PreferencesGroup(title="Direction")
        self.direction = ChoiceGroup([("remote", "Local → Server", "go-up-symbolic"),
                                      ("local", "Server → Local", "go-down-symbolic"),
                                      ("both", "Both ways", "media-playlist-repeat-symbolic")],
                                     on_changed=lambda _n: self._update_delete())
        self.direction.set_active_name("remote")
        dir_box = Gtk.Box(halign=Gtk.Align.CENTER)
        dir_box.append(self.direction)
        direction.add(dir_box)
        prefs.add(direction)

        opts = Adw.PreferencesGroup(title="Options")
        self.criteria = Adw.ComboRow(title="Compare by", model=Gtk.StringList.new(
            ["Modification time", "File size", "Time or size"]))
        opts.add(self.criteria)
        self.recursive = Adw.SwitchRow(title="Include subfolders", active=True)
        opts.add(self.recursive)
        self.delete = Adw.SwitchRow(title="Delete files", subtitle="Remove files that no longer exist on the source side (mirror)")
        opts.add(self.delete)
        self.exclude = Adw.EntryRow(title="Exclude (comma separated patterns)", text=".git, __pycache__, .DS_Store")
        opts.add(self.exclude)
        prefs.add(opts)
        self._update_delete()

        view.set_content(prefs)
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.END, spacing=8,
                         margin_top=10, margin_bottom=10, margin_end=12)
        self.compare_btn = Gtk.Button(label="_Compare", use_underline=True)
        self.compare_btn.add_css_class("suggested-action")
        self.compare_btn.connect("clicked", lambda *_a: self._compare())
        bottom.append(self.compare_btn)
        view.add_bottom_bar(bottom)
        return Adw.NavigationPage(title="Synchronize", child=view, tag="options")

    def _update_delete(self):
        both = self.direction.get_active_name() == "both"
        self.delete.set_sensitive(not both)
        if both:
            self.delete.set_active(False)

    # ------------------------------------------------------------------ compare
    def _compare(self):
        local_root = self.local_row.get_text().strip()
        remote_root = self.remote_row.get_text().strip()
        if not local_root or not remote_root:
            return
        opts = SyncOptions(direction=self.direction.get_active_name(),
                           criteria=("time", "size", "either")[self.criteria.get_selected()],
                           recursive=self.recursive.get_active(), delete=self.delete.get_active(),
                           exclude=[p.strip() for p in self.exclude.get_text().split(",") if p.strip()])
        self.opts = opts
        self.roots = (local_root, remote_root)
        self.compare_btn.set_sensitive(False)
        self.compare_btn.set_label("Comparing…")
        self.cancel_event.clear()
        cancel = self.cancel_event
        self.page.session.run(lambda fs: compare(LocalFileSystem(), fs, local_root, remote_root, opts, cancel),
                              self._show_results, self._failed)

    def _failed(self, exc):
        self.compare_btn.set_sensitive(True)
        self.compare_btn.set_label("_Compare")
        self.page.toast(f"Comparison failed: {exc}")

    def _show_results(self, actions):
        self.compare_btn.set_sensitive(True)
        self.compare_btn.set_label("_Compare")
        self.actions = actions
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        if not actions:
            status = Adw.StatusPage(icon_name="object-select-symbolic", title="Already in Sync",
                                    description="Both folders have the same files.")
            view.set_content(status)
            self.nav.push(Adw.NavigationPage(title="Preview", child=view))
            return

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for act in actions:
            icon, verb = KIND_INFO[act.kind]
            src = act.local if act.kind in (UPLOAD, DELETE_LOCAL) else act.remote
            detail = act.reason
            if src is not None and not src.is_dir:
                detail += f" · {format_size(src.size)} · {format_time(src.mtime)}"
            row = Adw.ActionRow(title=GLib.markup_escape_text(act.rel + ("/" if src and src.is_dir else "")),
                                subtitle=GLib.markup_escape_text(f"{verb}: {detail}"))
            image = Gtk.Image(icon_name=icon)
            if act.kind in (DELETE_LOCAL, DELETE_REMOTE):
                image.add_css_class("error")
            row.add_prefix(image)
            check = Gtk.CheckButton(active=act.enabled, valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda c, a=act: (setattr(a, "enabled", c.get_active()), self._update_summary()))
            row.add_suffix(check)
            row.set_activatable_widget(check)
            listbox.append(row)
        clamp = Adw.Clamp(child=listbox, maximum_size=800, margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
        view.set_content(Gtk.ScrolledWindow(child=clamp, vexpand=True))

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=10, margin_bottom=10,
                         margin_start=12, margin_end=12)
        self.summary = Gtk.Label(xalign=0, hexpand=True)
        self.summary.add_css_class("dim-label")
        bottom.append(self.summary)
        go = Gtk.Button(label="_Synchronize", use_underline=True)
        go.add_css_class("suggested-action")
        go.connect("clicked", lambda *_a: self._run())
        self.go_btn = go
        bottom.append(go)
        view.add_bottom_bar(bottom)
        self.nav.push(Adw.NavigationPage(title="Preview", child=view))
        self._update_summary()

    def _update_summary(self):
        chosen = [a for a in self.actions if a.enabled]
        ups = sum(1 for a in chosen if a.kind == UPLOAD)
        downs = sum(1 for a in chosen if a.kind == DOWNLOAD)
        dels = sum(1 for a in chosen if a.kind in (DELETE_LOCAL, DELETE_REMOTE))
        size = sum(a.size for a in chosen)
        parts = []
        if ups:
            parts.append(f"{ups} upload(s)")
        if downs:
            parts.append(f"{downs} download(s)")
        if dels:
            parts.append(f"{dels} deletion(s)")
        text = ", ".join(parts) or "Nothing selected"
        if size:
            text += f" · {format_size(size)}"
        self.summary.set_text(text)
        self.go_btn.set_sensitive(bool(chosen))

    # ------------------------------------------------------------------ run
    def _run(self):
        page = self.page
        local_root, remote_root = self.roots
        chosen = [a for a in self.actions if a.enabled]
        uploads = [(a.local, posixpath.join(remote_root, a.rel)) for a in chosen if a.kind == UPLOAD]
        downloads = [(a.remote, posixpath.join(local_root, a.rel)) for a in chosen if a.kind == DOWNLOAD]
        del_local = [a.local.path for a in chosen if a.kind == DELETE_LOCAL]
        del_remote = [a.remote.path for a in chosen if a.kind == DELETE_REMOTE]
        remote_ep = SessionEndpoint(page.session)
        jobs = []
        if uploads:
            jobs.append(TransferJob(f"Synchronize: upload {len(uploads)} item(s)", LocalEndpoint(), remote_ep,
                                    uploads, settings=page.settings, overwrite="overwrite", direction="upload",
                                    subtitle=f"to {page.title}:{remote_root}"))
        if downloads:
            jobs.append(TransferJob(f"Synchronize: download {len(downloads)} item(s)", remote_ep, LocalEndpoint(),
                                    downloads, settings=page.settings, overwrite="overwrite",
                                    direction="download", subtitle=f"to {local_root}"))
        if del_local:
            jobs.append(DeleteJob(f"Synchronize: delete {len(del_local)} local item(s)", LocalEndpoint(),
                                  del_local, subtitle=local_root))
        if del_remote:
            jobs.append(DeleteJob(f"Synchronize: delete {len(del_remote)} item(s) on the server", remote_ep,
                                  del_remote, subtitle=f"{page.title}:{remote_root}"))
        for job in jobs:
            job.on_finished = lambda _j: (page.local_pane.refresh(quiet=True), page.remote_pane.refresh(quiet=True))
            page.window.add_transfer(job)
        self.close()
