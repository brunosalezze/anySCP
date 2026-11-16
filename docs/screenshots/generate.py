#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Make the screenshots in docs/screenshots again.

    python3 docs/screenshots/generate.py [--keep]

Runs anySCP from this source tree with the Linux runtime of the AppImage
build (build/appimage/env, see packaging/setup-build-deps.sh) under Xvfb.
The server is the test SFTP server (tests/gui/sftp_server.py) holding a
made-up web site.  Nothing of the machine or the person running this shows:
the user and host names belong to the demo ("acme.com" is an ssh_config
alias of the local test server), and the demo's home folder is shown as
/home/deploy (a private mount namespace: unshare, no root needed; without
it the work folder's real path shows).  Needs Xvfb.

Work folder: build/screenshots (deleted afterwards unless --keep).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORK = ROOT / "build" / "screenshots"
ENV = ROOT / "build" / "appimage" / "env"
USER, PASSWORD = "deploy", "demo password"
DEMO_HOME = f"/home/{USER}"
SITE_DIR = "/var/www/acme.com"
SHOTS = ("welcome", "commander", "dark", "sites", "sync", "editor", "terminal")


# ============================================================ the demo (runs with python3)

def demo_files(today: datetime) -> tuple[dict, dict]:
    """(local files, server files): relative path -> (bytes, mtime)."""
    t = today.replace(hour=9, minute=12, second=0, microsecond=0).timestamp()
    old = (today - timedelta(days=74)).replace(hour=10, minute=14).timestamp()
    night = (today - timedelta(days=1)).replace(hour=23, minute=59).timestamp()
    page = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title} – ACME Anvils</title>
  <link rel="stylesheet" href="assets/css/site.css">
</head>
<body>
  <header class="site-header">
    <a class="logo" href="/">ACME Anvils</a>
    <nav>
      <a href="/about.html">About</a>
      <a href="/blog/">Blog</a>
      <a href="/contact.html">Contact</a>
    </nav>
  </header>
  <main>
    <h1>{title}</h1>
    <p>{text}</p>
  </main>
  <footer>&copy; 2026 ACME Anvils Ltd.</footer>
</body>
</html>
"""
    pdf = b"%PDF-1.7\n" + bytes(range(256)) * 2560
    local = {
        "index.html": (page.format(title="Anvils for every job",
                                   text="Drop-forged, guaranteed for 100 years.").encode(), t),
        "about.html": (page.format(title="About us", text="Family-run since 1949.").encode(), t),
        "contact.html": (page.format(title="Contact", text="Call us, or drop by the forge.").encode(), t),
        "CHANGELOG.md": (b"# Changes\n\n- New product pages\n- Faster images\n", t),
        "Makefile": (b"deploy:\n\tanyscp-sync . acme.com:/var/www/acme.com\n", t),
        "brochure.pdf": (pdf, t),
        "assets/css/site.css": (b"body { font-family: sans-serif; margin: 0 }\n", t),
        "assets/img/logo.svg": (b"<svg xmlns='http://www.w3.org/2000/svg'/>\n", t),
        "blog/index.html": (page.format(title="Blog", text="News from the forge.").encode(), t),
        "logs/build.log": (b"build ok\n", t),
    }
    server = {
        "index.html": (page.format(title="Anvils for every job",
                                   text="Drop-forged, guaranteed for 50 years.").encode(), old),
        "brochure.pdf": (pdf, t),
        "nginx.conf": (b"server {\n    listen 443 ssl;\n    server_name acme.com;\n    root /var/www/acme.com/html;\n}\n",
                       old),
        "backup-2026-11-14.tar.gz": (os.urandom(160 << 20), night),
        ".env": (b"APP_ENV=production\n", old),
        "assets/css/site.css": (b"body { font-family: serif }\n", old),
        "cgi-bin/contact.cgi": (b"#!/bin/sh\necho ok\n", old),
        "html/index.html": (b"<!-- served -->\n", old),
        "logs/access.log": (b"127.0.0.1 - - GET /\n" * 2000, t),
        "logs/error.log": (b"no errors\n" * 400, t),
    }
    return local, server


def write_tree(root: Path, files: dict):
    for rel, (data, mtime) in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
    for folder in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        mtime = max(c.stat().st_mtime for c in folder.iterdir()) if any(folder.iterdir()) else time.time()
        os.utime(folder, (mtime, mtime))


def ls_l(folder: Path) -> str:
    """`ls -l --color` of a server folder, as the demo user would see it."""
    rows, total = [], 0
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if path.name.startswith("."):
            continue
        st = path.stat()
        total += (st.st_size + 4095) // 4096 * 4
        when = datetime.fromtimestamp(st.st_mtime)
        stamp = when.strftime("%b %e %H:%M") if (datetime.now() - when).days < 180 else when.strftime("%b %e  %Y")
        mode = stat.filemode(st.st_mode)
        group = "www-data" if path.is_dir() else USER
        name = path.name
        if path.is_dir():
            name = f"\033[01;34m{name}\033[0m"
        elif name.endswith((".gz", ".tar", ".zip")):
            name = f"\033[01;31m{name}\033[0m"
        rows.append(f"{mode} {2 if path.is_dir() else 1} {USER} {group:8} {st.st_size:>9} {stamp} {name}")
    prompt = f"\033[01;32m{USER}@acme\033[0m:\033[01;34m{{}}\033[0m$ "
    return (prompt.format("~") + f"cd {SITE_DIR}\n" + prompt.format(SITE_DIR) + "ls -l\n"
            + f"total {total}\n" + "\n".join(rows) + "\n" + prompt.format(SITE_DIR))


def sites(port: int) -> list[dict]:
    web = {"protocol": "sftp", "port": 22, "user": USER, "auth": "password", "save_password": True,
           "folder": "ACME", "local_dir": "~/projects/acme-site"}
    return [
        dict(web, id="demo-production", name="acme.com production", host="acme.com", color="#2ec27e",
             remote_dir=SITE_DIR),
        dict(web, id="demo-staging", name="staging", host="staging.acme.com", color="#f6d32d",
             remote_dir="/var/www/staging"),
        {"id": "demo-nextcloud", "name": "Nextcloud", "protocol": "webdav", "tls": True, "host": "cloud.example.org",
         "port": 443, "user": "me", "remote_dir": "/remote.php/dav/files/me", "color": "#3584e4",
         "save_password": True},
        {"id": "demo-pi", "name": "Raspberry Pi", "protocol": "scp", "host": "raspberrypi.local", "user": "pi",
         "port": 22, "folder": "Home"},
        {"id": "demo-nas", "name": "NAS through the bastion", "protocol": "sftp", "host": "nas.internal",
         "user": "admin", "port": 22, "folder": "Home", "jump_hosts": "me@bastion.example.org"},
        {"id": "demo-ftp", "name": "Company FTP", "protocol": "ftp", "host": "ftp.example.com",
         "user": "deploy", "port": 21, "ftp_encryption": "explicit", "folder": "Work"},
    ]


# Run in a private mount namespace in which the demo's home is /home/deploy:
# a fresh /home holding the real home folders again (bound from the old
# /home, still the working directory) and the demo's.
NAMESPACE = """
cd /home && mount -t tmpfs tmpfs /home || exit 97
for d in * .[!.]*; do
    [ -d "$d" ] || continue
    mkdir -p "/home/$d" && mount --no-canonicalize --rbind "$d" "/home/$d" || exit 97
done
mkdir -p "$2" && mount --bind "$1" "$2" || exit 97
cd "$3" && shift 3 && exec "$@"
"""


def private_home_works() -> bool:
    if not shutil.which("unshare"):
        return False
    probe = subprocess.run(["unshare", "-Urm", "sh", "-c", "mount -t tmpfs tmpfs /home"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe.returncode == 0


def free_display() -> str:
    for n in range(180, 220):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
            return f":{n}"
    raise SystemExit("no free X display number")


def main():
    keep = "--keep" in sys.argv
    if not (ENV / "bin" / "python3").exists():
        raise SystemExit(f"{ENV} is missing: run packaging/setup-build-deps.sh first")
    if not shutil.which("Xvfb"):
        raise SystemExit("Xvfb is needed (apt install xvfb)")
    if WORK.exists():
        shutil.rmtree(WORK)
    home, server, config, cache = (WORK / n for n in ("home", "server", "config", "cache"))
    for d in (home, server, config, cache):
        d.mkdir(parents=True)
    home.chmod(0o700)
    today = datetime.now()
    local_files, server_files = demo_files(today)
    write_tree(home / "projects" / "acme-site", local_files)
    write_tree(server / SITE_DIR.lstrip("/"), server_files)
    write_tree(server / "var" / "www" / "staging", {"index.html": local_files["index.html"]})
    (home / "Downloads").mkdir()

    # the server: password logins, a pretend shell, slow downloads
    info_file = WORK / "server.json"
    server_env = dict(os.environ, ANYSCP_DEMO_SHELL=ls_l(server / SITE_DIR.lstrip("/")),
                      ANYSCP_DEMO_READ_DELAY="0.003")
    srv = subprocess.Popen([sys.executable, str(ROOT / "tests" / "gui" / "sftp_server.py"), str(server), USER,
                            PASSWORD, str(info_file)], env=server_env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    xvfb = None
    try:
        for _ in range(200):
            if info_file.exists():
                break
            time.sleep(0.05)
        info = json.loads(info_file.read_text())
        port = info["port"]
        key = info["known_hosts"].split(None, 1)[1]
        (home / ".ssh").mkdir(mode=0o700)
        (home / ".ssh" / "config").write_text(
            f"Host acme.com staging.acme.com\n    HostName 127.0.0.1\n    Port {port}\n")
        (config / "known_hosts").write_text(f"[127.0.0.1]:{port} {key}\n")
        (config / "sites.json").write_text(json.dumps({"version": 1, "sites": sites(port)}, indent=2))
        (config / "settings.json").write_text(json.dumps({
            "window_width": 1430, "window_height": 860, "window_maximized": False,
            "confirm_transfer": False, "confirm_overwrite": False, "show_hidden": False,
            "parallel_transfers": 2}, indent=2))

        display = free_display()
        xvfb = subprocess.Popen(["Xvfb", display, "-screen", "0", "1920x1200x24", "-nolisten", "tcp"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        private = private_home_works()
        if not private:
            print("unshare -Urm does not work here: the screenshots show the work folder's path")
        app_home = DEMO_HOME if private else str(home)
        env = {
            "HOME": app_home, "USER": USER, "LOGNAME": USER, "PATH": f"{ENV}/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8", "DISPLAY": display, "GDK_BACKEND": "x11", "GTK_A11Y": "none",
            "GDK_DEBUG": "no-portals", "XDG_RUNTIME_DIR": str(home), "TMPDIR": str(cache),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={WORK}/no-bus",
            "PYTHONPATH": str(ROOT), "PYTHONNOUSERSITE": "1",
            "GI_TYPELIB_PATH": f"{ENV}/lib/girepository-1.0",
            "XDG_DATA_DIRS": f"{ENV}/share:/usr/local/share:/usr/share",
            "GSETTINGS_SCHEMA_DIR": f"{ENV}/share/glib-2.0/schemas", "GSETTINGS_BACKEND": "memory",
            "GIO_MODULE_DIR": f"{ENV}/lib/gio/modules",
            "FONTCONFIG_FILE": str(ROOT / "packaging" / "appimage" / "anyscp-fonts.conf"),
            "FONTCONFIG_PATH": "/etc/fonts",
            "XLOCALEDIR": f"{ENV}/share/X11/locale",
            "GDK_PIXBUF_MODULE_FILE": f"{ENV}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache",
            "ANYSCP_CONFIG_DIR": str(config), "ANYSCP_CACHE_DIR": str(cache),
            "ANYSCP_SHOTS": json.dumps({"out": str(WORK / "shots"), "password": PASSWORD, "site_dir": SITE_DIR}),
        }
        (WORK / "shots").mkdir()
        cmd = [str(ENV / "bin" / "python3"), "-s", str(Path(__file__)), "--in-app"]
        if private:
            cmd = ["unshare", "-Urm", "sh", "-c", NAMESPACE, "sh", str(home), DEMO_HOME, str(WORK)] + cmd
        proc = subprocess.run(cmd, env=env, cwd=str(WORK), timeout=600)
        made = sorted(p.stem for p in (WORK / "shots").glob("*.png"))
        missing = [s for s in SHOTS if s not in made]
        for name in made:
            shutil.copy(WORK / "shots" / f"{name}.png", HERE / f"{name}.png")
        print(f"screenshots: {', '.join(made) or 'none'}" + (f"; MISSING: {', '.join(missing)}" if missing else ""))
        if proc.returncode or missing:
            sys.exit(1)
    finally:
        srv.terminate()
        if xvfb:
            xvfb.terminate()
        if not keep:
            shutil.rmtree(WORK, ignore_errors=True)


# ============================================================ inside anySCP (runs with the runtime's Python)

def in_app():
    import traceback

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, Gio, GLib, Graphene, Gtk

    from anyscp.app import AnySCPApplication
    from anyscp.config import secrets

    opts = json.loads(os.environ["ANYSCP_SHOTS"])
    t0 = time.monotonic()

    def say(text):
        print(f"{time.monotonic() - t0:6.1f}s {text}", flush=True)

    def spin(cond, timeout):
        ctx = GLib.MainContext.default()
        end = time.monotonic() + timeout
        tick = GLib.timeout_add(20, lambda: True)
        try:
            while time.monotonic() < end:
                if cond():
                    return True
                ctx.iteration(True)
            return cond()
        finally:
            GLib.source_remove(tick)

    def wait(seconds):
        end = time.monotonic() + seconds
        spin(lambda: time.monotonic() >= end, seconds + 1)

    def shoot(widget, name):
        wait(0.6)                                       # (animations: let them end)
        w, h = widget.get_width(), widget.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(widget).snapshot(snap, w, h)
        node = snap.to_node()
        tex = widget.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
        tex.save_to_png(os.path.join(opts["out"], f"{name}.png"))
        say(f"{name}.png ({w}×{h})")

    def listed(page):
        return page.session.state == "connected" and page.remote_pane is not None \
            and page.remote_pane.entries and page.local_pane.entries

    def entry(pane, name):
        return next(e for e in pane.entries if e.name == name)

    def new_window(app, before):
        spin(lambda: any(w not in before for w in app.get_windows()), 15)
        return next((w for w in app.get_windows() if w not in before), None)

    def run(app):
        win = app.window
        Gtk.Settings.get_default().set_property("gtk-font-name", "Cantarell 11")
        for site in app.sites.sites:
            if site.get("protocol") == "sftp" and site["id"].startswith("demo-") and site["user"] == USER:
                secrets.store(site["id"], site["name"], opts["password"])
        wait(1.0)
        shoot(win, "welcome")

        by_name = {s["name"]: s for s in app.sites.sites}
        win.open_session(dict(by_name["staging"]))
        staging = win.current_page()
        win.open_session(dict(by_name["acme.com production"]))
        page = win.current_page()
        if not spin(lambda: listed(staging) and listed(page), 60):
            raise RuntimeError(f"not connected: {staging.session.state}, {page.session.state} "
                               f"{page.session.error!r}")
        lp, rp = page.local_pane, page.remote_pane

        # the transfer queue: two finished transfers and one still running
        page.start_transfer(rp, [entry(rp, "logs")], lp, lp.path)
        page.start_transfer(lp, [entry(lp, "blog"), entry(lp, "assets")], rp, rp.path)
        spin(lambda: not app.transfers.active, 30)
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        page.start_transfer(rp, [entry(rp, "backup-2026-11-14.tar.gz")], lp, downloads)
        big = app.transfers.jobs[-1]
        spin(lambda: getattr(big, "done_bytes", 0) > (40 << 20) or big.state not in ("queued", "running"), 60)
        lp.select_names({"index.html", "about.html"})
        rp.select_names({"nginx.conf"})
        page.set_active_pane(rp)
        rp.column_view.grab_focus()
        shoot(win, "commander")

        style = Adw.StyleManager.get_default()
        style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        shoot(win, "dark")
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        app.transfers.cancel(big)
        spin(lambda: not app.transfers.active, 20)
        app.transfers.remove(big)

        win.edit_site(by_name["Nextcloud"])
        wait(0.8)
        shoot(win, "sites")
        dialog = win.get_visible_dialog()
        if dialog is not None:
            dialog.force_close()
        wait(0.5)

        from anyscp.ui.sync_dialog import SyncDialog
        sync = SyncDialog(page)
        sync.present(win)
        wait(0.5)
        sync.direction.set_active_name("both")
        sync._compare()
        spin(lambda: sync.nav.get_visible_page() is not None
             and sync.nav.get_visible_page().get_title() == "Preview", 30)
        shoot(win, "sync")
        sync.force_close()
        wait(0.5)

        from anyscp.ui.editor import open_editor
        before = list(app.get_windows())
        open_editor(page, rp, entry(rp, "index.html"))
        editor = new_window(app, before)
        wait(1.0)
        shoot(editor, "editor")
        editor.destroy()

        before = list(app.get_windows())
        page.open_terminal()
        term = new_window(app, before)
        spin(lambda: "total" in (term.term.get_text_format(1) if hasattr(term.term, "get_text_format")
                                 else ""), 10)
        wait(1.0)
        shoot(term, "terminal")
        term.destroy()

    class App(AnySCPApplication):
        def __init__(self):
            super().__init__()
            self.set_flags(Gio.ApplicationFlags.NON_UNIQUE)

    app = App()
    failed = []

    def go():
        try:
            run(app)
        except Exception:  # noqa: BLE001
            failed.append(True)
            say("failed: " + traceback.format_exc())
        for page in app.window.pages():
            page.shutdown()
        app.quit()
        return False

    app.connect("activate", lambda *_a: GLib.timeout_add(800, go))
    app.run([])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    in_app() if "--in-app" in sys.argv else main()
