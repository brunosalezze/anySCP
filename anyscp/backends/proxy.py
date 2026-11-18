# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Connections through a proxy, set per site.

SOCKS5 (RFC 1928, with the user name/password method of RFC 1929),
SOCKS4 and SOCKS4a, and HTTP proxies (``CONNECT``).  Host names are passed
to the proxy unresolved where the protocol allows it (SOCKS5, SOCKS4a,
HTTP), so names that only the proxy's side of the network knows work too.

Site fields: ``proxy_type`` (one of :data:`PROXY_TYPES`), ``proxy_host``,
``proxy_port``, ``proxy_user``; the password comes in ``_proxy_password``
(the session reads it from the password store).
"""

from __future__ import annotations

import base64
import ipaddress
import socket
import struct
from dataclasses import dataclass

from .base import FSError

PROXY_TYPES = ("none", "socks5", "socks4", "http")
PROXY_LABELS = ("None", "SOCKS5", "SOCKS4", "HTTP (CONNECT)")
DEFAULT_PORTS = {"socks5": 1080, "socks4": 1080, "http": 8080}

SOCKS5_ERRORS = {
    1: "general failure", 2: "not allowed by the proxy's rules", 3: "network unreachable",
    4: "host unreachable", 5: "connection refused", 6: "timed out", 7: "command not supported",
    8: "address type not supported",
}
SOCKS4_ERRORS = {91: "rejected or failed", 92: "no identd on the client", 93: "identd user mismatch"}


class ProxyError(FSError):
    """The proxy could not be reached, refused us or could not connect."""


@dataclass
class Proxy:
    kind: str                   # socks5 | socks4 | http
    host: str
    port: int
    user: str = ""
    password: str | None = None

    @property
    def label(self) -> str:
        name = PROXY_LABELS[PROXY_TYPES.index(self.kind)].split(" ")[0]
        return f"{name} proxy {self.host}:{self.port}"

    def connect(self, host: str, port: int, timeout: float | None) -> socket.socket:
        """A socket connected to host:port through the proxy."""
        try:
            sock = socket.create_connection((self.host, self.port), timeout)
        except socket.gaierror as exc:
            raise ProxyError(f"Proxy “{self.host}” could not be found ({exc.strerror})") from exc
        except TimeoutError as exc:
            raise ProxyError(f"Connection to the proxy {self.host}:{self.port} timed out") from exc
        except OSError as exc:
            raise ProxyError(f"Could not connect to the proxy {self.host}:{self.port}: "
                             f"{exc.strerror or exc}") from exc
        return self.handshake(sock, host, port)

    def handshake(self, sock, host: str, port: int):
        """Ask the proxy on the other end of ``sock`` (a socket, or an SSH
        channel to the proxy) to connect to host:port; returns ``sock``,
        closed if that fails."""
        try:
            {"socks5": self._socks5, "socks4": self._socks4, "http": self._http}[self.kind](sock, host, port)
        except TimeoutError as exc:
            sock.close()
            raise ProxyError(f"The {self.label} did not answer in time") from exc
        except OSError as exc:
            sock.close()
            raise ProxyError(f"The {self.label} closed the connection: {exc.strerror or exc}") from exc
        except BaseException:
            sock.close()
            raise
        return sock

    # -- SOCKS5 --------------------------------------------------------------
    def _socks5(self, sock, host, port):
        methods = b"\x00\x02" if self.user else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        version, method = _recv(sock, 2)
        if version != 5:
            raise ProxyError(f"{self.host}:{self.port} is not a SOCKS5 proxy")
        if method == 0xFF:
            raise ProxyError("The SOCKS5 proxy refused every way of logging in"
                             + ("" if self.user else ": it probably needs a user name and password"))
        if method == 2:
            if not self.user:
                raise ProxyError("The SOCKS5 proxy needs a user name and password")
            user, password = self.user.encode(), (self.password or "").encode()
            if len(user) > 255 or len(password) > 255:
                raise ProxyError("SOCKS5 user names and passwords can have at most 255 bytes")
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            _version, status = _recv(sock, 2)
            if status != 0:
                raise ProxyError("The SOCKS5 proxy rejected the user name or password")
        elif method != 0:
            raise ProxyError(f"The SOCKS5 proxy asked for an unsupported login method ({method})")
        sock.sendall(b"\x05\x01\x00" + _socks5_address(host) + struct.pack(">H", port))
        _version, reply, _reserved, address_type = _recv(sock, 4)
        if reply != 0:
            raise ProxyError(f"The SOCKS5 proxy could not connect to {host}:{port}: "
                             f"{SOCKS5_ERRORS.get(reply, f'error {reply}')}")
        length = {1: 4, 4: 16}.get(address_type)
        if length is None:
            length = _recv(sock, 1)[0]
        _recv(sock, length + 2)                 # the address the proxy bound

    # -- SOCKS4 / SOCKS4a ----------------------------------------------------
    def _socks4(self, sock, host, port):
        try:
            address, name = ipaddress.ip_address(host), b""
            if address.version != 4:
                raise ProxyError("SOCKS4 proxies cannot connect to IPv6 addresses; use SOCKS5")
            address = address.packed
        except ValueError:
            address, name = b"\x00\x00\x00\x01", host.encode("idna") + b"\x00"   # SOCKS4a
        sock.sendall(b"\x04\x01" + struct.pack(">H", port) + address + self.user.encode() + b"\x00" + name)
        reply = _recv(sock, 8)
        if reply[1] != 90:
            raise ProxyError(f"The SOCKS4 proxy could not connect to {host}:{port}: "
                             f"{SOCKS4_ERRORS.get(reply[1], f'error {reply[1]}')}")

    # -- HTTP CONNECT --------------------------------------------------------
    def _http(self, sock, host, port):
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if self.user:
            token = base64.b64encode(f"{self.user}:{self.password or ''}".encode()).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {token}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        head = bytearray()
        while not head.endswith(b"\r\n\r\n"):  # byte by byte: what follows belongs to the server
            chunk = sock.recv(1)
            if not chunk:
                raise ProxyError("The HTTP proxy closed the connection")
            head += chunk
            if len(head) > 32768:
                raise ProxyError("The HTTP proxy sent an invalid answer")
        status = head.split(b"\r\n", 1)[0].decode("latin-1").strip()
        parts = status.split(" ", 2)
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if code == 407:
            raise ProxyError("The HTTP proxy rejected the user name or password" if self.user
                             else "The HTTP proxy needs a user name and password")
        if not 200 <= code < 300:
            raise ProxyError(f"The HTTP proxy did not connect to {target}: {status}")


def _recv(sock, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ProxyError("The proxy closed the connection")
        data += chunk
    return bytes(data)


def _socks5_address(host: str) -> bytes:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        name = host.encode("idna")
        if len(name) > 255:
            raise ProxyError(f"Host name too long for SOCKS5: {host}") from None
        return b"\x03" + bytes([len(name)]) + name
    return (b"\x01" if address.version == 4 else b"\x04") + address.packed


def proxy_for(site: dict) -> Proxy | None:
    """The proxy a site's connections go through, or None."""
    kind = site.get("proxy_type") or "none"
    host = (site.get("proxy_host") or "").strip()
    if kind not in DEFAULT_PORTS or not host:
        return None
    try:
        port = int(site.get("proxy_port") or 0) or DEFAULT_PORTS[kind]
    except (TypeError, ValueError):
        port = DEFAULT_PORTS[kind]
    return Proxy(kind, host, port, (site.get("proxy_user") or "").strip(), site.get("_proxy_password"))


def ask_proxy_password(prompter, proxy: Proxy) -> str:
    message = f"Password of {proxy.user} for the {proxy.label}"
    try:
        password = prompter.ask_password("Proxy password", message, remember=False)
    except TypeError:                           # (a prompter without that option)
        password = prompter.ask_password("Proxy password", message)
    if password is None:
        from .base import Cancelled
        raise Cancelled()
    return password


def ready_proxy(site: dict, prompter=None) -> Proxy | None:
    """The site's proxy with its password (asked now if it needs one and
    none is known), or None."""
    proxy = proxy_for(site)
    if proxy is not None and proxy.user and proxy.password is None and prompter is not None:
        proxy.password = ask_proxy_password(prompter, proxy)
        site["_proxy_password"] = proxy.password        # (for the clones' connections)
    return proxy


def open_connection(site: dict, host: str, port: int, timeout: float | None, prompter=None) -> socket.socket:
    """A TCP connection to host:port for ``site``: through its proxy if it
    has one, else direct.  Errors become :class:`FSError` with a readable text."""
    proxy = ready_proxy(site, prompter)
    if proxy is not None:
        if prompter is not None:
            prompter.log(f"Connecting to {host}:{port} through the {proxy.label} …")
        return proxy.connect(host, port, timeout)
    try:
        return socket.create_connection((host, port), timeout=timeout)
    except socket.gaierror as exc:
        raise FSError(f"Host “{host}” could not be found ({exc.strerror})") from exc
    except TimeoutError as exc:
        raise FSError(f"Connection to {host}:{port} timed out") from exc
    except OSError as exc:
        raise FSError(f"Could not connect to {host}:{port}: {exc.strerror or exc}") from exc
