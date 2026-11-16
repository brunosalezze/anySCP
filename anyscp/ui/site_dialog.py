# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Login / Site Manager dialog."""

from __future__ import annotations

import copy
import os

from gi.repository import Adw, Gio, GLib, Gtk

from ..backends.proxy import DEFAULT_PORTS as DEFAULT_PROXY_PORTS
from ..backends.proxy import PROXY_LABELS, PROXY_TYPES
from ..config import (FTP_ENCRYPTION, FTP_ENCRYPTION_LABELS, PROTOCOL_ORDER, PROTOCOLS,
                      SSH_PROTOCOLS, default_port, new_site, proxy_secret_id, secrets, site_subtitle,
                      site_title)
from ..utils import SITE_COLORS, color_dot_icon
from .prompts import alert

AUTH_MODES = ["auto", "password", "key", "agent"]
AUTH_LABELS = ["Automatic", "Password", "Public key", "SSH agent"]

PROTOCOL_ICONS = {"sftp": "network-server-symbolic", "scp": "utilities-terminal-symbolic",
                  "ftp": "folder-remote-symbolic", "webdav": "network-workgroup-symbolic"}


class SiteRow(Gtk.ListBoxRow):
    def __init__(self, site: dict | None):
        super().__init__()
        self.site = site
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        if site is None:
            icon = Gtk.Image(icon_name="list-add-symbolic")
            title, subtitle = "New Site", "Enter connection details"
        else:
            icon = Gtk.Image(icon_name=PROTOCOL_ICONS.get(site.get("protocol"), "network-server-symbolic"))
            title, subtitle = site_title(site), site_subtitle(site)
        box.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        t = Gtk.Label(label=title, xalign=0, ellipsize=3)
        s = Gtk.Label(label=subtitle, xalign=0, ellipsize=3)
        s.add_css_class("caption")
        s.add_css_class("dim-label")
        text.append(t)
        text.append(s)
        box.append(text)
        if site is not None and site.get("color"):
            dot = Gtk.Image.new_from_gicon(color_dot_icon(site["color"]))
            box.append(dot)
        self.set_child(box)
        self.search_text = f"{title} {subtitle} {site.get('folder', '') if site else ''}".lower()


class SiteDialog(Adw.Dialog):
    def __init__(self, window, site: dict | None = None, *, select_new=False):
        super().__init__(title="Sites", content_width=920, content_height=660)
        self.set_size_request(360, 420)
        self.window = window
        self.store = window.sites
        self.current: dict = new_site()
        self.is_new = True
        self._loading = False
        self._last_proto = "sftp"

        split = Adw.NavigationSplitView(min_sidebar_width=250, max_sidebar_width=320,
                                        sidebar_width_fraction=0.32)
        split.set_sidebar(self._build_sidebar())
        split.set_content(self._build_form())
        self.split = split
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        breakpoint.add_setter(split, "collapsed", True)
        self.add_breakpoint(breakpoint)
        self.set_child(split)

        self._fill_sites()
        if site is not None and self.store.get(site.get("id", "")) is not None:
            self._select_site(site["id"])
        elif site is not None:
            self.load_site(site, new=True)
        elif not select_new and self.store.sites:
            first = self.store.sorted()[0]
            self._select_site(first["id"])
        else:
            self.listbox.select_row(self.listbox.get_row_at_index(0))

    # ------------------------------------------------------------------ sidebar
    def _build_sidebar(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Sites"))
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New site")
        add.connect("clicked", lambda *_a: self.listbox.select_row(self.listbox.get_row_at_index(0)))
        header.pack_start(add)
        menu = Gio.Menu()
        menu.append("Import from WinSCP…", "sites.import")
        menu.append("Duplicate Site", "sites.duplicate")
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="More")
        header.pack_end(more)
        group = Gio.SimpleActionGroup()
        imp = Gio.SimpleAction.new("import", None)
        imp.connect("activate", lambda *_a: self.window.import_winscp(self))
        group.add_action(imp)
        dup = Gio.SimpleAction.new("duplicate", None)
        dup.connect("activate", lambda *_a: self._duplicate())
        group.add_action(dup)
        self.insert_action_group("sites", group)
        view.add_top_bar(header)

        self.search = Gtk.SearchEntry(placeholder_text="Search sites")
        self.search.set_margin_start(8)
        self.search.set_margin_end(8)
        self.search.set_margin_bottom(6)
        self.search.connect("search-changed", lambda *_a: self.listbox.invalidate_filter())
        view.add_top_bar(self.search)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.set_filter_func(self._filter_row)
        self.listbox.set_header_func(self._row_header)
        self.listbox.connect("row-selected", self._on_row_selected)
        self.listbox.connect("row-activated", lambda *_a: self.split.set_show_content(True))
        view.set_content(Gtk.ScrolledWindow(child=self.listbox, vexpand=True,
                                            hscrollbar_policy=Gtk.PolicyType.NEVER))
        return Adw.NavigationPage(title="Sites", child=view)

    def _filter_row(self, row):
        text = self.search.get_text().strip().lower()
        return row.site is None or not text or text in row.search_text

    def _row_header(self, row, before):
        folder = row.site.get("folder", "") if row.site else ""
        prev = before.site.get("folder", "") if before is not None and before.site else None
        if row.site is not None and folder and folder != prev:
            label = Gtk.Label(label=folder, xalign=0)
            label.add_css_class("heading")
            label.add_css_class("dim-label")
            label.set_margin_top(12)
            label.set_margin_start(10)
            label.set_margin_bottom(4)
            row.set_header(label)
        else:
            row.set_header(None)

    def _fill_sites(self, select_id: str | None = None):
        self.listbox.remove_all()
        self.listbox.append(SiteRow(None))
        for site in self.store.sorted():
            row = SiteRow(site)
            self.listbox.append(row)
        if select_id:
            self._select_site(select_id)

    def _select_site(self, site_id):
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if row.site is not None and row.site["id"] == site_id:
                self.listbox.select_row(row)
                return
            i += 1

    def _on_row_selected(self, _lb, row):
        if row is None:
            return
        if row.site is None:
            self.load_site(new_site(), new=True)
        else:
            self.load_site(row.site, new=False)

    # ------------------------------------------------------------------ form
    def _build_form(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.form_title = Adw.WindowTitle(title="New Site")
        header.set_title_widget(self.form_title)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()

        # session ------------------------------------------------------------
        session = Adw.PreferencesGroup(title="Session")
        self.protocol_row = Adw.ComboRow(title="File protocol",
                                         model=Gtk.StringList.new([PROTOCOLS[p]["label"] for p in PROTOCOL_ORDER]))
        self.protocol_row.connect("notify::selected", self._on_protocol_changed)
        session.add(self.protocol_row)
        self.encryption_row = Adw.ComboRow(title="Encryption", model=Gtk.StringList.new(FTP_ENCRYPTION_LABELS))
        self.encryption_row.connect("notify::selected", self._on_encryption_changed)
        session.add(self.encryption_row)
        self.tls_row = Adw.SwitchRow(title="Secure connection", subtitle="Use HTTPS (TLS)")
        self.tls_row.connect("notify::active", self._on_encryption_changed)
        session.add(self.tls_row)
        self.host_row = Adw.EntryRow(title="Host name")
        self.host_row.connect("changed", self._on_host_changed)
        self.host_row.connect("entry-activated", lambda *_a: self._login())
        session.add(self.host_row)
        self.port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.port_row.set_title("Port number")
        session.add(self.port_row)
        page.add(session)

        login = Adw.PreferencesGroup(title="Login")
        self.user_row = Adw.EntryRow(title="User name")
        self.user_row.connect("entry-activated", lambda *_a: self._login())
        login.add(self.user_row)
        self.password_row = Adw.PasswordEntryRow(title="Password")
        self.password_row.connect("entry-activated", lambda *_a: self._login())
        login.add(self.password_row)
        self.save_pw_row = Adw.SwitchRow(title="Remember password",
                                         subtitle=f"Stored in the {secrets.backend_name}" if secrets.available
                                         else "No keyring available: asked on every login")
        self.save_pw_row.set_sensitive(secrets.available)
        login.add(self.save_pw_row)
        self.auth_row = Adw.ComboRow(title="Authentication", model=Gtk.StringList.new(AUTH_LABELS))
        self.auth_row.connect("notify::selected", lambda *_a: self._update_visibility())
        login.add(self.auth_row)
        self.key_row = Adw.ActionRow(title="Private key file", subtitle="None")
        self.key_row.set_subtitle_selectable(True)
        pick_key = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Choose key file")
        pick_key.add_css_class("flat")
        pick_key.connect("clicked", lambda *_a: self._choose_file(self._set_key, folder=False))
        clear_key = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Clear")
        clear_key.add_css_class("flat")
        clear_key.connect("clicked", lambda *_a: self._set_key(""))
        self.key_row.add_suffix(pick_key)
        self.key_row.add_suffix(clear_key)
        login.add(self.key_row)
        page.add(login)

        dirs = Adw.PreferencesGroup(title="Directories")
        self.remote_dir_row = Adw.EntryRow(title="Remote directory")
        dirs.add(self.remote_dir_row)
        self.local_dir_row = Adw.ActionRow(title="Local directory", subtitle="Last used")
        pick_dir = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Choose folder")
        pick_dir.add_css_class("flat")
        pick_dir.connect("clicked", lambda *_a: self._choose_file(self._set_local_dir, folder=True))
        clear_dir = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Clear")
        clear_dir.add_css_class("flat")
        clear_dir.connect("clicked", lambda *_a: self._set_local_dir(""))
        self.local_dir_row.add_suffix(pick_dir)
        self.local_dir_row.add_suffix(clear_dir)
        dirs.add(self.local_dir_row)
        page.add(dirs)

        meta = Adw.PreferencesGroup(title="Site")
        self.name_row = Adw.EntryRow(title="Site name")
        self.name_row.connect("changed", lambda *_a: self._update_title())
        meta.add(self.name_row)
        self.folder_row = Adw.EntryRow(title="Folder")
        meta.add(self.folder_row)
        self.color_row = Adw.ActionRow(title="Color", subtitle="Shown on the session tab")
        color_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, valign=Gtk.Align.CENTER)
        self.color_buttons = {}
        first = None
        for value, label in SITE_COLORS:
            btn = Gtk.ToggleButton(tooltip_text=label)
            btn.add_css_class("flat")
            btn.add_css_class("circular")
            btn.add_css_class("color-choice")
            if value:
                btn.set_child(Gtk.Image.new_from_gicon(color_dot_icon(value, 16)))
            else:
                btn.set_icon_name("action-unavailable-symbolic")
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            self.color_buttons[value] = btn
            color_box.append(btn)
        self.color_row.add_suffix(color_box)
        meta.add(self.color_row)
        self.notes_row = Adw.EntryRow(title="Notes")
        meta.add(self.notes_row)
        page.add(meta)

        adv = Adw.PreferencesGroup(title="Connection")

        self.proxy_expander = Adw.ExpanderRow(title="Proxy", subtitle="None")
        self.proxy_type_row = Adw.ComboRow(title="Proxy type", model=Gtk.StringList.new(list(PROXY_LABELS)))
        self.proxy_type_row.connect("notify::selected", self._on_proxy_type_changed)
        self.proxy_expander.add_row(self.proxy_type_row)
        self.proxy_host_row = Adw.EntryRow(title="Proxy host")
        self.proxy_host_row.connect("changed", lambda *_a: self._update_proxy_summary())
        self.proxy_expander.add_row(self.proxy_host_row)
        self.proxy_port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.proxy_port_row.set_title("Proxy port")
        self.proxy_port_row.connect("notify::value", lambda *_a: self._update_proxy_summary())
        self.proxy_expander.add_row(self.proxy_port_row)
        self.proxy_user_row = Adw.EntryRow(title="Proxy user name (if it needs a login)")
        self.proxy_expander.add_row(self.proxy_user_row)
        self.proxy_password_row = Adw.PasswordEntryRow(title="Proxy password")
        self.proxy_expander.add_row(self.proxy_password_row)
        adv.add(self.proxy_expander)

        self.jump_expander = Adw.ExpanderRow(title="Jump hosts", subtitle="None: connect directly")
        self.jump_row = Adw.EntryRow(title="Through these SSH servers, in this order")
        self.jump_row.connect("changed", lambda *_a: self._update_jump_summary())
        self.jump_menu = Gtk.MenuButton(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                                        tooltip_text="Add a saved site")
        self.jump_menu.add_css_class("flat")
        self.jump_popover = Gtk.Popover()
        self.jump_popover.add_css_class("menu")
        self.jump_popover.connect("show", lambda *_a: self._fill_jump_menu())
        self.jump_menu.set_popover(self.jump_popover)
        self.jump_row.add_suffix(self.jump_menu)
        self.jump_expander.add_row(self.jump_row)
        hint = Adw.ActionRow(subtitle="Saved sites (each logs in with its own settings) or user@host:port, "
                                      "separated by commas, like ssh -J. A proxy is used to reach the first.")
        hint.set_subtitle_lines(3)
        hint.add_css_class("property")
        self.jump_expander.add_row(hint)
        adv.add(self.jump_expander)

        expander = Adw.ExpanderRow(title="Advanced settings", subtitle="Timeouts, keepalive, compression")
        self.timeout_row = Adw.SpinRow.new_with_range(3, 300, 1)
        self.timeout_row.set_title("Timeout (seconds)")
        expander.add_row(self.timeout_row)
        self.keepalive_row = Adw.SpinRow.new_with_range(0, 3600, 5)
        self.keepalive_row.set_title("Keepalive interval (seconds, 0 = off)")
        expander.add_row(self.keepalive_row)
        self.compression_row = Adw.SwitchRow(title="Compression", subtitle="Useful on slow links")
        expander.add_row(self.compression_row)
        self.passive_row = Adw.SwitchRow(title="Passive mode", subtitle="Recommended behind NAT/firewalls")
        expander.add_row(self.passive_row)
        adv.add(expander)
        page.add(adv)

        view.set_content(page)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.add_css_class("dialog-action-area")
        bottom.set_margin_top(10)
        bottom.set_margin_bottom(10)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        self.delete_btn = Gtk.Button(label="_Delete", use_underline=True)
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.add_css_class("flat")
        self.delete_btn.connect("clicked", lambda *_a: self._delete())
        bottom.append(self.delete_btn)
        bottom.append(Gtk.Box(hexpand=True))
        self.save_btn = Gtk.Button(label="_Save", use_underline=True)
        self.save_btn.connect("clicked", lambda *_a: self._save())
        bottom.append(self.save_btn)
        self.login_btn = Gtk.Button(label="_Log In", use_underline=True)
        self.login_btn.add_css_class("suggested-action")
        self.login_btn.connect("clicked", lambda *_a: self._login())
        bottom.append(self.login_btn)
        view.add_bottom_bar(bottom)
        self.set_default_widget(self.login_btn)
        self.page = page
        return Adw.NavigationPage(title="Session", child=view)

    # ------------------------------------------------------------------ load/save
    def load_site(self, site: dict, new: bool):
        self._loading = True
        self.current = copy.deepcopy(site)
        self.is_new = new
        proto = site.get("protocol", "sftp")
        self._last_proto = proto
        self.protocol_row.set_selected(PROTOCOL_ORDER.index(proto) if proto in PROTOCOL_ORDER else 0)
        enc = site.get("ftp_encryption", "none")
        self.encryption_row.set_selected(FTP_ENCRYPTION.index(enc) if enc in FTP_ENCRYPTION else 0)
        self.tls_row.set_active(bool(site.get("tls")))
        self.host_row.set_text(site.get("host", ""))
        self.port_row.set_value(int(site.get("port") or default_port(site)))
        self.user_row.set_text(site.get("user", ""))
        password = site.get("_password") or ""
        if not password and not new and site.get("save_password"):
            password = secrets.lookup(site["id"]) or ""
        self.password_row.set_text(password)
        self.save_pw_row.set_active(bool(site.get("save_password", True)) and secrets.available)
        auth = site.get("auth", "auto")
        self.auth_row.set_selected(AUTH_MODES.index(auth) if auth in AUTH_MODES else 0)
        self._set_key(site.get("key_file", ""))
        self.remote_dir_row.set_text(site.get("remote_dir", ""))
        self._set_local_dir(site.get("local_dir", ""))
        self.name_row.set_text(site.get("name", ""))
        self.folder_row.set_text(site.get("folder", ""))
        color = site.get("color", "")
        (self.color_buttons.get(color) or self.color_buttons[""]).set_active(True)
        self.notes_row.set_text(site.get("notes", ""))
        self.timeout_row.set_value(int(site.get("timeout") or 15))
        self.keepalive_row.set_value(int(site.get("keepalive") or 0))
        self.compression_row.set_active(bool(site.get("compression")))
        self.passive_row.set_active(bool(site.get("ftp_passive", True)))
        kind = site.get("proxy_type") or "none"
        self.proxy_type_row.set_selected(PROXY_TYPES.index(kind) if kind in PROXY_TYPES else 0)
        self.proxy_host_row.set_text(site.get("proxy_host", ""))
        self.proxy_port_row.set_value(int(site.get("proxy_port") or DEFAULT_PROXY_PORTS.get(kind, 1080)))
        self.proxy_user_row.set_text(site.get("proxy_user", ""))
        proxy_password = site.get("_proxy_password") or ""
        if not proxy_password and not new and site.get("proxy_user"):
            proxy_password = secrets.lookup(proxy_secret_id(site["id"])) or ""
        self.proxy_password_row.set_text(proxy_password)
        self.jump_row.set_text(site.get("jump_hosts", ""))
        self._update_proxy_summary()
        self._update_jump_summary()
        self.delete_btn.set_visible(not new)
        self._loading = False
        self._update_visibility()
        self._update_title()
        GLib.idle_add(lambda: (self.host_row.grab_focus() if new else None, False)[1])

    def read_form(self) -> dict:
        site = copy.deepcopy(self.current)
        site.pop("_jumps", None)            # (a session's resolved jump hosts, with their passwords)
        site["protocol"] = PROTOCOL_ORDER[self.protocol_row.get_selected()]
        site["ftp_encryption"] = FTP_ENCRYPTION[self.encryption_row.get_selected()]
        site["tls"] = self.tls_row.get_active()
        host = self.host_row.get_text().strip()
        if "@" in host and not self.user_row.get_text().strip():
            user, host = host.rsplit("@", 1)
            site["user"] = user
        else:
            site["user"] = self.user_row.get_text().strip()
        site["host"] = host
        site["port"] = int(self.port_row.get_value())
        site["save_password"] = self.save_pw_row.get_active()
        site["auth"] = AUTH_MODES[self.auth_row.get_selected()]
        site["key_file"] = self._key_file
        site["remote_dir"] = self.remote_dir_row.get_text().strip()
        site["local_dir"] = self._local_dir
        site["name"] = self.name_row.get_text().strip()
        site["folder"] = self.folder_row.get_text().strip()
        site["color"] = next((v for v, b in self.color_buttons.items() if b.get_active()), "")
        site["notes"] = self.notes_row.get_text().strip()
        site["timeout"] = int(self.timeout_row.get_value())
        site["keepalive"] = int(self.keepalive_row.get_value())
        site["compression"] = self.compression_row.get_active()
        site["ftp_passive"] = self.passive_row.get_active()
        site["_password"] = self.password_row.get_text() or None
        site["proxy_type"] = PROXY_TYPES[self.proxy_type_row.get_selected()]
        site["proxy_host"] = self.proxy_host_row.get_text().strip()
        site["proxy_port"] = int(self.proxy_port_row.get_value())
        site["proxy_user"] = self.proxy_user_row.get_text().strip()
        site["_proxy_password"] = self.proxy_password_row.get_text() or None
        hops = [h.strip() for h in self.jump_row.get_text().split(",") if h.strip()]
        site["jump_hosts"] = ", ".join(hops) if site["protocol"] in SSH_PROTOCOLS else ""
        return site

    def _persist(self, site: dict) -> dict:
        password = site.get("_password")
        if not site["name"]:
            site["name"] = site_title({**site, "name": ""})
        saved = self.store.upsert(site)
        if site["save_password"] and password:
            secrets.store(saved["id"], saved["name"], password)
        elif not site["save_password"]:
            secrets.clear(saved["id"])
        proxy_password = site.get("_proxy_password")
        if site["proxy_type"] != "none" and site["proxy_user"] and proxy_password:
            secrets.store(proxy_secret_id(saved["id"]), f"{saved['name']} (proxy)", proxy_password)
        else:
            secrets.clear(proxy_secret_id(saved["id"]))
        self.is_new = False
        self.current = copy.deepcopy(saved)
        self.window.sites_changed()
        return saved

    def _save(self):
        site = self.read_form()
        if not site["host"]:
            self.window.toast("Enter a host name first")
            self.host_row.grab_focus()
            return
        saved = self._persist(site)
        self._fill_sites(saved["id"])
        self.window.toast(f"Saved “{saved['name']}”")

    def _login(self):
        site = self.read_form()
        if not site["host"]:
            self.host_row.add_css_class("error")
            self.host_row.grab_focus()
            return
        # Logging in saves the site, new or changed, as it is in the form
        # (the password only goes to the keyring if "save password" is on).
        password, proxy_password = site.get("_password"), site.get("_proxy_password")
        site = copy.deepcopy(self._persist(site))
        site["_password"] = password
        site["_proxy_password"] = proxy_password
        self.close()
        self.window.open_session(site)

    def _delete(self):
        if self.is_new:
            return
        site = self.current

        def response(rid):
            if rid == "delete":
                self.store.delete(site["id"])
                self.window.sites_changed()
                self._fill_sites()
                self.listbox.select_row(self.listbox.get_row_at_index(0))
        alert(self, f"Delete “{site_title(site)}”?", "Its saved password is removed too.",
              responses=(("cancel", "_Cancel"), ("delete", "_Delete")), default="cancel",
              close="cancel", destructive="delete", callback=response)

    def _duplicate(self):
        site = self.read_form()
        dup = new_site(**{k: v for k, v in site.items() if k not in ("id",)})
        dup["id"] = new_site()["id"]
        dup["name"] = (site.get("name") or site_title(site)) + " (copy)"
        self.listbox.unselect_all()
        self.load_site(dup, new=True)

    def refresh_sites(self):
        self._fill_sites(self.current.get("id") if not self.is_new else None)

    # ------------------------------------------------------------------ widgets
    def _on_protocol_changed(self, *_a):
        if self._loading:
            return
        proto = PROTOCOL_ORDER[self.protocol_row.get_selected()]
        old_default = default_port({"protocol": self._last_proto,
                                    "ftp_encryption": FTP_ENCRYPTION[self.encryption_row.get_selected()],
                                    "tls": self.tls_row.get_active()})
        if int(self.port_row.get_value()) == old_default:
            self.port_row.set_value(default_port({
                "protocol": proto, "ftp_encryption": FTP_ENCRYPTION[self.encryption_row.get_selected()],
                "tls": self.tls_row.get_active()}))
        self._last_proto = proto
        self._update_visibility()
        self._update_title()

    def _on_encryption_changed(self, *_a):
        if self._loading:
            return
        proto = PROTOCOL_ORDER[self.protocol_row.get_selected()]
        port = int(self.port_row.get_value())
        if proto == "ftp" and port in (21, 990):
            self.port_row.set_value(990 if FTP_ENCRYPTION[self.encryption_row.get_selected()] == "implicit" else 21)
        elif proto == "webdav" and port in (80, 443):
            self.port_row.set_value(443 if self.tls_row.get_active() else 80)

    def _on_host_changed(self, *_a):
        self.host_row.remove_css_class("error")
        self._update_title()

    def _update_visibility(self):
        proto = PROTOCOL_ORDER[self.protocol_row.get_selected()]
        ssh = proto in SSH_PROTOCOLS
        self.encryption_row.set_visible(proto == "ftp")
        self.tls_row.set_visible(proto == "webdav")
        self.auth_row.set_visible(ssh)
        auth = AUTH_MODES[self.auth_row.get_selected()]
        self.key_row.set_visible(ssh and auth in ("auto", "key"))
        self.compression_row.set_visible(ssh)
        self.passive_row.set_visible(proto == "ftp")
        self.password_row.set_visible(not ssh or auth in ("auto", "password"))
        self.jump_expander.set_visible(ssh)

    def _on_proxy_type_changed(self, *_a):
        kind = PROXY_TYPES[self.proxy_type_row.get_selected()]
        if not self._loading and int(self.proxy_port_row.get_value()) in DEFAULT_PROXY_PORTS.values():
            self.proxy_port_row.set_value(DEFAULT_PROXY_PORTS.get(kind, 1080))
        self._update_proxy_summary()

    def _update_proxy_summary(self):
        kind = PROXY_TYPES[self.proxy_type_row.get_selected()]
        for row in (self.proxy_host_row, self.proxy_port_row, self.proxy_user_row, self.proxy_password_row):
            row.set_sensitive(kind != "none")
        host = self.proxy_host_row.get_text().strip()
        if kind == "none":
            text = "None"
        else:
            label = PROXY_LABELS[PROXY_TYPES.index(kind)].split(" ")[0]
            text = f"{label} · {host}:{int(self.proxy_port_row.get_value())}" if host else f"{label}: no host yet"
        self.proxy_expander.set_subtitle(GLib.markup_escape_text(text))

    def _update_jump_summary(self):
        hops = [h.strip() for h in self.jump_row.get_text().split(",") if h.strip()]
        self.jump_expander.set_subtitle(GLib.markup_escape_text(
            "Through " + " → ".join(hops) if hops else "None: connect directly"))

    def _fill_jump_menu(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        own = self.current.get("id") if self.current else None
        candidates = [s for s in self.store.sorted()
                      if s.get("protocol") in SSH_PROTOCOLS and s.get("id") != own and s.get("name")]
        if not candidates:
            box.append(Gtk.Label(label="No saved SFTP/SCP sites yet", margin_top=6, margin_bottom=6,
                                 margin_start=10, margin_end=10, css_classes=["dim-label"]))
        for site in candidates:
            button = Gtk.Button(label=site["name"], tooltip_text=site_subtitle(site))
            button.add_css_class("flat")
            button.get_child().set_xalign(0)
            button.connect("clicked", lambda _b, name=site["name"]: self._add_jump(name))
            box.append(button)
        self.jump_popover.set_child(box)

    def _add_jump(self, name):
        self.jump_popover.popdown()
        hops = [h.strip() for h in self.jump_row.get_text().split(",") if h.strip()]
        hops.append(name)
        self.jump_row.set_text(", ".join(hops))

    def _update_title(self):
        name = self.name_row.get_text().strip()
        host = self.host_row.get_text().strip()
        if name:
            self.form_title.set_title(name)
        elif host:
            self.form_title.set_title(host)
        else:
            self.form_title.set_title("New Site")
        self.form_title.set_subtitle("Not saved" if self.is_new else "")

    def _set_key(self, path):
        self._key_file = path or ""
        self.key_row.set_subtitle(GLib.markup_escape_text(path) if path else "Default keys from ~/.ssh")

    def _set_local_dir(self, path):
        self._local_dir = path or ""
        self.local_dir_row.set_subtitle(GLib.markup_escape_text(path) if path else "Last used")

    def _choose_file(self, setter, folder: bool):
        dialog = Gtk.FileDialog(title="Choose Folder" if folder else "Choose Private Key", modal=True)
        start = os.path.expanduser("~/.ssh") if not folder else GLib.get_home_dir()
        if os.path.isdir(start):
            dialog.set_initial_folder(Gio.File.new_for_path(start))

        def done(dlg, result):
            try:
                f = dlg.select_folder_finish(result) if folder else dlg.open_finish(result)
            except GLib.Error:
                return
            if f is not None and f.get_path():
                setter(f.get_path())
        if folder:
            dialog.select_folder(self.window, None, done)
        else:
            dialog.open(self.window, None, done)
