# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""A throwaway proxy for tests: SOCKS5 (optionally with a user name and
password), SOCKS4/4a and HTTP CONNECT (optionally with Basic auth), all on
one port.  Used by the pytest fixture ``proxy_server`` and, as a program,
by the GUI tests:

    python3 proxy_server.py <info.json>

writes {"port": ...} to info.json and, while running, the targets it
connected to (one "host:port" per line) to info.json + ".targets".
"""

import json
import os
import socket
import sys



class ProxyServer:
    """A throwaway proxy for tests: SOCKS5 (optionally with a user name and
    password), SOCKS4/4a and HTTP CONNECT (optionally with Basic auth), all
    on one port.  ``targets`` records where it connected its clients."""

    def __init__(self, user=None, password=None, on_target=None):
        import threading
        self.user, self.password = user, password
        self.targets = []
        self.on_target = on_target
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self._stop = True
        self._sock.close()

    def _serve(self):
        import threading
        while not self._stop:
            try:
                client, _addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    @staticmethod
    def _read(sock, n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def _handle(self, client):
        import base64
        import struct
        try:
            first = self._read(client, 1)
            if first == b"\x05":
                nmethods = self._read(client, 1)[0]
                methods = self._read(client, nmethods)
                want = 2 if self.user else 0
                if want not in methods:
                    client.sendall(b"\x05\xff")
                    return
                client.sendall(bytes([5, want]))
                if want == 2:
                    self._read(client, 1)
                    user = self._read(client, self._read(client, 1)[0]).decode()
                    password = self._read(client, self._read(client, 1)[0]).decode()
                    ok = (user, password) == (self.user, self.password)
                    client.sendall(b"\x01" + (b"\x00" if ok else b"\x01"))
                    if not ok:
                        return
                _ver, _cmd, _rsv, atyp = self._read(client, 4)
                if atyp == 1:
                    host = socket.inet_ntoa(self._read(client, 4))
                elif atyp == 3:
                    host = self._read(client, self._read(client, 1)[0]).decode()
                else:
                    host = socket.inet_ntop(socket.AF_INET6, self._read(client, 16))
                port = struct.unpack(">H", self._read(client, 2))[0]
                try:
                    target = socket.create_connection((host, port), timeout=5)
                except OSError:
                    client.sendall(b"\x05\x05\x00\x01" + bytes(6))      # connection refused
                    return
                client.sendall(b"\x05\x00\x00\x01" + bytes(6))
            elif first == b"\x04":
                _cmd = self._read(client, 1)
                port = struct.unpack(">H", self._read(client, 2))[0]
                ip = self._read(client, 4)
                while self._read(client, 1) != b"\x00":                 # user id
                    pass
                if ip[:3] == b"\x00\x00\x00" and ip[3]:                    # SOCKS4a: a host name follows
                    name = b""
                    while (c := self._read(client, 1)) != b"\x00":
                        name += c
                    host = name.decode()
                else:
                    host = socket.inet_ntoa(ip)
                target = socket.create_connection((host, port), timeout=5)
                client.sendall(b"\x00\x5a" + bytes(6))
            else:
                head = first
                while not head.endswith(b"\r\n\r\n"):
                    head += self._read(client, 1)
                lines = head.decode().split("\r\n")
                hostport = lines[0].split(" ")[1]
                host, _sep, port = hostport.rpartition(":")
                port = int(port)
                host = host.strip("[]")
                if self.user:
                    token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
                    if f"Proxy-Authorization: Basic {token}" not in lines:
                        client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                        return
                target = socket.create_connection((host, port), timeout=5)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.targets.append((host, port))
            if self.on_target is not None:
                self.on_target(host, port)
            target.settimeout(None)
            client.settimeout(None)
            self._relay(client, target)
        except OSError:
            pass
        finally:
            client.close()

    @staticmethod
    def _relay(a, b):
        import threading

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        t = threading.Thread(target=pump, args=(b, a), daemon=True)
        t.start()
        pump(a, b)
        t.join(5)
        b.close()



def main():
    info = sys.argv[1]
    targets = info + ".targets"
    open(targets, "w").close()

    def note(host, port):
        with open(targets, "a", encoding="utf-8") as fh:
            fh.write(f"{host}:{port}\n")
    server = ProxyServer(on_target=note)
    with open(info + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"port": server.port}, fh)
    os.replace(info + ".tmp", info)
    import time
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
