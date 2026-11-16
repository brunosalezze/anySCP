# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""FTP / FTPS backend built on the standard library :mod:`ftplib`.

Supports plain FTP, explicit FTPS (``AUTH TLS``) and implicit FTPS.  Every
TLS connection (control and data) is validated through
:mod:`anyscp.hostkeys`; data connections reuse the control connection's TLS
session because many servers (vsftpd, FileZilla Server, ...) require it.

An FTP control connection can only do one thing at a time.  The app uses
:meth:`FTPFileSystem.clone` for background transfers; while a file object
returned by :meth:`open_read` / :meth:`open_write` is open, the instance
refuses other commands with an :class:`FSError` instead of corrupting the
protocol state.
"""

from __future__ import annotations

import calendar
import ftplib
import posixpath
import re
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .. import hostkeys
from .base import (
    CAP_CHMOD,
    CAP_RESUME,
    CAP_UTIME,
    AlreadyExists,
    AuthFailed,
    Cancelled,
    ConnectionLost,
    FileInfo,
    FileSystem,
    FSError,
    NotFound,
    NotSupported,
    PermissionDenied,
)
from .proxy import open_connection, proxy_for

__all__ = ["FTPFileSystem", "parse_list", "parse_list_line", "parse_mlsx_line"]

DEFAULT_TIMEOUT = 15
ANONYMOUS_PASSWORD = "anonymous@example.com"
MAX_PASSWORD_PROMPTS = 3
MAX_LINK_PROBES = 200          # CWD probes per listing to find symlinked dirs
FINAL_REPLY_TIMEOUT = 120      # waiting for "226" after a big upload
_CHUNK = 256 * 1024


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Make text with surrogate-escaped bytes safe for display."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _norm(path: str) -> str:
    path = posixpath.normpath("/" + (path or "/").lstrip("/"))
    return "/" + path.lstrip("/")


def _reply_code(resp: str) -> str:
    return resp[:3]


def _reply_text(resp: str) -> str:
    """Server reply without the numeric codes, multi-line replies joined."""
    parts = []
    for line in str(resp).splitlines():
        if re.match(r"^\d{3}[ -]", line):
            line = line[4:]
        line = line.strip()
        if line:
            parts.append(line)
    return _clean(" ".join(parts) or str(resp))


_NOT_FOUND_WORDS = ("no such", "not found", "not exist", "doesn't exist", "does not exist",
                    "cannot find", "can't find", "could not find", "nonexistent",
                    "non-existent", "no file", "unknown file")
_PERMISSION_WORDS = ("permission", "denied", "not allowed", "forbidden", "not permitted",
                     "read-only", "read only", "readonly", "access")


def _reply_error(resp, path: str = "", default=FSError) -> FSError | None:
    """Translate an FTP error reply into the FSError hierarchy.

    ``default`` is used for ambiguous 550 replies (e.g. vsftpd's "Delete
    operation failed."); pass ``None`` to get ``None`` back in that case.
    """
    resp = str(resp)
    code = _reply_code(resp)
    text = _reply_text(resp)
    low = text.lower()
    msg = f"{_clean(path)}: {text}" if path else text
    if code == "530":
        return AuthFailed(text)
    if code == "421":
        return ConnectionLost(text)
    if code in ("500", "502", "504"):
        return NotSupported(msg)
    if "not empty" in low or re.search(r"\b(is|not) a (directory|regular file|plain file)", low):
        return FSError(msg)
    if any(w in low for w in _NOT_FOUND_WORDS):
        return NotFound(msg)
    if "exist" in low:
        return AlreadyExists(msg)
    if code == "521":          # some servers: "521 Directory already exists"
        return AlreadyExists(msg)
    if any(w in low for w in _PERMISSION_WORDS) or code == "553":
        return PermissionDenied(msg)
    if code == "550":
        return default(msg) if default is not None else None
    return FSError(msg)


def _os_error_text(exc: BaseException) -> str:
    if isinstance(exc, EOFError):
        return "The server closed the connection"
    if isinstance(exc, TimeoutError):
        return "The connection timed out"
    if isinstance(exc, ssl.SSLError):
        return f"TLS error: {exc.reason or exc}"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or exc.__class__.__name__


def _tune_socket(sock: socket.socket) -> None:
    """TCP keepalive keeps NAT routers from dropping an idle control
    connection during long transfers."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 30), ("TCP_KEEPCNT", 4)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Listing parsers
# --------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_UNIX_RE = re.compile(
    r"^(?P<type>[-dlcbpsDnLw?])(?P<perm>[-rwxsStTlLA?]{9})[.+@*]?\s+"
    r"(?:(?P<middle>.*?)\s+)?"
    r"(?P<size>\d+|\d+,\s*\d+)\s+"
    r"(?:(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<ty>\d{1,2}:\d{2}(?::\d{2})?|\d{4})"
    r"|(?P<iso>\d{4}-\d{2}-\d{2})\s+(?P<isotime>\d{1,2}:\d{2}(?::\d{2})?))"
    r"\s+(?P<name>.+)$")

_DOS_RE = re.compile(
    r"^(?P<date>\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?)\s+"
    r"(?:(?P<dir><DIR>)|(?P<size>[\d,]+))\s+(?P<name>.+)$")


def _perm_bits(perm: str) -> int:
    """``rwxr-xr-x`` (with s/S/t/T) -> mode bits."""
    mode = 0
    for i, (r, w, x) in enumerate((perm[0:3], perm[3:6], perm[6:9])):
        shift = 6 - 3 * i
        if r == "r":
            mode |= 4 << shift
        if w == "w":
            mode |= 2 << shift
        if x in "xst":
            mode |= 1 << shift
        if x in "sS" and i < 2:
            mode |= 0o4000 if i == 0 else 0o2000
        if x in "tT" and i == 2:
            mode |= 0o1000
    return mode


def _timestamp(year: int, month: int, day: int, hour: int = 0, minute: int = 0,
               second: int = 0) -> float:
    return float(calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)))


def _unix_time(m: re.Match, now: datetime) -> float | None:
    try:
        if m.group("iso"):
            y, mo, d = (int(x) for x in m.group("iso").split("-"))
            hms = [int(x) for x in m.group("isotime").split(":")] + [0]
            return _timestamp(y, mo, d, hms[0], hms[1], hms[2])
        month = _MONTHS.get(m.group("mon").lower())
        if month is None:
            return None
        day = int(m.group("day"))
        ty = m.group("ty")
        if ":" in ty:
            hms = [int(x) for x in ty.split(":")] + [0]
            year = now.year
            try:
                candidate = datetime(year, month, day, hms[0], hms[1], tzinfo=timezone.utc)
            except ValueError:            # Feb 29 in a non-leap year
                candidate = None
            # "ls" shows the time (instead of the year) for files from the
            # last ~6 months, so a date in the future means last year.
            if candidate is None or candidate > now + timedelta(days=2):
                year -= 1
            return _timestamp(year, month, day, hms[0], hms[1], hms[2])
        return _timestamp(int(ty), month, day)
    except (ValueError, OverflowError):
        return None


def _dos_time(date: str, clock: str) -> float | None:
    try:
        if re.match(r"^\d{4}-", date):
            year, month, day = (int(x) for x in date.split("-"))
        else:
            month, day, year = (int(x) for x in re.split(r"[-/.]", date))
            if month > 12 and day <= 12:        # DD-MM-YY
                month, day = day, month
            if year < 100:
                year += 2000 if year < 70 else 1900
        clock = clock.strip().upper()
        ampm = clock[-2:] if clock[-2:] in ("AM", "PM") else ""
        hms = [int(x) for x in clock.rstrip("APM ").split(":")] + [0]
        hour = hms[0]
        if ampm == "PM" and hour < 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        return _timestamp(year, month, day, hour, hms[1], hms[2])
    except (ValueError, OverflowError):
        return None


def parse_list_line(line: str, dirpath: str = "/", now: datetime | None = None) -> FileInfo | None:
    """Parse one line of ``LIST`` output (Unix ``ls -l`` or DOS/IIS style).

    Returns ``None`` for lines that are not entries (``total 12``, ``.``,
    ``..``, unparsable junk).  Symlinks have ``is_link=True`` and
    ``is_dir=False``; the caller has to find out whether they point to a
    directory.
    """
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    now = now or datetime.now(timezone.utc)

    m = _UNIX_RE.match(line)
    if m:
        kind = m.group("type")
        name = m.group("name")
        target = None
        if kind in "lL" and " -> " in name:
            name, target = name.split(" -> ", 1)
        if name in (".", "..") or not name:
            return None
        tokens = (m.group("middle") or "").split()
        owner = group = ""
        if len(tokens) >= 3:
            owner, group = tokens[1], " ".join(tokens[2:])
        elif len(tokens) == 2:
            if tokens[0].isdigit() and not tokens[1].isdigit():
                owner = tokens[1]
            else:
                owner, group = tokens
        elif len(tokens) == 1 and not tokens[0].isdigit():
            owner = tokens[0]
        size_text = m.group("size")
        size = 0 if "," in size_text else int(size_text)
        name = posixpath.basename(name) or name
        return FileInfo(
            name=name, path=posixpath.join(dirpath, name),
            is_dir=kind == "d", is_link=kind in "lL",
            size=0 if kind == "d" else size,
            mtime=_unix_time(m, now), mode=_perm_bits(m.group("perm")),
            owner=owner, group=group, link_target=target)

    m = _DOS_RE.match(line)
    if m:
        name = m.group("name")
        if name in (".", ".."):
            return None
        is_dir = m.group("dir") is not None
        size = 0 if is_dir else int(m.group("size").replace(",", ""))
        return FileInfo(name=name, path=posixpath.join(dirpath, name), is_dir=is_dir,
                        size=size, mtime=_dos_time(m.group("date"), m.group("time")))
    return None


def parse_list(lines, dirpath: str = "/", now: datetime | None = None) -> list[FileInfo]:
    out = []
    seen = set()
    for line in lines:
        info = parse_list_line(line, dirpath, now)
        if info is not None and info.name not in seen:
            seen.add(info.name)
            out.append(info)
    return out


def _parse_modify(value: str) -> float | None:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\.\d+)?$", value.strip())
    if not m:
        return None
    try:
        ts = _timestamp(*(int(g) for g in m.groups()[:6]))
    except (ValueError, OverflowError):
        return None
    if m.group(7):
        ts += float(m.group(7))
    return ts


def parse_mlsx_line(line: str, dirpath: str = "/") -> FileInfo | None:
    """Parse one ``MLSD`` line (or the fact line of an ``MLST`` reply).

    Returns ``None`` for the ``cdir``/``pdir`` entries."""
    line = line.rstrip("\r\n")
    if line.startswith(" "):
        line = line[1:]
    facts_text, sep, name = line.partition(" ")
    if not sep or not name:
        return None
    facts = {}
    for item in facts_text.split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            facts[key.strip().lower()] = value
    kind = facts.get("type", "").lower()
    if kind in ("cdir", "pdir"):
        return None
    name = posixpath.basename(name.rstrip("/")) or name
    if name in (".", ".."):
        return None
    is_dir = kind == "dir"
    is_link = kind.startswith("os.unix=slink") or kind.startswith("os.unix=symlink")
    target = facts.get("unix.slink")
    if kind.startswith("os.unix=slink:"):
        target = facts["type"].split(":", 1)[1]
    size = 0
    for key in ("size", "sizd"):
        if key in facts:
            try:
                size = int(facts[key])
            except ValueError:
                pass
            break
    mode = None
    if "unix.mode" in facts:
        try:
            mode = int(facts["unix.mode"], 8) & 0o7777
        except ValueError:
            pass
    owner = facts.get("unix.owner") or facts.get("unix.ownername") or facts.get("unix.uid", "")
    group = facts.get("unix.group") or facts.get("unix.groupname") or facts.get("unix.gid", "")
    return FileInfo(
        name=name, path=posixpath.join(dirpath, name),
        is_dir=is_dir, is_link=is_link, size=0 if is_dir else size,
        mtime=_parse_modify(facts["modify"]) if "modify" in facts else None,
        mode=mode, owner=owner, group=group, link_target=target)


# --------------------------------------------------------------------------
# ftplib client with anySCP's TLS handling
# --------------------------------------------------------------------------

class _FTPClient(ftplib.FTP_TLS):
    """ftplib client whose TLS connections go through :mod:`hostkeys`.

    Used for plain FTP too (TLS is then simply never started).  Text on the
    control connection is UTF-8 with ``surrogateescape`` so that file names
    in other encodings survive a round trip.
    """

    def __init__(self, owner: "FTPFileSystem", timeout: float):
        super().__init__(timeout=timeout, encoding="utf-8")
        self._owner = owner
        self._peer_fp: str | None = None
        self._proxied = False

    # -- control connection ------------------------------------------------
    def open(self, host: str, port: int, implicit: bool) -> str:
        self.host, self.port = host, port
        if proxy_for(self._owner.site) is not None:
            sock = open_connection(self._owner.site, host, port, self.timeout, self._owner.prompter)
            self._proxied = True
        else:
            sock = socket.create_connection((host, port), self.timeout)
        _tune_socket(sock)
        try:
            if implicit:
                sock = self._wrap_control(sock)
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        self.af = sock.family
        self._make_file()
        self.welcome = self.getresp()
        return self.welcome

    def _wrap_control(self, sock):
        owner = self._owner
        tls = hostkeys.tls_wrap(sock, self.host, self.port, owner.prompter, owner._session_trust)
        self.context = tls.context
        der = tls.getpeercert(binary_form=True)
        self._peer_fp = hostkeys.cert_fingerprint(der) if der else None
        return tls

    def _make_file(self):
        self.file = self.sock.makefile("r", encoding=self.encoding, errors="surrogateescape")

    def auth(self):
        """Explicit FTPS: ``AUTH TLS`` (``AUTH SSL`` for old servers)."""
        if isinstance(self.sock, ssl.SSLSocket):
            raise ValueError("Already using TLS")
        try:
            resp = self.voidcmd("AUTH TLS")
        except ftplib.error_perm:
            resp = self.voidcmd("AUTH SSL")
        self.sock = self._wrap_control(self.sock)
        self._make_file()
        return resp

    def putline(self, line):
        if "\r" in line or "\n" in line:
            raise ValueError("an illegal newline character should not be contained")
        self.sock.sendall((line + "\r\n").encode(self.encoding, "surrogateescape"))

    # -- data connections --------------------------------------------------
    def ntransfercmd(self, cmd, rest=None):
        if self._proxied:
            conn, size = self._proxied_transfercmd(cmd, rest)
        else:
            conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            try:
                conn = self.context.wrap_socket(
                    conn, server_hostname=self.host, session=self.sock.session)
                self._check_data_cert(conn)
            except BaseException:
                conn.close()
                raise
        return conn, size

    def _proxied_transfercmd(self, cmd, rest):
        """ftplib's passive data connection, but through the proxy, to the
        server's own host name: the address a PASV reply names is the
        server's view of its network, and the control connection's peer is
        the proxy."""
        if not self.passiveserver:
            raise NotSupported("Active mode FTP does not work through a proxy: turn on passive mode")
        try:
            port = ftplib.parse229(self.sendcmd("EPSV"), (self.host, 0))[1]
        except ftplib.error_perm:
            port = ftplib.parse227(self.sendcmd("PASV"))[1]
        conn = open_connection(self._owner.site, self.host, port, self.timeout)
        try:
            if rest is not None:
                self.sendcmd(f"REST {rest}")
            resp = self.sendcmd(cmd)
            if resp[0] == "2":                  # (see ftplib.FTP.ntransfercmd)
                resp = self.getresp()
            if resp[0] != "1":
                raise ftplib.error_reply(resp)
        except BaseException:
            conn.close()
            raise
        return conn, ftplib.parse150(resp) if resp[:3] == "150" else None

    def _check_data_cert(self, conn: ssl.SSLSocket) -> None:
        der = conn.getpeercert(binary_form=True)
        if der and self._peer_fp and hostkeys.cert_fingerprint(der) == self._peer_fp:
            return
        hostkeys.verify_tls_peer(conn, self.host, self.port, self._owner.prompter,
                                 self._owner._session_trust)

    def tls_description(self) -> str | None:
        if not isinstance(self.sock, ssl.SSLSocket):
            return None
        cipher = self.sock.cipher()
        version = self.sock.version() or "TLS"
        if cipher:
            return f"{version}, {cipher[0]} ({cipher[2]} bit)"
        return version


# --------------------------------------------------------------------------
# File objects
# --------------------------------------------------------------------------

class _FTPTransfer:
    """Common part of the reader and writer: owns the data socket and the
    pending final reply on the control connection."""

    def __init__(self, fs: "FTPFileSystem", ftp: _FTPClient, conn, path: str, size: int | None):
        self._fs = fs
        self._ftp = ftp
        self._conn = conn
        self.path = path
        self.size = size
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self.abort()

    def _kill(self):
        """Close the data socket without talking to the server."""
        self.closed = True
        try:
            self._conn.close()
        except OSError:
            pass

    def _final_reply(self):
        """Read the transfer's final reply (with a generous timeout)."""
        ftp = self._ftp
        sock = ftp.sock
        try:
            sock.settimeout(max(FINAL_REPLY_TIMEOUT, ftp.timeout or 0))
        except (OSError, AttributeError):
            pass
        try:
            return ftp.voidresp()
        finally:
            try:
                sock.settimeout(ftp.timeout)
            except (OSError, AttributeError):
                pass

    def _release(self):
        if self._fs._busy is self:
            self._fs._busy = None

    def abort(self):
        """Stop the transfer early.  Never raises."""
        if self.closed:
            return
        self.closed = True
        fs = self._fs
        with fs._lock:
            try:
                try:
                    self._conn.close()
                except OSError:
                    pass
                if fs._ftp is not self._ftp:
                    return
                # Closing the data connection makes the server answer the
                # RETR/STOR with 426 (or 226 if it had finished already).
                try:
                    self._ftp.getresp()
                except (ftplib.error_temp, ftplib.error_perm, ftplib.error_reply):
                    pass
                except Exception:  # noqa: BLE001 - lost sync with the server
                    fs._mark_dead()
            finally:
                self._release()

    def readable(self):
        return False

    def writable(self):
        return False

    def seekable(self):
        return False


class _FTPReader(_FTPTransfer):
    def __init__(self, *args):
        super().__init__(*args)
        self._eof = False

    def readable(self):
        return True

    def read(self, n: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if self._eof or n == 0:
            return b""
        buf = bytearray()
        try:
            while n is None or n < 0 or len(buf) < n:
                want = _CHUNK if n is None or n < 0 else min(n - len(buf), _CHUNK)
                chunk = self._conn.recv(want)
                if not chunk:
                    self._eof = True
                    break
                buf += chunk
        except (OSError, ValueError) as exc:
            raise ConnectionLost(f"Download of {_clean(self.path)} failed: "
                                 f"{_os_error_text(exc)}") from None
        return bytes(buf)

    def readinto(self, b) -> int:
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def close(self):
        if self.closed:
            return
        if not self._eof:
            self.abort()
            return
        self.closed = True
        fs = self._fs
        with fs._lock:
            try:
                if isinstance(self._conn, ssl.SSLSocket):
                    try:
                        self._conn.unwrap()
                    except (OSError, ValueError):
                        pass
                try:
                    self._conn.close()
                except OSError:
                    pass
                if fs._ftp is not self._ftp:
                    raise ConnectionLost("The connection was closed during the transfer")
                try:
                    self._final_reply()
                except Exception as exc:  # noqa: BLE001
                    raise fs._translate(exc, self.path, FSError) from None
            finally:
                self._release()


class _FTPWriter(_FTPTransfer):
    def __init__(self, *args):
        super().__init__(*args)
        self.written = 0
        self._failed = False

    def writable(self):
        return True

    def write(self, data) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        try:
            self._conn.sendall(data)
        except (OSError, ValueError) as exc:
            raise self._fail(exc) from None
        n = len(data)
        self.written += n
        return n

    def _fail(self, exc) -> FSError:
        """The data connection broke; ask the control connection why."""
        self._failed = True
        self.closed = True
        fs = self._fs
        with fs._lock:
            try:
                try:
                    self._conn.close()
                except OSError:
                    pass
                if fs._ftp is not self._ftp:
                    return ConnectionLost("The connection was closed during the transfer")
                try:
                    resp = self._ftp.getresp()
                except (ftplib.error_temp, ftplib.error_perm) as reply:
                    return _reply_error(reply, self.path, FSError)
                except Exception:  # noqa: BLE001
                    fs._mark_dead()
                    return ConnectionLost(f"Upload of {_clean(self.path)} failed: "
                                          f"{_os_error_text(exc)}")
                return FSError(f"Upload of {_clean(self.path)} failed: {_os_error_text(exc)} "
                               f"({_reply_text(resp)})")
            finally:
                self._release()

    def flush(self):
        pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        fs = self._fs
        with fs._lock:
            try:
                if isinstance(self._conn, ssl.SSLSocket):
                    # Servers treat a missing close_notify as a failed upload.
                    try:
                        self._conn.unwrap()
                    except (OSError, ValueError):
                        pass
                try:
                    self._conn.close()
                except OSError:
                    pass
                if fs._ftp is not self._ftp:
                    raise ConnectionLost("The connection was closed during the transfer")
                try:
                    self._final_reply()
                except Exception as exc:  # noqa: BLE001
                    raise fs._translate(exc, self.path, FSError) from None
            finally:
                self._release()


# --------------------------------------------------------------------------
# The file system
# --------------------------------------------------------------------------

class FTPFileSystem(FileSystem):
    protocol = "ftp"

    def __init__(self, site: dict | None = None, prompter=None):
        super().__init__(site, prompter)
        self._ftp: _FTPClient | None = None
        self._lock = threading.RLock()
        self._session_trust: set = set()
        self._password: str | None = None
        self._busy: _FTPTransfer | None = None
        self._features: dict[str, str] = {}
        self._mlsd = False
        self._mlst = False
        self._list_cmd = "LIST"
        self._home = "/"
        self._welcome = ""
        self._syst = ""
        self._tls_info: str | None = None
        self.capabilities = frozenset()

    # -- site helpers ------------------------------------------------------
    @property
    def _host(self) -> str:
        return (self.site.get("host") or "").strip()

    @property
    def _encryption(self) -> str:
        enc = self.site.get("ftp_encryption") or "none"
        return enc if enc in ("none", "explicit", "implicit") else "none"

    @property
    def _port(self) -> int:
        try:
            port = int(self.site.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        return port or (990 if self._encryption == "implicit" else 21)

    @property
    def _timeout(self) -> float:
        try:
            return float(self.site.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT

    def _log(self, message: str) -> None:
        try:
            self.prompter.log(_clean(message))
        except Exception:  # noqa: BLE001 - logging must never break a session
            pass

    # -- error handling ----------------------------------------------------
    def _mark_dead(self) -> None:
        ftp, self._ftp = self._ftp, None
        busy, self._busy = self._busy, None
        if busy is not None:
            busy._kill()
        if ftp is not None:
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass

    def _translate(self, exc: BaseException, path: str = "", default=NotFound) -> BaseException:
        if isinstance(exc, (FSError, Cancelled)):
            return exc
        if isinstance(exc, (ftplib.error_perm, ftplib.error_temp)):
            err = _reply_error(exc, path, default or FSError)
            if isinstance(err, ConnectionLost):
                self._mark_dead()
            return err
        if isinstance(exc, ftplib.error_reply):
            return FSError(f"Unexpected server reply: {_reply_text(exc)}")
        if isinstance(exc, ftplib.Error):       # error_proto, "got more than 8192 bytes"
            self._mark_dead()
            return ConnectionLost(f"FTP protocol error: {_clean(str(exc))}")
        if isinstance(exc, (EOFError, OSError)):
            self._mark_dead()
            return ConnectionLost(_os_error_text(exc))
        if isinstance(exc, ValueError):
            return FSError(f"Invalid path {_clean(path)!r}: {exc}")
        return exc

    def _client(self) -> _FTPClient:
        if self._ftp is None:
            raise ConnectionLost("Not connected")
        return self._ftp

    @contextmanager
    def _op(self, path: str = "", default=NotFound):
        with self._lock:
            ftp = self._client()
            if self._busy is not None:
                raise FSError("The connection is busy with a file transfer")
            try:
                yield ftp
            except (FSError, Cancelled):
                raise
            except (ftplib.Error, EOFError, OSError, ValueError) as exc:
                raise self._translate(exc, path, default) from None

    # -- lifecycle ---------------------------------------------------------
    def _open(self) -> _FTPClient:
        host, port = self._host, self._port
        ftp = _FTPClient(self, self._timeout)
        ftp.set_pasv(bool(self.site.get("ftp_passive", True)))
        where = f"{host}:{port}"
        self._log(f"Connecting to {where}...")
        try:
            welcome = ftp.open(host, port, implicit=self._encryption == "implicit")
            if self._encryption == "explicit":
                try:
                    ftp.auth()
                except ftplib.error_perm as exc:
                    raise NotSupported("The server does not support explicit TLS "
                                       f"(AUTH TLS): {_reply_text(exc)}") from None
        except BaseException as exc:
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass
            if isinstance(exc, (FSError, Cancelled)):
                raise
            if isinstance(exc, socket.gaierror):
                raise ConnectionLost(f"Cannot resolve host name {host!r}: {exc.strerror}") from None
            if isinstance(exc, ssl.SSLError):
                raise ConnectionLost(f"TLS handshake with {where} failed: "
                                     f"{exc.reason or exc}") from None
            if isinstance(exc, TimeoutError):
                raise ConnectionLost(f"Connection to {where} timed out") from None
            if isinstance(exc, (ftplib.error_temp, ftplib.error_perm, ftplib.error_proto)):
                raise ConnectionLost(f"{where} refused the connection: {_reply_text(exc)}") from None
            if isinstance(exc, (OSError, EOFError)):
                raise ConnectionLost(f"Could not connect to {where}: {_os_error_text(exc)}") from None
            raise
        self._welcome = _reply_text(welcome)
        self._tls_info = ftp.tls_description()
        self._log(f"Connected to {where}")
        if self._welcome:
            self._log(self._welcome)
        if self._tls_info:
            self._log(f"TLS connection established: {self._tls_info}")
        return ftp

    def _ask_password(self, user: str, reason: str) -> str:
        message = f"Password for {user}@{self._host}"
        if reason:
            message = f"{reason}\n\n{message}"
        answer = self.prompter.ask_password("FTP password", message)
        if answer is None:
            raise Cancelled()
        return answer

    def connect(self) -> None:
        with self._lock:
            if self._ftp is not None:
                return
            if not self._host:
                raise FSError("No host name given")
            user = (self.site.get("user") or "").strip()
            anonymous = user == "" or user.lower() in ("anonymous", "ftp")
            login_user = user or "anonymous"
            if self._password is not None:
                candidate = self._password
            else:
                candidate = self.site.get("_password") or None
            if anonymous and candidate is None:
                candidate = ANONYMOUS_PASSWORD
            prompts = 0
            reason = ""
            while True:
                ftp = self._open()
                stage = "user"
                try:
                    resp = ftp.sendcmd("USER " + login_user)
                    if resp[:1] == "3":
                        stage = "pass"
                        if candidate is None:
                            candidate = self._ask_password(login_user, reason)
                            prompts += 1
                        try:
                            resp = ftp.sendcmd("PASS " + candidate)
                        except (EOFError, OSError):
                            # The server gave up on us while the user was typing.
                            ftp.close()
                            ftp = self._open()
                            ftp.sendcmd("USER " + login_user)
                            resp = ftp.sendcmd("PASS " + candidate)
                    if resp[:1] == "3":
                        raise AuthFailed("The server requires an account (ACCT) for login, "
                                         "which is not supported")
                except ftplib.error_perm as exc:
                    ftp.close()
                    code, text = _reply_code(str(exc)), _reply_text(exc)
                    if stage == "pass" and code == "530" and not anonymous:
                        self._log(f"Login incorrect: {text}")
                        if prompts >= MAX_PASSWORD_PROMPTS:
                            raise AuthFailed(f"Login incorrect: {text}") from None
                        reason = f"Login incorrect: {text}"
                        candidate = None
                        continue
                    if anonymous and code == "530":
                        raise AuthFailed(f"Anonymous login refused: {text}") from None
                    if code == "530" or stage == "pass":
                        raise AuthFailed(text) from None
                    raise _reply_error(exc) from None
                except BaseException as exc:
                    ftp.close()
                    if isinstance(exc, (FSError, Cancelled)):
                        raise
                    if isinstance(exc, (ftplib.Error, EOFError, OSError)):
                        raise ConnectionLost(f"Login failed: {self._translate(exc)}") from None
                    raise
                break

            self._password = candidate
            if not anonymous and candidate is not None:
                self.site["_password"] = candidate
            self._ftp = ftp
            self._log(f"Logged in as {login_user}")
            try:
                self._after_login(ftp)
            except BaseException as exc:
                self._mark_dead()
                if isinstance(exc, (ftplib.Error, EOFError, OSError, ValueError)):
                    raise self._translate(exc) from None
                raise

    def _after_login(self, ftp: _FTPClient) -> None:
        self._features = self._read_features(ftp)
        feats = self._features
        if "UTF8" in feats:
            try:
                ftp.sendcmd("OPTS UTF8 ON")
            except ftplib.Error:
                pass
        if self._encryption != "none":
            try:
                ftp.prot_p()
            except (ftplib.error_perm, ftplib.error_temp) as exc:
                raise FSError("The server refused to encrypt data connections "
                              f"(PROT P): {_reply_text(exc)}") from None
        ftp.voidcmd("TYPE I")
        try:
            self._syst = _reply_text(ftp.sendcmd("SYST"))
        except (ftplib.error_perm, ftplib.error_temp):
            self._syst = ""
        try:
            self._home = _norm(ftp.pwd() or "/")
        except (ftplib.error_perm, ftplib.error_temp, ftplib.error_reply):
            self._home = "/"

        self._mlst = self._mlsd = "MLST" in feats or "MLSD" in feats
        if self._mlst:
            wanted = ["type", "size", "modify", "unix.mode", "unix.owner", "unix.group",
                      "unix.ownername", "unix.groupname", "unix.uid", "unix.gid", "unix.slink"]
            offered = [f.rstrip("*").lower() for f in feats.get("MLST", "").split(";") if f]
            facts = [f for f in wanted if f in offered] if offered else []
            if facts:
                try:
                    ftp.sendcmd("OPTS MLST " + ";".join(facts) + ";")
                except ftplib.Error:
                    pass
            self._log("Using MLSD for directory listings")
        else:
            self._log("Using LIST for directory listings")
        self._list_cmd = "LIST -a" if "UNIX" in self._syst.upper() else "LIST"

        caps = set()
        rest = feats.get("REST", "").upper()
        if "REST" in feats and ("STREAM" in rest or not rest):
            caps.add(CAP_RESUME)
        if "MFMT" in feats:
            caps.add(CAP_UTIME)
        if "UNIX" in self._syst.upper() or any(k.startswith("UNIX.") for k in
                                               feats.get("MLST", "").lower().split(";")):
            caps.add(CAP_CHMOD)
        self.capabilities = frozenset(caps)

    @staticmethod
    def _read_features(ftp: _FTPClient) -> dict[str, str]:
        try:
            resp = ftp.sendcmd("FEAT")
        except ftplib.Error:
            return {}
        feats = {}
        for line in resp.splitlines()[1:]:
            if re.match(r"^\d{3}[ -]", line):
                continue
            line = line.strip()
            if not line:
                continue
            key, _, value = line.partition(" ")
            feats[key.upper()] = value.strip()
        return feats

    def close(self) -> None:
        with self._lock:
            ftp, self._ftp = self._ftp, None
            busy, self._busy = self._busy, None
            if busy is not None:
                busy._kill()
            if ftp is None:
                return
            try:
                if busy is None:
                    ftp.sock.settimeout(min(3.0, self._timeout))
                    ftp.quit()
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    ftp.close()
                except Exception:  # noqa: BLE001
                    pass

    def clone(self) -> "FTPFileSystem":
        other = FTPFileSystem(self.site, self.prompter)
        other._session_trust = self._session_trust
        other._password = self._password
        other.connect()
        return other

    def keepalive(self) -> None:
        if not self._lock.acquire(blocking=False):
            return                   # in use right now, so it is alive
        try:
            if self._ftp is None or self._busy is not None:
                return
            with self._op() as ftp:
                ftp.voidcmd("NOOP")
        finally:
            self._lock.release()

    # -- paths -------------------------------------------------------------
    def home(self) -> str:
        return self._home

    def realpath(self, path: str) -> str:
        return _norm(path)

    # -- listing -----------------------------------------------------------
    def _data_lines(self, ftp: _FTPClient, cmd: str) -> list[str]:
        conn = ftp.transfercmd(cmd)
        chunks = []
        try:
            while True:
                data = conn.recv(_CHUNK)
                if not data:
                    break
                chunks.append(data)
            if isinstance(conn, ssl.SSLSocket):
                try:
                    conn.unwrap()
                except (OSError, ValueError):
                    pass
        finally:
            conn.close()
        ftp.voidresp()
        text = b"".join(chunks).decode("utf-8", "surrogateescape")
        return [line.rstrip("\r") for line in text.split("\n") if line.strip("\r")]

    def _list_lines(self, ftp: _FTPClient) -> list[str]:
        """LIST the current directory (after a successful CWD)."""
        def run(cmd):
            try:
                return self._data_lines(ftp, cmd)
            except (ftplib.error_perm, ftplib.error_temp) as exc:
                low = _reply_text(exc).lower()
                if "no files" in low or "empty" in low:
                    return []
                raise
        try:
            return run(self._list_cmd)
        except ftplib.error_perm:
            if self._list_cmd == "LIST":
                raise
            self._list_cmd = "LIST"       # server does not like "-a"
            return run("LIST")

    def _list(self, ftp: _FTPClient, path: str) -> list[FileInfo]:
        if self._mlsd:
            try:
                lines = self._data_lines(ftp, "MLSD " + path)
            except ftplib.error_perm as exc:
                if _reply_code(str(exc)) not in ("500", "502", "504"):
                    raise
                self._mlsd = False
                self._log("MLSD is not supported after all; using LIST")
            else:
                out, seen = [], set()
                for line in lines:
                    info = parse_mlsx_line(line, path)
                    if info is not None and info.name not in seen:
                        seen.add(info.name)
                        out.append(info)
                return out
        ftp.cwd(path)
        return parse_list(self._list_lines(ftp), path)

    def _resolve_links(self, ftp: _FTPClient, entries: list[FileInfo]) -> None:
        """Find out which symlinks point to directories (CWD probe)."""
        probes = 0
        for entry in entries:
            if not entry.is_link or entry.is_dir:
                continue
            if probes >= MAX_LINK_PROBES:
                break
            probes += 1
            try:
                ftp.cwd(entry.path)
                entry.is_dir = True
                entry.size = 0
            except (ftplib.error_perm, ftplib.error_temp):
                pass

    def listdir(self, path: str) -> list[FileInfo]:
        path = _norm(path)
        with self._op(path, NotFound) as ftp:
            entries = self._list(ftp, path)
            self._resolve_links(ftp, entries)
            return entries

    # -- stat --------------------------------------------------------------
    def _stat_by_listing(self, ftp: _FTPClient, path: str) -> FileInfo:
        parent, name = self.dirname(path), self.basename(path)
        try:
            entries = self._list(ftp, parent)
        except (ftplib.error_perm, ftplib.error_temp):
            entries = None
        if entries is not None:
            for entry in entries:
                if entry.name == name:
                    self._resolve_links(ftp, [entry])
                    return entry
            if not (name.startswith(".") and self._list_cmd == "LIST" and not self._mlsd):
                raise NotFound(f"{_clean(path)}: No such file or directory")
        # Parent not listable (or a dot file hidden by LIST): probe directly.
        try:
            ftp.cwd(path)
            return FileInfo(name=name, path=path, is_dir=True)
        except (ftplib.error_perm, ftplib.error_temp):
            pass
        try:
            size = ftp.size(path)
        except (ftplib.error_perm, ftplib.error_temp, ftplib.error_reply, ValueError):
            raise NotFound(f"{_clean(path)}: No such file or directory") from None
        mtime = None
        try:
            resp = ftp.sendcmd("MDTM " + path)
            mtime = _parse_modify(resp[4:].strip())
        except (ftplib.error_perm, ftplib.error_temp):
            pass
        return FileInfo(name=name, path=path, size=size or 0, mtime=mtime)

    def stat(self, path: str) -> FileInfo:
        path = _norm(path)
        if path == "/":
            return FileInfo(name="/", path="/", is_dir=True)
        with self._op(path, NotFound) as ftp:
            if self._mlst:
                try:
                    resp = ftp.sendcmd("MLST " + path)
                except ftplib.error_perm as exc:
                    if _reply_code(str(exc)) not in ("500", "502", "504"):
                        raise
                    self._mlst = False
                else:
                    lines = resp.splitlines()
                    fact_lines = [ln for ln in lines[1:] if not re.match(r"^\d{3}[ -]", ln)]
                    if not fact_lines and len(lines) > 1:
                        fact_lines = lines[1:-1]
                    info = parse_mlsx_line(fact_lines[0], self.dirname(path)) if fact_lines else None
                    if info is not None:
                        info.name = self.basename(path)
                        info.path = path
                        self._resolve_links(ftp, [info])
                        return info
            return self._stat_by_listing(ftp, path)

    def stat_nofollow(self, path: str) -> FileInfo:
        path = _norm(path)
        if path == "/":
            return FileInfo(name="/", path="/", is_dir=True)
        with self._op(path, NotFound) as ftp:
            return self._stat_by_listing(ftp, path)

    def readlink(self, path: str) -> str | None:
        info = self.stat_nofollow(path)
        return info.link_target if info.is_link else None

    def _exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except NotFound:
            return False

    # -- modifications -----------------------------------------------------
    def mkdir(self, path: str) -> None:
        path = _norm(path)
        with self._op(path, PermissionDenied) as ftp:
            try:
                ftp.sendcmd("MKD " + path)
            except ftplib.error_perm as exc:
                err = _reply_error(exc, path, None)
                if err is None:
                    err = (AlreadyExists if self._exists(path) else PermissionDenied)(
                        f"{_clean(path)}: {_reply_text(exc)}")
                raise err from None

    def remove(self, path: str) -> None:
        path = _norm(path)
        with self._op(path, NotFound) as ftp:
            try:
                ftp.voidcmd("DELE " + path)
            except ftplib.error_perm as exc:
                err = _reply_error(exc, path, None)
                if err is None:
                    err = (PermissionDenied if self._exists(path) else NotFound)(
                        f"{_clean(path)}: {_reply_text(exc)}")
                raise err from None

    def rmdir(self, path: str) -> None:
        path = _norm(path)
        with self._op(path, NotFound) as ftp:
            try:
                ftp.voidcmd("RMD " + path)
            except ftplib.error_perm as exc:
                err = _reply_error(exc, path, None)
                if err is None:
                    err = (FSError if self._exists(path) else NotFound)(
                        f"{_clean(path)}: {_reply_text(exc)}")
                raise err from None

    def rename(self, src: str, dst: str) -> None:
        src, dst = _norm(src), _norm(dst)
        with self._op(src, NotFound) as ftp:
            resp = ftp.sendcmd("RNFR " + src)
            if resp[:1] != "3":
                raise FSError(f"Unexpected reply to RNFR: {_reply_text(resp)}")
            try:
                ftp.voidcmd("RNTO " + dst)
            except ftplib.error_perm as exc:
                err = _reply_error(exc, dst, None)
                if err is None:
                    err = (AlreadyExists if self._exists(dst) else PermissionDenied)(
                        f"{_clean(dst)}: {_reply_text(exc)}")
                raise err from None

    def chmod(self, path: str, mode: int) -> None:
        path = _norm(path)
        with self._op(path, PermissionDenied) as ftp:
            try:
                ftp.voidcmd(f"SITE CHMOD {mode & 0o7777:03o} {path}")
            except ftplib.error_perm as exc:
                if _reply_code(str(exc)) in ("500", "502", "504"):
                    self.capabilities = self.capabilities - {CAP_CHMOD}
                    raise NotSupported("The server does not support changing permissions "
                                       f"(SITE CHMOD): {_reply_text(exc)}") from None
                raise

    def utime(self, path: str, mtime: float) -> None:
        path = _norm(path)
        if "MFMT" not in self._features:
            raise NotSupported("The server does not support setting timestamps (MFMT)")
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(mtime))
        with self._op(path, NotFound) as ftp:
            try:
                ftp.voidcmd(f"MFMT {stamp} {path}")
            except ftplib.error_perm as exc:
                if _reply_code(str(exc)) in ("500", "502", "504"):
                    self.capabilities = self.capabilities - {CAP_UTIME}
                raise

    def remove_tree(self, path: str, cancel=None, progress=None) -> None:
        # Uses the listing's own type information instead of one stat() per
        # entry (each FTP stat may cost a directory listing).
        self._remove_tree(self.stat_nofollow(path), cancel, progress)

    def _remove_tree(self, info: FileInfo, cancel, progress) -> None:
        if not info.is_dir or info.is_link:
            self.remove(info.path)
        else:
            for entry in self.listdir(info.path):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                self._remove_tree(entry, cancel, progress)
            self.rmdir(info.path)
        if progress:
            progress(info.path)

    # -- data --------------------------------------------------------------
    def open_read(self, path: str, offset: int = 0) -> _FTPReader:
        path = _norm(path)
        offset = int(offset or 0)
        if offset > 0 and CAP_RESUME not in self.capabilities:
            raise NotSupported("The server does not support resuming transfers (REST)")
        with self._op(path, NotFound) as ftp:
            conn, size = ftp.ntransfercmd("RETR " + path, rest=offset or None)
            reader = _FTPReader(self, ftp, conn, path, size)
            self._busy = reader
            return reader

    def open_write(self, path: str, offset: int = 0, size: int | None = None) -> _FTPWriter:
        path = _norm(path)
        offset = int(offset or 0)
        with self._op(path, PermissionDenied) as ftp:
            cmd, rest = "STOR " + path, None
            if offset > 0:
                remote_size = None
                if "SIZE" in self._features:
                    try:
                        remote_size = ftp.size(path)
                    except (ftplib.error_perm, ftplib.error_temp, ftplib.error_reply, ValueError):
                        remote_size = None
                if remote_size == offset or (remote_size is None and
                                             CAP_RESUME not in self.capabilities):
                    cmd = "APPE " + path
                elif CAP_RESUME in self.capabilities:
                    rest = offset
                else:
                    raise NotSupported(f"Cannot resume the upload of {_clean(path)}: the remote "
                                       f"file has {remote_size} bytes, not {offset}")
            conn, _ = ftp.ntransfercmd(cmd, rest=rest)
            writer = _FTPWriter(self, ftp, conn, path, size)
            self._busy = writer
            return writer

    # -- extras ------------------------------------------------------------
    def info(self) -> list[tuple[str, str]]:
        enc = self._encryption
        proto = {"none": "FTP", "explicit": "FTPS (explicit TLS)",
                 "implicit": "FTPS (implicit TLS)"}[enc]
        rows = [("Protocol", proto), ("Server", f"{self._host}:{self._port}")]
        if self._welcome:
            rows.append(("Welcome message", self._welcome))
        if self._syst:
            rows.append(("System", self._syst))
        rows.append(("Encryption", self._tls_info or "None"))
        mode = "Passive" if self.site.get("ftp_passive", True) else "Active"
        if enc != "none":
            mode += ", encrypted"
        rows.append(("Data connections", mode))
        rows.append(("Directory listing", "MLSD" if self._mlsd else self._list_cmd))
        if self._features:
            feats = []
            for key in sorted(self._features):
                value = self._features[key]
                feats.append(f"{key} {value}".strip() if key in ("REST", "AUTH") else key)
            rows.append(("Features", ", ".join(feats)))
        rows.append(("Encoding", "UTF-8" if "UTF8" in self._features else "UTF-8 (assumed)"))
        return [(label, _clean(value)) for label, value in rows]
