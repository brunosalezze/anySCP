# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Application object."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .config import APP_ID, Settings, SiteStore, site_from_url  # noqa: E402
from .transfer import TransferManager  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"


class AnySCPApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name("anySCP")
        GLib.set_prgname(APP_ID)
        self.settings = Settings()
        self.sites = SiteStore()
        self.transfers = None
        self.window = None
        self.self_test = False
        self.self_test_status = 0
        self.add_main_option("self-test", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start, check the environment (settings folder, libraries, "
                             "network-share paths) and quit", None)

    def do_handle_local_options(self, options):
        if options.contains("self-test"):
            self.self_test = True
            # run even if another anySCP is already open
            self.set_flags(self.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
        return -1

    # ------------------------------------------------------------------ lifecycle
    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        display = Gdk.Display.get_default()
        theme = Gtk.IconTheme.get_for_display(display)
        theme.add_search_path(str(DATA_DIR / "icons"))

        css = Gtk.CssProvider()
        css.load_from_path(str(DATA_DIR / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._apply_color_scheme()
        self.settings.connect(self._on_setting)

        from .ui import dragout
        dragout.cleanup()               # temporary folders an earlier run left behind

        from .ui.prompts import TransferDecider
        self.transfers = TransferManager(
            TransferDecider(lambda: self.get_active_window() or self.window, self.settings),
            parallel=self.settings["parallel_transfers"])

        for name, cb, accels in (
            ("quit", self._quit, ["<Control>q"]),
            ("preferences", self._preferences, ["<Control>comma"]),
            ("about", self._about, []),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=cb: cb())
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_shutdown(self):
        from .ui import dragout
        dragout.cleanup(leaving=True)
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        if self.window is None:
            from .ui.window import MainWindow
            self.window = MainWindow(self)
        self.window.present()
        if self.self_test:
            from .selftest import run_and_quit
            GLib.timeout_add(1500, run_and_quit, self)

    def do_open(self, files, _n, _hint):
        self.do_activate()
        for gfile in files:
            site = site_from_url(gfile.get_uri())
            if site is not None:
                site["_quick"] = True
                self.window.open_session(site)

    # ------------------------------------------------------------------ actions
    def _apply_color_scheme(self):
        scheme = {
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }.get(self.settings["color_scheme"], Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def _on_setting(self, key, _value):
        if key == "color_scheme":
            self._apply_color_scheme()

    def _quit(self):
        if self.window is not None:
            self.window.close()
        else:
            self.quit()

    def _preferences(self):
        from .ui.preferences import PreferencesDialog
        PreferencesDialog(self.settings).present(self.get_active_window())

    def _about(self):
        about = Adw.AboutDialog(
            application_name="anySCP",
            application_icon=APP_ID,
            version=__version__,
            developer_name="anySCP contributors",
            copyright="© 2026 anySCP contributors",
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/brunosalezze/anySCP",
            issue_url="https://github.com/brunosalezze/anySCP/issues",
            comments="A WinSCP-style file transfer client for Linux and Windows. Browse and transfer files over "
                     "SFTP, SCP, FTP/FTPS and WebDAV with a dual-pane commander, a transfer queue, "
                     "an integrated editor and terminal.",
        )
        about.add_credit_section("Inspired by", ["WinSCP by Martin Přikryl https://winscp.net"])
        about.add_legal_section(
            "WinSCP.ini import",
            "Password decoding ported from WinSCP (source/core/Security.cpp), © Martin Přikryl",
            Gtk.License.GPL_3_0, None)
        about.add_legal_section(
            "Disclaimer", None, Gtk.License.CUSTOM,
            "anySCP is an independent project. It is not affiliated with, endorsed by or "
            "sponsored by WinSCP or its author; the name WinSCP is used only to describe "
            "compatibility and inspiration. No WinSCP code (apart from the attributed "
            "password decoding), icons or artwork are used.")
        about.add_legal_section(
            "Bundled components", None, Gtk.License.CUSTOM,
            "The packaged builds (AppImage, Windows .exe) include Python, GTK, libadwaita, "
            "GtkSourceView, VTE, paramiko, cryptography and other open source components. "
            "Their licenses are in THIRD-PARTY-LICENSES.txt and the licenses folder of the package.")
        about.add_credit_section("Built with", ["GTK 4 https://gtk.org", "libadwaita https://gnome.pages.gitlab.gnome.org/libadwaita/",
                                                "Paramiko https://www.paramiko.org",
                                                "GtkSourceView", "VTE"])
        about.present(self.get_active_window())


def main(argv=None) -> int:
    if os.environ.get("ANYSCP_DEBUG"):
        os.environ.setdefault("G_MESSAGES_DEBUG", "")
    app = AnySCPApplication()
    status = app.run(argv if argv is not None else sys.argv)
    return app.self_test_status if app.self_test else status
