# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""SCP backend: file operations through shell commands, data through the
classic ``scp -f`` / ``scp -t`` protocol.  Useful for servers (routers,
embedded devices) without the SFTP subsystem."""

from __future__ import annotations

import calendar
import posixpath
import re
import shlex
import socket
import time

import paramiko

from .base import (CAP_CHMOD, CAP_CHOWN, CAP_EXEC, CAP_SHELL, CAP_SYMLINK, CAP_UTIME,
                   AlreadyExists, ConnectionLost, FileInfo, FSError, NotFound, PermissionDenied)
from .ssh import SSHFileSystem

q = shlex.quote

FIND_FORMAT = r"%y\t%Y\t%m\t%s\t%T@\t%u\t%g\t%l\t%f\0"
# BSD stat(1) (macOS, FreeBSD, ...): the same, with exact times, where find
# has no -printf.  Type and permissions, size, mtime, owner, group, link, name.
STAT_FORMAT = "%Sp%t%Lp%t%z%t%m%t%Su%t%Sg%t%Y%t%N"

_LS_RE = re.compile(
    r"^(?P<perm>[\-dlcbps][rwxsStTl\-]{9})[.+@]?\s+\d+\s+(?P<owner>\S+)\s+(?P<group>\S+)\s+"
    r"(?P<size>\d+|\d+,\s*\d+)\s+"
    r"(?:(?P<epoch>\d{9,})|(?P<iso>\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d(?:\.\d+)?)?(?: [+-]\d{4})?)|"
    r"(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<yt>\d{4}|\d{1,2}:\d\d))\s"
    r"(?P<name>.*)$")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def perm_string_to_mode(perm: str) -> int:
    mode = 0
    bits = [(1, "r", 0o400), (2, "w", 0o200), (4, "r", 0o040), (5, "w", 0o020), (7, "r", 0o004), (8, "w", 0o002)]
    for idx, ch, bit in bits:
        if perm[idx] == ch:
            mode |= bit
    for idx, xbit, special in ((3, 0o100, 0o4000), (6, 0o010, 0o2000), (9, 0o001, 0o1000)):
        c = perm[idx]
        if c in "xst":
            mode |= xbit
        if c in "sStT":
            mode |= special
    return mode


def parse_ls_line(line: str, directory: str) -> FileInfo | None:
    m = _LS_RE.match(line.rstrip("\n"))
    if not m:
        return None
    perm = m.group("perm")
    name = m.group("name")
    target = None
    if perm[0] == "l" and " -> " in name:
        name, target = name.split(" -> ", 1)
    if name in (".", ".."):
        return None
    size_text = m.group("size")
    size = 0 if "," in size_text else int(size_text)
    if m.group("epoch"):
        mtime = float(m.group("epoch"))
    elif m.group("iso"):
        iso = m.group("iso")
        fmt = "%Y-%m-%d %H:%M:%S" if iso.count(":") >= 2 else "%Y-%m-%d %H:%M"
        main = iso.split(".")[0].split(" +")[0].split(" -")[0]
        mtime = float(calendar.timegm(time.strptime(main[:19] if fmt.endswith("%S") else main[:16], fmt)))
    else:
        month = _MONTHS.get(m.group("mon").title(), 1)
        day = int(m.group("day"))
        yt = m.group("yt")
        now = time.gmtime()
        if ":" in yt:
            hour, minute = (int(x) for x in yt.split(":"))
            year = now.tm_year
            ts = calendar.timegm((year, month, day, hour, minute, 0))
            if ts > time.time() + 86400 * 2:
                ts = calendar.timegm((year - 1, month, day, hour, minute, 0))
        else:
            ts = calendar.timegm((int(yt), month, day, 0, 0, 0))
        mtime = float(ts)
    return FileInfo(name=name, path=posixpath.join(directory, name), is_dir=perm[0] == "d",
                    is_link=perm[0] == "l", size=size if perm[0] != "d" else 0, mtime=mtime,
                    mode=perm_string_to_mode(perm), owner=m.group("owner"), group=m.group("group"),
                    link_target=target)


def _error_from_stderr(stderr: str, path: str) -> FSError:
    text = stderr.strip().splitlines()[-1] if stderr.strip() else "Command failed"
    low = text.lower()
    if "no such file" in low or "not found" in low:
        return NotFound(f"No such file or directory: {path}")
    if "permission denied" in low or "operation not permitted" in low:
        return PermissionDenied(f"Permission denied: {path}")
    if "file exists" in low:
        return AlreadyExists(f"Already exists: {path}")
    return FSError(text)


class _ScpReader:
    def __init__(self, transport: paramiko.Transport, path: str, offset: int):
        self.path = path
        self.chan = transport.open_session()
        self.chan.settimeout(60)
        self.chan.exec_command(f"scp -f {q(path)}")
        self.chan.sendall(b"\0")
        header = self._read_line()
        while header.startswith(b"T"):          # timestamps (only with -p)
            self.chan.sendall(b"\0")
            header = self._read_line()
        if header[:1] in (b"\x01", b"\x02"):
            self.chan.close()
            raise _error_from_stderr(header[1:].decode("utf-8", "replace"), path)
        if not header.startswith(b"C"):
            self.chan.close()
            raise FSError(f"Unexpected SCP response: {header[:80]!r}")
        try:
            _mode, size, _name = header[1:].decode("utf-8", "replace").split(" ", 2)
            self.remaining = int(size)
        except ValueError as exc:
            self.chan.close()
            raise FSError(f"Malformed SCP header: {header[:80]!r}") from exc
        self.chan.sendall(b"\0")
        self._finished = False
        if offset:
            self._skip(offset)

    def _read_line(self) -> bytes:
        buf = bytearray()
        while True:
            ch = self.chan.recv(1)
            if not ch:
                raise ConnectionLost("SCP channel closed")
            if ch == b"\n":
                return bytes(buf)
            buf += ch
            if ch in (b"\x01", b"\x02") and len(buf) == 1:
                continue

    def _skip(self, count: int):
        while count > 0:
            data = self.read(min(count, 65536))
            if not data:
                break
            count -= len(data)

    def read(self, n=-1):
        if self.remaining <= 0:
            self._finish()
            return b""
        want = self.remaining if n is None or n < 0 else min(n, self.remaining)
        try:
            data = self.chan.recv(min(want, 65536))
        except socket.timeout as exc:
            raise ConnectionLost("SCP transfer timed out") from exc
        if not data:
            raise ConnectionLost("SCP channel closed during transfer")
        self.remaining -= len(data)
        if self.remaining <= 0:
            self._finish()
        return data

    def _finish(self):
        if self._finished:
            return
        self._finished = True
        status = self.chan.recv(1)
        if status not in (b"\0", b""):
            rest = self._read_line()
            raise FSError(rest.decode("utf-8", "replace") or "SCP error")
        self.chan.sendall(b"\0")

    def close(self):
        try:
            self.chan.close()
        except Exception:  # noqa: BLE001
            pass


class _ScpWriter:
    def __init__(self, transport: paramiko.Transport, path: str, size: int, mode: int = 0o644):
        self.path = path
        self.size = size
        self.sent = 0
        self.chan = transport.open_session()
        self.chan.settimeout(60)
        self.chan.exec_command(f"scp -t {q(path)}")
        self._ack()
        name = posixpath.basename(path).replace("\n", " ")
        self.chan.sendall(f"C{mode:04o} {size} {name}\n".encode())
        self._ack()

    def _ack(self):
        status = self.chan.recv(1)
        if status == b"\0":
            return
        if not status:
            raise ConnectionLost("SCP channel closed")
        msg = bytearray()
        while True:
            ch = self.chan.recv(1)
            if not ch or ch == b"\n":
                break
            msg += ch
        raise _error_from_stderr(msg.decode("utf-8", "replace"), self.path)

    def write(self, data):
        if self.sent + len(data) > self.size:
            raise FSError("More data than announced for SCP upload")
        self.chan.sendall(data)
        self.sent += len(data)

    def close(self):
        try:
            if self.sent != self.size:
                raise FSError("Upload incomplete")
            self.chan.sendall(b"\0")
            self._ack()
        finally:
            try:
                self.chan.close()
            except Exception:  # noqa: BLE001
                pass


class SCPFileSystem(SSHFileSystem):
    protocol = "scp"
    capabilities = frozenset({CAP_CHMOD, CAP_CHOWN, CAP_UTIME, CAP_SYMLINK, CAP_EXEC, CAP_SHELL})

    def __init__(self, site=None, prompter=None, connection=None):
        super().__init__(site, prompter, connection)
        self._home = "/"
        self._use_find = False      # GNU find -printf
        self._use_stat = False      # BSD stat -f (else ls -la, whose times are rough)

    def connect(self):
        self._open_connection()
        status, out, _err = self.exec("pwd")
        self._home = out.strip() or "/"
        status, out, _err = self.exec("find / -maxdepth 0 -printf '%y' 2>/dev/null")
        self._use_find = status == 0 and out.strip() == "d"
        if not self._use_find:
            status, out, _err = self.exec("stat -f '%m' / 2>/dev/null")
            self._use_stat = status == 0 and out.strip().isdigit()
        how = "find" if self._use_find else "stat" if self._use_stat else "ls"
        self.prompter.log(f"SCP session started, home is {self._home}; listing with {how}")

    def clone(self):
        other = SCPFileSystem(self.site, self.prompter, self.conn)
        other._home = self._home
        other._use_find = self._use_find
        other._use_stat = self._use_stat
        return other

    def _run(self, command: str, path: str) -> str:
        try:
            status, out, err = self.exec(command)
        except (paramiko.SSHException, EOFError, OSError) as exc:
            raise ConnectionLost(f"Connection lost: {exc}") from exc
        if status != 0:
            raise _error_from_stderr(err or out, path)
        return out

    def home(self):
        return self._home

    def realpath(self, path):
        return self._run(f"cd {q(path)} && pwd -P", path).strip() or path

    # -- listing -------------------------------------------------------------
    def _parse_find(self, out: str, directory: str | None) -> list[FileInfo]:
        result = []
        for record in out.split("\0"):
            if not record:
                continue
            fields = record.split("\t", 8)
            if len(fields) != 9:
                continue
            kind, target_kind, mode, size, mtime, owner, group, link, name = fields
            if directory is None:
                path = name
                name = posixpath.basename(name.rstrip("/")) or "/"
            else:
                path = posixpath.join(directory, name)
            is_link = kind == "l"
            is_dir = (target_kind if is_link else kind) == "d"
            result.append(FileInfo(
                name=name, path=path, is_dir=is_dir, is_link=is_link,
                size=0 if is_dir else int(size or 0), mtime=float(mtime or 0),
                mode=int(mode, 8) if mode else None, owner=owner, group=group,
                link_target=link or None))
        return result

    @staticmethod
    def _parse_stat(out: str) -> list[FileInfo]:
        result = []
        for record in out.splitlines():
            fields = record.split("\t", 7)
            if len(fields) != 8 or not fields[0]:
                continue
            perms, mode, size, mtime, owner, group, link, path = fields
            is_dir = perms[0] == "d"
            result.append(FileInfo(
                name=posixpath.basename(path.rstrip("/")) or "/", path=path, is_dir=is_dir,
                is_link=perms[0] == "l", size=0 if is_dir else int(size or 0), mtime=float(mtime or 0),
                mode=int(mode, 8) if mode else None, owner=owner, group=group, link_target=link or None))
        return result

    def _mark_linked_dirs(self, entries: list[FileInfo]) -> None:
        """ls -l and stat report links, not targets: ask which link targets
        are directories."""
        links = [e for e in entries if e.is_link]
        if not links:
            return
        script = "; ".join(f"[ -d {q(e.path)} ] && echo {q(e.name)}" for e in links[:200])
        try:
            _st, dirs_out, _err = self.exec(script)
        except (paramiko.SSHException, OSError):
            return
        dir_names = set(dirs_out.splitlines())
        for e in links:
            if e.name in dir_names:
                e.is_dir = True

    def listdir(self, path):
        if self._use_find:
            out = self._run(f"find {q(path)} -mindepth 1 -maxdepth 1 -printf {q(FIND_FORMAT)}", path)
            return self._parse_find(out, path)
        if self._use_stat:
            out = self._run(f"find {q(path)} -mindepth 1 -maxdepth 1 -exec stat -f {q(STAT_FORMAT)} {{}} +", path)
            entries = self._parse_stat(out)
            for e in entries:                       # (find gave them as path/name)
                e.path = posixpath.join(path, e.name)
        else:
            out = self._run(f"LC_ALL=C ls -la {q(path + '/')}", path)
            entries = [e for e in (parse_ls_line(line, path) for line in out.splitlines()) if e]
        self._mark_linked_dirs(entries)
        return entries

    def stat(self, path):
        if self._use_find:
            out = self._run(f"find {q(path)} -maxdepth 0 -printf {q(FIND_FORMAT.replace('%f', '%p'))}", path)
            items = self._parse_find(out, None)
            if not items:
                raise NotFound(f"No such file or directory: {path}")
            items[0].path = path
            return items[0]
        if self._use_stat:
            items = self._parse_stat(self._run(f"stat -f {q(STAT_FORMAT)} {q(path)}", path))
            if not items:
                raise NotFound(f"No such file or directory: {path}")
            info = items[0]
            info.path = path
            info.name = posixpath.basename(path.rstrip("/")) or "/"
            if info.is_link:
                info.is_dir = self.exec(f"[ -d {q(path)} ] && echo d")[1].strip() == "d"
            return info
        out = self._run(f"LC_ALL=C ls -lad {q(path)}", path)
        for line in out.splitlines():
            info = parse_ls_line(line, posixpath.dirname(path))
            if info:
                info.path = path
                info.name = posixpath.basename(path.rstrip("/")) or "/"
                if info.is_link:
                    st_out = self.exec(f"[ -d {q(path)} ] && echo d")[1]
                    info.is_dir = st_out.strip() == "d"
                return info
        raise NotFound(f"No such file or directory: {path}")

    # -- modifications -------------------------------------------------------
    def mkdir(self, path):
        self._run(f"mkdir {q(path)}", path)

    def remove(self, path):
        self._run(f"rm -f {q(path)}", path)

    def rmdir(self, path):
        self._run(f"rmdir {q(path)}", path)

    def rename(self, src, dst):
        self._run(f"[ ! -e {q(dst)} ] || {{ echo 'File exists' >&2; exit 1; }}; mv {q(src)} {q(dst)}", src)

    def chmod(self, path, mode):
        self._run(f"chmod {mode:04o} {q(path)}", path)

    def chown(self, path, owner, group):
        if owner:
            spec = owner + (f":{group}" if group else "")
            self._run(f"chown {q(spec)} {q(path)}", path)
        elif group:
            self._run(f"chgrp {q(group)} {q(path)}", path)

    def utime(self, path, mtime):
        stamp = time.strftime("%Y%m%d%H%M.%S", time.gmtime(mtime))
        self._run(f"TZ=UTC touch -m -t {stamp} {q(path)}", path)

    def symlink(self, target, link_path):
        self._run(f"ln -s {q(target)} {q(link_path)}", link_path)

    def readlink(self, path):
        try:
            return self._run(f"readlink {q(path)}", path).rstrip("\n") or None
        except FSError:
            return None

    def remove_tree(self, path, cancel=None, progress=None):
        self._run(f"rm -rf {q(path)}", path)
        if progress:
            progress(path)

    # -- data ------------------------------------------------------------------
    def open_read(self, path, offset=0):
        try:
            return _ScpReader(self.transport, path, offset)
        except (paramiko.SSHException, EOFError) as exc:
            raise ConnectionLost(f"Connection lost: {exc}") from exc

    def open_write(self, path, offset=0, size=None):
        if offset:
            from .base import NotSupported
            raise NotSupported("SCP cannot resume transfers")
        if size is None:
            raise FSError("SCP uploads need the file size in advance")
        try:
            return _ScpWriter(self.transport, path, size)
        except (paramiko.SSHException, EOFError) as exc:
            raise ConnectionLost(f"Connection lost: {exc}") from exc

    def info(self):
        rows = [("Protocol", "SCP (Secure Copy) with shell commands")]
        try:
            rows += self.ssh_info()
        except ConnectionLost:
            rows.append(("State", "Disconnected"))
        return rows

    def disk_free(self, path):
        try:
            out = self._run(f"df -Pk {q(path)} | tail -1", path)
            return int(out.split()[3]) * 1024
        except (FSError, ValueError, IndexError):
            return None
