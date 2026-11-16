# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""``anySCP --self-test``: start the app, check the environment and quit.

Meant for diagnosing problems such as running anySCP from a network share:
it reports where it runs from, where settings go, whether that folder is
writable (also with concurrent writers), whether libraries, icons and fonts
load, and whether the window comes up.  Prints a human readable report and
one machine readable line ``ANYSCP-SELF-TEST {json}``; the exit status is 0
when every check passed.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import threading
import time
import traceback


def _check(results, name, fn):
    t0 = time.monotonic()
    try:
        detail = fn()
        ok = True
    except Exception as exc:  # noqa: BLE001 - report everything
        detail = f"{type(exc).__name__}: {exc}"
        ok = False
    results.append({"name": name, "ok": ok, "detail": "" if detail is None else str(detail),
                    "ms": int((time.monotonic() - t0) * 1000)})


def run_checks(app) -> dict:
    import gi
    from gi.repository import Adw, Gtk, Pango

    from . import __version__, compat
    from .backends.local import LocalFileSystem
    from .config import CACHE_DIR, CONFIG_DIR, PORTABLE, load_json, save_json, secrets

    results: list[dict] = []
    info = {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "executable": sys.executable,
        "prefix": sys.prefix,
        "cwd": os.getcwd(),
        "launcher_dir": os.environ.get("ANYSCP_LAUNCHER_DIR", ""),
        "portable": PORTABLE,
        "config_dir": str(CONFIG_DIR),
        "cache_dir": str(CACHE_DIR),
        "gtk": f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        "libadwaita": f"{Adw.get_major_version()}.{Adw.get_minor_version()}",
        "pygobject": gi.__version__,
        "bundled": compat.is_bundled(),
    }

    pid = os.getpid()

    def settings_write():
        probe = CONFIG_DIR / f"self-test-{pid}.json"
        save_json(probe, {"time": time.time(), "pid": pid})
        data = load_json(probe, None)
        assert data and data["pid"] == pid, "read back a different value"
        os.unlink(probe)
        return str(CONFIG_DIR)

    def concurrent_writes():
        """4 threads hammer a private file and a file shared with any other
        instance running at the same time (e.g. from the same network share).
        Every read must see a complete document, and no temporary file of
        ours may be left behind."""
        private = CONFIG_DIR / f"self-test-{pid}-concurrent.json"
        shared = CONFIG_DIR / "self-test-shared.json"
        errors = []

        def writer(n):
            try:
                for i in range(15):
                    for path in (private, shared):
                        save_json(path, {"pid": pid, "writer": n, "i": i, "pad": "x" * 2000})
                        data = load_json(path, None)
                        if not (isinstance(data, dict) and data.get("pad")):
                            errors.append(f"incomplete read of {path.name}: {data!r}"[:200])
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            os.unlink(private)
        except OSError:
            pass
        # The shared probe stays: another instance may still be using it (it
        # is tiny and simply overwritten by the next self-test).
        leftovers = [p.name for p in CONFIG_DIR.glob(f".self-test-*.json.{pid}.*.tmp")]
        assert not errors, errors[0]
        assert not leftovers, f"temporary files left behind: {leftovers[:3]}"
        return "4 writers × 15 writes, private and shared file"

    def list_dir(path):
        def inner():
            fs = LocalFileSystem()
            entries = fs.listdir(path)
            return f"{path}: {len(entries)} entries"
        return inner

    def launcher_dir():
        d = os.environ.get("ANYSCP_LAUNCHER_DIR")
        if not d:
            return "not started by a launcher (running from source)"
        internal = compat.to_internal(d)
        assert compat.to_native(internal).rstrip("\\/").lower() == os.path.abspath(d).rstrip("\\/").lower() \
            or not compat.IS_WINDOWS, f"path round trip failed: {d} -> {internal}"
        return list_dir(internal)()

    def temp_dir():
        with tempfile.NamedTemporaryFile(prefix="anyscp-selftest-") as fh:
            fh.write(b"ok")
        return tempfile.gettempdir()

    def optional_libs():
        found = []
        for ns, ver in (("GtkSource", "5"), ("Vte", "3.91"), ("Secret", "1")):
            try:
                gi.require_version(ns, ver)
                __import__("gi.repository." + ns)
                found.append(ns)
            except (ValueError, ImportError):
                pass
        return ", ".join(found) or "none"

    def keyring():
        if not secrets.available:
            return f"not available ({secrets.backend_name})"
        sid = f"self-test-{os.getpid()}"
        assert secrets.store(sid, "anySCP self-test", "pässwörd"), "store failed"
        secrets._memory.pop(sid, None)
        value = secrets.lookup(sid)
        secrets.clear(sid)
        assert value == "pässwörd", "read back a different password"
        return secrets.backend_name

    def icons():
        from gi.repository import Gdk
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        missing = [n for n in ("folder", "text-x-generic", "go-up-symbolic", "io.github.anyscp.AnySCP")
                   if not theme.has_icon(n)]
        assert not missing, f"missing icons: {missing}"
        return f"theme {theme.get_theme_name()}"

    def fonts():
        lbl = Gtk.Label(label="Hello")
        ctx = lbl.get_pango_context()
        fontmap = ctx.get_font_map()
        font = fontmap.load_font(ctx, ctx.get_font_description())
        assert font is not None, "the default font could not be loaded"
        families = len(fontmap.list_families())
        assert families > 0, "no font families"
        # The editor and the terminal ask for "Monospace": fontconfig's
        # aliases have to work (on Windows, Pango has its own).
        mono = fontmap.load_font(ctx, Pango.FontDescription.from_string("Monospace 11"))
        family = mono.get_face().get_family() if mono is not None and mono.get_face() else None
        name = family.get_name() if family is not None else "no font"
        if not compat.IS_WINDOWS:
            assert family is not None and family.is_monospace(), \
                f"Monospace is drawn with {name}, which is not a fixed-width font"
        return f"{font.describe().to_string()}, Monospace: {name} ({families} families)"

    def window():
        win = app.get_active_window() or getattr(app, "window", None)
        assert win is not None, "no window"
        assert win.get_mapped(), "window not shown"
        return f"{win.get_width()}×{win.get_height()}"

    _check(results, "settings folder writable", settings_write)
    _check(results, "concurrent settings writes", concurrent_writes)
    _check(results, "home folder listing", list_dir(compat.home_dir()))
    _check(results, "launch folder listing", launcher_dir)
    _check(results, "temporary folder", temp_dir)
    _check(results, "optional libraries", optional_libs)
    _check(results, "password storage", keyring)
    _check(results, "icons", icons)
    _check(results, "fonts", fonts)
    _check(results, "main window", window)
    if compat.IS_WINDOWS:
        _check(results, "drives", lambda: ", ".join(compat.list_drives()))

    return {"ok": all(r["ok"] for r in results), "info": info, "checks": results}


def print_report(report: dict) -> None:
    lines = [f"anySCP {report['info']['version']} self-test: "
             + ("all checks passed" if report["ok"] else "SOME CHECKS FAILED"), ""]
    for key in ("executable", "launcher_dir", "portable", "config_dir", "gtk", "libadwaita", "python"):
        lines.append(f"  {key:<13} {report['info'][key]}")
    lines.append("")
    for r in report["checks"]:
        mark = "OK  " if r["ok"] else "FAIL"
        lines.append(f"  [{mark}] {r['name']}: {r['detail']}  ({r['ms']} ms)")
    text = "\n".join(lines)
    print(text, flush=True)
    print("ANYSCP-SELF-TEST " + json.dumps(report, ensure_ascii=True), flush=True)


def run_and_quit(app) -> bool:
    """GLib timeout callback."""
    try:
        report = run_checks(app)
    except Exception:  # noqa: BLE001
        report = {"ok": False, "info": {}, "checks": [
            {"name": "self-test", "ok": False, "detail": traceback.format_exc(), "ms": 0}]}
        print(report["checks"][0]["detail"], flush=True)
    else:
        print_report(report)
    app.self_test_status = 0 if report["ok"] else 1
    app.quit()
    return False
