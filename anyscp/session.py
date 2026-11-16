# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""A connected session: one backend connection, a serial worker thread for
browsing operations, and a small pool of extra connections for transfers."""

from __future__ import annotations

import queue
import threading
import time
import traceback

from gi.repository import GLib, GObject

from .backends import create_fs
from .backends.base import ConnectionLost, FileSystem, FSError
from .backends.local import LocalFileSystem
from .config import jump_hops, proxy_secret_id, secrets, site_title


def _idle(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - never let a callback kill the main loop
            traceback.print_exc()
        return False
    # Default (not idle) priority, like GIO's async callbacks: results must not
    # wait behind a busy frame clock (e.g. a spinner animating while connecting).
    GLib.idle_add(run, priority=GLib.PRIORITY_DEFAULT)


class SerialWorker:
    """Runs callables one at a time on a dedicated thread.  Results are
    delivered to the GTK main loop."""

    def __init__(self, name: str):
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()
        self.busy = False

    def submit(self, fn, on_done=None, on_error=None):
        self._queue.put((fn, on_done, on_error))

    def _loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, on_done, on_error = item
            self.busy = True
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001
                if on_error is not None:
                    _idle(on_error, exc)
                else:
                    traceback.print_exc()
            else:
                if on_done is not None:
                    _idle(on_done, result)
            finally:
                self.busy = False

    def stop(self):
        self._queue.put(None)


class Session(GObject.Object):
    """Remote session.  ``state`` is one of ``connecting``, ``connected``,
    ``disconnected``, ``failed``."""

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "log": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, site: dict, prompter):
        super().__init__()
        self.site = dict(site)
        self.prompter = prompter
        prompter.session = self
        self.fs: FileSystem | None = None
        self.state = "disconnected"
        self.error: BaseException | None = None
        self.log_lines: list[str] = []
        self.worker = SerialWorker(f"session:{site_title(site)}")
        self._pool: list[FileSystem] = []
        self._clones: set = set()
        self._pool_lock = threading.Lock()
        self._keepalive_id = 0
        self._closed = False
        self.connected_at: float | None = None

    @property
    def title(self) -> str:
        return site_title(self.site)

    # -- logging -------------------------------------------------------------
    def log(self, message: str):
        line = time.strftime("%H:%M:%S ") + message
        self.log_lines.append(line)
        if len(self.log_lines) > 5000:
            del self.log_lines[:1000]
        _idle(self.emit, "log", line)

    def _set_state(self, state: str):
        self.state = state
        self.emit("state-changed", state)

    # -- connection ----------------------------------------------------------
    def connect_async(self, on_done=None, on_error=None):
        self._set_state("connecting")

        def job():
            site = self.site
            if site.get("_password") is None and site.get("id") and site.get("save_password"):
                stored = secrets.lookup(site["id"])
                if stored is not None:
                    site["_password"] = stored
            if site.get("proxy_type", "none") != "none" and site.get("proxy_user") \
                    and site.get("_proxy_password") is None and site.get("id"):
                site["_proxy_password"] = secrets.lookup(proxy_secret_id(site["id"]))
            if site.get("protocol") in ("sftp", "scp") and (site.get("jump_hosts") or "").strip():
                try:
                    site["_jumps"] = jump_hops(site)
                except ValueError as exc:
                    raise FSError(str(exc)) from exc
            fs = create_fs(site, self.prompter)
            fs.connect()
            return fs

        def done(fs):
            if self._closed:            # tab was closed while connecting
                threading.Thread(target=fs.close, daemon=True).start()
                return
            self.fs = fs
            self.error = None
            self.connected_at = time.time()
            hook = getattr(self.prompter, "connection_succeeded", None)
            if hook:
                hook()
            self._set_state("connected")
            self._start_keepalive()
            if on_done:
                on_done()

        def failed(exc):
            if self._closed:
                return
            self.error = exc
            self.log(f"Connection failed: {exc}")
            self._set_state("failed")
            if on_error:
                on_error(exc)

        self.worker.submit(job, done, failed)

    def reconnect_async(self, on_done=None, on_error=None):
        self._close_connections()
        self.connect_async(on_done, on_error)

    def _start_keepalive(self):
        if self._keepalive_id:
            GLib.source_remove(self._keepalive_id)
        interval = int(self.site.get("keepalive") or 0)
        if interval <= 0:
            self._keepalive_id = 0
            return

        def tick():
            if self.state != "connected" or self.fs is None:
                self._keepalive_id = 0
                return False
            if not self.worker.busy:
                self.run(lambda fs: fs.keepalive(), on_error=self._on_background_error)
            return True
        self._keepalive_id = GLib.timeout_add_seconds(interval, tick)

    def _on_background_error(self, exc):
        if isinstance(exc, ConnectionLost):
            self.mark_lost(exc)

    def mark_lost(self, exc=None):
        if self.state == "connected":
            self.error = exc
            self.log(f"Connection lost: {exc}" if exc else "Connection lost")
            self._set_state("disconnected")

    def run(self, fn, on_done=None, on_error=None):
        """Run ``fn(fs)`` on the session worker."""
        def job():
            if self.fs is None:
                raise ConnectionLost("Not connected")
            return fn(self.fs)

        def failed(exc):
            if isinstance(exc, ConnectionLost):
                self.mark_lost(exc)
            if on_error:
                on_error(exc)
        self.worker.submit(job, on_done, failed)

    # -- transfer connections (called from transfer threads) ----------------
    def acquire_fs(self) -> FileSystem:
        while True:
            with self._pool_lock:
                fs = self._pool.pop() if self._pool else None
            if fs is None:
                break
            try:
                fs.keepalive()          # idle FTP connections may have timed out
                return fs
            except Exception:  # noqa: BLE001
                with self._pool_lock:
                    self._clones.discard(fs)
                try:
                    fs.close()
                except Exception:  # noqa: BLE001
                    pass
        if self.fs is None:
            raise ConnectionLost("Not connected")
        fs = self.fs.clone()
        with self._pool_lock:
            self._clones.add(fs)
        return fs

    def release_fs(self, fs: FileSystem, ok: bool = True):
        with self._pool_lock:
            if ok and self.state == "connected" and len(self._pool) < 6:
                self._pool.append(fs)
                return
            self._clones.discard(fs)
        try:
            fs.close()
        except Exception:  # noqa: BLE001
            pass

    def _close_connections(self):
        with self._pool_lock:
            clones = list(self._clones)
            self._clones.clear()
            self._pool.clear()
        fs, self.fs = self.fs, None
        for c in clones:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        if fs is not None:
            try:
                fs.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        self._closed = True
        if self._keepalive_id:
            GLib.source_remove(self._keepalive_id)
            self._keepalive_id = 0
        self.state = "disconnected"
        threading.Thread(target=self._close_connections, daemon=True).start()
        self.worker.stop()


class LocalEndpoint:
    """Transfer endpoint for the local disk."""

    label = "Local"
    is_local = True

    def acquire_fs(self):
        return LocalFileSystem()

    def release_fs(self, fs, ok=True):
        pass

    def same_as(self, other) -> bool:
        return isinstance(other, LocalEndpoint)


class SessionEndpoint:
    is_local = False

    def __init__(self, session: Session):
        self.session = session

    @property
    def label(self):
        return self.session.title

    def acquire_fs(self):
        return self.session.acquire_fs()

    def release_fs(self, fs, ok=True):
        self.session.release_fs(fs, ok)

    def same_as(self, other) -> bool:
        return isinstance(other, SessionEndpoint) and other.session is self.session
