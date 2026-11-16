# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Built-in text editor (GtkSourceView 5) for local and remote files."""

from __future__ import annotations

import posixpath

import gi
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource
    GtkSource.init()
except (ValueError, ImportError):  # pragma: no cover - optional dependency
    GtkSource = None

from ..utils import format_size
from .prompts import alert, show_error

MAX_SIZE = 50 * 1024 * 1024
_open_editors: dict[tuple, "EditorWindow"] = {}
_font_css: Gtk.CssProvider | None = None


def _set_editor_font(font: Pango.FontDescription):
    """One display-wide provider styles every editor view."""
    global _font_css
    family = font.get_family() or "monospace"
    size = font.get_size() / Pango.SCALE if font.get_size() else 11
    if _font_css is None:
        _font_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), _font_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    _font_css.load_from_string(
        f"textview.source-view {{ font-family: \"{family}\"; font-size: {size}pt; }}")


def open_editor(page, pane, info):
    key = (id(page), pane.is_local, info.path)
    existing = _open_editors.get(key)
    if existing is not None:
        existing.present()
        return
    if info.size > MAX_SIZE:
        show_error(page.window, "File Too Large",
                   f"{info.name} is {format_size(info.size)}. Use Open With Default App instead.")
        return
    backend = page._backend_for(pane)

    def load(fs):
        reader = fs.open_read(info.path)
        try:
            chunks = []
            while chunk := reader.read(256 * 1024):
                chunks.append(chunk)
        finally:
            reader.close()
        return b"".join(chunks)

    def loaded(data):
        if b"\0" in data[:8192]:
            def resp(rid):
                if rid == "open":
                    _show(data)
            alert(page.window, "Binary File", f"{info.name} looks like a binary file. Edit it anyway?",
                  responses=(("cancel", "_Cancel"), ("open", "_Edit Anyway")), close="cancel",
                  default="cancel", callback=resp)
            return
        _show(data)

    def _show(data):
        win = EditorWindow(page, pane, info, data, backend)
        _open_editors[key] = win
        win.connect("close-request", lambda *_a: (_open_editors.pop(key, None), False)[1])
        win.present()

    backend.run(load, loaded, lambda exc: show_error(page.window, f"Cannot Open {info.name}", exc))


def _decode(data: bytes) -> tuple[str, str]:
    for enc in ("utf-8", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8",):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


class EditorWindow(Adw.ApplicationWindow):
    def __init__(self, page, pane, info, data: bytes, backend):
        super().__init__(application=page.window.get_application(), default_width=920, default_height=680)
        self.page = page
        self.pane = pane
        self.info = info
        self.backend = backend
        self.settings = page.settings
        text, self.encoding = _decode(data)
        self.has_bom = text.startswith("﻿")
        if self.has_bom:
            text = text[1:]

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=info.name,
                                            subtitle=("" if pane.is_local else f"{page.title}:") +
                                            posixpath.dirname(info.path))
        header.set_title_widget(self.title_widget)
        self.save_btn = Gtk.Button(label="_Save", use_underline=True, tooltip_text="Save (Ctrl+S)")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_a: self.save())
        header.pack_start(self.save_btn)
        self.search_btn = Gtk.ToggleButton(icon_name="edit-find-symbolic", tooltip_text="Find (Ctrl+F)")
        header.pack_end(self.search_btn)
        menu = Gio.Menu()
        menu.append("Wrap Lines", "editor.wrap")
        menu.append("Reload from Server" if not pane.is_local else "Reload", "editor.reload")
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu))
        view.add_top_bar(header)

        # search bar
        self.search_entry = Gtk.SearchEntry(placeholder_text="Find", hexpand=True)
        self.replace_entry = Gtk.Entry(placeholder_text="Replace with", hexpand=True)
        sbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sbox.append(self.search_entry)
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        nav.add_css_class("linked")
        prev_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Previous (Shift+Enter)")
        next_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Next (Enter)")
        nav.append(prev_btn)
        nav.append(next_btn)
        sbox.append(nav)
        sbox.append(self.replace_entry)
        replace_btn = Gtk.Button(label="Replace")
        replace_all_btn = Gtk.Button(label="All")
        sbox.append(replace_btn)
        sbox.append(replace_all_btn)
        self.search_bar = Gtk.SearchBar(child=Adw.Clamp(child=sbox, maximum_size=900))
        self.search_bar.connect_entry(self.search_entry)
        self.search_btn.bind_property("active", self.search_bar, "search-mode-enabled",
                                      GObject.BindingFlags.BIDIRECTIONAL)
        view.add_top_bar(self.search_bar)

        # text view
        if GtkSource is not None:
            self.buffer = GtkSource.Buffer()
            self.view = GtkSource.View(buffer=self.buffer, show_line_numbers=True, highlight_current_line=True,
                                       auto_indent=True, smart_backspace=True, monospace=True,
                                       show_right_margin=False)
            self.view.set_smart_home_end(GtkSource.SmartHomeEndType.BEFORE)
            lm = GtkSource.LanguageManager.get_default()
            ctype, _u = Gio.content_type_guess(info.name, data[:4096])
            lang = lm.guess_language(info.name, ctype)
            if lang is not None:
                self.buffer.set_language(lang)
            self.language_name = lang.get_name() if lang else "Plain Text"
            self._apply_scheme()
            Adw.StyleManager.get_default().connect("notify::dark", lambda *_a: self._apply_scheme())
            self.search_settings = GtkSource.SearchSettings(wrap_around=True, case_sensitive=False)
            self.search_context = GtkSource.SearchContext(buffer=self.buffer, settings=self.search_settings)
            self.buffer.begin_irreversible_action()
            self.buffer.set_text(text)
            self.buffer.end_irreversible_action()
        else:
            self.buffer = Gtk.TextBuffer()
            self.view = Gtk.TextView(buffer=self.buffer, monospace=True)
            self.language_name = "Plain Text"
            self.search_context = None
            self.buffer.set_text(text)
        self.view.add_css_class("source-view")
        self.view.set_top_margin(6)
        self.view.set_bottom_margin(6)
        self.view.set_left_margin(6)
        self._apply_settings()
        self.buffer.set_modified(False)
        self.buffer.place_cursor(self.buffer.get_start_iter())
        self._version = 0
        self.buffer.connect("changed", self._on_buffer_changed)
        self.buffer.connect("modified-changed", lambda *_a: self._update_title())
        self.buffer.connect("notify::cursor-position", lambda *_a: self._update_status())

        scroller = Gtk.ScrolledWindow(child=self.view, vexpand=True)
        self.toasts = Adw.ToastOverlay(child=scroller)
        view.set_content(self.toasts)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("editor-status")
        self.status.add_css_class("dim-label")
        self.status.add_css_class("numeric")
        view.add_bottom_bar(self.status)
        self.set_content(view)

        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_a: self._find(forward=True))
        next_btn.connect("clicked", lambda *_a: self._find(forward=True))
        prev_btn.connect("clicked", lambda *_a: self._find(forward=False))
        replace_btn.connect("clicked", lambda *_a: self._replace(all_=False))
        replace_all_btn.connect("clicked", lambda *_a: self._replace(all_=True))
        skeys = Gtk.EventControllerKey()
        skeys.connect("key-pressed", self._on_search_key)
        self.search_entry.add_controller(skeys)

        self._install_actions()
        self._settings_cb = lambda k, _v: self._apply_settings() if k.startswith("editor_") else None
        self.settings.connect(self._settings_cb)
        self.connect("close-request", self._on_close_request)
        self._update_title()
        self._update_status()
        self.view.grab_focus()

    # ------------------------------------------------------------------ setup
    def _install_actions(self):
        group = Gio.SimpleActionGroup()
        wrap = Gio.SimpleAction.new_stateful("wrap", None, GLib.Variant.new_boolean(bool(self.settings["editor_wrap"])))
        wrap.connect("change-state", lambda a, v: (a.set_state(v), self.settings.__setitem__("editor_wrap", v.get_boolean())))
        group.add_action(wrap)
        reload_ = Gio.SimpleAction.new("reload", None)
        reload_.connect("activate", lambda *_a: self.reload())
        group.add_action(reload_)
        self.insert_action_group("editor", group)

        controller = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        controller.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>s"),
                                             action=Gtk.CallbackAction.new(lambda *_a: (self.save(), True)[1])))
        controller.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>f"),
                                             action=Gtk.CallbackAction.new(self._start_search)))
        controller.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>w"),
                                             action=Gtk.CallbackAction.new(lambda *_a: (self.close(), True)[1])))
        controller.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("F3"),
                                             action=Gtk.CallbackAction.new(lambda *_a: (self._find(True), True)[1])))
        self.add_controller(controller)

    def _apply_scheme(self):
        if GtkSource is None:
            return
        sm = GtkSource.StyleSchemeManager.get_default()
        dark = Adw.StyleManager.get_default().get_dark()
        for name in (("Adwaita-dark", "oblivion") if dark else ("Adwaita", "classic")):
            scheme = sm.get_scheme(name)
            if scheme is not None:
                self.buffer.set_style_scheme(scheme)
                break

    def _apply_settings(self):
        s = self.settings
        font = Pango.FontDescription.from_string(s["editor_font"] or "Monospace 11")
        _set_editor_font(font)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if s["editor_wrap"] else Gtk.WrapMode.NONE)
        if GtkSource is not None:
            self.view.set_tab_width(int(s["editor_tab_width"] or 4))
            self.view.set_indent_width(-1)

    def _on_buffer_changed(self, *_a):
        self._version += 1

    # ------------------------------------------------------------------ status
    def _update_title(self):
        modified = self.buffer.get_modified()
        self.title_widget.set_title(("• " if modified else "") + self.info.name)
        self.set_title(("• " if modified else "") + f"{self.info.name} – anySCP")
        self.save_btn.set_sensitive(modified)

    def _update_status(self):
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        line, col = it.get_line() + 1, it.get_line_offset() + 1
        self.status.set_text(f"Ln {line}, Col {col}   ·   {self.language_name}   ·   {self.encoding.upper()}")

    # ------------------------------------------------------------------ search
    def _start_search(self, *_a):
        self.search_btn.set_active(True)
        sel = self.buffer.get_selection_bounds()
        if sel:
            self.search_entry.set_text(self.buffer.get_text(sel[0], sel[1], False))
        self.search_entry.grab_focus()
        return True

    def _on_search_changed(self, entry):
        if self.search_context is not None:
            self.search_settings.set_search_text(entry.get_text() or None)
            self._find(True, from_start=True)

    def _on_search_key(self, _c, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.SHIFT_MASK:
            self._find(False)
            return True
        if keyval == Gdk.KEY_Escape:
            self.search_btn.set_active(False)
            self.view.grab_focus()
            return True
        return False

    def _find(self, forward=True, from_start=False):
        if self.search_context is None or not self.search_entry.get_text():
            return
        buf = self.buffer
        sel = buf.get_selection_bounds()
        if from_start:
            start = sel[0] if sel else buf.get_iter_at_mark(buf.get_insert())
        elif forward:
            start = sel[1] if sel else buf.get_iter_at_mark(buf.get_insert())
        else:
            start = sel[0] if sel else buf.get_iter_at_mark(buf.get_insert())
        if forward:
            found, s, e, _wrapped = self.search_context.forward(start)
        else:
            found, s, e, _wrapped = self.search_context.backward(start)
        if found:
            buf.select_range(s, e)
            self.view.scroll_to_iter(s, 0.2, False, 0, 0)

    def _replace(self, all_=False):
        if self.search_context is None or not self.search_entry.get_text():
            return
        repl = self.replace_entry.get_text()
        if all_:
            n = self.search_context.replace_all(repl, -1)
            self.toasts.add_toast(Adw.Toast(title=f"Replaced {n} occurrence(s)", timeout=2))
            return
        sel = self.buffer.get_selection_bounds()
        if sel:
            try:
                self.search_context.replace(sel[0], sel[1], repl, -1)
            except GLib.Error:
                pass
        self._find(True)

    # ------------------------------------------------------------------ io
    def _text_bytes(self) -> bytes:
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, True)
        if self.has_bom:
            text = "﻿" + text
        try:
            return text.encode(self.encoding)
        except UnicodeEncodeError:
            self.encoding = "utf-8"
            return text.encode("utf-8")

    def save(self, then=None):
        if not self.buffer.get_modified() and then is None:
            return
        data = self._text_bytes()
        path = self.info.path
        saved_version = self._version
        self.save_btn.set_sensitive(False)

        def job(fs):
            writer = fs.open_write(path, 0, len(data))
            try:
                for i in range(0, len(data), 256 * 1024):
                    writer.write(data[i:i + 256 * 1024])
            finally:
                writer.close()
            return True

        def done(_r):
            # Edits typed while the upload was running stay unsaved.
            if self._version == saved_version:
                self.buffer.set_modified(False)
            else:
                self._update_title()
            self.toasts.add_toast(Adw.Toast(title="Saved" if self.pane.is_local else "Saved to the server",
                                            timeout=2))
            if self.pane.path == posixpath.dirname(path):
                self.pane.refresh(quiet=True)
            if then:
                then()

        def failed(exc):
            self._update_title()
            show_error(self, "Save Failed", exc)
        self.backend.run(job, done, failed)

    def reload(self):
        def do_reload():
            def load(fs):
                reader = fs.open_read(self.info.path)
                try:
                    out = b""
                    while chunk := reader.read(256 * 1024):
                        out += chunk
                    return out
                finally:
                    reader.close()

            def loaded(data):
                text, self.encoding = _decode(data)
                self.buffer.set_text(text.lstrip("﻿"))
                self.buffer.set_modified(False)
                self.buffer.place_cursor(self.buffer.get_start_iter())
            self.backend.run(load, loaded, lambda exc: show_error(self, "Reload Failed", exc))
        if self.buffer.get_modified():
            alert(self, "Discard Changes?", "Reloading replaces your unsaved changes.",
                  responses=(("cancel", "_Cancel"), ("reload", "_Reload")), close="cancel",
                  destructive="reload", callback=lambda rid: do_reload() if rid == "reload" else None)
        else:
            do_reload()

    def _on_close_request(self, *_a):
        if not self.buffer.get_modified():
            self.settings.disconnect(self._settings_cb)
            return False

        def response(rid):
            if rid == "save":
                self.save(then=self._force_close)
            elif rid == "discard":
                self._force_close()
        alert(self, "Save Changes?", f"“{self.info.name}” has unsaved changes.",
              responses=(("cancel", "_Cancel"), ("discard", "_Discard"), ("save", "_Save")),
              default="save", close="cancel", suggested="save", destructive="discard", callback=response)
        return True

    def _force_close(self):
        self.buffer.set_modified(False)
        self.close()

