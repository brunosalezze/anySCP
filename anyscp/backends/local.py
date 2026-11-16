# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Local disk backend."""

from __future__ import annotations

import errno
import functools
import os
import posixpath
import shutil
import stat as st

from ..compat import IS_WINDOWS, home_dir, is_hidden_native, list_drives, to_internal, to_native
from .base import (CAP_CHMOD, CAP_CHOWN, CAP_EXEC, CAP_RESUME, CAP_SYMLINK, CAP_UTIME,
                   AlreadyExists, FileInfo, FileSystem, FSError, NotFound, NotSupported,
                   PermissionDenied)

try:                                   # POSIX only
    import grp
    import pwd
except ImportError:                    # Windows
    grp = pwd = None


@functools.lru_cache(maxsize=512)
def _user(uid: int) -> str:
    if pwd is None:
        return ""
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


@functools.lru_cache(maxsize=512)
def _group(gid: int) -> str:
    if grp is None:
        return ""
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _map(exc: OSError, path: str) -> FSError:
    msg = f"{exc.strerror or exc}: {path}"
    if exc.errno == errno.ENOENT:
        return NotFound(msg)
    if exc.errno in (errno.EACCES, errno.EPERM):
        return PermissionDenied(msg)
    if exc.errno == errno.EEXIST:
        return AlreadyExists(msg)
    return FSError(msg)


def _wrap(fn):
    @functools.wraps(fn)
    def inner(self, path, *args, **kwargs):
        try:
            return fn(self, path, *args, **kwargs)
        except OSError as exc:
            raise _map(exc, path) from exc
    return inner


def info_from_stat(name: str, path: str, lst: os.stat_result, native: str | None = None) -> FileInfo:
    """``path`` is the internal path, ``native`` the OS path (same on POSIX)."""
    native = native or to_native(path)
    is_link = st.S_ISLNK(lst.st_mode)
    target_st = lst
    link_target = None
    if is_link:
        try:
            link_target = os.readlink(native)
            target_st = os.stat(native)
        except OSError:
            target_st = lst
    info = FileInfo(
        name=name, path=path,
        is_dir=st.S_ISDIR(target_st.st_mode),
        is_link=is_link,
        size=target_st.st_size if not st.S_ISDIR(target_st.st_mode) else 0,
        mtime=lst.st_mtime if not is_link else target_st.st_mtime,
        mode=None if IS_WINDOWS else st.S_IMODE(lst.st_mode if not is_link else target_st.st_mode),
        owner=_user(lst.st_uid), group=_group(lst.st_gid),
        link_target=link_target)
    if is_hidden_native(native, lst) and not info.name.startswith("."):
        info.attr_hidden = True
    return info


def _drive_info(drive: str) -> FileInfo:
    return FileInfo(name=drive, path="/" + drive, is_dir=True)


class LocalFileSystem(FileSystem):
    """Local disk.  Paths are internal POSIX-style paths (see
    :mod:`anyscp.compat`); on Windows ``/`` lists the drives."""

    protocol = "local"
    is_local = True
    capabilities = (frozenset({CAP_UTIME, CAP_RESUME, CAP_EXEC}) if IS_WINDOWS else
                    frozenset({CAP_CHMOD, CAP_CHOWN, CAP_UTIME, CAP_SYMLINK, CAP_RESUME, CAP_EXEC}))

    def __init__(self, site=None, prompter=None):
        super().__init__(site, prompter)

    def clone(self):
        return LocalFileSystem()

    def home(self) -> str:
        return home_dir()

    def realpath(self, path):
        if IS_WINDOWS and path == "/":
            return "/"
        return to_internal(os.path.realpath(to_native(path)))

    def _native(self, path: str) -> str:
        native = to_native(path)
        if IS_WINDOWS and not native:
            raise PermissionDenied("This operation is not possible on the drive list")
        return native

    @_wrap
    def listdir(self, path):
        if IS_WINDOWS and path in ("", "/"):
            return [_drive_info(d) for d in list_drives()]
        native = self._native(path)
        if IS_WINDOWS and path.startswith("//") and path.rstrip("/").count("/") < 3:
            raise NotSupported("Type the full share path, e.g. \\\\server\\share")
        out = []
        with os.scandir(native) as it:
            for entry in it:
                try:
                    lst = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                out.append(info_from_stat(entry.name, posixpath.join(path, entry.name), lst, entry.path))
        return out

    @_wrap
    def stat(self, path):
        if IS_WINDOWS and path in ("", "/"):
            return FileInfo(name="/", path="/", is_dir=True)
        native = self._native(path)
        lst = os.lstat(native)
        name = posixpath.basename(path.rstrip("/")) or "/"
        return info_from_stat(name, path, lst, native)

    def disk_free(self, path):
        if IS_WINDOWS and path in ("", "/"):
            return None
        try:
            return shutil.disk_usage(to_native(path)).free
        except OSError:
            return None

    @_wrap
    def mkdir(self, path):
        os.mkdir(self._native(path))

    @_wrap
    def remove(self, path):
        native = self._native(path)
        try:
            os.unlink(native)
        except PermissionError:
            if not IS_WINDOWS:
                raise
            os.chmod(native, st.S_IWRITE)      # read-only files on Windows
            os.unlink(native)

    @_wrap
    def rmdir(self, path):
        os.rmdir(self._native(path))

    @_wrap
    def rename(self, src, dst):
        nsrc, ndst = self._native(src), self._native(dst)
        if os.path.exists(ndst) and not os.path.samefile(nsrc, ndst):
            raise OSError(errno.EEXIST, "Destination already exists", dst)
        os.rename(nsrc, ndst)

    @_wrap
    def chmod(self, path, mode):
        if IS_WINDOWS:
            raise NotSupported("Windows does not use Unix permissions")
        os.chmod(self._native(path), mode)

    @_wrap
    def chown(self, path, owner, group):
        if pwd is None:
            raise NotSupported("Changing ownership is not supported on Windows")
        uid = pwd.getpwnam(owner).pw_uid if owner else -1
        gid = grp.getgrnam(group).gr_gid if group else -1
        os.chown(self._native(path), uid, gid, follow_symlinks=False)

    @_wrap
    def utime(self, path, mtime):
        os.utime(self._native(path), (mtime, mtime))

    def symlink(self, target, link_path):
        try:
            os.symlink(to_native(target) if target.startswith("/") else target, self._native(link_path))
        except OSError as exc:
            raise _map(exc, link_path) from exc

    def readlink(self, path):
        try:
            return os.readlink(to_native(path))
        except OSError:
            return None

    @_wrap
    def open_read(self, path, offset=0):
        fh = open(self._native(path), "rb")  # noqa: SIM115
        if offset:
            fh.seek(offset)
        return fh

    @_wrap
    def open_write(self, path, offset=0, size=None):
        native = self._native(path)
        if offset:
            fh = open(native, "r+b")  # noqa: SIM115
            fh.seek(offset)
            fh.truncate()
            return fh
        return open(native, "wb")  # noqa: SIM115

    def exec(self, command):
        import subprocess
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, check=False)  # noqa: S602
        return proc.returncode, proc.stdout, proc.stderr

    def info(self):
        return [("Protocol", "Local file system")]
