# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""How a connection is going, machine by machine: the jump hosts, then the
server, from top to bottom, each with what happened while connecting to it
(the session's ``stages``)."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .prompts import make_spinner

ICONS = {"waiting": "content-loading-symbolic", "done": "object-select-symbolic",
         "failed": "dialog-error-symbolic"}


class _Stage(Gtk.Box):
    def __init__(self, stage: dict, last: bool):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class("connect-stage")
        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.mark = Gtk.Stack(width_request=20, height_request=20, valign=Gtk.Align.START)
        self.mark.add_named(make_spinner(width_request=18, height_request=18), "active")
        for state, icon in ICONS.items():
            image = Gtk.Image(icon_name=icon, pixel_size=16)
            image.add_css_class(f"stage-{state}")
            self.mark.add_named(image, state)
        rail.append(self.mark)
        if not last:                        # the line down to the next machine
            line = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, vexpand=True,
                                 halign=Gtk.Align.CENTER)
            line.add_css_class("connect-rail")
            rail.append(line)
        self.append(rail)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        text.set_margin_bottom(14)
        self.title = Gtk.Label(label=stage["title"], xalign=0, wrap=True, wrap_mode=2)
        self.title.add_css_class("heading")
        text.append(self.title)
        role = Gtk.Label(label=stage["role"], xalign=0, wrap=True)
        role.add_css_class("caption")
        role.add_css_class("dim-label")
        text.append(role)
        self.events = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.events.set_margin_top(4)
        text.append(self.events)
        self.append(text)
        self.shown = 0
        self.state = None
        self.update(stage)

    def update(self, stage: dict):
        if stage["state"] != self.state:
            self.state = stage["state"]
            self.mark.set_visible_child_name(self.state)
            for state in ("waiting", "active", "done", "failed"):
                self.title.remove_css_class(f"stage-{state}")
            self.title.add_css_class(f"stage-{self.state}")
        for n in range(self.shown, len(stage["events"])):
            failed = self.state == "failed" and n == len(stage["events"]) - 1
            label = Gtk.Label(label=stage["events"][n], xalign=0, wrap=True, wrap_mode=2)
            label.add_css_class("caption")
            label.add_css_class("error" if failed else "dim-label")
            self.events.append(label)
        self.shown = len(stage["events"])


class ConnectionProgress(Gtk.Box):
    """The session's stages, kept up to date."""

    def __init__(self, session):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("connect-progress")
        self.session = session
        self._rows: list[_Stage] = []
        self._plan: list[tuple] = []
        self._handler = session.connect("progress", lambda *_a: self.refresh())
        self.connect("destroy", lambda *_a: self.session.disconnect(self._handler))
        self.refresh()

    def refresh(self):
        stages = self.session.stages
        plan = [(s["title"], s["role"]) for s in stages]
        if plan != self._plan or any(row.shown > len(s["events"]) for row, s in zip(self._rows, stages)):
            for row in self._rows:
                self.remove(row)
            self._rows = [_Stage(s, n == len(stages) - 1) for n, s in enumerate(stages)]
            for row in self._rows:
                self.append(row)
            self._plan = plan
        else:
            for row, stage in zip(self._rows, stages):
                row.update(stage)
        GLib.idle_add(self._follow)

    def _follow(self):
        """Keep the newest line in sight while the list grows."""
        scroller = self.get_ancestor(Gtk.ScrolledWindow)
        if scroller is not None:
            adj = scroller.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
        return False


def progress_clamp(session) -> tuple[Adw.Clamp, ConnectionProgress]:
    progress = ConnectionProgress(session)
    return Adw.Clamp(maximum_size=560, child=progress), progress
