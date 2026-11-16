# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Background transfer queue.

A :class:`TransferJob` copies a set of files/directories from one endpoint
(local disk or a session) to another, recursively, with progress, speed,
overwrite handling, resume and optional move semantics.  The
:class:`TransferManager` runs a bounded number of jobs in parallel threads.
"""

from __future__ import annotations

import itertools
import posixpath
import threading
import time
import traceback
from dataclasses import dataclass

from gi.repository import GLib, GObject

from .backends.base import (CAP_CHMOD, CAP_RESUME, CAP_UTIME, Cancelled, ConnectionLost,
                            FileInfo, FSError, NotFound, NotSupported)

CHUNK = 256 * 1024

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"


@dataclass
class _Op:
    is_dir: bool
    src: str
    dst: str
    size: int = 0
    mtime: float | None = None
    mode: int | None = None
    source_info: FileInfo | None = None
    via_link: bool = False          # inside a followed symlinked directory


class AutoDecider:
    """Non-interactive decisions (tests, sync)."""

    def __init__(self, overwrite="overwrite", on_error="abort"):
        self.overwrite = overwrite
        self.on_error = on_error

    def ask_overwrite(self, job, op, existing, can_resume):
        return self.overwrite, True

    def ask_error(self, job, message):
        return self.on_error


_ids = itertools.count(1)


class Job:
    """Common bookkeeping for queue entries."""

    kind = "job"

    def __init__(self, title: str, subtitle: str = ""):
        self.id = next(_ids)
        self.title = title
        self.subtitle = subtitle
        self.state = QUEUED
        self.error: str | None = None
        self.warnings: list[str] = []
        self.total_bytes = 0
        self.done_bytes = 0
        self.total_files = 0
        self.done_files = 0
        self.current = ""
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.cancel_event = threading.Event()
        self.on_finished = None          # callable(job), main thread
        self._speed_samples: list[tuple[float, int]] = []

    # progress ------------------------------------------------------------
    @property
    def fraction(self) -> float:
        if self.state == DONE:
            return 1.0
        if self.total_bytes > 0:
            return min(1.0, self.done_bytes / self.total_bytes)
        if self.total_files > 0:
            return min(1.0, self.done_files / self.total_files)
        return 0.0

    def sample_speed(self) -> float:
        now = time.monotonic()
        self._speed_samples.append((now, self.done_bytes))
        cutoff = now - 3.0
        while len(self._speed_samples) > 2 and self._speed_samples[0][0] < cutoff:
            self._speed_samples.pop(0)
        (t0, b0), (t1, b1) = self._speed_samples[0], self._speed_samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def eta(self, speed: float) -> float | None:
        if speed <= 0 or self.total_bytes <= 0:
            return None
        return max(0.0, (self.total_bytes - self.done_bytes) / speed)

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def reset(self):
        self.state = QUEUED
        self.error = None
        self.warnings = []
        self.done_bytes = 0
        self.done_files = 0
        self.current = ""
        self.cancel_event = threading.Event()
        self._speed_samples = []

    def execute(self, decider):  # pragma: no cover - abstract
        raise NotImplementedError


class TransferJob(Job):
    """Copy (or move) ``pairs`` of ``(source FileInfo, destination path)``."""

    kind = "transfer"

    def __init__(self, title, src_endpoint, dst_endpoint, pairs, *, move=False,
                 settings=None, overwrite: str | None = None, direction="upload", subtitle=""):
        super().__init__(title, subtitle)
        self.src = src_endpoint
        self.dst = dst_endpoint
        self.pairs: list[tuple[FileInfo, str]] = list(pairs)
        self.move = move
        self.direction = direction       # upload | download | copy
        self.preserve_mtime = True if settings is None else bool(settings["preserve_mtime"])
        self.preserve_perms = False if settings is None else bool(settings["preserve_permissions"])
        self.confirm_overwrite = True if settings is None else bool(settings["confirm_overwrite"])
        self.initial_overwrite = overwrite
        self.overwrite_all: str | None = overwrite
        self.skip_all_errors = False
        # Bookkeeping that survives retries, so a retry only resumes what this
        # job itself left half-written and keeps the user's skip decisions.
        self.retrying = False
        self.done_dsts: set[str] = set()
        self.skipped_dsts: set[str] = set()
        self.partial_dsts: set[str] = set()
        self.dst_dirs = sorted({posixpath.dirname(d) for _s, d in self.pairs})
        self.src_dirs = sorted({posixpath.dirname(s.path) for s, _d in self.pairs})

    def reset(self):
        super().reset()
        self.overwrite_all = self.initial_overwrite
        self.skip_all_errors = False

    @property
    def _same_endpoint(self) -> bool:
        same = getattr(self.src, "same_as", None)
        return bool(same and same(self.dst))

    # -- planning ------------------------------------------------------------
    def _plan(self, fs) -> list[_Op]:
        ops: list[_Op] = []
        for info, dst in self.pairs:
            self.check_cancel()
            if not info.is_dir:
                # Refresh: the pane listing that produced ``info`` may be stale.
                try:
                    fresh = fs.stat(info.path)
                    if not fresh.is_dir:
                        info = fresh
                except FSError:
                    pass
            if info.is_dir:
                self._plan_dir(fs, info.path, dst, info, ops, set(), info.is_link)
            else:
                ops.append(_Op(False, info.path, dst, info.size, info.mtime, info.mode, info))
        return ops

    def _plan_dir(self, fs, src, dst, info, ops, visited, via_link, depth=0):
        """Expand a directory.  Symlinked directories are followed (their
        content is copied, like WinSCP does) with loop protection."""
        try:
            real = fs.realpath(src)
        except FSError:
            real = src
        if real in visited or depth > 32:
            self.warnings.append(f"Skipped {src}: symbolic link loop")
            return
        visited = visited | {real}
        ops.append(_Op(True, src, dst, 0, info.mtime, info.mode, info, via_link))
        self.current = src
        for entry in sorted(fs.listdir(src), key=lambda e: e.name):
            self.check_cancel()
            child_dst = posixpath.join(dst, entry.name)
            if entry.is_dir:
                self._plan_dir(fs, entry.path, child_dst, entry, ops, visited,
                               via_link or entry.is_link, depth + 1)
            else:
                ops.append(_Op(False, entry.path, child_dst, entry.size, entry.mtime,
                               entry.mode, entry, via_link))

    # -- execution -----------------------------------------------------------
    def execute(self, decider):
        src_fs = self.src.acquire_fs()
        ok_src = True
        try:
            dst_fs = self.dst.acquire_fs()
        except BaseException:
            self.src.release_fs(src_fs, True)
            raise
        ok_dst = True
        try:
            self.current = "Scanning…"
            ops = self._plan(src_fs)
            self.total_files = sum(1 for op in ops if not op.is_dir)
            self.total_bytes = sum(op.size for op in ops if not op.is_dir)
            completed: list[_Op] = []
            dir_ops: list[_Op] = []
            for op in ops:
                self.check_cancel()
                if op.is_dir:
                    self._ensure_dir(dst_fs, op.dst)
                    dir_ops.append(op)
                    completed.append(op)
                    continue
                if self._with_retry(decider, op, lambda op=op: self._copy_file(src_fs, dst_fs, op, decider)):
                    completed.append(op)
                self.done_files += 1
            # Directory timestamps after their content is written.
            if self.preserve_mtime:
                for op in reversed(dir_ops):
                    if op.mtime and (dst_fs.is_local or dst_fs.has(CAP_UTIME)):
                        try:
                            dst_fs.utime(op.dst, op.mtime)
                        except FSError:
                            pass
            if self.move:
                self.current = "Removing sources…"
                self._delete_sources(src_fs, ops, completed)
        except (ConnectionLost, Cancelled):
            ok_src = ok_dst = False
            raise
        finally:
            self.src.release_fs(src_fs, ok_src)
            self.dst.release_fs(dst_fs, ok_dst)

    def _with_retry(self, decider, op, fn) -> bool:
        while True:
            try:
                fn()
                return True
            except (Cancelled, ConnectionLost):
                raise
            except _Skipped:
                return False
            except FSError as exc:
                message = f"{posixpath.basename(op.src)}: {exc}"
                if self.skip_all_errors:
                    self.warnings.append(message)
                    return False
                answer = decider.ask_error(self, message)
                if answer == "retry":
                    continue
                if answer in ("skip", "skip_all"):
                    self.skip_all_errors = answer == "skip_all"
                    self.warnings.append(message)
                    return False
                raise

    def _ensure_dir(self, fs, path):
        try:
            info = fs.stat(path)
        except NotFound:
            fs.mkdir(path)
            return
        if not info.is_dir:
            raise FSError(f"Cannot create folder {path}: a file with that name exists")

    def _decide(self, decider, op, existing, can_resume) -> str:
        if self.retrying:
            if op.dst in self.done_dsts or op.dst in self.skipped_dsts:
                return "skip"
            if op.dst in self.partial_dsts:
                return "resume" if can_resume and existing.size <= op.size else "overwrite"
        policy = self.overwrite_all
        if policy is None and not self.confirm_overwrite:
            policy = "overwrite"
        if policy is None:
            action, apply_all = decider.ask_overwrite(self, op, existing, can_resume)
            if action == "cancel":
                raise Cancelled()
            if apply_all:
                self.overwrite_all = action
            policy = action
        if policy == "newer":
            if op.mtime is not None and existing.mtime is not None and op.mtime <= existing.mtime + 1:
                return "skip"
            return "overwrite"
        if policy == "resume" and not can_resume:
            return "overwrite"
        return policy

    def _copy_file(self, src_fs, dst_fs, op, decider):
        self.current = op.src
        offset = 0
        try:
            existing = dst_fs.stat(op.dst)
        except NotFound:
            existing = None
        if existing is not None:
            if existing.is_dir and not existing.is_link:
                raise FSError(f"Cannot overwrite folder {op.dst} with a file")
            can_resume = (0 < existing.size < op.size and src_fs.has(CAP_RESUME)
                          and dst_fs.has(CAP_RESUME))
            action = self._decide(decider, op, existing, can_resume)
            if action == "skip":
                self.done_bytes += op.size
                if op.dst not in self.done_dsts:
                    self.skipped_dsts.add(op.dst)
                raise _Skipped()
            if action == "resume" and can_resume:
                offset = existing.size
        start_bytes = self.done_bytes
        if self._same_endpoint and op.src == op.dst:
            raise FSError(f"“{posixpath.basename(op.src)}” cannot be copied onto itself")
        reader = src_fs.open_read(op.src, offset)
        try:
            try:
                writer = dst_fs.open_write(op.dst, offset, op.size)
            except NotSupported:
                if offset == 0:
                    raise
                reader.close()
                offset = 0
                reader = src_fs.open_read(op.src, 0)
                writer = dst_fs.open_write(op.dst, 0, op.size)
            self.done_bytes += offset
            try:
                while True:
                    if self.cancel_event.is_set():
                        raise Cancelled()
                    chunk = reader.read(CHUNK)
                    if not chunk:
                        break
                    writer.write(chunk)
                    self.done_bytes += len(chunk)
            except BaseException:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass
                raise
            writer.close()
        except BaseException:
            self.done_bytes = start_bytes
            raise
        finally:
            reader.close()
        # Account for files whose size changed while copying.
        self.done_bytes = start_bytes + op.size
        self.partial_dsts.discard(op.dst)
        self.done_dsts.add(op.dst)
        if self.preserve_mtime and op.mtime and (dst_fs.is_local or dst_fs.has(CAP_UTIME)):
            try:
                dst_fs.utime(op.dst, op.mtime)
            except FSError:
                pass
        if self.preserve_perms and op.mode is not None and (dst_fs.is_local or dst_fs.has(CAP_CHMOD)):
            try:
                dst_fs.chmod(op.dst, op.mode & 0o7777)
            except FSError:
                pass

    def _delete_sources(self, fs, ops, completed):
        """Remove moved sources bottom-up, keeping any folder that still
        contains something that failed to transfer."""
        done = {id(op) for op in completed}
        link_roots = [op.src for op in ops
                      if op.is_dir and op.source_info is not None and op.source_info.is_link]

        def inside_link(path):
            return any(path.startswith(root + "/") for root in link_roots)

        keep: set[str] = set()
        for op in reversed(ops):
            self.check_cancel()
            parent = posixpath.dirname(op.src)
            if id(op) not in done:
                keep.add(parent)
                continue
            if inside_link(op.src):
                continue                  # content reached through a symlink: not ours
            is_link = bool(op.source_info and op.source_info.is_link)
            try:
                if op.is_dir and not is_link:
                    if op.src in keep:
                        keep.add(parent)
                        continue
                    fs.rmdir(op.src)
                else:
                    fs.remove(op.src)     # files and the symlinks themselves
            except FSError as exc:
                keep.add(parent)
                self.warnings.append(f"Could not remove {op.src}: {exc}")


class _Skipped(Exception):
    pass


class DeleteJob(Job):
    """Recursive delete running in the queue (used by synchronization)."""

    kind = "delete"

    def __init__(self, title, endpoint, paths, subtitle=""):
        super().__init__(title, subtitle)
        self.endpoint = endpoint
        self.paths = list(paths)
        self.total_files = len(self.paths)
        self.dst_dirs = sorted({posixpath.dirname(p) for p in self.paths})
        self.src_dirs = []

    def execute(self, decider):
        fs = self.endpoint.acquire_fs()
        ok = True
        try:
            for path in self.paths:
                self.check_cancel()
                self.current = path
                try:
                    fs.remove_tree(path, self.cancel_event)
                except NotFound:
                    pass
                self.done_files += 1
        except ConnectionLost:
            ok = False
            raise
        finally:
            self.endpoint.release_fs(fs, ok)


class TransferManager(GObject.Object):
    """Runs jobs in up to ``parallel`` threads."""

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "job-finished": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, decider=None, parallel=3):
        super().__init__()
        self.decider = decider or AutoDecider()
        self.parallel = parallel
        self.jobs: list[Job] = []
        self._lock = threading.Lock()
        self._running = 0

    def add(self, job: Job):
        with self._lock:
            self.jobs.append(job)
        self.emit("changed")
        self._kick()
        return job

    def _kick(self):
        start = []
        with self._lock:
            for job in self.jobs:
                if self._running >= max(1, self.parallel):
                    break
                if job.state == QUEUED:
                    job.state = RUNNING
                    self._running += 1
                    start.append(job)
        for job in start:
            threading.Thread(target=self._run, args=(job,), daemon=True,
                             name=f"transfer-{job.id}").start()
        if start:
            GLib.idle_add(self._emit_changed, priority=GLib.PRIORITY_DEFAULT)

    def _emit_changed(self):
        self.emit("changed")
        return False

    def _run(self, job: Job):
        job.started_at = time.time()
        try:
            job.execute(self.decider)
            job.state = CANCELLED if job.cancel_event.is_set() else DONE
        except Cancelled:
            job.state = CANCELLED
        except FSError as exc:
            job.state = FAILED
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            job.state = FAILED
            job.error = f"{exc.__class__.__name__}: {exc}"
        job.finished_at = time.time()
        with self._lock:
            self._running -= 1
        GLib.idle_add(self._finished, job, priority=GLib.PRIORITY_DEFAULT)
        self._kick()

    def _finished(self, job):
        if job.on_finished is not None:
            try:
                job.on_finished(job)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self.emit("job-finished", job)
        self.emit("changed")
        return False

    # -- control ---------------------------------------------------------------
    def cancel(self, job: Job):
        job.cancel_event.set()
        with self._lock:
            if job.state == QUEUED:
                job.state = CANCELLED
        self.emit("changed")

    def retry(self, job: Job):
        if job.state in (FAILED, CANCELLED):
            job.reset()
            # Resume what this job left half-written, skip what it finished.
            if isinstance(job, TransferJob):
                job.retrying = True
            self.emit("changed")
            self._kick()

    def remove(self, job: Job):
        if job.state in (QUEUED, RUNNING):
            self.cancel(job)
        with self._lock:
            if job in self.jobs and job.state not in (RUNNING,):
                self.jobs.remove(job)
        self.emit("changed")

    def clear_finished(self):
        with self._lock:
            self.jobs = [j for j in self.jobs if j.state in (QUEUED, RUNNING)]
        self.emit("changed")

    def set_parallel(self, n: int):
        self.parallel = max(1, int(n))
        self._kick()

    @property
    def active(self) -> list[Job]:
        return [j for j in self.jobs if j.state in (QUEUED, RUNNING)]

    def overall_fraction(self) -> float:
        active = self.active
        total = sum(j.total_bytes for j in active)
        done = sum(j.done_bytes for j in active)
        if total > 0:
            return done / total
        if active:
            return sum(j.fraction for j in active) / len(active)
        return 1.0
