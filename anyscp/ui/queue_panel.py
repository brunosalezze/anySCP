# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Transfer queue (a strip along the bottom of the window, as in WinSCP)
and the header-bar progress indicator."""

from __future__ import annotations

import math

from gi.repository import Adw, GLib, Graphene, Gsk, Gtk, Pango

from ..transfer import CANCELLED, DONE, FAILED, QUEUED, RUNNING
from ..utils import format_duration, format_size, format_speed

ICONS = {"upload": "go-up-symbolic", "download": "go-down-symbolic", "copy": "edit-copy-symbolic",
         "delete": "user-trash-symbolic"}

# The columns right of the destination have fixed widths, so that they line
# up from row to row; together they still fit the smallest window (640 px).
PROGRESS_WIDTH = 150        # px
STATUS_CHARS = 36           # the full text is in the tooltip
BUTTONS_WIDTH = 54          # px: room for two buttons, however many are shown
MAX_LIST_HEIGHT = 172       # px: about five transfers, then the list scrolls


class JobRow(Gtk.ListBoxRow):
    """One transfer on one line: what, where to, progress, figures, buttons."""

    def __init__(self, panel, job):
        super().__init__(activatable=False, selectable=False)
        self.panel = panel
        self.job = job
        self.add_css_class("job-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon_name = ICONS.get(getattr(job, "direction", job.kind), "folder-download-symbolic")
        self.icon = Gtk.Image(icon_name=icon_name)
        self.icon.add_css_class("job-icon")
        box.append(self.icon)

        self.title = Gtk.Label(label=job.title, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                               max_width_chars=48)
        self.title.add_css_class("job-title")
        self.subtitle = Gtk.Label(label=job.subtitle, xalign=0, hexpand=True,
                                  ellipsize=Pango.EllipsizeMode.START)
        self.subtitle.add_css_class("dim-label")
        self.subtitle.set_tooltip_text(job.subtitle)
        box.append(self.title)
        box.append(self.subtitle)

        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        self.progress.set_size_request(PROGRESS_WIDTH, -1)
        self.progress.add_css_class("job-progress")
        box.append(self.progress)

        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                width_chars=STATUS_CHARS, max_width_chars=STATUS_CHARS)
        self.status.add_css_class("caption")
        self.status.add_css_class("numeric")
        box.append(self.status)

        self.buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.buttons.set_size_request(BUTTONS_WIDTH, -1)
        self.cancel_btn = self._button("process-stop-symbolic", "Cancel", lambda: panel.manager.cancel(job))
        self.retry_btn = self._button("view-refresh-symbolic", "Retry", lambda: panel.manager.retry(job))
        self.remove_btn = self._button("window-close-symbolic", "Remove", lambda: panel.manager.remove(job))
        for b in (self.cancel_btn, self.retry_btn, self.remove_btn):
            self.buttons.append(b)
        box.append(self.buttons)
        self.set_child(box)
        self.update()

    def _button(self, icon, tip, cb):
        b = Gtk.Button(icon_name=icon, tooltip_text=tip)
        b.add_css_class("flat")
        b.add_css_class("circular")
        b.connect("clicked", lambda *_a: cb())
        return b

    def update(self):
        job = self.job
        state = job.state
        self.title.set_text(job.title)
        self.progress.set_fraction(job.fraction)
        for cls in ("success", "error", "warning", "accent"):
            self.progress.remove_css_class(cls)
        self.cancel_btn.set_visible(state in (QUEUED, RUNNING))
        self.retry_btn.set_visible(state in (FAILED, CANCELLED))
        self.remove_btn.set_visible(state not in (QUEUED, RUNNING))
        if state == QUEUED:
            self.status.set_text("Waiting…")
            self.progress.set_fraction(0)
        elif state == RUNNING:
            speed = job.sample_speed()
            if job.total_bytes:
                parts = [f"{format_size(job.done_bytes)} of {format_size(job.total_bytes)}"]
                if speed > 0:
                    parts.append(format_speed(speed))
                    eta = job.eta(speed)
                    if eta is not None:
                        parts.append(f"{format_duration(eta)} left")
                if job.total_files > 1:
                    parts.append(f"file {min(job.done_files + 1, job.total_files)}/{job.total_files}")
                text = " · ".join(parts)
            else:
                text = job.current or "Starting…"
                if not job.total_files:
                    self.progress.pulse()
            self.status.set_text(text)
            self.status.set_tooltip_text("\n".join(t for t in (text, job.current) if t))
        elif state == DONE:
            self.progress.add_css_class("success")
            took = (job.finished_at or 0) - (job.started_at or 0)
            text = f"Done · {format_size(job.total_bytes)}" if job.total_bytes else "Done"
            if job.total_files > 1:
                text += f" · {job.total_files} files"
            if took > 1:
                text += f" in {format_duration(took)}"
            self.status.set_text(text)
            self.status.set_tooltip_text(text)
            if job.warnings:
                self.status.set_text(f"{text} · {len(job.warnings)} skipped")
                self.progress.add_css_class("warning")
                self.status.set_tooltip_text("\n".join([text, ""] + job.warnings[:20]))
        elif state == FAILED:
            self.progress.add_css_class("error")
            self.status.set_text(job.error or "Failed")
            self.status.set_tooltip_text(job.error)
        elif state == CANCELLED:
            self.status.set_text("Cancelled")
            self.status.set_tooltip_text(None)


class QueuePanel(Gtk.Box):
    """The queue: a title line and one line per transfer, newest first."""

    def __init__(self, manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("queue-panel")
        self.manager = manager
        self.rows: dict[int, JobRow] = {}
        self._timer = 0

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.add_css_class("queue-bar")
        title = Gtk.Label(label="Transfers", xalign=0)
        title.add_css_class("heading")
        bar.append(title)
        self.summary = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.summary.add_css_class("caption")
        self.summary.add_css_class("dim-label")
        bar.append(self.summary)
        self.clear_btn = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Clear finished")
        self.clear_btn.add_css_class("flat")
        self.clear_btn.connect("clicked", lambda *_a: manager.clear_finished())
        bar.append(self.clear_btn)
        self.close_btn = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Hide (Ctrl+J)")
        self.close_btn.add_css_class("flat")
        bar.append(self.close_btn)          # the window connects it
        self.append(bar)

        self.empty = Gtk.Label(label="No transfers. Drag files between the panes, or select files and press F5.",
                               xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.empty.add_css_class("queue-empty")
        self.empty.add_css_class("dim-label")
        self.append(self.empty)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("queue-list")
        self.scroller = Gtk.ScrolledWindow(child=self.listbox, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           propagate_natural_height=True,
                                           max_content_height=MAX_LIST_HEIGHT)
        self.append(self.scroller)

        manager.connect("changed", lambda *_a: self.sync_rows())
        self.sync_rows()

    def sync_rows(self):
        current = {job.id: job for job in self.manager.jobs}
        for jid in list(self.rows):
            if jid not in current:
                self.listbox.remove(self.rows.pop(jid))
        for job in reversed(self.manager.jobs):   # newest on top
            if job.id not in self.rows:
                row = JobRow(self, job)
                self.rows[job.id] = row
                self.listbox.prepend(row)
        for row in self.rows.values():
            row.update()
        self.scroller.set_visible(bool(self.rows))
        self.empty.set_visible(not self.rows)
        # As tall as its rows, up to MAX_LIST_HEIGHT, and no smaller: the
        # window's toolbar view sizes its content with the bars' natural
        # heights, so a queue that could shrink below them would leave the
        # panes less than their minimum height (and GTK warning about it).
        if self.rows:
            height = min(MAX_LIST_HEIGHT, self.listbox.measure(Gtk.Orientation.VERTICAL, -1)[1])
            if height != self.scroller.get_min_content_height():
                self.scroller.set_min_content_height(height)
        self.clear_btn.set_sensitive(any(j.state not in (QUEUED, RUNNING) for j in self.manager.jobs))
        self._update_summary()
        if self.manager.active and not self._timer:
            self._timer = GLib.timeout_add(250, self._tick)

    def _tick(self):
        for row in self.rows.values():
            if row.job.state in (RUNNING, QUEUED):
                row.update()
        self._update_summary()
        if not self.manager.active:
            self._timer = 0
            return False
        return True

    def _update_summary(self):
        active = self.manager.active
        if active:
            running = sum(1 for j in active if j.state == RUNNING)
            self.summary.set_text(f"{running} running, {len(active) - running} waiting")
        else:
            done = sum(1 for j in self.manager.jobs if j.state == DONE)
            failed = sum(1 for j in self.manager.jobs if j.state == FAILED)
            text = f"{done} completed"
            if failed:
                text += f", {failed} failed"
            self.summary.set_text(text if self.manager.jobs else "")


class ProgressIcon(Gtk.Widget):
    """Header-bar icon with a circular progress ring around it."""

    __gtype_name__ = "AnyscpProgressIcon"

    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.set_size_request(24, 24)
        self.icon = Gtk.Image(icon_name="folder-download-symbolic")
        self.icon.set_parent(self)
        self._fraction = None
        self._timer = 0
        manager.connect("changed", lambda *_a: self._changed())

    def do_dispose(self):
        if self.icon is not None:
            self.icon.unparent()
            self.icon = None

    def do_measure(self, orientation, for_size):
        return 24, 24, -1, -1

    def do_size_allocate(self, width, height, baseline):
        nat = 16
        point = Graphene.Point()
        point.x, point.y = (width - nat) // 2, (height - nat) // 2
        self.icon.allocate(nat, nat, baseline, Gsk.Transform.new().translate(point))

    def _changed(self):
        if self.manager.active and not self._timer:
            self._timer = GLib.timeout_add(250, self._tick)
        self._tick()

    def _tick(self):
        if self.manager.active:
            self._fraction = self.manager.overall_fraction()
            self.queue_draw()
            return True
        self._fraction = None
        self._timer = 0
        self.queue_draw()
        return False

    def do_snapshot(self, snapshot):
        if self._fraction is not None:
            width, height = self.get_width(), self.get_height()
            size = min(width, height)
            cx, cy = width / 2, height / 2
            r = size / 2 - 1.5
            color = self.get_color()
            try:
                rect = Graphene.Rect().init(0, 0, width, height)
                cr = snapshot.append_cairo(rect)
                cr.set_line_width(2.5)
                cr.set_source_rgba(color.red, color.green, color.blue, 0.18)
                cr.arc(cx, cy, r, 0, 2 * math.pi)
                cr.stroke()
                accent = Adw.StyleManager.get_default().get_accent_color_rgba()
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
                cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * max(0.02, self._fraction))
                cr.stroke()
            except (ImportError, TypeError):
                pass
        self.snapshot_child(self.icon, snapshot)
