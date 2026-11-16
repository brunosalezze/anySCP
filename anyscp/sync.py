# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Directory comparison for synchronization (no GTK)."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from .backends.base import Cancelled, FileInfo, FileSystem, FSError, NotFound

UPLOAD, DOWNLOAD, DELETE_LOCAL, DELETE_REMOTE = "upload", "download", "delete_local", "delete_remote"

TIME_TOLERANCE = 2.0  # seconds; FAT and many FTP servers have coarse timestamps


@dataclass
class SyncAction:
    kind: str
    rel: str
    local: FileInfo | None
    remote: FileInfo | None
    reason: str
    enabled: bool = True

    @property
    def size(self) -> int:
        src = self.local if self.kind == UPLOAD else self.remote if self.kind == DOWNLOAD else None
        return 0 if src is None or src.is_dir else src.size


@dataclass
class SyncOptions:
    direction: str = "remote"        # remote (local→remote) | local (remote→local) | both
    criteria: str = "time"           # time | size | either
    recursive: bool = True
    delete: bool = False             # mirror: delete files missing on the source side
    exclude: list[str] = field(default_factory=lambda: [".git", "__pycache__", ".DS_Store"])


def _scan(fs: FileSystem, root: str, recursive: bool, exclude, cancel) -> tuple[dict[str, FileInfo], bool]:
    """Return ``(entries by relative path, root_exists)``.  Symlinked folders
    are listed but not descended into.  Only a missing *root* counts as
    empty; anything vanishing below it aborts the scan (a partial scan
    would make mirror mode delete files that were simply not seen)."""
    import fnmatch
    out: dict[str, FileInfo] = {}

    def excluded(name):
        return any(fnmatch.fnmatch(name, pat) for pat in exclude)

    def walk(path, rel):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        for entry in fs.listdir(path):
            if excluded(entry.name):
                continue
            key = posixpath.join(rel, entry.name) if rel else entry.name
            out[key] = entry
            if entry.is_dir and recursive and not entry.is_link:
                walk(entry.path, key)
    try:
        fs.listdir(root)
    except NotFound:
        return out, False
    walk(root, "")
    return out, True


def _differs(a: FileInfo, b: FileInfo, criteria: str) -> bool:
    newer = (a.mtime or 0) > (b.mtime or 0) + TIME_TOLERANCE
    sized = a.size != b.size
    if criteria == "size":
        return sized
    if criteria == "either":
        return newer or (sized and (a.mtime or 0) >= (b.mtime or 0) - TIME_TOLERANCE)
    return newer


def compare(local_fs: FileSystem, remote_fs: FileSystem, local_root: str, remote_root: str,
            opts: SyncOptions, cancel=None) -> list[SyncAction]:
    local, local_ok = _scan(local_fs, local_root, opts.recursive, opts.exclude, cancel)
    remote, remote_ok = _scan(remote_fs, remote_root, opts.recursive, opts.exclude, cancel)
    if not local_ok and not remote_ok:
        raise NotFound("Neither folder exists")
    source_ok = {"remote": local_ok, "local": remote_ok}.get(opts.direction, True)
    if not source_ok:
        raise FSError("The source folder does not exist: "
                      + (local_root if opts.direction == "remote" else remote_root))
    actions: list[SyncAction] = []
    # Folders whose content is not compared: copied as a whole, or reached
    # through a symlink on either side (the link side was not descended).
    covered: set[str] = {rel for rel, e in list(local.items()) + list(remote.items())
                         if e.is_dir and e.is_link}

    def under_covered(rel):
        parent = posixpath.dirname(rel)
        while parent:
            if parent in covered:
                return True
            parent = posixpath.dirname(parent)
        return False

    for rel in sorted(set(local) | set(remote)):
        if under_covered(rel):
            continue
        lo, re_ = local.get(rel), remote.get(rel)
        if lo is not None and re_ is not None:
            if lo.is_dir != re_.is_dir:
                actions.append(SyncAction(UPLOAD if opts.direction != "local" else DOWNLOAD, rel, lo, re_,
                                          "File and folder with the same name", enabled=False))
                continue
            if lo.is_dir:
                continue
            if opts.direction in ("remote", "both") and _differs(lo, re_, opts.criteria):
                actions.append(SyncAction(UPLOAD, rel, lo, re_, "Newer locally" if opts.criteria != "size"
                                          else "Different size"))
            elif opts.direction in ("local", "both") and _differs(re_, lo, opts.criteria):
                actions.append(SyncAction(DOWNLOAD, rel, lo, re_, "Newer on server" if opts.criteria != "size"
                                          else "Different size"))
        elif lo is not None:
            if opts.direction in ("remote", "both"):
                actions.append(SyncAction(UPLOAD, rel, lo, None, "New folder" if lo.is_dir else "New file"))
                if lo.is_dir:
                    covered.add(rel)
            elif opts.delete:
                actions.append(SyncAction(DELETE_LOCAL, rel, lo, None, "Not on the server"))
                if lo.is_dir:
                    covered.add(rel)
        else:
            if opts.direction in ("local", "both"):
                actions.append(SyncAction(DOWNLOAD, rel, None, re_, "New folder" if re_.is_dir else "New file"))
                if re_.is_dir:
                    covered.add(rel)
            elif opts.delete:
                actions.append(SyncAction(DELETE_REMOTE, rel, None, re_, "Not on this computer"))
                if re_.is_dir:
                    covered.add(rel)
    return actions
