# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Formatting helpers and main-thread marshalling."""

from __future__ import annotations

import functools
import mimetypes
import os
import stat as st
import sys
import threading
import time

from gi.repository import Gio, GLib


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------

def call_in_main(fn, *args):
    """Schedule ``fn(*args)`` on the GTK main loop (fire and forget)."""
    def run():
        fn(*args)
        return False
    GLib.idle_add(run, priority=GLib.PRIORITY_DEFAULT)


def ask_main(fn):
    """Block the calling *worker* thread until the main loop answers.

    ``fn(reply)`` runs on the main thread and must eventually call
    ``reply(value)`` (typically from a dialog response handler)."""
    if threading.current_thread() is threading.main_thread():
        raise RuntimeError("ask_main() called from the main thread")
    done = threading.Event()
    box = {}

    def reply(value):
        box["value"] = value
        done.set()

    def run():
        try:
            fn(reply)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            reply(None)
        return False
    GLib.idle_add(run, priority=GLib.PRIORITY_DEFAULT)
    done.wait()
    return box.get("value")


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def format_size(size: int | None, mode: str = "human") -> str:
    if size is None:
        return ""
    if mode == "bytes":
        return f"{size:,}".replace(",", " ")
    return GLib.format_size(size)


def format_speed(bps: float) -> str:
    return GLib.format_size(int(bps)) + "/s"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


def format_time(ts: float | None, mode: str = "short") -> str:
    if not ts:
        return ""
    try:
        lt = time.localtime(ts)
    except (OverflowError, OSError, ValueError):
        return ""
    if mode == "iso":
        return time.strftime("%Y-%m-%d %H:%M:%S", lt)
    now = time.time()
    if mode == "relative":
        diff = now - ts
        if 0 <= diff < 60:
            return "Just now"
        if 0 <= diff < 3600:
            return f"{int(diff // 60)} min ago"
        if 0 <= diff < 86400 and time.localtime(now).tm_yday == lt.tm_yday:
            return time.strftime("%H:%M", lt)
    today = time.localtime(now)
    if lt.tm_year == today.tm_year and lt.tm_yday == today.tm_yday:
        return "Today " + time.strftime("%H:%M", lt)
    if lt.tm_year == today.tm_year:
        return time.strftime("%d %b %H:%M", lt)
    return time.strftime("%d %b %Y", lt)


def format_mode(mode: int | None, is_dir: bool = False, is_link: bool = False) -> str:
    if mode is None:
        return ""
    kind = "l" if is_link else "d" if is_dir else "-"
    return kind + st.filemode(mode | st.S_IFREG)[1:]


def mode_to_octal(mode: int | None) -> str:
    return "" if mode is None else f"{mode & 0o7777:04o}"


# --------------------------------------------------------------------------
# Icons and content types
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=2048)
def content_type_for(name: str) -> str:
    ctype, _uncertain = Gio.content_type_guess(name, None)
    return ctype or "application/octet-stream"


@functools.lru_cache(maxsize=1024)
def icon_for_type(ctype: str) -> Gio.Icon:
    return Gio.content_type_get_icon(ctype)


@functools.lru_cache(maxsize=1024)
def description_for_type(ctype: str) -> str:
    return Gio.content_type_get_description(ctype) or ctype


# Windows' content types are file extensions and macOS' are UTIs, not MIME
# types: there Python's table is used.
_WIN = sys.platform in ("win32", "darwin")
_ARCHIVES = ("zip", "tar", "gzip", "bzip", "xz", "7z", "rar", "zstd", "compressed", "archive")


@functools.lru_cache(maxsize=2048)
def mime_for(name: str) -> str:
    """MIME type from the file name (Python's table on Windows and macOS)."""
    if _WIN:
        return mimetypes.guess_type(name, strict=False)[0] or "application/octet-stream"
    ctype = content_type_for(name)
    return Gio.content_type_get_mime_type(ctype) or ctype


@functools.lru_cache(maxsize=1024)
def _icon_from_mime(mime: str) -> Gio.Icon:
    top = mime.split("/", 1)[0]
    if top in ("text", "image", "audio", "video", "font"):
        generic = f"{top}-x-generic"
    elif any(k in mime for k in _ARCHIVES):
        generic = "package-x-generic"
    elif mime in ("application/x-msdownload", "application/x-executable"):
        generic = "application-x-executable"
    else:
        generic = "application-x-generic"
    return Gio.ThemedIcon.new_from_names([mime.replace("/", "-"), generic, "text-x-generic"])


FOLDER_ICON = Gio.ThemedIcon.new_with_default_fallbacks("folder")
FOLDER_LINK_ICON = Gio.ThemedIcon.new_from_names(["folder-remote", "folder"])
PARENT_ICON = Gio.ThemedIcon.new_from_names(["go-up-symbolic"])


def icon_for(name: str, is_dir: bool, is_link: bool = False) -> Gio.Icon:
    if is_dir:
        return FOLDER_ICON
    if _WIN:
        return _icon_from_mime(mime_for(name))
    return icon_for_type(content_type_for(name))


def type_description(name: str, is_dir: bool, is_link: bool = False) -> str:
    if is_dir:
        return "Folder (link)" if is_link else "Folder"
    desc = description_for_type(content_type_for(name))
    return desc + " (link)" if is_link else desc


TEXT_TYPES_EXTRA = {"application/json", "application/xml", "application/javascript",
                    "application/x-shellscript", "application/x-yaml", "application/toml",
                    "application/x-php", "application/sql", "application/x-desktop",
                    "application/x-perl", "application/x-ruby", "application/x-python"}


def looks_like_text(name: str) -> bool:
    """False only for files whose name maps to a known non-text type
    (images, PDFs, archives, office documents, media).  Unknown types such
    as ``nginx.conf`` or ``.env`` count as text; the editor sniffs the
    content for binary data before showing it."""
    if _WIN:
        mime = mime_for(name)
        return mime == "application/octet-stream" or mime.startswith("text/") or mime in TEXT_TYPES_EXTRA \
            or mime.endswith(("+xml", "+json"))
    ctype = content_type_for(name)
    if Gio.content_type_is_unknown(ctype) or ctype == "application/octet-stream":
        return True
    if Gio.content_type_is_a(ctype, "text/plain") or ctype in TEXT_TYPES_EXTRA:
        return True
    return False


def unique_name(existing: set[str], name: str) -> str:
    if name not in existing:
        return name
    stem, ext = os.path.splitext(name)
    i = 2
    while f"{stem} ({i}){ext}" in existing:
        i += 1
    return f"{stem} ({i}){ext}"


def color_dot_icon(color: str, size: int = 16) -> Gio.Icon:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
           f'viewBox="0 0 16 16"><circle cx="8" cy="8" r="5" fill="{color}"/></svg>')
    return Gio.BytesIcon.new(GLib.Bytes.new(svg.encode()))


SITE_COLORS = [
    ("", "None"),
    ("#3584e4", "Blue"),
    ("#2ec27e", "Green"),
    ("#f6d32d", "Yellow"),
    ("#ff7800", "Orange"),
    ("#e01b24", "Red"),
    ("#9141ac", "Purple"),
    ("#986a44", "Brown"),
]
