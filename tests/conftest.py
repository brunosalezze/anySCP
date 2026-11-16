# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Shared pytest fixtures.

Tests never touch the user's real configuration: ``ANYSCP_CONFIG_DIR`` is
pointed at a temporary directory before :mod:`anyscp` is imported.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP_CONFIG = tempfile.mkdtemp(prefix="anyscp-test-config-")
os.environ.setdefault("ANYSCP_CONFIG_DIR", _TMP_CONFIG)
os.environ.setdefault("ANYSCP_CACHE_DIR", os.path.join(_TMP_CONFIG, "cache"))
os.environ.setdefault("ANYSCP_NO_KEYRING", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

SSHD = shutil.which("sshd") or "/usr/sbin/sshd"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AcceptAllPrompter:
    """Prompter that accepts host keys once and answers from fixed values."""

    def __init__(self, password=None, passphrase=None):
        self.password = password
        self.passphrase = passphrase
        self.messages = []
        self.host_key_prompts = 0

    def ask_password(self, title, message):
        self.messages.append(message)
        if "passphrase" in title.lower():
            return self.passphrase
        return self.password

    def ask_text(self, title, message, default=""):
        return default

    def ask_interactive(self, title, instructions, prompts):
        return [self.password or "" for _ in prompts]

    def verify_host_key(self, host, port, key_type, fingerprint, changed):
        self.host_key_prompts += 1
        return "once"

    def verify_certificate(self, host, port, fingerprint, subject, issuer, error):
        return "once"

    def log(self, message):
        self.messages.append(message)


@pytest.fixture(scope="session")
def sshd():
    """Run a throwaway OpenSSH server as the current user (key auth only)."""
    if not os.path.exists(SSHD):
        pytest.skip("OpenSSH server not installed")
    work = Path(tempfile.mkdtemp(prefix="anyscp-sshd-"))
    for name, extra in (("host", []), ("client", []), ("client_enc", ["-N", "secret pass"])):
        args = ["ssh-keygen", "-q", "-t", "ed25519", "-f", str(work / name), "-C", name]
        args += extra or ["-N", ""]
        subprocess.run(args, check=True)
    (work / "authorized_keys").write_text(
        (work / "client.pub").read_text() + (work / "client_enc.pub").read_text())
    port = free_port()
    (work / "sshd_config").write_text(f"""
Port {port}
ListenAddress 127.0.0.1
HostKey {work}/host
PidFile {work}/sshd.pid
AuthorizedKeysFile {work}/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes no
Subsystem sftp internal-sftp
""")
    proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(work / "sshd_config")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    root = work / "files"
    root.mkdir()
    yield {"port": port, "key": str(work / "client"), "enc_key": str(work / "client_enc"),
           "root": str(root), "user": os.environ.get("USER") or os.getlogin()}
    proc.terminate()
    proc.wait(5)
    shutil.rmtree(work, ignore_errors=True)


from proxy_server import ProxyServer  # noqa: E402

@pytest.fixture
def proxy_server():
    servers = []

    def make(user=None, password=None):
        server = ProxyServer(user, password)
        servers.append(server)
        return server
    yield make
    for server in servers:
        server.close()
