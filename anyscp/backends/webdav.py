# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""WebDAV backend built on the standard library :mod:`http.client`.

Paths handed to this file system are absolute, *decoded* URL paths (e.g.
``/remote.php/dav/files/me/docs/a.txt``); they are percent-encoded when a
request is built and hrefs in PROPFIND replies are decoded again.

Connections are pooled (a few idle keep-alive connections per instance) so
one instance can be used from several threads, and a file object returned by
:meth:`open_read` / :meth:`open_write` owns its own HTTP connection until it
is closed.  HTTPS connections are validated through :mod:`anyscp.hostkeys`.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import os
import posixpath
import re
import select
import socket
import ssl
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urljoin, urlsplit

from .. import hostkeys
from .base import (
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

__all__ = ["WebDAVFileSystem", "parse_multistatus", "parse_challenges"]

USER_AGENT = "anySCP"
DEFAULT_TIMEOUT = 15
MAX_PASSWORD_PROMPTS = 3
POOL_SIZE = 4
STREAM_MAX_IDLE = 2.0          # reuse only very fresh connections for streamed PUTs
SPOOL_MEMORY = 8 * 1024 * 1024
DIGEST_REFRESH_AFTER = 60.0    # seconds; re-validate a digest nonce before a streamed PUT
BLOCK_SIZE = 256 * 1024

DAV = "{DAV:}"

PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/><d:displayname/>"
    b"</d:prop></d:propfind>")

QUOTA_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:quota-available-bytes/><d:quota-used-bytes/>"
    b"</d:prop></d:propfind>")

XML_TYPE = 'application/xml; charset="utf-8"'


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    return str(text).encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _norm(path: str) -> str:
    path = posixpath.normpath("/" + (path or "/").lstrip("/"))
    return "/" + path.lstrip("/")


def _os_error_text(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        return f"Cannot resolve host name ({exc.strerror})"
    if isinstance(exc, TimeoutError):
        return "The connection timed out"
    if isinstance(exc, ssl.SSLError):
        return f"TLS error: {exc.reason or exc}"
    if isinstance(exc, http.client.RemoteDisconnected):
        return "The server closed the connection"
    if isinstance(exc, http.client.HTTPException):
        return f"HTTP protocol error: {exc.__class__.__name__} {exc}".strip()
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or exc.__class__.__name__


def _sock_unusable(sock) -> bool:
    """An idle keep-alive socket that is readable has been closed by the
    server (or sent something unexpected): either way, don't reuse it."""
    try:
        if isinstance(sock, ssl.SSLSocket) and sock.pending():
            return True
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable)
    except (OSError, ValueError):
        return True


def _status_code(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\s(\d{3})(?:\s|$)", " " + text.strip())
    return int(m.group(1)) if m else None


def _http_date(text: str | None) -> float | None:
    if not text or not text.strip():
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _href_path(href: str, base: str) -> tuple[str, bool]:
    """Decoded, normalized path of an href (absolute URL, absolute path or
    relative reference) and whether it ended with a slash."""
    href = href.strip()
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        raw = parts.path or "/"
    elif href.startswith("/"):
        raw = parts.path
    else:
        raw = urlsplit(urljoin(base, href)).path
    had_slash = raw.endswith("/")
    return _norm(unquote(raw, encoding="utf-8", errors="surrogateescape")), had_slash


class DAVEntry:
    """One ``<response>`` of a multistatus document."""

    __slots__ = ("path", "had_slash", "props", "status")

    def __init__(self, path: str, had_slash: bool, props: dict, status: int | None):
        self.path = path
        self.had_slash = had_slash
        self.props = props
        self.status = status

    def text(self, name: str) -> str | None:
        el = self.props.get(DAV + name)
        return el.text if el is not None else None

    def is_collection(self) -> bool:
        rt = self.props.get(DAV + "resourcetype")
        if rt is not None:
            return rt.find(DAV + "collection") is not None
        return self.had_slash

    def to_info(self, path: str | None = None) -> FileInfo:
        path = path or self.path
        name = posixpath.basename(path.rstrip("/")) or (self.text("displayname") or "/")
        is_dir = self.is_collection()
        size = 0
        length = self.text("getcontentlength")
        if length and length.strip().isdigit() and not is_dir:
            size = int(length.strip())
        return FileInfo(name=name, path=path, is_dir=is_dir, size=size,
                        mtime=_http_date(self.text("getlastmodified")))


def parse_multistatus(data: bytes, base: str = "/") -> list[DAVEntry]:
    """Parse a ``207 Multi-Status`` body.  Only properties from ``2xx``
    propstat blocks are kept."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FSError(f"Invalid WebDAV reply from the server: {exc}") from None
    out = []
    for resp in root.iter(DAV + "response"):
        href = resp.findtext(DAV + "href")
        if not href or not href.strip():
            continue
        path, had_slash = _href_path(href, base)
        status = _status_code(resp.findtext(DAV + "status"))
        props = {}
        for propstat in resp.findall(DAV + "propstat"):
            code = _status_code(propstat.findtext(DAV + "status"))
            if code is not None and not 200 <= code < 300:
                continue
            prop = propstat.find(DAV + "prop")
            if prop is None:
                continue
            for child in prop:
                props[child.tag] = child
            if status is None:
                status = code or 200
        out.append(DAVEntry(path, had_slash, props, status))
    return out


# --------------------------------------------------------------------------
# HTTP authentication
# --------------------------------------------------------------------------

_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_PARAM_RE = re.compile(r"(" + _TOKEN + r")\s*=\s*(\"(?:[^\"\\]|\\.)*\"|[^,\s]*)")
_TOKEN_RE = re.compile(_TOKEN)


def parse_challenges(values) -> list[tuple[str, dict]]:
    """Parse ``WWW-Authenticate`` header values into ``[(scheme, params)]``
    (scheme lower case).  Handles several challenges in one header."""
    result: list[tuple[str, dict]] = []
    for value in values or []:
        i, n, current = 0, len(value), None
        while i < n:
            while i < n and value[i] in " \t,":
                i += 1
            if i >= n:
                break
            m = _PARAM_RE.match(value, i)
            if m and current is not None:
                key, raw = m.group(1).lower(), m.group(2)
                if raw.startswith('"'):
                    raw = re.sub(r"\\(.)", r"\1", raw[1:-1])
                current[1][key] = raw
                i = m.end()
                continue
            m = _TOKEN_RE.match(value, i)
            if not m:
                break
            current = (m.group(0).lower(), {})
            result.append(current)
            i = m.end()
    return result


_DIGEST_HASHES = {"MD5": hashlib.md5, "MD5-SESS": hashlib.md5,
                  "SHA-256": hashlib.sha256, "SHA-256-SESS": hashlib.sha256}


def _choose_challenge(challenges) -> tuple[str, dict] | None:
    digests = []
    for scheme, params in challenges:
        if scheme != "digest" or not params.get("nonce"):
            continue
        algo = params.get("algorithm", "MD5").upper()
        qops = [q.strip().lower() for q in params.get("qop", "").split(",") if q.strip()]
        if algo in _DIGEST_HASHES and (not qops or "auth" in qops):
            digests.append((0 if algo.startswith("SHA-256") else 1, params))
    if digests:
        digests.sort(key=lambda d: d[0])
        return "digest", digests[0][1]
    for scheme, params in challenges:
        if scheme == "basic":
            return "basic", params
    return None


def _quote_param(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Auth:
    """Credentials plus the server's last challenge (Basic or Digest)."""

    def __init__(self, user: str = ""):
        self.user = user
        self.password: str | None = None
        self.scheme: str | None = None
        self.params: dict = {}
        self.nc = 0
        self.challenge_time = 0.0
        self._lock = threading.Lock()

    def copy_for_clone(self) -> "_Auth":
        other = _Auth(self.user)
        other.password = self.password
        if self.scheme == "basic":           # digest nonces are per client
            other.scheme, other.params = self.scheme, dict(self.params)
        return other

    def set_challenge(self, scheme: str, params: dict) -> None:
        with self._lock:
            if scheme != "digest" or params.get("nonce") != self.params.get("nonce"):
                self.nc = 0
            self.scheme = scheme
            self.params = dict(params)
            self.challenge_time = time.monotonic()

    def header(self, method: str, uri: str) -> str | None:
        if self.scheme is None or self.password is None:
            return None
        if self.scheme == "basic":
            token = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8"))
            return "Basic " + token.decode("ascii")
        with self._lock:
            self.nc += 1
            nc = f"{self.nc:08x}"
            params = self.params
        algo = params.get("algorithm", "MD5").upper()
        hfunc = _DIGEST_HASHES.get(algo, hashlib.md5)

        def h(text: str) -> str:
            return hfunc(text.encode("utf-8")).hexdigest()

        realm, nonce = params.get("realm", ""), params.get("nonce", "")
        cnonce = os.urandom(8).hex()
        ha1 = h(f"{self.user}:{realm}:{self.password}")
        if algo.endswith("-SESS"):
            ha1 = h(f"{ha1}:{nonce}:{cnonce}")
        ha2 = h(f"{method}:{uri}")
        qops = [q.strip().lower() for q in params.get("qop", "").split(",") if q.strip()]
        if self.user.isascii():
            parts = [f"username={_quote_param(self.user)}"]
        else:
            parts = [f"username*=UTF-8''{quote(self.user, safe='')}"]
        parts += [f"realm={_quote_param(realm)}", f"nonce={_quote_param(nonce)}",
                  f"uri={_quote_param(uri)}"]
        if "auth" in qops:
            response = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
            parts.append(f"response={_quote_param(response)}")
            parts += ["qop=auth", f"nc={nc}", f"cnonce={_quote_param(cnonce)}"]
        else:
            parts.append(f"response={_quote_param(h(f'{ha1}:{nonce}:{ha2}'))}")
        if "algorithm" in params:
            parts.append(f"algorithm={params['algorithm']}")
        if "opaque" in params:
            parts.append(f"opaque={_quote_param(params['opaque'])}")
        return "Digest " + ", ".join(parts)


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

class _DAVConnection(http.client.HTTPConnection):
    """HTTP(S) connection; TLS goes through :func:`hostkeys.tls_wrap`."""

    def __init__(self, fs: "WebDAVFileSystem"):
        super().__init__(fs._host, fs._port, timeout=fs._timeout, blocksize=BLOCK_SIZE)
        self._fs = fs
        self.last_used = 0.0

    def connect(self):
        fs = self._fs
        if proxy_for(fs.site) is not None:
            self.sock = open_connection(fs.site, self.host, self.port, self.timeout, fs.prompter)
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        else:
            super().connect()
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        if self._fs._tls:
            fs = self._fs
            raw, self.sock = self.sock, None
            try:
                self.sock = hostkeys.tls_wrap(raw, fs._host, fs._port, fs.prompter,
                                              fs._session_trust)
            except BaseException:
                raw.close()
                raise
            fs._note_tls(self.sock)


class _Result:
    __slots__ = ("resp", "data", "conn", "target")

    def __init__(self, resp, data, conn, target):
        self.resp, self.data, self.conn, self.target = resp, data, conn, target

    @property
    def status(self) -> int:
        return self.resp.status


class _AuthState:
    __slots__ = ("prompts", "stale", "rounds")

    def __init__(self):
        self.prompts = 0
        self.stale = 0
        self.rounds = 0


# --------------------------------------------------------------------------
# File objects
# --------------------------------------------------------------------------

class _DAVReader:
    def __init__(self, fs: "WebDAVFileSystem", conn, resp, path: str):
        self._fs = fs
        self._conn = conn
        self._resp = resp
        self.path = path
        self.size = resp.length if resp is not None else 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return False

    def read(self, n: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        resp = self._resp
        if resp is None or n == 0:
            return b""
        try:
            data = resp.read() if n is None or n < 0 else resp.read(n)
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionLost(f"Download of {_clean(self.path)} failed: "
                                 f"{_os_error_text(exc)}") from None
        if not data and resp.length:          # EOF before Content-Length bytes
            raise ConnectionLost(f"Download of {_clean(self.path)} was truncated by the server")
        return data

    def readinto(self, b) -> int:
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._resp is None:
            return
        if self._resp.isclosed():
            self._fs._release(self._conn)
        else:
            self._conn.close()               # abandoned mid-body: can't reuse

    abort = close


class _DAVWriter:
    def __init__(self, fs: "WebDAVFileSystem", path: str, size: int | None):
        self._fs = fs
        self.path = path
        self.size = size
        self.written = 0
        self.closed = False
        self._conn: _DAVConnection | None = None
        self._spool = None
        if size is None:
            self._spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY)
        else:
            self._start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self.abort()

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def flush(self):
        pass

    def _start(self) -> None:
        fs = self._fs
        fs._refresh_digest(self.path)
        target = fs._target(self.path)
        for attempt in range(2):
            conn = fs._acquire(max_idle=STREAM_MAX_IDLE)
            reused = conn.sock is not None
            headers = fs._headers("PUT", target, {
                "Content-Length": str(self.size),
                "Content-Type": "application/octet-stream",
            })
            try:
                conn.putrequest("PUT", target)
                for key, value in headers.items():
                    conn.putheader(key, value)
                conn.endheaders()
            except (FSError, Cancelled):
                conn.close()
                raise
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                if reused and attempt == 0:
                    continue
                raise fs._connection_error(exc) from None
            self._conn = conn
            return

    def write(self, data) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        n = len(data)
        if self._spool is not None:
            self._spool.write(data)
            self.written += n
            return n
        if self.written + n > self.size:
            raise FSError(f"Upload of {_clean(self.path)}: more data than the announced "
                          f"{self.size} bytes")
        sock = self._conn.sock
        if sock is None:
            raise self._failure(ConnectionResetError("connection closed"))
        try:
            sock.sendall(data)
        except OSError as exc:
            raise self._failure(exc) from None
        self.written += n
        return n

    def _failure(self, exc) -> FSError:
        """Sending the body failed; the server may have sent an error reply
        (e.g. 401, 403, 413, 507) before closing.  Try to read it."""
        self.closed = True
        conn = self._conn
        err: FSError = self._fs._connection_error(exc)
        try:
            conn.sock.settimeout(2)
            resp = conn.getresponse()
            resp.read()
            if resp.status >= 300:
                if resp.status == 401:
                    self._fs._note_challenge(resp)
                err = self._fs._http_error(resp.status, resp.reason, self.path, "PUT")
        except Exception:  # noqa: BLE001 - best effort
            pass
        conn.close()
        return err

    def abort(self):
        """Give up on the upload (the server discards the partial body)."""
        if self.closed:
            return
        self.closed = True
        if self._conn is not None:
            self._conn.close()
        if self._spool is not None:
            self._spool.close()

    def close(self):
        if self.closed:
            return
        fs = self._fs
        if self._spool is not None:
            self.closed = True
            spool = self._spool
            try:
                size = spool.tell()
                spool.seek(0)
                res = fs._send("PUT", self.path, body=spool, headers={
                    "Content-Length": str(size),
                    "Content-Type": "application/octet-stream"})
                if not 200 <= res.status < 300:
                    raise fs._http_error(res.status, res.resp.reason, self.path, "PUT")
            finally:
                spool.close()
            return
        if self.written != self.size:
            self.abort()
            raise FSError(f"Upload of {_clean(self.path)} is incomplete: "
                          f"{self.written} of {self.size} bytes were written")
        self.closed = True
        conn = self._conn
        try:
            resp = conn.getresponse()
            resp.read()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise fs._connection_error(exc) from None
        fs._release(conn)
        if resp.status == 401:
            fs._note_challenge(resp)
            raise AuthFailed(f"Upload of {_clean(self.path)} was refused: authentication "
                             "required (try again)")
        if not 200 <= resp.status < 300:
            raise fs._http_error(resp.status, resp.reason, self.path, "PUT")


# --------------------------------------------------------------------------
# The file system
# --------------------------------------------------------------------------

class WebDAVFileSystem(FileSystem):
    protocol = "webdav"
    capabilities = frozenset()

    def __init__(self, site: dict | None = None, prompter=None):
        super().__init__(site, prompter)
        self._session_trust: set = set()
        self._password: str | None = None
        self._candidate: str | None = None
        self._auth = _Auth((self.site.get("user") or "").strip())
        self._auth_lock = threading.RLock()
        self._lock = threading.RLock()
        self._pool: list[_DAVConnection] = []
        self._pool_lock = threading.Lock()
        self._connected = False
        self._server = ""
        self._dav = ""
        self._tls_info: str | None = None

    # -- site helpers ------------------------------------------------------
    @property
    def _host(self) -> str:
        return (self.site.get("host") or "").strip().strip("[]")

    @property
    def _tls(self) -> bool:
        return bool(self.site.get("tls"))

    @property
    def _port(self) -> int:
        try:
            port = int(self.site.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        return port or (443 if self._tls else 80)

    @property
    def _timeout(self) -> float:
        try:
            return float(self.site.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT

    @property
    def _origin(self) -> str:
        scheme = "https" if self._tls else "http"
        host = self._host
        if ":" in host:
            host = f"[{host}]"
        default = 443 if self._tls else 80
        return f"{scheme}://{host}" + ("" if self._port == default else f":{self._port}")

    def _target(self, path: str, slash: bool = False) -> str:
        p = _norm(path)
        if slash and not p.endswith("/"):
            p += "/"
        return quote(p, safe="/", encoding="utf-8", errors="surrogateescape")

    def _log(self, message: str) -> None:
        try:
            self.prompter.log(_clean(message))
        except Exception:  # noqa: BLE001
            pass

    def _note_tls(self, sock: ssl.SSLSocket) -> None:
        cipher = sock.cipher()
        version = sock.version() or "TLS"
        self._tls_info = f"{version}, {cipher[0]} ({cipher[2]} bit)" if cipher else version

    # -- connection pool ---------------------------------------------------
    def _acquire(self, max_idle: float | None = None) -> _DAVConnection:
        with self._pool_lock:
            conn = self._pool.pop() if self._pool else None
        if conn is None:
            return _DAVConnection(self)
        if conn.sock is not None and (
                _sock_unusable(conn.sock) or
                (max_idle is not None and time.monotonic() - conn.last_used > max_idle)):
            conn.close()                     # reconnects on next use
        return conn

    def _release(self, conn: _DAVConnection) -> None:
        conn.last_used = time.monotonic()
        with self._pool_lock:
            if self._connected and len(self._pool) < POOL_SIZE:
                self._pool.append(conn)
                return
        conn.close()

    # -- errors ------------------------------------------------------------
    def _connection_error(self, exc: BaseException) -> FSError:
        if isinstance(exc, (FSError,)):
            return exc
        return ConnectionLost(f"{self._origin}: {_os_error_text(exc)}")

    @staticmethod
    def _http_error(status: int, reason: str, path: str, method: str = "") -> FSError:
        where = _clean(path)
        msg = f"{where}: {status} {reason}".rstrip()
        if status == 401:
            return AuthFailed(f"Authentication required for {where}")
        if status == 403:
            return PermissionDenied(msg)
        if status in (404, 410):
            return NotFound(msg)
        if status == 405:
            if method == "MKCOL":
                return AlreadyExists(f"{where}: already exists")
            return NotSupported(msg)
        if status == 409:
            if method in ("MKCOL", "PUT", "MOVE", "COPY"):
                return NotFound(f"{where}: the parent folder does not exist ({status} {reason})")
            return FSError(msg)
        if status == 412 and method in ("MOVE", "COPY"):
            return AlreadyExists(f"{where}: the destination already exists")
        if status == 423:
            return PermissionDenied(f"{where}: the resource is locked")
        if status == 507:
            return FSError(f"{where}: insufficient storage on the server")
        if status == 413:
            return FSError(f"{where}: the file is too large for the server")
        if status in (501,):
            return NotSupported(msg)
        return FSError(msg)

    def _check_multistatus(self, res: _Result, path: str, method: str) -> None:
        """DELETE/MOVE may answer 207 listing the members that failed."""
        for entry in parse_multistatus(res.data or b"<x/>", res.target):
            if entry.status is not None and entry.status >= 300:
                raise self._http_error(entry.status, http.client.responses.get(entry.status, ""),
                                       entry.path, method)

    # -- requests ----------------------------------------------------------
    def _headers(self, method: str, target: str, extra: dict | None = None) -> dict:
        headers = {"User-Agent": USER_AGENT}
        if extra:
            headers.update(extra)
        auth = self._auth.header(method, target)
        if auth:
            headers["Authorization"] = auth
        return headers

    def _roundtrip(self, method, target, headers, body, stream) -> _Result:
        for attempt in range(2):
            conn = self._acquire()
            reused = conn.sock is not None
            try:
                if hasattr(body, "seek"):
                    body.seek(0)
                conn.request(method, target, body=body, headers=headers)
                resp = conn.getresponse()
                if stream and 200 <= resp.status < 300:
                    return _Result(resp, None, conn, target)
                data = resp.read()
            except (FSError, Cancelled):
                conn.close()
                raise
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as exc:
                conn.close()
                if reused and attempt == 0:
                    continue                 # stale keep-alive connection
                raise self._connection_error(exc) from None
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                raise self._connection_error(exc) from None
            self._release(conn)
            return _Result(resp, data, None, target)
        raise ConnectionLost(f"{self._origin}: could not send the request")  # pragma: no cover

    def _send(self, method: str, path: str, *, headers: dict | None = None, body=None,
              slash: bool = False, stream: bool = False) -> _Result:
        """Send a (replayable) request; handles authentication challenges,
        stale keep-alive connections and same-server redirects."""
        target = self._target(path, slash)
        state = _AuthState()
        redirects = 0
        extra = dict(headers or {})
        if isinstance(body, (bytes, bytearray)):
            extra.setdefault("Content-Length", str(len(body)))
        while True:
            hdrs = self._headers(method, target, extra)
            sent_auth = "Authorization" in hdrs
            res = self._roundtrip(method, target, hdrs, body, stream)
            status = res.status
            if status == 401:
                self._unauthorized(res.resp, sent_auth, state)
                continue
            if status in (301, 302, 303, 307, 308) and redirects < 5:
                new = self._redirect_target(res.resp.getheader("Location"), target)
                if new is not None and new != target:
                    redirects += 1
                    target = new
                    continue
            if sent_auth and self._password != self._auth.password:
                self._password = self._auth.password
                self.site["_password"] = self._auth.password
            return res

    def _redirect_target(self, location: str | None, current: str) -> str | None:
        if not location:
            return None
        url = urlsplit(urljoin(self._origin + current, location))
        here = urlsplit(self._origin)
        if (url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)) != \
                (here.scheme, here.hostname, self._port):
            return None
        return (url.path or "/") + (f"?{url.query}" if url.query else "")

    def _note_challenge(self, resp) -> None:
        choice = _choose_challenge(parse_challenges(resp.headers.get_all("WWW-Authenticate")))
        if choice is not None:
            self._auth.set_challenge(*choice)

    def _unauthorized(self, resp, sent_auth: bool, state: _AuthState) -> None:
        """React to a 401: retry with a fresh nonce, with known credentials,
        or ask the user.  Raises when giving up."""
        state.rounds += 1
        if state.rounds > 10:
            raise AuthFailed("Authentication failed")
        challenges = parse_challenges(resp.headers.get_all("WWW-Authenticate"))
        choice = _choose_challenge(challenges)
        if choice is None:
            schemes = ", ".join(sorted({s.title() for s, _ in challenges})) or "none offered"
            raise AuthFailed(f"The server requires an unsupported authentication method "
                             f"({schemes})")
        with self._auth_lock:
            auth = self._auth
            scheme, params = choice
            stale = scheme == "digest" and params.get("stale", "").lower() == "true"
            auth.set_challenge(scheme, params)
            if sent_auth and stale and state.stale < 3:
                state.stale += 1
                return
            if not sent_auth and auth.password is not None and auth.user:
                return                       # now we know which scheme to use
            if not auth.user:
                user = self.prompter.ask_text("WebDAV login", f"User name for {self._origin}", "")
                if user is None:
                    raise Cancelled()
                auth.user = user.strip()
                self.site["user"] = auth.user
            if self._candidate is not None:
                auth.password, self._candidate = self._candidate, None
                return
            if state.prompts >= MAX_PASSWORD_PROMPTS:
                raise AuthFailed(f"Authentication failed for {auth.user}@{self._host}: "
                                 "wrong user name or password")
            message = f"Password for {auth.user} on {self._origin}"
            if sent_auth:
                message = f"Authentication failed.\n\n{message}"
                self._log(f"Authentication failed for {auth.user}")
            password = self.prompter.ask_password("WebDAV password", message)
            if password is None:
                raise Cancelled()
            state.prompts += 1
            auth.password = password

    def _refresh_digest(self, path: str) -> None:
        """A streamed PUT cannot be replayed after a "stale nonce" 401, so
        refresh an old digest nonce with a cheap request first."""
        auth = self._auth
        if auth.scheme == "digest" and time.monotonic() - auth.challenge_time > DIGEST_REFRESH_AFTER:
            try:
                self._propfind(self.dirname(path), 0, slash=True, body=None)
            except FSError:
                pass

    def _propfind(self, path: str, depth: int, slash: bool, body: bytes | None = PROPFIND_BODY):
        headers = {"Depth": str(depth)}
        if body is not None:
            headers["Content-Type"] = XML_TYPE
        else:
            body = b""
        res = self._send("PROPFIND", path, headers=headers, body=body, slash=slash)
        if res.status not in (200, 207):
            raise self._http_error(res.status, res.resp.reason, path, "PROPFIND")
        return res

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            if not self._host:
                raise FSError("No host name given")
            if self._candidate is None:
                self._candidate = self._password if self._password is not None \
                    else self.site.get("_password")
            self._connected = True       # lets the pool keep connections
            self._log(f"Connecting to {self._origin}...")
            try:
                res = self._send("PROPFIND", self.home(), slash=True, body=PROPFIND_BODY,
                                 headers={"Depth": "0", "Content-Type": XML_TYPE})
            except BaseException:
                self.close()
                raise
            status = res.status
            if status in (405, 501):
                self.close()
                raise NotSupported(f"{self._origin} does not support WebDAV "
                                   f"(PROPFIND: {status} {res.resp.reason})")
            if status in (403, 404):
                self._log(f"Warning: {self.home()}: {status} {res.resp.reason}")
            elif status not in (200, 207):
                self.close()
                raise self._http_error(status, res.resp.reason, self.home(), "PROPFIND")
            self._server = res.resp.getheader("Server", "") or ""
            self._dav = res.resp.getheader("DAV", "") or ""
            self._log(f"Connected to {self._origin}")
            if self._tls_info:
                self._log(f"TLS connection established: {self._tls_info}")
            if self._auth.scheme and self._auth.password is not None:
                self._log(f"Authenticated as {self._auth.user} ({self._auth.scheme.title()})")

    def close(self) -> None:
        with self._pool_lock:
            self._connected = False
            pool, self._pool = self._pool, []
        for conn in pool:
            conn.close()

    def clone(self) -> "WebDAVFileSystem":
        other = WebDAVFileSystem(self.site, self.prompter)
        other._session_trust = self._session_trust
        other._password = self._password
        other._auth = self._auth.copy_for_clone()
        other.connect()
        return other

    def keepalive(self) -> None:
        # HTTP needs no keepalive; just drop pooled sockets the server closed.
        with self._pool_lock:
            alive = []
            for conn in self._pool:
                if conn.sock is not None and _sock_unusable(conn.sock):
                    conn.close()
                alive.append(conn)
            self._pool = alive

    # -- paths -------------------------------------------------------------
    def home(self) -> str:
        return _norm(self.site.get("remote_dir") or "/")

    def realpath(self, path: str) -> str:
        return _norm(path)

    # -- queries -----------------------------------------------------------
    def listdir(self, path: str) -> list[FileInfo]:
        path = _norm(path)
        res = self._propfind(path, 1, slash=True)
        entries = parse_multistatus(res.data, res.target)
        me = next((e for e in entries if e.path == path), None)
        if me is not None and me.props and not me.is_collection():
            raise FSError(f"{_clean(path)}: Not a directory")
        children = [e for e in entries if e.path != path and
                    (e.status is None or 200 <= e.status < 300)]
        direct = [e for e in children if posixpath.dirname(e.path) == path]
        if not direct and len(children) == len(entries) and children:
            # hrefs use a prefix we don't know (rewriting proxy): the first
            # response describes the collection itself.
            direct = children[1:]
        out, seen = [], set()
        for entry in direct:
            name = posixpath.basename(entry.path)
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(entry.to_info(posixpath.join(path, name)))
        return out

    def stat(self, path: str) -> FileInfo:
        path = _norm(path)
        res = self._propfind(path, 0, slash=False)
        entries = parse_multistatus(res.data, res.target)
        if not entries:
            raise NotFound(f"{_clean(path)}: not found")
        entry = next((e for e in entries if e.path == path), entries[0])
        if entry.status is not None and entry.status >= 300:
            raise self._http_error(entry.status, "", path, "PROPFIND")
        info = entry.to_info(path)
        if path == "/":
            info.name = "/"
        return info

    def disk_free(self, path: str) -> int | None:
        try:
            res = self._propfind(path, 0, slash=True, body=QUOTA_BODY)
            for entry in parse_multistatus(res.data, res.target):
                value = entry.text("quota-available-bytes")
                if value and value.strip().lstrip("-").isdigit():
                    free = int(value.strip())
                    return free if free >= 0 else None
        except FSError:
            pass
        return None

    # -- modifications -----------------------------------------------------
    def mkdir(self, path: str) -> None:
        path = _norm(path)
        res = self._send("MKCOL", path, slash=True)
        if not 200 <= res.status < 300:
            raise self._http_error(res.status, res.resp.reason, path, "MKCOL")

    def _delete(self, path: str, slash: bool) -> None:
        res = self._send("DELETE", path, slash=slash)
        if res.status == 207:
            self._check_multistatus(res, path, "DELETE")
        elif not 200 <= res.status < 300:
            raise self._http_error(res.status, res.resp.reason, path, "DELETE")

    def remove(self, path: str) -> None:
        self._delete(_norm(path), slash=False)

    def rmdir(self, path: str) -> None:
        path = _norm(path)
        # DELETE on a collection is recursive in WebDAV; rmdir must not be.
        if self.listdir(path):
            raise FSError(f"{_clean(path)}: Directory not empty")
        self._delete(path, slash=True)

    def remove_tree(self, path: str, cancel=None, progress=None) -> None:
        path = _norm(path)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        info = self.stat(path)
        self._delete(path, slash=info.is_dir)    # one recursive DELETE
        if progress:
            progress(path)

    def rename(self, src: str, dst: str) -> None:
        src, dst = _norm(src), _norm(dst)
        res = self._send("MOVE", src, headers={
            "Destination": self._origin + self._target(dst),
            "Overwrite": "F",
        })
        if res.status == 207:
            self._check_multistatus(res, src, "MOVE")
        elif res.status == 412:
            raise AlreadyExists(f"{_clean(dst)}: the destination already exists")
        elif not 200 <= res.status < 300:
            raise self._http_error(res.status, res.resp.reason, src, "MOVE")

    # -- data --------------------------------------------------------------
    def open_read(self, path: str, offset: int = 0) -> _DAVReader:
        path = _norm(path)
        offset = int(offset or 0)
        headers = {"Range": f"bytes={offset}-"} if offset > 0 else None
        res = self._send("GET", path, headers=headers, stream=True)
        if res.conn is None:
            if res.status == 416 and offset > 0:
                return _DAVReader(self, None, None, path)       # nothing left to read
            raise self._http_error(res.status, res.resp.reason, path, "GET")
        resp, conn = res.resp, res.conn
        if offset > 0:
            start = None
            m = re.match(r"\s*bytes\s+(\d+)-", resp.getheader("Content-Range") or "")
            if m:
                start = int(m.group(1))
            if resp.status != 206 or start != offset:
                conn.close()
                raise NotSupported("The server does not support resuming downloads "
                                   "(Range requests)")
        return _DAVReader(self, conn, resp, path)

    def open_write(self, path: str, offset: int = 0, size: int | None = None) -> _DAVWriter:
        if offset and offset > 0:
            raise NotSupported("Resuming uploads is not supported by WebDAV")
        return _DAVWriter(self, _norm(path), size)

    # -- extras ------------------------------------------------------------
    def info(self) -> list[tuple[str, str]]:
        rows = [("Protocol", "WebDAV over HTTPS" if self._tls else "WebDAV"),
                ("URL", self._origin + self._target(self.home(), slash=True))]
        if self._server:
            rows.append(("Server software", self._server))
        if self._dav:
            rows.append(("DAV compliance", self._dav))
        auth = self._auth
        if auth.scheme and auth.password is not None:
            rows.append(("Authentication", f"{auth.scheme.title()} ({auth.user})"))
        else:
            rows.append(("Authentication", "None"))
        rows.append(("Encryption", self._tls_info or "None"))
        return [(label, _clean(value)) for label, value in rows]
