# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Breadcrumb path bar that turns into a text entry (Ctrl+L)."""

from __future__ import annotations

import posixpath

from gi.repository import GLib, GObject, Gtk


class PathBar(Gtk.Box):
    __gsignals__ = {"navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, root_icon: str, home: str | None = None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.add_css_class("pathbar")
        self.path = "/"
        self.home = home
        self.root_icon = root_icon

        self.stack = Gtk.Stack(hexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=120)
        self.append(self.stack)

        # breadcrumbs
        self.crumb_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.crumb_box.add_css_class("crumbs")
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                           vscrollbar_policy=Gtk.PolicyType.NEVER,
                                           hexpand=True, child=self.crumb_box)
        self.scroller.add_css_class("crumb-scroller")
        # Keep the deepest folder visible whenever the content or size changes.
        self._scroll_pending = False
        self.scroller.get_hadjustment().connect("changed", lambda *_a: self._queue_scroll_end())
        click = Gtk.GestureClick()
        click.connect("released", self._on_background_click)
        self.scroller.add_controller(click)
        self.stack.add_named(self.scroller, "crumbs")

        # entry
        self.entry = Gtk.Entry(hexpand=True)
        self.entry.add_css_class("path-entry")
        self.entry.connect("activate", self._on_entry_activate)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_a: GLib.timeout_add(150, self._leave_entry))
        self.entry.add_controller(focus)
        self.stack.add_named(self.entry, "entry")

        self.edit_button = Gtk.Button(icon_name="document-edit-symbolic",
                                      tooltip_text="Type a path (Ctrl+L)")
        self.edit_button.add_css_class("flat")
        self.edit_button.add_css_class("pathbar-edit")
        self.edit_button.connect("clicked", lambda *_a: self.start_editing())
        self.append(self.edit_button)

    # -- public ------------------------------------------------------------
    def set_path(self, path: str):
        self.path = path
        self._rebuild()
        if self.stack.get_visible_child_name() == "entry":
            self.entry.set_text(path)

    def start_editing(self):
        self.entry.set_text(self.path)
        self.stack.set_visible_child_name("entry")
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def stop_editing(self):
        self.stack.set_visible_child_name("crumbs")

    # -- internals -----------------------------------------------------------
    def _rebuild(self):
        child = self.crumb_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.crumb_box.remove(child)
            child = nxt

        # (label, target) for every crumb after the root icon
        crumbs = []
        if self.path.startswith("//"):          # Windows share: \\server\share
            parts = [p for p in self.path[2:].split("/") if p]
            if len(parts) >= 2:
                acc = f"//{parts[0]}/{parts[1]}"
                crumbs.append((f"\\\\{parts[0]}\\{parts[1]}", acc))
                parts = parts[2:]
            else:
                acc, parts = self.path, []
        else:
            acc, parts = "/", [p for p in self.path.split("/") if p]
        for part in parts:
            acc = posixpath.join(acc, part)
            crumbs.append((part, acc))

        root = Gtk.Button()
        root.set_child(Gtk.Image(icon_name=self.root_icon))
        root.set_tooltip_text("/")
        root.add_css_class("flat")
        root.add_css_class("crumb")
        root.connect("clicked", lambda *_a: self.emit("navigate", "/"))
        self.crumb_box.append(root)

        for i, (label, target) in enumerate(crumbs):
            sep = Gtk.Label(label="/")
            sep.add_css_class("crumb-sep")
            self.crumb_box.append(sep)
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.add_css_class("crumb")
            if i == len(crumbs) - 1:
                btn.add_css_class("current")
            btn.connect("clicked", lambda _b, t=target: self.emit("navigate", t))
            self.crumb_box.append(btn)
        self._queue_scroll_end()

    def _queue_scroll_end(self):
        # Run after the viewport has finished configuring its adjustment;
        # setting the value from inside its "changed" signal gets overridden.
        if not self._scroll_pending:
            self._scroll_pending = True
            GLib.idle_add(self._scroll_end, priority=GLib.PRIORITY_LOW)

    def _scroll_end(self):
        self._scroll_pending = False
        adj = self.scroller.get_hadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def _on_background_click(self, gesture, _n, x, y):
        picked = self.scroller.pick(x, y, Gtk.PickFlags.DEFAULT)
        w = picked
        while w is not None and w is not self.scroller:
            if isinstance(w, Gtk.Button):
                return
            w = w.get_parent()
        self.start_editing()

    def _on_entry_activate(self, entry):
        text = entry.get_text().strip()
        if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
            # Windows-style "C:\Users" (local Windows pane or Windows SFTP servers)
            text = "/" + text[0].upper() + ":" + text[2:].replace("\\", "/")
        elif text.startswith("\\\\"):
            text = text.replace("\\", "/")
        if text:
            if text.startswith("~") and self.home:
                text = self.home + text[1:]
            if not text.startswith("/"):
                text = posixpath.join(self.path, text)
            self.emit("navigate", posixpath.normpath(text))
        self.stop_editing()

    def _on_entry_key(self, _ctrl, keyval, _code, _state):
        from gi.repository import Gdk
        if keyval == Gdk.KEY_Escape:
            self.stop_editing()
            return True
        return False

    def _leave_entry(self):
        if not self.entry.has_focus():
            self.stop_editing()
        return False
