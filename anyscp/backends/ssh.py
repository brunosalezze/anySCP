# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Shared SSH connection logic (transport, host key check, authentication)."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import paramiko

from .. import hostkeys
from .base import AuthFailed, Cancelled, ConnectionLost, FileSystem, FSError

DEFAULT_KEYS = ("id_ed25519", "id_ecdsa", "id_rsa")


class SSHConnection:
    """An authenticated SSH transport shared by an SFTP/SCP file system
    and all of its clones (background transfers open extra channels on the
    same transport instead of logging in again)."""

    def __init__(self, transport: paramiko.Transport, user: str):
        self.transport = transport
        self.user = user
        self.jumps: list[SSHConnection] = []    # jump hosts this connection goes through
        self.refs = 0
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            self.refs += 1
        return self

    def release(self):
        with self.lock:
            self.refs -= 1
            last = self.refs <= 0
        if last:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001
                pass
            for jump in reversed(self.jumps):
                jump.release()

    @property
    def active(self) -> bool:
        return self.transport.is_active()


def connect_ssh(site: dict, prompter, password_holder: dict) -> SSHConnection:
    """Open, verify and authenticate an SSH transport for ``site``.

    Goes through the site's jump hosts first, if it has any (``_jumps``:
    site dicts the session prepares from ``jump_hosts``; or ``ProxyJump`` in
    ``~/.ssh/config``), each reached through the one before, like ``ssh -J``.
    The first connection goes through the site's proxy, if it has one.

    ``password_holder`` receives ``"password"`` when a password worked, so
    the session can offer to remember it."""
    if not (site.get("host") or "").strip():
        raise FSError("No host name given")
    from .sshconfig import resolve
    resolved = resolve(site)                       # ~/.ssh/config: aliases, User, Port, …
    timeout = float(site.get("timeout") or 15)
    if resolved.identity_files and not site.get("key_file"):
        site = dict(site, _config_keys=resolved.identity_files)

    hops = list(site.get("_jumps") or [])
    if not hops and resolved.jump_hosts:
        hops = [hop_site(spec, site) for spec in resolved.jump_hosts]
    chain = [(hop, resolve(hop)) for hop in hops] + [(site, resolved)]

    jumps: list[SSHConnection] = []
    try:
        sock = _first_socket(site, chain[0][1], timeout, prompter)
        for n, (hop, hop_resolved) in enumerate(chain[:-1]):
            if hop_resolved.identity_files and not hop.get("key_file"):
                hop = dict(hop, _config_keys=hop_resolved.identity_files)
            conn = _start(sock, hop, hop_resolved, _HopPrompter(prompter), {}, timeout)
            jumps.append(conn)
            target = chain[n + 1][1]
            prompter.log(f"Opening a tunnel through {hop_resolved.hostname} to "
                         f"{target.hostname}:{target.port} …")
            try:
                sock = conn.transport.open_channel("direct-tcpip", (target.hostname, target.port),
                                                   ("127.0.0.1", 0), timeout=timeout)
            except (paramiko.SSHException, OSError, EOFError) as exc:
                raise FSError(f"The jump host {hop_resolved.hostname} could not connect to "
                              f"{target.hostname}:{target.port}: {exc}") from exc
        conn = _start(sock, site, resolved, prompter, password_holder, timeout)
    except BaseException:
        for jump in reversed(jumps):
            jump.release()
        raise
    conn.jumps = [jump.acquire() for jump in jumps]
    return conn


def _first_socket(site, first, timeout, prompter):
    """The connection to the first host: through its ProxyCommand (ssh
    config) or the site's proxy, else direct."""
    host, port = first.hostname, first.port
    if first.proxy_command:
        prompter.log(f"Connecting to {host}:{port} via proxy: {first.proxy_command}")
        try:
            return paramiko.ProxyCommand(first.proxy_command)
        except (OSError, paramiko.SSHException) as exc:
            raise FSError(f"Proxy command failed: {exc}") from exc
    from .proxy import open_connection, proxy_for
    if proxy_for(site) is None:
        prompter.log(f"Connecting to {host}:{port} …")
    return open_connection(site, host, port, timeout, prompter)


def _start(sock, site, resolved, prompter, password_holder, timeout) -> SSHConnection:
    """SSH over ``sock``: negotiate, check the host key, log in."""
    host, port = resolved.hostname, resolved.port
    transport = paramiko.Transport(sock)
    transport.banner_timeout = timeout
    transport.auth_timeout = timeout
    transport.use_compression(bool(site.get("compression")))
    try:
        transport.start_client(timeout=timeout)
    except (paramiko.SSHException, EOFError, OSError) as exc:
        transport.close()
        raise FSError(f"SSH negotiation with {host} failed: {exc or exc.__class__.__name__}") from exc
    prompter.log(f"Server version: {transport.remote_version}")

    try:
        _verify_host_key(transport, host, port, prompter)
        user = resolved.user
        if not user:
            user = prompter.ask_text("Username", f"Log in to {host} as:", os.environ.get("USER", ""))
            if not user:
                raise Cancelled()
        _authenticate(transport, site, user, prompter, password_holder)
    except BaseException:
        transport.close()
        raise

    keepalive = int(site.get("keepalive") or 0)
    if keepalive > 0:
        transport.set_keepalive(keepalive)
    if isinstance(sock, socket.socket):
        sock.settimeout(None)
    elif isinstance(sock, paramiko.Channel):
        sock.settimeout(None)
    prompter.log(f"Authenticated as {user}" + (f" on {host}" if site.get("_jump_label") else ""))
    return SSHConnection(transport, user)


def hop_site(spec: str, site: dict) -> dict:
    """A jump host given as ``[user@]host[:port]``: it logs in like ``site``
    (its key file, agent, default keys or a password prompt)."""
    from .sshconfig import parse_host_spec
    user, host, port = parse_host_spec(spec)
    if not host:
        raise FSError(f"Invalid jump host “{spec}”: use user@host:port or the name of a saved site")
    return {"id": "", "protocol": "sftp", "host": host, "port": port or 22,
            "user": user or "", "auth": site.get("auth") or "auto", "key_file": site.get("key_file") or "",
            "timeout": site.get("timeout") or 15, "keepalive": site.get("keepalive") or 0,
            "_jump_label": spec}


class _HopPrompter:
    """Prompts while logging in to a jump host: its passwords are not
    offered for remembering as the site's own (nor kept as such)."""

    def __init__(self, inner):
        self._inner = inner

    def ask_password(self, title, message):
        try:
            return self._inner.ask_password(title, message, remember=False)
        except TypeError:
            return self._inner.ask_password(title, message)

    def ask_interactive(self, title, instructions, prompts):
        try:
            return self._inner.ask_interactive(title, instructions, prompts, remember=False)
        except TypeError:
            return self._inner.ask_interactive(title, instructions, prompts)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _verify_host_key(transport, host, port, prompter):
    key = transport.get_remote_server_key()
    fingerprint = hostkeys.sha256_fingerprint(key.asbytes())
    status = hostkeys.check_host_key(host, port, key)
    prompter.log(f"Host key: {key.get_name()} {fingerprint} ({status})")
    if status == "ok":
        return
    answer = prompter.verify_host_key(host, port, key.get_name(), fingerprint, status == "changed")
    if answer == "save":
        hostkeys.save_host_key(host, port, key)
    elif answer != "once":
        raise AuthFailed("Host key was not accepted")


def _load_key(path: str, prompter):
    path = os.path.expanduser(path)
    try:
        head = Path(path).read_bytes()[:64]
    except OSError as exc:
        raise AuthFailed(f"Cannot read private key {path}: {exc.strerror}") from exc
    if head.startswith(b"PuTTY-User-Key-File"):
        raise AuthFailed(
            "PuTTY .ppk keys are not supported. Convert it to OpenSSH format with:\n"
            f"puttygen '{path}' -O private-openssh -o '{path[:-4] if path.endswith('.ppk') else path}.pem'")
    passphrase = None
    for _attempt in range(4):
        try:
            return paramiko.PKey.from_path(
                path, passphrase=passphrase.encode() if passphrase is not None else None)
        except paramiko.PasswordRequiredException:
            pass                                   # encrypted: ask below
        except TypeError as exc:                   # newer cryptography: "password-protected"
            if "password" not in str(exc).lower():
                raise AuthFailed(f"Unsupported or invalid private key {path}: {exc}") from exc
        except (paramiko.SSHException, ValueError, OSError) as exc:
            if passphrase is None:                 # not a passphrase problem
                raise AuthFailed(f"Unsupported or invalid private key {path}: {exc}") from exc
        message = f"Enter the passphrase for {os.path.basename(path)}"
        if passphrase is not None:
            message = "Wrong passphrase. " + message
        passphrase = prompter.ask_password("Key passphrase", message)
        if passphrase is None:
            raise Cancelled()
    raise AuthFailed("Wrong passphrase for private key")


def _candidate_keys(site, prompter):
    auth = site.get("auth", "auto")
    key_file = site.get("key_file") or ""
    if key_file and auth in ("auto", "key"):
        yield key_file, _load_key(key_file, prompter)
    if auth in ("auto", "agent"):
        try:
            agent = paramiko.Agent()
            for key in agent.get_keys():
                yield "agent", key
        except paramiko.SSHException:
            pass
    if auth in ("auto", "key") and not key_file:
        for path in site.get("_config_keys") or []:
            try:
                yield path, _load_key(path, prompter)
            except AuthFailed as exc:
                prompter.log(str(exc))
    if auth == "auto" and not key_file:
        ssh_dir = Path.home() / ".ssh"
        for name in DEFAULT_KEYS:
            path = ssh_dir / name
            if path.exists():
                try:
                    yield str(path), paramiko.PKey.from_path(str(path))
                except (paramiko.SSHException, ValueError, TypeError, OSError):
                    continue  # encrypted or unsupported default key: skip silently


def _authenticate(transport, site, user, prompter, password_holder):
    try:
        transport.auth_none(user)
        if transport.is_authenticated():
            return
        allowed = {"publickey", "password", "keyboard-interactive"}
    except paramiko.BadAuthenticationType as exc:
        allowed = set(exc.allowed_types)
    except (paramiko.SSHException, EOFError) as exc:
        if not transport.is_active():
            raise ConnectionLost(f"Server closed the connection: {exc}") from exc
        allowed = {"publickey", "password", "keyboard-interactive"}
    prompter.log("Server accepts: " + ", ".join(sorted(allowed)))

    auth_mode = site.get("auth", "auto")

    # -- public keys ---------------------------------------------------------
    if "publickey" in allowed and auth_mode in ("auto", "key", "agent"):
        for label, key in _candidate_keys(site, prompter):
            try:
                remaining = transport.auth_publickey(user, key)
            except paramiko.AuthenticationException:
                prompter.log(f"Key {label} ({key.get_name()}) refused")
                continue
            except (paramiko.SSHException, EOFError) as exc:
                if not transport.is_active():
                    raise ConnectionLost(str(exc)) from exc
                continue
            prompter.log(f"Authenticated with key {label}")
            if transport.is_authenticated():
                return
            allowed = set(remaining or [])  # partial success (multi-factor)
            break

    if transport.is_authenticated():
        return
    if auth_mode in ("key", "agent") and not ({"password", "keyboard-interactive"} & allowed):
        raise AuthFailed("The server refused the key")

    # -- password / keyboard-interactive -------------------------------------
    if not ({"password", "keyboard-interactive"} & allowed):
        raise AuthFailed("No supported authentication method succeeded "
                         f"(server accepts: {', '.join(sorted(allowed)) or 'nothing'})")

    password = site.get("_password")
    host = site.get("host")
    used_password = {"done": False}

    def interactive_handler(title, instructions, prompts):
        if not prompts:
            return []
        if (len(prompts) == 1 and not prompts[0][1] and "password" in prompts[0][0].lower()
                and password is not None and not used_password["done"]):
            used_password["done"] = True
            return [password]
        answers = prompter.ask_interactive(
            title or f"Authentication for {user}@{host}", instructions or "", list(prompts))
        if answers is None:
            raise Cancelled()
        return answers

    for attempt in range(3):
        if password is None and "password" in allowed:
            message = f"Password for {user}@{host}" if attempt == 0 else \
                f"Access denied. Password for {user}@{host}"
            password = prompter.ask_password("Password", message)
            if password is None:
                raise Cancelled()
        used_password["done"] = False
        try:
            if "password" in allowed and password is not None:
                transport.auth_password(user, password, fallback=True)
            else:
                transport.auth_interactive(user, interactive_handler)
        except paramiko.BadAuthenticationType as exc:
            allowed = set(exc.allowed_types)
            if "keyboard-interactive" in allowed:
                try:
                    transport.auth_interactive(user, interactive_handler)
                except paramiko.AuthenticationException:
                    password = None
                    continue
            else:
                raise AuthFailed("Password authentication is not allowed by the server") from exc
        except paramiko.AuthenticationException:
            prompter.log("Access denied")
            password = None
            continue
        except (paramiko.SSHException, EOFError) as exc:
            raise ConnectionLost(f"Connection closed during authentication: {exc}") from exc
        if transport.is_authenticated():
            if password is not None:
                password_holder["password"] = password
            return
    raise AuthFailed("Access denied")


class SSHFileSystem(FileSystem):
    """Base for SFTP and SCP backends."""

    def __init__(self, site=None, prompter=None, connection: SSHConnection | None = None):
        super().__init__(site, prompter)
        self.conn = connection.acquire() if connection else None
        self.password_holder: dict = {}

    @property
    def transport(self) -> paramiko.Transport:
        if self.conn is None or not self.conn.active:
            raise ConnectionLost("Not connected")
        return self.conn.transport

    def _open_connection(self):
        if self.conn is None:
            self.conn = connect_ssh(self.site, self.prompter, self.password_holder).acquire()

    def close(self):
        if self.conn is not None:
            conn, self.conn = self.conn, None
            conn.release()

    def keepalive(self):
        if self.conn is not None and self.conn.active:
            self.conn.transport.send_ignore()

    def exec(self, command: str, input_data: bytes | None = None, timeout: float | None = 120) -> tuple[int, str, str]:
        chan = self.transport.open_session()
        try:
            chan.settimeout(timeout)
            chan.exec_command(command)
            if input_data:
                chan.sendall(input_data)
                chan.shutdown_write()
            out = bytearray()
            err = bytearray()
            while True:
                got = False
                if chan.recv_ready():
                    out += chan.recv(65536)
                    got = True
                if chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                    got = True
                if got:
                    continue
                if chan.exit_status_ready() or chan.closed or chan.eof_received:
                    break
                time.sleep(0.003)
            status = chan.recv_exit_status()
        except socket.timeout as exc:
            raise FSError(f"Command timed out: {command}") from exc
        finally:
            chan.close()
        return status, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def open_shell(self, term="xterm-256color", cols=80, rows=24):
        """Return a paramiko channel running an interactive login shell."""
        chan = self.transport.open_session()
        chan.get_pty(term=term, width=cols, height=rows)
        chan.invoke_shell()
        return chan

    def ssh_info(self) -> list[tuple[str, str]]:
        t = self.transport
        key = t.get_remote_server_key()
        rows = [
            ("Server", t.remote_version or ""),
            ("Host key", f"{key.get_name()}  {hostkeys.sha256_fingerprint(key.asbytes())}"),
            ("Cipher", f"{t.remote_cipher} / {t.local_cipher}"),
            ("MAC", f"{t.remote_mac} / {t.local_mac}"),
            ("Compression", t.remote_compression or "none"),
        ]
        kex = getattr(t, "kex_engine", None)
        if kex is not None:
            rows.insert(2, ("Key exchange", getattr(kex, "name", kex.__class__.__name__)))
        return rows
