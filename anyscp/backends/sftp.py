# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""SFTP backend (paramiko)."""

from __future__ import annotations

import errno
import functools
import posixpath
import socket
import stat as st

import paramiko

from .base import (CAP_CHMOD, CAP_CHOWN, CAP_EXEC, CAP_RESUME, CAP_SHELL, CAP_SYMLINK,
                   CAP_UTIME, AlreadyExists, ConnectionLost, FileInfo, FSError, NotFound,
                   NotSupported, PermissionDenied)
from .ssh import SSHFileSystem

WINDOW_SIZE = 16 * 1024 * 1024
MAX_PACKET = 32 * 1024
CHANNEL_TIMEOUT = 90   # seconds without any answer => treat the connection as dead


def _map_error(exc: BaseException, path: str) -> Exception:
    if isinstance(exc, FSError):
        return exc
    if isinstance(exc, (EOFError, paramiko.SSHException)):
        return ConnectionLost(f"Connection lost: {exc or exc.__class__.__name__}")
    if isinstance(exc, OSError):
        code = exc.errno
        text = exc.strerror or str(exc) or "Error"
        msg = f"{text}: {path}"
        if code == errno.ENOENT:
            return NotFound(msg)
        if code == errno.EACCES:
            return PermissionDenied(msg)
        if code == errno.EEXIST:
            return AlreadyExists(msg)
        if isinstance(exc, (socket.timeout, ConnectionError)) or code in (errno.EPIPE, errno.ECONNRESET):
            return ConnectionLost(f"Connection lost: {text}")
        if "Socket is closed" in str(exc):
            return ConnectionLost("Connection lost")
        return FSError(msg)
    if isinstance(exc, paramiko.SFTPError):
        return FSError(f"SFTP error: {exc}: {path}")
    return FSError(f"{exc}: {path}")


def _wrap(fn):
    @functools.wraps(fn)
    def inner(self, path, *args, **kwargs):
        try:
            return fn(self, path, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            mapped = _map_error(exc, path)
            if mapped is exc:
                raise
            raise mapped from exc
    return inner


def _owner_group(attr) -> tuple[str, str]:
    longname = getattr(attr, "longname", None)
    if longname:
        fields = longname.split(None, 8)
        if len(fields) >= 4:
            return fields[2], fields[3]
    uid = getattr(attr, "st_uid", None)
    gid = getattr(attr, "st_gid", None)
    return ("" if uid is None else str(uid)), ("" if gid is None else str(gid))


class _Reader:
    """Prefetching reader around a paramiko SFTPFile."""

    def __init__(self, fh, path):
        self.fh = fh
        self.path = path

    def read(self, n=-1):
        try:
            return self.fh.read(n)
        except Exception as exc:  # noqa: BLE001
            raise _map_error(exc, self.path) from exc

    def close(self):
        try:
            self.fh.close()
        except Exception:  # noqa: BLE001
            pass


class _Writer:
    def __init__(self, fh, path):
        self.fh = fh
        self.path = path

    def write(self, data):
        try:
            self.fh.write(data)
        except Exception as exc:  # noqa: BLE001
            raise _map_error(exc, self.path) from exc

    def close(self):
        try:
            self.fh.close()
        except Exception as exc:  # noqa: BLE001
            raise _map_error(exc, self.path) from exc


class SFTPFileSystem(SSHFileSystem):
    protocol = "sftp"
    capabilities = frozenset({CAP_CHMOD, CAP_CHOWN, CAP_UTIME, CAP_SYMLINK, CAP_EXEC,
                              CAP_SHELL, CAP_RESUME})

    def __init__(self, site=None, prompter=None, connection=None):
        super().__init__(site, prompter, connection)
        self.sftp: paramiko.SFTPClient | None = None
        self._home = "/"

    # -- lifecycle -------------------------------------------------------------
    def connect(self):
        self._open_connection()
        try:
            self.sftp = paramiko.SFTPClient.from_transport(
                self.transport, window_size=WINDOW_SIZE, max_packet_size=MAX_PACKET)
        except paramiko.SSHException as exc:
            self.close()
            raise FSError(f"The server does not provide SFTP ({exc}). Try the SCP protocol.") from exc
        if self.sftp is None:
            self.close()
            raise FSError("Could not start the SFTP subsystem")
        self.sftp.get_channel().settimeout(CHANNEL_TIMEOUT)
        try:
            self._home = self.sftp.normalize(".")
        except OSError:
            self._home = "/"
        self.prompter.log(f"SFTP session started, home is {self._home}")

    def clone(self):
        other = SFTPFileSystem(self.site, self.prompter, self.conn)
        try:
            other.sftp = paramiko.SFTPClient.from_transport(
                other.transport, window_size=WINDOW_SIZE, max_packet_size=MAX_PACKET)
        except (paramiko.SSHException, EOFError, OSError) as exc:
            other.close()
            raise ConnectionLost(f"Could not open a new SFTP channel: {exc}") from exc
        other.sftp.get_channel().settimeout(CHANNEL_TIMEOUT)
        other._home = self._home
        return other

    def close(self):
        if self.sftp is not None:
            try:
                self.sftp.close()
            except Exception:  # noqa: BLE001
                pass
            self.sftp = None
        super().close()

    def _client(self) -> paramiko.SFTPClient:
        if self.sftp is None or self.conn is None or not self.conn.active:
            raise ConnectionLost("Not connected")
        return self.sftp

    # -- paths -------------------------------------------------------------------
    def home(self):
        return self._home

    @_wrap
    def realpath(self, path):
        return self._client().normalize(path)

    # -- queries ---------------------------------------------------------------
    def _info(self, name, path, attr, target=None, resolve=True) -> FileInfo:
        """Build a FileInfo; for symlinks ``target`` is the stat() of the
        link target (looked up here when not supplied and ``resolve``)."""
        mode = attr.st_mode or 0
        is_link = st.S_ISLNK(mode)
        is_dir = st.S_ISDIR(mode)
        size = attr.st_size or 0
        mtime = attr.st_mtime
        if is_link:
            if target is None and resolve:
                try:
                    target = self._client().stat(path)
                except (OSError, paramiko.SSHException):
                    target = None
            if target is not None:
                is_dir = st.S_ISDIR(target.st_mode or 0)
                size = target.st_size or 0
                mtime = target.st_mtime
                mode = target.st_mode or mode
        owner, group = _owner_group(attr)
        return FileInfo(name=name, path=path, is_dir=is_dir, is_link=is_link,
                        size=0 if is_dir else size, mtime=mtime,
                        mode=st.S_IMODE(mode) if mode else None, owner=owner, group=group)

    def _stat_many(self, paths: list[str]) -> dict:
        """stat() many paths with pipelined requests: one network round
        trip instead of one per path (folders like /usr/lib are full of
        symlinks).  Falls back to sequential calls if paramiko's internals
        are not what we expect."""
        client = self._client()
        out: dict[str, object] = {}
        try:
            from paramiko.sftp import CMD_ATTRS, CMD_STAT
            from paramiko.sftp_attr import SFTPAttributes

            class _Collector:
                def __init__(self):
                    self.results = {}

                def _async_response(self, t, msg, num):
                    self.results[num] = (t, msg)

            for start in range(0, len(paths), 128):
                batch = paths[start:start + 128]
                collector = _Collector()
                pending = {client._async_request(collector, CMD_STAT, p): p for p in batch}
                while len(collector.results) < len(pending):
                    client._read_response()
                for num, p in pending.items():
                    t, msg = collector.results[num]
                    out[p] = SFTPAttributes._from_msg(msg) if t == CMD_ATTRS else None
            return out
        except (AttributeError, ImportError, TypeError):
            for p in paths:
                try:
                    out[p] = client.stat(p)
                except (OSError, paramiko.SSHException):
                    out[p] = None
            return out

    @_wrap
    def listdir(self, path):
        client = self._client()
        attrs = [a for a in client.listdir_attr(path) if a.filename not in (".", "..")]
        links = [posixpath.join(path, a.filename) for a in attrs if st.S_ISLNK(a.st_mode or 0)]
        targets = self._stat_many(links) if links else {}
        out = []
        for attr in attrs:
            full = posixpath.join(path, attr.filename)
            out.append(self._info(attr.filename, full, attr, targets.get(full), resolve=False))
        return out

    @_wrap
    def stat(self, path):
        attr = self._client().lstat(path)
        info = self._info(posixpath.basename(path.rstrip("/")) or "/", path, attr)
        if info.is_link:
            info.link_target = self.readlink(path)
        return info

    # -- modifications -----------------------------------------------------------
    @_wrap
    def mkdir(self, path):
        client = self._client()
        try:
            client.mkdir(path)
        except OSError as exc:
            # SFTPv3 has no "already exists" status: servers send FAILURE.
            if exc.errno is None or exc.errno not in (errno.EACCES, errno.ENOENT):
                try:
                    client.stat(path)
                except OSError:
                    raise exc from None
                raise AlreadyExists(f"Already exists: {path}") from exc
            raise

    @_wrap
    def remove(self, path):
        self._client().remove(path)

    @_wrap
    def rmdir(self, path):
        self._client().rmdir(path)

    @_wrap
    def rename(self, src, dst):
        client = self._client()
        # posix-rename replaces the target; never overwrite silently.
        if src != dst:
            try:
                client.lstat(dst)
            except OSError:
                pass
            else:
                raise AlreadyExists(f"“{posixpath.basename(dst)}” already exists in {posixpath.dirname(dst) or '/'}")
        try:
            client.posix_rename(src, dst)
        except (OSError, paramiko.SSHException):
            client.rename(src, dst)

    @_wrap
    def chmod(self, path, mode):
        self._client().chmod(path, mode)

    @_wrap
    def chown(self, path, owner, group):
        # SFTPv3 needs numeric ids; resolve via the remote shell when names are given.
        client = self._client()
        attr = client.stat(path)
        uid, gid = attr.st_uid, attr.st_gid
        if owner:
            uid = int(owner) if owner.isdigit() else self._lookup_id("-u", owner)
        if group:
            gid = int(group) if group.isdigit() else self._lookup_id("-g", group)
        client.chown(path, uid, gid)

    def _lookup_id(self, flag, name):
        import shlex
        status, out, _err = self.exec(f"id {flag} {shlex.quote(name)}" if flag == "-u"
                                      else f"getent group {shlex.quote(name)} | cut -d: -f3")
        out = out.strip()
        if status != 0 or not out.isdigit():
            raise NotSupported(f"Unknown {'user' if flag == '-u' else 'group'} “{name}” on the server")
        return int(out)

    @_wrap
    def utime(self, path, mtime):
        client = self._client()
        try:
            atime = client.stat(path).st_atime or mtime
        except OSError:
            atime = mtime
        client.utime(path, (atime, mtime))

    def symlink(self, target, link_path):
        try:
            self._client().symlink(target, link_path)
        except Exception as exc:  # noqa: BLE001
            raise _map_error(exc, link_path) from exc

    def readlink(self, path):
        try:
            return self._client().readlink(path)
        except (OSError, paramiko.SSHException):
            return None

    # -- data --------------------------------------------------------------------
    @_wrap
    def open_read(self, path, offset=0):
        client = self._client()
        size = client.stat(path).st_size or 0
        fh = client.open(path, "rb", bufsize=MAX_PACKET)
        if offset:
            fh.seek(offset)
        if size > offset:
            # Bounded read-ahead: unbounded prefetch makes cancelling a large
            # download drain (and buffer) the whole rest of the file.
            fh.prefetch(size, max_concurrent_requests=64)
        return _Reader(fh, path)

    @_wrap
    def open_write(self, path, offset=0, size=None):
        client = self._client()
        if offset:
            fh = client.open(path, "r+b")
            fh.seek(offset)
            fh.truncate(offset)
        else:
            fh = client.open(path, "wb")
        fh.set_pipelined(True)
        return _Writer(fh, path)

    # -- extras ------------------------------------------------------------------
    def info(self):
        rows = [("Protocol", "SFTP (SSH File Transfer Protocol) v3")]
        try:
            rows += self.ssh_info()
        except ConnectionLost:
            rows.append(("State", "Disconnected"))
        return rows

    def disk_free(self, path):
        # statvfs@openssh.com via the SFTP channel would need raw packets;
        # fall back to `df` when the server lets us run commands.
        return None
