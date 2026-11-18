# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Dialogs shown while connecting/transferring, and the thread-safe
prompter that backends and the transfer queue use to reach them."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..config import secrets
from ..utils import ask_main, format_size, format_time


def _boxed(*rows) -> Gtk.ListBox:
    box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    box.add_css_class("boxed-list")
    for row in rows:
        box.append(row)
    return box


def _focus_later(widget):
    GLib.timeout_add(80, lambda: (widget.grab_focus(), False)[1])


def alert(parent, heading, body="", *, responses=(("ok", "_OK"),), default=None,
          close=None, suggested=None, destructive=None, extra=None, callback=None,
          body_markup=False):
    """Small helper around :class:`Adw.AlertDialog`."""
    dialog = Adw.AlertDialog(heading=heading)
    if body_markup:
        dialog.set_body_use_markup(True)
    dialog.set_body(body)
    for rid, label in responses:
        dialog.add_response(rid, label)
    if suggested:
        dialog.set_response_appearance(suggested, Adw.ResponseAppearance.SUGGESTED)
    if destructive:
        dialog.set_response_appearance(destructive, Adw.ResponseAppearance.DESTRUCTIVE)
    if default:
        dialog.set_default_response(default)
    dialog.set_close_response(close or responses[0][0])
    if extra is not None:
        dialog.set_extra_child(extra)
    if callback is not None:
        dialog.connect("response", lambda _d, rid: callback(rid))
    dialog.present(parent)
    return dialog


def show_error(parent, heading, exc_or_text):
    alert(parent, heading, GLib.markup_escape_text(str(exc_or_text)), body_markup=True)


def ask_text_dialog(parent, heading, body, initial, ok_label, callback, *,
                    select_stem=False, title="Name", validate=None):
    """Ask for a single line of text (rename, new folder, …)."""
    entry = Adw.EntryRow(title=title, text=initial or "")
    entry.set_activates_default(True)
    dialog = alert(parent, heading, body,
                   responses=(("cancel", "_Cancel"), ("ok", ok_label)),
                   default="ok", close="cancel", suggested="ok", extra=_boxed(entry))

    def update(*_a):
        text = entry.get_text().strip()
        ok = bool(text) and (validate is None or validate(text))
        dialog.set_response_enabled("ok", ok)
    entry.connect("changed", update)
    update()

    def on_response(_d, rid):
        callback(entry.get_text().strip() if rid == "ok" else None)
    dialog.connect("response", on_response)

    def focus():
        entry.grab_focus()
        text = entry.get_text()
        if select_stem and "." in text.lstrip("."):
            stem_len = len(text) - len(text.rsplit(".", 1)[1]) - 1
            entry.select_region(0, stem_len)
        else:
            entry.select_region(0, -1)
        return False
    GLib.timeout_add(80, focus)
    return dialog


class UIPrompter:
    """Implements :class:`anyscp.backends.base.Prompter` and the transfer
    decider interface by showing dialogs on the main thread.  All public
    methods are called from worker threads and block until answered."""

    def __init__(self, window_getter, site: dict | None = None):
        self._window = window_getter
        self.site = site or {}
        self.session = None
        self.before_prompt = None        # e.g. select the asking session's tab
        self.closed = False              # session tab closed: cancel every prompt
        self._entered_password: str | None = None
        self._remember = False

    @property
    def parent(self):
        if self.before_prompt is not None:
            try:
                self.before_prompt()
            except Exception:  # noqa: BLE001
                pass
        return self._window()

    # -- logging -------------------------------------------------------------
    def log(self, message):
        if self.session is not None:
            self.session.log(message)

    def connection_plan(self, stages):
        if self.session is not None:
            self.session.connection_plan(stages)

    def connection_stage(self, index):
        if self.session is not None:
            self.session.connection_stage(index)

    # -- connect-time prompts ----------------------------------------------
    def ask_password(self, title, message, remember=True):
        """remember=False: not the site's own password (a jump host's, the
        proxy's): it is neither offered for remembering nor kept."""
        if self.closed:
            return None
        is_password = "passphrase" not in title.lower()
        can_remember = remember and is_password and bool(self.site.get("id")) and not self.site.get("_quick")

        def show(reply):
            entry = Adw.PasswordEntryRow(title=title)
            entry.set_activates_default(True)
            extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            extra.append(_boxed(entry))
            remember = None
            if can_remember:
                remember = Gtk.CheckButton(label=f"_Remember password in the {secrets.backend_name}",
                                           use_underline=True,
                                           active=bool(self.site.get("save_password")))
                if not secrets.available:
                    remember.set_sensitive(False)
                    remember.set_label("Remember password (no keyring available)")
                extra.append(remember)

            def done(rid):
                if rid == "ok":
                    if remember is not None:
                        self._remember = remember.get_active()
                    reply(entry.get_text())
                else:
                    reply(None)
            alert(self.parent, title, message,
                  responses=(("cancel", "_Cancel"), ("ok", "_Log In")),
                  default="ok", close="cancel", suggested="ok", extra=extra, callback=done)
            _focus_later(entry)

        value = ask_main(show)
        if is_password and value is not None and remember:
            self._entered_password = value
        return value

    def ask_text(self, title, message, default=""):
        if self.closed:
            return None
        def show(reply):
            ask_text_dialog(self.parent, title, message, default, "_Continue", reply, title=title)
        return ask_main(show)

    def ask_interactive(self, title, instructions, prompts, remember=True):
        if self.closed:
            return None
        def show(reply):
            rows = []
            for text, echo in prompts:
                label = text.strip().rstrip(":") or "Response"
                row = Adw.EntryRow(title=label) if echo else Adw.PasswordEntryRow(title=label)
                row.set_activates_default(True)
                rows.append(row)

            def done(rid):
                reply([r.get_text() for r in rows] if rid == "ok" else None)
            alert(self.parent, title or "Authentication", instructions or "",
                  responses=(("cancel", "_Cancel"), ("ok", "_Continue")),
                  default="ok", close="cancel", suggested="ok",
                  extra=_boxed(*rows), callback=done)
            if rows:
                _focus_later(rows[0])
        answers = ask_main(show)
        if remember and answers and len(prompts) == 1 and not prompts[0][1]:
            self._entered_password = answers[0]
        return answers

    def verify_host_key(self, host, port, key_type, fingerprint, changed):
        if self.closed:
            return "reject"
        def show(reply):
            target = host if int(port) == 22 else f"{host}:{port}"
            if changed:
                heading = "Host Key Has Changed!"
                body = (f"The host key of <b>{GLib.markup_escape_text(target)}</b> does not match the "
                        "one cached for this server. Someone could be intercepting the "
                        "connection, or the server was reinstalled.\n\n"
                        f"New key: <tt>{key_type}</tt>\n<tt>{GLib.markup_escape_text(fingerprint)}</tt>\n\n"
                        "Only continue if you know why the key changed.")
                responses = (("reject", "_Cancel"), ("once", "Connect _Once"), ("save", "_Update Key"))
                dlg = alert(self.parent, heading, body, responses=responses, default="reject",
                            close="reject", destructive="save", callback=reply, body_markup=True)
            else:
                heading = "Unknown Server"
                body = (f"This is the first connection to <b>{GLib.markup_escape_text(target)}</b>. "
                        "Verify that the fingerprint matches the server's key before trusting it.\n\n"
                        f"<tt>{key_type}</tt>\n<tt>{GLib.markup_escape_text(fingerprint)}</tt>")
                responses = (("reject", "_Cancel"), ("once", "Connect _Once"), ("save", "_Accept & Save"))
                dlg = alert(self.parent, heading, body, responses=responses, default="save",
                            close="reject", suggested="save", callback=reply, body_markup=True)
            return dlg
        return ask_main(show) or "reject"

    def verify_certificate(self, host, port, fingerprint, subject, issuer, error):
        if self.closed:
            return "reject"
        def show(reply):
            esc = GLib.markup_escape_text
            body = (f"The certificate presented by <b>{esc(host)}:{port}</b> could not be verified:\n"
                    f"<i>{esc(error)}</i>\n\n"
                    + (f"Subject: {esc(subject)}\n" if subject else "")
                    + (f"Issuer: {esc(issuer)}\n" if issuer else "")
                    + f"\nSHA-256:\n<tt>{esc(fingerprint)}</tt>")
            alert(self.parent, "Untrusted Certificate", body,
                  responses=(("reject", "_Cancel"), ("once", "Trust _Once"), ("save", "_Always Trust")),
                  default="reject", close="reject", destructive="save", callback=reply,
                  body_markup=True)
        return ask_main(show) or "reject"

    def connection_succeeded(self):
        """Called on the main thread once logged in: store the password
        the user typed if they asked us to."""
        pw = self._entered_password
        site = self.site
        if pw is not None:
            if self.session is not None:
                self.session.site["_password"] = pw
            if self._remember and site.get("id") and not site.get("_quick"):
                secrets.store(site["id"], site.get("name") or site.get("host", ""), pw)
        self._entered_password = None


class TransferDecider:
    """Overwrite / error decisions for the transfer queue (main-thread dialogs)."""

    def __init__(self, window_getter, settings):
        self._window = window_getter
        self.settings = settings

    def ask_overwrite(self, job, op, existing, can_resume):
        def show(reply):
            esc = GLib.markup_escape_text
            fmt = self.settings["size_format"]
            name = op.dst.rsplit("/", 1)[-1]
            src_desc = f"{format_size(op.size, fmt)}, {format_time(op.mtime, 'iso')}"
            dst_desc = f"{format_size(existing.size, fmt)}, {format_time(existing.mtime, 'iso')}"
            newer = ""
            if op.mtime and existing.mtime:
                if op.mtime > existing.mtime + 1:
                    newer = "\nThe source file is <b>newer</b>."
                elif op.mtime + 1 < existing.mtime:
                    newer = "\nThe existing file is <b>newer</b>."
            body = (f"<b>{esc(name)}</b> already exists in the destination folder.\n\n"
                    f"New: {esc(src_desc)}\nExisting: {esc(dst_desc)}{newer}")
            apply_all = Gtk.CheckButton(label="_Apply to all remaining conflicts", use_underline=True)
            responses = [("cancel", "_Cancel"), ("skip", "_Skip"), ("newer", "_Newer Only")]
            if can_resume:
                responses.append(("resume", "_Resume"))
            responses.append(("overwrite", "_Overwrite"))
            dialog = alert(self._window(), "Replace File?", body, responses=responses,
                           default="overwrite", close="cancel", destructive="overwrite",
                           extra=apply_all, body_markup=True,
                           callback=lambda rid: reply((rid, apply_all.get_active())))
            dialog.set_prefer_wide_layout(True)
        return ask_main(show) or ("cancel", False)

    def ask_error(self, job, message):
        def show(reply):
            dialog = alert(self._window(), "Transfer Error", message,
                           responses=(("abort", "_Abort"), ("skip_all", "Skip _All"),
                                      ("skip", "_Skip"), ("retry", "_Retry")),
                           default="retry", close="abort", suggested="retry", callback=reply)
            dialog.set_prefer_wide_layout(True)
        return ask_main(show) or "abort"


# --------------------------------------------------------------------------
# Compatibility with older libadwaita releases (Ubuntu 24.04 ships 1.5)
# --------------------------------------------------------------------------

def make_spinner(**props) -> Gtk.Widget:
    """Adw.Spinner (libadwaita ≥ 1.6) or a spinning Gtk.Spinner."""
    if hasattr(Adw, "Spinner"):
        return Adw.Spinner(**props)
    spinner = Gtk.Spinner(spinning=True, **props)
    return spinner


class ChoiceGroup(Gtk.Box):
    """Segmented single-choice control: Adw.ToggleGroup on libadwaita ≥ 1.7,
    linked toggle buttons otherwise.  Use get_active_name()/set_active_name()
    and the ``changed`` callback."""

    def __init__(self, choices, on_changed=None):
        super().__init__(halign=Gtk.Align.CENTER)
        self._on_changed = on_changed
        self._buttons = {}
        if hasattr(Adw, "ToggleGroup"):
            self._group = Adw.ToggleGroup(homogeneous=True)
            for name, label, icon in choices:
                self._group.add(Adw.Toggle(name=name, label=label, icon_name=icon))
            self._group.connect("notify::active-name", lambda *_a: self._emit())
            self.append(self._group)
            return
        self._group = None
        self.add_css_class("linked")
        first = None
        for name, label, icon in choices:
            btn = Gtk.ToggleButton()
            content = Adw.ButtonContent(label=label, icon_name=icon)
            btn.set_child(content)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.connect("toggled", lambda b: b.get_active() and self._emit())
            self._buttons[name] = btn
            self.append(btn)

    def _emit(self):
        if self._on_changed:
            self._on_changed(self.get_active_name())

    def get_active_name(self) -> str | None:
        if self._group is not None:
            return self._group.get_active_name()
        return next((n for n, b in self._buttons.items() if b.get_active()), None)

    def set_active_name(self, name: str):
        if self._group is not None:
            self._group.set_active_name(name)
        elif name in self._buttons:
            self._buttons[name].set_active(True)
