# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Embedded SSH terminal (VTE) running over the session's own SSH
connection — no second login needed."""

from __future__ import annotations

import shlex
import threading

import gi
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

try:
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte
except (ValueError, ImportError):  # pragma: no cover - optional dependency
    Vte = None

from .prompts import show_error

PALETTE_DARK = ["#241f31", "#c01c28", "#2ec27e", "#f5c211", "#1e78e4", "#9841bb", "#0ab9dc", "#c0bfbc",
                "#5e5c64", "#ed333b", "#57e389", "#f8e45c", "#51a1ff", "#c061cb", "#4fd2fd", "#f6f5f4"]


def open_terminal(page, cwd: str | None):
    if Vte is None:
        show_error(page.window, "Terminal Unavailable",
                   "Install the VTE library for GTK 4 (package gir1.2-vte-3.91) to use the terminal.")
        return
    win = TerminalWindow(page, cwd)
    win.present()


class TerminalWindow(Adw.ApplicationWindow):
    def __init__(self, page, cwd):
        super().__init__(application=page.window.get_application(), default_width=900, default_height=560)
        self.page = page
        self.channel = None
        self._closed = False
        self.set_title(f"Terminal – {page.title}")

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=page.title, subtitle="Terminal")
        header.set_title_widget(self.title_widget)
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy (Ctrl+Shift+C)")
        copy_btn.connect("clicked", lambda *_a: self.term.copy_clipboard_format(Vte.Format.TEXT))
        paste_btn = Gtk.Button(icon_name="edit-paste-symbolic", tooltip_text="Paste (Ctrl+Shift+V)")
        paste_btn.connect("clicked", lambda *_a: self.term.paste_clipboard())
        header.pack_start(copy_btn)
        header.pack_start(paste_btn)
        view.add_top_bar(header)

        self.term = Vte.Terminal(hexpand=True, vexpand=True)
        self.term.set_font(Pango.FontDescription.from_string(page.settings["editor_font"] or "Monospace 11"))
        self.term.set_scrollback_lines(10000)
        self.term.set_mouse_autohide(True)
        self.term.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        self._apply_colors()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_a: self._apply_colors())
        self.term.connect("commit", self._on_commit)
        self.term.connect("notify::column-count", self._on_resize)
        self.term.connect("notify::row-count", self._on_resize)
        try:
            self.term.connect("termprop-changed::" + Vte.TERMPROP_XTERM_TITLE, self._on_title)
        except (AttributeError, TypeError):
            self.term.connect("window-title-changed", self._on_title)

        self.toasts = Adw.ToastOverlay(child=self.term)
        view.set_content(self.toasts)
        self.set_content(view)

        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control><Shift>c"),
                                       action=Gtk.CallbackAction.new(
                                           lambda *_a: (self.term.copy_clipboard_format(Vte.Format.TEXT), True)[1])))
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control><Shift>v"),
                                       action=Gtk.CallbackAction.new(
                                           lambda *_a: (self.term.paste_clipboard(), True)[1])))
        self.add_controller(keys)
        self.connect("close-request", self._on_close)
        self._start(cwd)

    def _apply_colors(self):
        dark = Adw.StyleManager.get_default().get_dark()
        fg, bg = (Gdk.RGBA(), Gdk.RGBA())
        fg.parse("#f6f5f4" if dark else "#241f31")
        bg.parse("#1e1e1e" if dark else "#ffffff")
        palette = []
        for c in PALETTE_DARK:
            rgba = Gdk.RGBA()
            rgba.parse(c)
            palette.append(rgba)
        self.term.set_colors(fg, bg, palette)

    def _start(self, cwd):
        cols, rows = self.term.get_column_count() or 100, self.term.get_row_count() or 30

        def job(fs):
            chan = fs.open_shell("xterm-256color", cols, rows)
            if cwd:
                chan.sendall(f"cd {shlex.quote(cwd)} && clear\n".encode())
            return chan

        def started(chan):
            self.channel = chan
            threading.Thread(target=self._reader, daemon=True, name="terminal").start()
            self.term.grab_focus()

        def failed(exc):
            self.close()
            show_error(self.page.window, "Cannot Open Terminal", exc)
        self.page.session.run(job, started, failed)

    def _reader(self):
        chan = self.channel
        while not self._closed:
            try:
                data = chan.recv(65536)
            except Exception:  # noqa: BLE001
                data = b""
            if not data:
                break
            GLib.idle_add(self._feed, data)
        GLib.idle_add(self._ended)

    def _feed(self, data):
        if not self._closed:
            self.term.feed(data)
        return False

    def _ended(self):
        if self._closed:
            return False
        self.title_widget.set_subtitle("Session ended")
        self.toasts.add_toast(Adw.Toast(title="The remote shell has exited", timeout=0))
        return False

    def _on_commit(self, _term, text, _size):
        if self.channel is not None and not self.channel.closed:
            try:
                self.channel.sendall(text.encode("utf-8"))
            except OSError:
                pass

    def _on_resize(self, *_a):
        if self.channel is not None and not self.channel.closed:
            try:
                self.channel.resize_pty(self.term.get_column_count(), self.term.get_row_count())
            except Exception:  # noqa: BLE001
                pass

    def _on_title(self, *_a):
        title = None
        try:
            title = self.term.get_termprop_string(Vte.TERMPROP_XTERM_TITLE)
            if isinstance(title, tuple):
                title = title[0]
        except (AttributeError, TypeError):
            try:
                title = self.term.get_window_title()
            except AttributeError:
                title = None
        if title:
            self.title_widget.set_subtitle(title)

    def _on_close(self, *_a):
        self._closed = True
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:  # noqa: BLE001
                pass
        return False
