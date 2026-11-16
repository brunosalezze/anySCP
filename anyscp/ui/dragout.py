# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Dragging files out of anySCP into another program's folder: Windows
Explorer, GNOME Files, Dolphin, the desktop.

Other programs can only take files that exist on this computer.  Files of
the local pane are handed over as they are.  Files of a server are first
downloaded to a temporary folder (what WinSCP does without its shell
extension), and the other program then moves or copies them from there.
That download starts when the drag leaves the anySCP window, shows in the
transfer queue, and the drop completes once it has finished.

What the other program is given:

* Linux: ``text/uri-list``.
* Windows: a "Shell IDList Array", the format Explorer itself uses.  GTK does
  not make one from a file list (gdk/win32/gdkdrag-win32.c: "not
  implemented"), but it passes ``application/x.windows.<format>`` data on
  unchanged, so the list is built here with the shell's own functions.  On
  Windows GTK advertises only the formats a drag's content provider lists
  itself, so the data comes from a LazyProvider (see lazyprovider.py) rather
  than from a serializer as on Linux.

Drags between anySCP's own panes carry only a marker, PANE_MIME, which every
platform advertises; the panes never read the files of such a drag.

Windows programs ask for the data while the pointer is still moving over
them, and after a drop Explorer is blocked until it gets its answer, for
which GTK allows 30 seconds.  So: while the mouse button is down the answer
is immediate (the files are empty placeholders at that point); after the
drop it waits for the download; and a download that needs longer is refused
with a notice – it carries on, and the files can be dragged out again once
it is done.
"""

from __future__ import annotations

import ntpath
import os
import shutil
import struct
import tempfile
import time
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, GObject

from .. import compat
from ..session import LocalEndpoint, SessionEndpoint
from ..transfer import DONE, TransferJob

SHELL_ID_LIST = "application/x.windows.Shell IDList Array"
URI_LIST = "text/uri-list"
MIME_TYPE = SHELL_ID_LIST if compat.IS_WINDOWS else URI_LIST
PANE_MIME = "application/x-anyscp-pane"     # a drag from one of anySCP's panes

DROP_WAIT_SECONDS = 25          # for one answer after a drop on Windows (GTK gives up after 30)
STALE_SECONDS = 12 * 3600       # temporary folders older than this are leftovers
RECENT_SECONDS = 120            # a program may still be copying out of a folder this fresh

_counter = 0
_downloads: list["RemoteDownload"] = []     # this run's, for cleanup()


# --------------------------------------------------------------------------
# What is handed over
# --------------------------------------------------------------------------

def uri_list(paths: list[str]) -> bytes:
    return "".join(Gio.File.new_for_path(p).get_uri() + "\r\n" for p in paths).encode()


def shell_id_list(paths: list[str]) -> bytes:
    """CFSTR_SHELLIDLIST data (a CIDA structure) for existing files: a count,
    offsets, the item ID list of their folder and one per file, relative to
    that folder.  Files of several folders (or drives themselves) are given
    in full, relative to the desktop."""
    import ctypes

    shell32 = ctypes.windll.shell32
    shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILFindLastID.argtypes = [ctypes.c_void_p]
    shell32.ILFindLastID.restype = ctypes.c_void_p
    shell32.ILGetSize.argtypes = [ctypes.c_void_p]
    shell32.ILGetSize.restype = ctypes.c_uint
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.ILFree.restype = None

    def id_list(path: str, last_only: bool = False) -> bytes:
        pidl = shell32.ILCreateFromPathW(path)
        if not pidl:
            raise OSError(f"Windows cannot find {path}")
        try:
            start = shell32.ILFindLastID(pidl) if last_only else pidl
            return ctypes.string_at(start, shell32.ILGetSize(start))
        finally:
            shell32.ILFree(pidl)

    paths = [ntpath.normpath(p) for p in paths]
    folders = {ntpath.dirname(p) for p in paths}
    if len(folders) == 1 and all(ntpath.dirname(p) != p for p in paths):
        lists = [id_list(folders.pop())] + [id_list(p, last_only=True) for p in paths]
    else:
        lists = [b"\0\0"] + [id_list(p) for p in paths]
    count = len(paths)
    offsets, position = [], 4 * (count + 2)
    for item in lists:
        offsets.append(position)
        position += len(item)
    return struct.pack(f"<{count + 2}I", count, *offsets) + b"".join(lists)


def _dropped() -> bool:
    """Has the mouse button of the drag been released?  (Only Windows can
    tell, and only there it matters.)"""
    if not compat.IS_WINDOWS:
        return True
    import ctypes
    user32 = ctypes.windll.user32
    button = 0x02 if user32.GetSystemMetrics(23) else 0x01      # SM_SWAPBUTTON: VK_RBUTTON, else VK_LBUTTON
    return not user32.GetAsyncKeyState(button) & 0x8000


# --------------------------------------------------------------------------
# Server files: the download into a temporary folder
# --------------------------------------------------------------------------

def temp_root() -> Path:
    return Path(tempfile.gettempdir()) / "anySCP-drag"


class RemoteDownload:
    """Files of a server on their way into a temporary folder, for a drag –
    and for the next drag of the same files."""

    def __init__(self, page, infos):
        global _counter
        _counter += 1
        self.page = page
        self.infos = list(infos)
        self.key = tuple((i.path, i.size, i.mtime, i.is_dir) for i in self.infos)
        self.what = self.infos[0].name if len(self.infos) == 1 else f"{len(self.infos)} items"
        self.dir = temp_root() / f"{os.getpid()}-{_counter}"
        self.paths = [str(self.dir / i.name) for i in self.infos]
        self.state = "idle"             # idle → running → ready | failed
        self.error = ""
        self.job = None
        self.ready_at = 0.0
        self.keep = False               # a drop is waiting for the files, or has them
        self.announce = False           # a drop was refused: say when the files are there
        self._discard = False
        self._waiters: list[tuple] = []
        self._deadline = 0
        _downloads.append(self)

    def usable(self) -> bool:
        """Can another drag of the same files take this download over?"""
        if self.state in ("idle", "running"):
            return not self._discard
        return self.state == "ready" and all(os.path.lexists(p) for p in self.paths)

    def start(self):
        if self.state != "idle" or self._discard:
            return
        if self.page.session.state != "connected":
            self.state, self.error = "failed", "Not connected"
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            for info, path in zip(self.infos, self.paths):
                # Something for the other program to look at meanwhile
                if info.is_dir:
                    os.makedirs(path, exist_ok=True)
                else:
                    open(path, "ab").close()
        except OSError as exc:
            self.state, self.error = "failed", str(exc)
            return
        pairs = [(i, compat.to_internal(p)) for i, p in zip(self.infos, self.paths)]
        self.job = TransferJob(f"Downloading {self.what}", SessionEndpoint(self.page.session),
                               LocalEndpoint(), pairs, settings=self.page.settings,
                               overwrite="overwrite", direction="download",
                               subtitle="to a temporary folder, to be dropped into another program")
        self.job.on_finished = self._finished
        self.state = "running"
        self.page.window.add_transfer(self.job)

    def when_ready(self, ok, failed, dropped: bool):
        """Call ``ok()`` once the files are there, or ``failed(message)``.
        Before the drop (``dropped`` false) the placeholders are enough."""
        self.start()
        if self.state == "ready":
            ok()
        elif self.state == "failed" or self._discard:
            failed(self.error or "Cancelled")
        elif not dropped:
            ok()
        else:
            self.keep = True
            if compat.IS_WINDOWS:
                speed = self.job.sample_speed()
                eta = self.job.eta(speed)
                if eta is not None and eta > DROP_WAIT_SECONDS:
                    self._refuse(failed)
                    return
                if not self._deadline:
                    self._deadline = GLib.timeout_add_seconds(DROP_WAIT_SECONDS, self._on_deadline)
            self._waiters.append((ok, failed))

    def _refuse(self, failed):
        if not self.announce:
            self.announce = True
            self.page.toast(f"{self.what} is still downloading. "
                            "Drag it out again when the transfer has finished.")
        failed("still downloading")

    def _on_deadline(self):
        self._deadline = 0
        waiters, self._waiters = self._waiters, []
        for _ok, failed in waiters:
            self._refuse(failed)
        return False

    def _finished(self, job):
        if self._deadline:
            GLib.source_remove(self._deadline)
            self._deadline = 0
        if job.state == DONE and not self._discard:
            self.state, self.ready_at = "ready", time.time()
        else:
            self.state, self.error = "failed", job.error or "Cancelled"
        waiters, self._waiters = self._waiters, []
        for ok, failed in waiters:
            if self.state == "ready":
                ok()
            else:
                failed(self.error)
        if self.state == "failed":
            if self._discard:
                self.page.window.transfers.remove(job)      # not worth a "Cancelled" line
            self.remove_files()
        elif self.announce:
            self.announce = False
            self.page.toast(f"{self.what} is ready: drag it out again")
        self.page = None                # (keeps no closed session tab alive)

    def drag_finished(self, cancelled: bool):
        """The drag is over.  A download nobody is waiting for is stopped."""
        if self.state == "running":
            if cancelled and self.announce:
                return                  # refused as too slow: it carries on for the next drag
            if cancelled or not self.keep:
                self.abort()

    def abort(self):
        """Dropped inside anySCP after all, or not dropped at all."""
        if self.state == "running" and not self._discard:
            self.page.window.transfers.cancel(self.job)
        self._discard = True

    def remove_files(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------------------------------
# The drag's content for other programs
# --------------------------------------------------------------------------

class DragFiles(GObject.Object):
    """Files a drag offers to other programs: existing ones (``download`` is
    None) or the ones a RemoteDownload is fetching."""

    __gtype_name__ = "AnyscpDragFiles"

    def __init__(self, paths, download: RemoteDownload | None = None):
        super().__init__()
        self.paths = list(paths)
        self.download = download


def _write_all(stream, data: bytes, priority, cancellable, done):
    # (Synchronous writes would block: GDK reads from the same main loop.)
    def step(out, result, chunk):
        try:
            written = out.write_bytes_finish(result)
        except GLib.Error as err:
            done(err)
            return
        rest = chunk.get_data()[written:]
        if rest:
            send(rest)
        else:
            done(None)

    def send(part):
        chunk = GLib.Bytes.new(part)
        stream.write_bytes_async(chunk, priority, cancellable, step, chunk)
    send(data)


def _serialize(serializer):
    files = serializer.get_value()

    def fail(message):
        serializer.return_error(GLib.Error.new_literal(Gio.io_error_quark(), str(message),
                                                       Gio.IOErrorEnum.FAILED))

    def send():
        try:
            if serializer.get_mime_type() == SHELL_ID_LIST:
                data = shell_id_list(files.paths)
            else:
                data = uri_list(files.paths)
        except OSError as exc:
            fail(exc)
            return
        _write_all(serializer.get_output_stream(), data, serializer.get_priority(),
                   serializer.get_cancellable(),
                   lambda err: serializer.return_error(err) if err else serializer.return_success())

    if files.download is None:
        send()
    else:
        files.download.when_ready(send, fail, _dropped())


Gdk.content_register_serializer(DragFiles, MIME_TYPE, _serialize)


def drag_icon_misplaced(display) -> bool:
    """Would GTK draw the drag icon in the wrong place?  On Windows it places it
    with the main monitor's scale and unsigned screen coordinates
    (gdk/win32/gdkdrag-win32.c), so on a monitor with another scale, or left
    of or above the main one, the icon stays behind on the wrong monitor."""
    if not compat.IS_WINDOWS:
        return False
    try:
        import ctypes
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.GetDC.restype = ctypes.c_void_p
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        hdc = user32.GetDC(None)
        dpi = gdi32.GetDeviceCaps(hdc, 88)                  # LOGPIXELSX: the main monitor's
        user32.ReleaseDC(None, hdc)
    except (OSError, AttributeError):
        return False
    main_scale = max(1, dpi // 96)
    monitors = display.get_monitors()
    for i in range(monitors.get_n_items()):
        monitor = monitors.get_item(i)
        geometry = monitor.get_geometry()
        if monitor.get_scale_factor() != main_scale or geometry.x < 0 or geometry.y < 0:
            return True
    return False


def blank_icon() -> Gdk.Texture:
    return Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(bytes(4)), 4)


def pane_marker(pane_id: int) -> Gdk.ContentProvider:
    return Gdk.ContentProvider.new_for_bytes(PANE_MIME, GLib.Bytes.new(f"anyscp-pane:{pane_id}".encode()))


def provider_for_paths(paths: list[str]) -> Gdk.ContentProvider | None:
    """Existing local files for Explorer (Windows; it does not take GTK's
    file list).  None if the shell cannot describe them."""
    try:
        return Gdk.ContentProvider.new_for_bytes(SHELL_ID_LIST, GLib.Bytes.new(shell_id_list(paths)))
    except OSError:
        return None


def provider_for_download(download: RemoteDownload) -> Gdk.ContentProvider:
    if not compat.IS_WINDOWS:
        return Gdk.ContentProvider.new_for_value(DragFiles(download.paths, download))
    from .lazyprovider import LazyProvider

    def produce(_mime_type, done):
        def send():
            try:
                data = shell_id_list(download.paths)
            except OSError as exc:
                done(None, str(exc))
                return
            done(data)
        download.when_ready(send, lambda message: done(None, str(message)), _dropped())
    return LazyProvider([SHELL_ID_LIST], produce)


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------

def cleanup(leaving: bool = False):
    """Remove temporary folders.  The files of a drag stay while anySCP runs:
    the program they were dropped on may still be copying them, and a later
    drag of the same files takes them again.  At exit this run's go, except
    those that were dropped moments ago; folders of earlier runs go once they
    are STALE_SECONDS old (folder times: file times are the server's)."""
    now = time.time()
    if leaving:
        for download in _downloads:
            if download.state == "running":
                # (The window is gone: no queue updates, just stop the thread.)
                download.job.cancel_event.set()
            if download.state != "ready" or now - download.ready_at > RECENT_SECONDS:
                download.remove_files()
    root = temp_root()
    ours = {download.dir.name for download in _downloads}
    try:
        folders = [p for p in root.iterdir() if p.is_dir() and p.name not in ours]
    except OSError:
        return
    for folder in folders:
        try:
            age = now - folder.stat().st_mtime
        except OSError:
            continue
        if age > STALE_SECONDS:
            shutil.rmtree(folder, ignore_errors=True)
    try:
        root.rmdir()                    # only if it is empty now
    except OSError:
        pass
