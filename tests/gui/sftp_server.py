# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Throwaway SFTP server with password authentication (paramiko), for tests
that need a password login, e.g. one stored in the system's password store.

usage: sftp_server.py <root dir> <user> <password> <info.json> [delay]

With ANYSCP_DEMO_SHELL set (the documentation screenshots), a shell session
prints that text (a canned "ls -l" and a prompt) instead of failing.

Serves <root dir> as "/" on 127.0.0.1, on a free port, until killed.  Writes
{"port": ..., "known_hosts": "<line for a known_hosts file>"} to <info.json>
once it accepts connections.
"""

import json
import os
import posixpath
import socket
import sys
import threading
import time

import paramiko
from paramiko import (AUTH_FAILED, AUTH_SUCCESSFUL, OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED, OPEN_SUCCEEDED,
                      SFTP_OK, SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface)

ROOT, USER, PASSWORD, INFO = os.path.realpath(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
DELAY = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0   # seconds: answer like a distant server
DEMO_SHELL = os.environ.get("ANYSCP_DEMO_SHELL", "")
READ_DELAY = float(os.environ.get("ANYSCP_DEMO_READ_DELAY", "0"))   # seconds per read request


class Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        time.sleep(DELAY)
        return AUTH_SUCCESSFUL if (username, password) == (USER, PASSWORD) else AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return OPEN_SUCCEEDED if kind == "session" else OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    # a pretend shell, for the screenshots of the terminal
    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        return bool(DEMO_SHELL)

    def check_channel_shell_request(self, channel):
        if not DEMO_SHELL:
            return False

        def talk():
            channel.sendall(DEMO_SHELL.replace("\n", "\r\n").encode())
            try:
                while channel.recv(1024):
                    pass
            except OSError:
                pass
        threading.Thread(target=talk, daemon=True).start()
        return True


def _errno(exc):
    return SFTPServer.convert_errno(exc.errno)


class Handle(SFTPHandle):
    def read(self, offset, length):
        if READ_DELAY:
            time.sleep(READ_DELAY)
        return super().read(offset, length)

    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat((self.readfile or self.writefile).fileno()))
        except OSError as exc:
            return _errno(exc)

    def chattr(self, attr):
        try:
            _apply(self.filename, attr)
        except OSError as exc:
            return _errno(exc)
        return SFTP_OK


def _apply(path, attr):
    if attr._flags & attr.FLAG_PERMISSIONS:
        os.chmod(path, attr.st_mode & 0o7777)
    if attr._flags & attr.FLAG_AMTIME:
        os.utime(path, (attr.st_atime, attr.st_mtime))
    if attr._flags & attr.FLAG_SIZE:
        with open(path, "r+b") as fh:
            fh.truncate(attr.st_size)


class Files(SFTPServerInterface):
    def _real(self, path):
        rel = self.canonicalize(path).lstrip("/")
        return os.path.join(ROOT, *rel.split("/")) if rel else ROOT

    def canonicalize(self, path):
        # SFTP paths are POSIX paths, also when this server runs on Windows
        if path in ("", "."):
            return "/"
        normal = posixpath.normpath("/" + path.replace("\\", "/"))
        return "/" + normal.lstrip("/")

    def list_folder(self, path):
        time.sleep(DELAY)
        real = self._real(path)
        try:
            out = []
            for name in os.listdir(real):
                attr = SFTPAttributes.from_stat(os.lstat(os.path.join(real, name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as exc:
            return _errno(exc)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return _errno(exc)

    def lstat(self, path):
        try:
            return SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as exc:
            return _errno(exc)

    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as exc:
            return _errno(exc)
        mode = ("ab" if flags & os.O_APPEND else "r+b" if flags & os.O_RDWR else "wb") \
            if flags & (os.O_WRONLY | os.O_RDWR) else "rb"
        handle = Handle(flags)
        handle.filename = real
        fobj = os.fdopen(fd, mode)
        if "r" in mode:
            handle.readfile = fobj
        if "w" in mode or "a" in mode or "+" in mode:
            handle.writefile = fobj
        return handle

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as exc:
            return _errno(exc)
        return SFTP_OK

    def rename(self, old, new):
        try:
            os.rename(self._real(old), self._real(new))
        except OSError as exc:
            return _errno(exc)
        return SFTP_OK

    posix_rename = rename

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as exc:
            return _errno(exc)
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as exc:
            return _errno(exc)
        return SFTP_OK

    def chattr(self, path, attr):
        try:
            _apply(self._real(path), attr)
        except OSError as exc:
            return _errno(exc)
        return SFTP_OK

    def readlink(self, path):
        try:
            return os.readlink(self._real(path))
        except OSError as exc:
            return _errno(exc)

    def symlink(self, target, path):
        try:
            os.symlink(target, self._real(path))
        except OSError as exc:
            return _errno(exc)
        return SFTP_OK


def serve(conn, host_key):
    transport = paramiko.Transport(conn)
    transport.add_server_key(host_key)
    transport.set_subsystem_handler("sftp", SFTPServer, Files)
    try:
        transport.start_server(server=Server())
        while transport.is_active():
            transport.join(1)
    except Exception:  # noqa: BLE001 - one broken client must not stop the server
        pass


def main():
    host_key = paramiko.RSAKey.generate(2048)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    with open(INFO + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"port": port,
                   "known_hosts": f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}"}, fh)
    os.replace(INFO + ".tmp", INFO)
    while True:
        conn, _addr = sock.accept()
        threading.Thread(target=serve, args=(conn, host_key), daemon=True).start()


main()
