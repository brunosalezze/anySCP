# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Tests for the FTP/FTPS backend against a real local pyftpdlib server.

Needs the test-only packages ``pytest pyftpdlib pyopenssl cryptography``.
"""

from __future__ import annotations

import os
import sys
import tempfile

# Never touch the user's real configuration (trusted certificates etc.).
if "anyscp.config" not in sys.modules:
    _CONFIG_DIR = tempfile.mkdtemp(prefix="anyscp-test-config-")
    os.environ["ANYSCP_CONFIG_DIR"] = _CONFIG_DIR
    os.environ["ANYSCP_CACHE_DIR"] = os.path.join(_CONFIG_DIR, "cache")
os.environ["ANYSCP_NO_KEYRING"] = "1"

import datetime  # noqa: E402
import ipaddress  # noqa: E402
import socket  # noqa: E402
import stat as statmod  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

pyftpdlib = pytest.importorskip("pyftpdlib")
pytest.importorskip("OpenSSL")

from pyftpdlib.authorizers import DummyAuthorizer  # noqa: E402
from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler  # noqa: E402
from pyftpdlib.ioloop import IOLoop  # noqa: E402
from pyftpdlib.servers import FTPServer  # noqa: E402

from anyscp import config  # noqa: E402
from anyscp.backends import create_fs  # noqa: E402
from anyscp.backends.base import (  # noqa: E402
    CAP_CHMOD,
    CAP_RESUME,
    CAP_UTIME,
    AlreadyExists,
    AuthFailed,
    Cancelled,
    ConnectionLost,
    FSError,
    NotFound,
    NotSupported,
    PermissionDenied,
)
from anyscp.backends.ftp import (  # noqa: E402
    FTPFileSystem,
    parse_list,
    parse_list_line,
    parse_mlsx_line,
)

USER, PASSWORD = "alice", "s3cret"


def test_config_dir_is_isolated():
    assert str(config.CONFIG_DIR).startswith(tempfile.gettempdir())
    assert Path.home() / ".config" / "anyscp" != config.CONFIG_DIR


# --------------------------------------------------------------------------
# Fixtures: certificate, servers, prompter
# --------------------------------------------------------------------------

def make_cert(directory: Path) -> tuple[Path, Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    certfile, keyfile, combined = directory / "cert.pem", directory / "key.pem", directory / "both.pem"
    certfile.write_bytes(cert_pem)
    keyfile.write_bytes(key_pem)
    combined.write_bytes(cert_pem + key_pem)
    return certfile, keyfile, combined


class Prompter:
    def __init__(self, passwords=(), cert="once"):
        self.passwords = list(passwords)
        self.cert = cert
        self.password_prompts: list[str] = []
        self.cert_prompts: list[tuple] = []
        self.logs: list[str] = []

    def ask_password(self, title, message):
        self.password_prompts.append(message)
        return self.passwords.pop(0) if self.passwords else None

    def ask_text(self, title, message, default=""):
        return None

    def ask_interactive(self, title, instructions, prompts):
        return None

    def verify_host_key(self, host, port, key_type, fingerprint, changed):
        return "reject"

    def verify_certificate(self, host, port, fingerprint, subject, issuer, error):
        self.cert_prompts.append((host, port, fingerprint, error))
        return self.cert

    def log(self, message):
        self.logs.append(message)


class ServerThread(threading.Thread):
    def __init__(self, handler):
        super().__init__(daemon=True)
        # pyftpdlib's default IOLoop is a process-wide singleton: give each
        # server its own so several can run in parallel threads.
        self.server = FTPServer(("127.0.0.1", 0), handler, ioloop=IOLoop())
        self.port = self.server.address[1]
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            self.server.ioloop.loop(0.02, blocking=False)
        self.server.close_all()

    def stop(self):
        self._halt.set()
        self.join(5)


def _make_handler(base, mode: str, root: Path, anon_root: Path, combined: Path):
    authorizer = DummyAuthorizer()
    authorizer.add_user(USER, PASSWORD, str(root), perm="elradfmwMT")
    authorizer.add_anonymous(str(anon_root))
    attrs = {
        "authorizer": authorizer,
        "auth_failed_timeout": 0,
        "banner": f"anySCP test server ({mode})",
    }
    if base is TLS_FTPHandler:
        attrs.update(certfile=str(combined), tls_control_required=True,
                     tls_data_required=True, ssl_context=None)
    if mode == "list":
        # An old-fashioned server: no MLSD/MLST, so LIST output is parsed.
        attrs["proto_cmds"] = {k: v for k, v in FTPHandler.proto_cmds.items()
                               if k not in ("MLSD", "MLST")}
    if mode == "minimal":
        # ... and one without REST, MFMT and SITE CHMOD either.
        attrs["proto_cmds"] = {k: v for k, v in FTPHandler.proto_cmds.items()
                               if k not in ("MLSD", "MLST", "REST", "MFMT", "SITE CHMOD")}
    if mode == "implicit":
        def handle(self):
            self.secure_connection(self.ssl_context)

        def handle_ssl_established(self):
            TLS_FTPHandler.handle(self)      # banner once TLS is up
        attrs.update(handle=handle, handle_ssl_established=handle_ssl_established)
    return type(f"Handler_{mode}", (base,), attrs)


@pytest.fixture(scope="module")
def ftp_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("ftp")
    root, anon_root = base / "root", base / "anon"
    root.mkdir()
    anon_root.mkdir()
    (anon_root / "readme.txt").write_text("public\n")
    _, _, combined = make_cert(base)
    servers = {}
    for mode, handler_base in (("plain", FTPHandler), ("list", FTPHandler), ("minimal", FTPHandler),
                               ("explicit", TLS_FTPHandler), ("implicit", TLS_FTPHandler)):
        thread = ServerThread(_make_handler(handler_base, mode, root, anon_root, combined))
        thread.start()
        servers[mode] = thread
    yield {"root": root, "servers": servers}
    for thread in servers.values():
        thread.stop()


def site_for(env, mode: str, **extra) -> dict:
    site = config.new_site(
        protocol="ftp", host="127.0.0.1", port=env["servers"][mode].port, user=USER,
        timeout=10,
        ftp_encryption={"explicit": "explicit", "implicit": "implicit"}.get(mode, "none"))
    site["_password"] = PASSWORD
    site.update(extra)
    return site


@pytest.fixture
def workdir(ftp_env):
    """A fresh directory on the server: (local Path, remote path)."""
    name = "t-" + uuid.uuid4().hex[:10]
    local = ftp_env["root"] / name
    local.mkdir()
    return local, "/" + name


def connect(env, mode, prompter=None, **extra) -> FTPFileSystem:
    fs = create_fs(site_for(env, mode, **extra), prompter or Prompter())
    assert isinstance(fs, FTPFileSystem)
    fs.connect()
    return fs


ALL_MODES = ["plain", "list", "explicit", "implicit"]
TLS_MODES = ["explicit", "implicit"]


# --------------------------------------------------------------------------
# Connecting and authentication
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ALL_MODES)
def test_connect_info_and_capabilities(ftp_env, mode):
    prompter = Prompter()
    fs = connect(ftp_env, mode, prompter)
    try:
        assert fs.home() == "/"
        info = dict(fs.info())
        assert info["Protocol"].startswith("FTPS" if mode in TLS_MODES else "FTP")
        assert "anySCP test server" in info["Welcome message"]
        assert "UNIX" in info["System"]
        assert fs.has(CAP_CHMOD) and fs.has(CAP_UTIME) and fs.has(CAP_RESUME)
        if mode in TLS_MODES:
            assert info["Encryption"].startswith("TLS")
            assert len(prompter.cert_prompts) == 1          # self-signed, accepted once
            assert "self-signed" in prompter.cert_prompts[0][3] or prompter.cert_prompts[0][3]
        else:
            assert info["Encryption"] == "None"
            assert prompter.cert_prompts == []
        assert info["Directory listing"] == ("LIST -a" if mode == "list" else "MLSD")
        assert any("Connected to" in line for line in prompter.logs)
        fs.keepalive()
    finally:
        fs.close()


def test_password_prompt_and_clone_reuses_it(ftp_env):
    prompter = Prompter(passwords=[PASSWORD])
    site = site_for(ftp_env, "explicit")
    del site["_password"]
    fs = FTPFileSystem(site, prompter)
    fs.connect()
    try:
        assert len(prompter.password_prompts) == 1
        assert fs.site["_password"] == PASSWORD
        other = fs.clone()
        try:
            assert other is not fs and other._ftp is not fs._ftp
            assert other._session_trust is fs._session_trust
            assert other.listdir("/") is not None
        finally:
            other.close()
        assert len(prompter.password_prompts) == 1      # clone did not ask again
        assert len(prompter.cert_prompts) == 1          # nor about the certificate
    finally:
        fs.close()


def test_wrong_password_then_cancel(ftp_env):
    prompter = Prompter(passwords=[])                   # user dismisses the prompt
    fs = FTPFileSystem(site_for(ftp_env, "plain", _password="wrong"), prompter)
    with pytest.raises(Cancelled):
        fs.connect()
    assert len(prompter.password_prompts) == 1
    assert "Login incorrect" in prompter.password_prompts[0]


def test_wrong_password_three_times(ftp_env):
    prompter = Prompter(passwords=["bad1", "bad2", "bad3", PASSWORD])
    site = site_for(ftp_env, "plain")
    del site["_password"]
    fs = FTPFileSystem(site, prompter)
    with pytest.raises(AuthFailed):
        fs.connect()
    assert len(prompter.password_prompts) == 3


def test_wrong_password_then_right(ftp_env):
    prompter = Prompter(passwords=[PASSWORD])
    fs = FTPFileSystem(site_for(ftp_env, "implicit", _password="wrong"), prompter)
    fs.connect()
    try:
        assert len(prompter.password_prompts) == 1
        assert len(prompter.cert_prompts) == 1          # reconnect did not re-ask
    finally:
        fs.close()


def test_anonymous_login(ftp_env):
    site = site_for(ftp_env, "plain", user="")
    del site["_password"]
    prompter = Prompter()
    fs = FTPFileSystem(site, prompter)
    fs.connect()
    try:
        assert prompter.password_prompts == []
        assert [e.name for e in fs.listdir("/")] == ["readme.txt"]
        with pytest.raises(PermissionDenied):
            fs.mkdir("/new")
        with fs.open_read("/readme.txt") as fh:
            assert fh.read() == b"public\n"
    finally:
        fs.close()


def test_certificate_rejected(ftp_env):
    fs = FTPFileSystem(site_for(ftp_env, "explicit"), Prompter(cert="reject"))
    with pytest.raises(AuthFailed):
        fs.connect()


def test_certificate_saved(ftp_env):
    try:
        fs = connect(ftp_env, "implicit", Prompter(cert="save"))
        fs.close()
        assert config.TRUSTED_CERTS_FILE.exists()
        prompter = Prompter(cert="reject")              # would fail if asked again
        fs = connect(ftp_env, "implicit", prompter)
        try:
            assert prompter.cert_prompts == []
            fs.listdir("/")
        finally:
            fs.close()
    finally:
        config.TRUSTED_CERTS_FILE.unlink(missing_ok=True)   # don't affect other tests


def test_connection_refused():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    site = config.new_site(protocol="ftp", host="127.0.0.1", port=port, user=USER, timeout=3)
    with pytest.raises(ConnectionLost):
        FTPFileSystem(site, Prompter()).connect()


def test_dropped_connection_and_reconnect(ftp_env):
    fs = connect(ftp_env, "plain")
    try:
        fs._ftp.sock.shutdown(socket.SHUT_RDWR)        # simulate a network failure
        with pytest.raises(ConnectionLost):
            fs.listdir("/")
        with pytest.raises(ConnectionLost):              # stays failed until reconnect
            fs.mkdir("/x")
        fs.connect()                                   # reconnect without prompting
        assert isinstance(fs.listdir("/"), list)
    finally:
        fs.close()


# --------------------------------------------------------------------------
# File system operations
# --------------------------------------------------------------------------

def _populate(local: Path) -> None:
    (local / "a.txt").write_bytes(b"hello")
    (local / "with space.txt").write_bytes(b"x" * 10)
    (local / "ünïcødé ✓.txt").write_bytes(b"unicode")
    (local / ".hidden").write_bytes(b"h")
    (local / "sub").mkdir()
    (local / "sub" / "inner.txt").write_bytes(b"inner")
    os.symlink("sub", local / "link_to_sub")
    os.symlink("a.txt", local / "link_to_file")
    os.utime(local / "a.txt", (1_600_000_000, 1_600_000_000))
    os.chmod(local / "a.txt", 0o640)


@pytest.mark.parametrize("mode", ALL_MODES)
def test_listdir_and_stat(ftp_env, workdir, mode):
    local, remote = workdir
    _populate(local)
    fs = connect(ftp_env, mode)
    try:
        entries = {e.name: e for e in fs.listdir(remote)}
        assert set(entries) == {"a.txt", "with space.txt", "ünïcødé ✓.txt", ".hidden", "sub",
                                "link_to_sub", "link_to_file"}
        a = entries["a.txt"]
        assert a.path == f"{remote}/a.txt"
        assert not a.is_dir and a.size == 5
        # LIST shows only the date for files older than ~6 months
        assert abs(a.mtime - 1_600_000_000) < (86400 if mode == "list" else 1)
        assert a.mode == 0o640
        assert entries["sub"].is_dir and not entries["sub"].is_link
        assert entries["with space.txt"].size == 10
        assert entries["ünïcødé ✓.txt"].size == 7
        assert entries["link_to_sub"].is_dir               # symlink to a directory
        assert not entries["link_to_file"].is_dir
        if mode == "list":
            # LIST shows symlinks as such (pyftpdlib's MLSD follows them).
            assert entries["link_to_sub"].is_link and entries["link_to_file"].is_link
            assert entries["link_to_file"].link_target == "a.txt"
            assert a.owner

        st = fs.stat(f"{remote}/a.txt")
        assert st.name == "a.txt" and st.size == 5 and not st.is_dir
        assert fs.stat(f"{remote}/sub").is_dir
        assert fs.stat(f"{remote}/ünïcødé ✓.txt").size == 7
        assert fs.stat(f"{remote}/.hidden").size == 1
        assert fs.stat("/").is_dir
        assert fs.exists(f"{remote}/with space.txt")
        assert not fs.exists(f"{remote}/missing")
        with pytest.raises(NotFound):
            fs.stat(f"{remote}/missing")
        with pytest.raises(NotFound):
            fs.listdir(f"{remote}/missing")
        assert [e.name for e in fs.listdir(f"{remote}/sub")] == ["inner.txt"]
        nofollow = fs.stat_nofollow(f"{remote}/link_to_file")
        if mode == "list":
            assert nofollow.is_link and fs.readlink(f"{remote}/link_to_file") == "a.txt"
    finally:
        fs.close()


@pytest.mark.parametrize("mode", ["plain", "list", "explicit"])
def test_modifications(ftp_env, workdir, mode):
    local, remote = workdir
    _populate(local)
    fs = connect(ftp_env, mode)
    try:
        fs.mkdir(f"{remote}/new dir")
        assert (local / "new dir").is_dir()
        with pytest.raises(AlreadyExists):
            fs.mkdir(f"{remote}/new dir")
        with pytest.raises(NotFound):
            fs.mkdir(f"{remote}/no/such/parent")

        fs.rename(f"{remote}/a.txt", f"{remote}/new dir/b ü.txt")
        assert (local / "new dir" / "b ü.txt").read_bytes() == b"hello"
        assert not (local / "a.txt").exists()
        with pytest.raises(NotFound):
            fs.rename(f"{remote}/missing", f"{remote}/x")

        fs.chmod(f"{remote}/new dir/b ü.txt", 0o600)
        assert statmod.S_IMODE(os.stat(local / "new dir" / "b ü.txt").st_mode) == 0o600
        fs.utime(f"{remote}/new dir/b ü.txt", 1_500_000_000)
        assert int(os.stat(local / "new dir" / "b ü.txt").st_mtime) == 1_500_000_000

        with pytest.raises(FSError) as excinfo:
            fs.rmdir(f"{remote}/new dir")                 # not empty
        assert not isinstance(excinfo.value, NotFound)
        fs.remove(f"{remote}/new dir/b ü.txt")
        with pytest.raises(NotFound):
            fs.remove(f"{remote}/new dir/b ü.txt")
        fs.rmdir(f"{remote}/new dir")
        assert not (local / "new dir").exists()
        with pytest.raises(NotFound):
            fs.rmdir(f"{remote}/new dir")

        fs.remove_tree(f"{remote}/sub")
        assert not (local / "sub").exists()
        fs.remove(f"{remote}/link_to_sub")             # removes the link, not the target
        assert not os.path.lexists(local / "link_to_sub")
    finally:
        fs.close()


@pytest.mark.parametrize("mode", ["plain", "explicit", "implicit"])
def test_read_write_roundtrip(ftp_env, workdir, mode):
    local, remote = workdir
    payload = os.urandom(3 * 1024 * 1024 + 123)
    fs = connect(ftp_env, mode)
    try:
        with fs.open_write(f"{remote}/big.bin", size=len(payload)) as fh:
            for i in range(0, len(payload), 100_000):
                fh.write(payload[i:i + 100_000])
        assert (local / "big.bin").read_bytes() == payload
        assert fs.stat(f"{remote}/big.bin").size == len(payload)

        fh = fs.open_read(f"{remote}/big.bin")
        chunks = []
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
        fh.close()
        assert b"".join(chunks) == payload
        with fs.open_read(f"{remote}/big.bin") as fh:
            assert fh.read() == payload
        with pytest.raises(NotFound):
            fs.open_read(f"{remote}/missing.bin")
        fs.listdir(remote)                              # connection still in sync
    finally:
        fs.close()


@pytest.mark.parametrize("mode", ["plain", "implicit"])
def test_resume(ftp_env, workdir, mode):
    local, remote = workdir
    payload = os.urandom(3 * 1024 * 1024)
    split = 1024 * 1024 + 7
    fs = connect(ftp_env, mode)
    try:
        with fs.open_write(f"{remote}/r.bin", size=split) as fh:
            fh.write(payload[:split])
        assert (local / "r.bin").stat().st_size == split
        with fs.open_write(f"{remote}/r.bin", offset=split, size=len(payload)) as fh:
            fh.write(payload[split:])
        assert (local / "r.bin").read_bytes() == payload

        with fs.open_read(f"{remote}/r.bin", offset=2_000_000) as fh:
            assert fh.read() == payload[2_000_000:]
    finally:
        fs.close()


def test_early_close_and_busy(ftp_env, workdir):
    local, remote = workdir
    (local / "big.bin").write_bytes(os.urandom(4 * 1024 * 1024))
    fs = connect(ftp_env, "explicit")
    try:
        fh = fs.open_read(f"{remote}/big.bin")
        assert len(fh.read(65536)) == 65536
        with pytest.raises(FSError):
            fs.listdir(remote)                          # busy with the transfer
        fs.keepalive()                                   # silently skipped while busy
        fh.close()                                       # abort mid-transfer
        assert {e.name for e in fs.listdir(remote)} == {"big.bin"}
        with fs.open_read(f"{remote}/big.bin") as fh:     # and transfers still work
            assert len(fh.read()) == 4 * 1024 * 1024
    finally:
        fs.close()


def test_clone_is_independent(ftp_env, workdir):
    local, remote = workdir
    payload = os.urandom(2 * 1024 * 1024)
    (local / "x.bin").write_bytes(payload)
    prompter = Prompter()
    fs = connect(ftp_env, "implicit", prompter)
    try:
        other = fs.clone()
        try:
            with other.open_read(f"{remote}/x.bin") as fh:
                first = fh.read(1000)
                # the original connection stays usable during the transfer
                assert [e.name for e in fs.listdir(remote)] == ["x.bin"]
                rest = fh.read()
            assert first + rest == payload
            fs.close()
            assert other.stat(f"{remote}/x.bin").size == len(payload)
        finally:
            other.close()
        assert len(prompter.cert_prompts) == 1
    finally:
        fs.close()


@pytest.mark.parametrize("mode", TLS_MODES)
def test_data_connections_reuse_tls_session(ftp_env, workdir, mode, monkeypatch):
    from anyscp.backends import ftp as ftpmod
    local, remote = workdir
    (local / "f.txt").write_bytes(b"data")
    seen = []
    original = ftpmod._FTPClient.ntransfercmd

    def recording(self, cmd, rest=None):
        conn, size = original(self, cmd, rest)
        seen.append((cmd.split()[0], conn.version(), conn.session_reused))
        return conn, size

    monkeypatch.setattr(ftpmod._FTPClient, "ntransfercmd", recording)
    fs = connect(ftp_env, mode)
    try:
        fs.listdir(remote)
        with fs.open_read(f"{remote}/f.txt") as fh:
            assert fh.read() == b"data"
    finally:
        fs.close()
    assert [cmd for cmd, _, _ in seen] == ["MLSD", "RETR"]
    assert all(version and version.startswith("TLS") for _, version, _ in seen)
    assert all(reused for _, _, reused in seen)


def test_minimal_server_fallbacks(ftp_env, workdir):
    local, remote = workdir
    (local / "f.txt").write_bytes(b"abc")
    fs = connect(ftp_env, "minimal")
    try:
        assert not fs.has(CAP_RESUME) and not fs.has(CAP_UTIME)
        assert fs.has(CAP_CHMOD)                     # guessed from SYST "UNIX" ...
        with pytest.raises(NotSupported):
            fs.chmod(f"{remote}/f.txt", 0o600)
        assert not fs.has(CAP_CHMOD)                 # ... and dropped once refused
        with pytest.raises(NotSupported):
            fs.utime(f"{remote}/f.txt", 1_500_000_000)
        with pytest.raises(NotSupported):
            fs.open_read(f"{remote}/f.txt", offset=1)
        with fs.open_write(f"{remote}/f.txt", offset=3, size=6) as fh:     # APPE
            fh.write(b"def")
        assert (local / "f.txt").read_bytes() == b"abcdef"
        assert fs.stat(f"{remote}/f.txt").size == 6
        with pytest.raises(FSError):
            fs.listdir(f"{remote}/f.txt")            # not a directory
    finally:
        fs.close()


def test_reply_error_mapping():
    from anyscp.backends.ftp import _reply_error
    cases = {
        "550 No such file or directory.": NotFound,
        "550 The system cannot find the file specified.": NotFound,
        "550 Permission denied.": PermissionDenied,
        "550 Access is denied.": PermissionDenied,
        "550 Cannot create a file when that file already exists.": AlreadyExists,
        "550 Directory not empty": FSError,
        "550 Is a directory.": FSError,
        "553 Could not create file.": PermissionDenied,
        "530 Login incorrect.": AuthFailed,
        "502 Command not implemented.": NotSupported,
        "421 Timeout.": ConnectionLost,
        "552 Quota exceeded": FSError,
    }
    for reply, cls in cases.items():
        assert type(_reply_error(reply, "/x")) is cls, reply
    assert _reply_error("550 Delete operation failed.", "/x", None) is None      # ambiguous
    assert type(_reply_error("550 Delete operation failed.", "/x", NotFound)) is NotFound
    assert "/x" in str(_reply_error("550 No such file or directory.", "/x"))


def test_active_mode(ftp_env, workdir):
    local, remote = workdir
    fs = connect(ftp_env, "explicit", ftp_passive=False)
    try:
        with fs.open_write(f"{remote}/active.txt", size=6) as fh:
            fh.write(b"active")
        assert (local / "active.txt").read_bytes() == b"active"
        assert [e.name for e in fs.listdir(remote)] == ["active.txt"]
    finally:
        fs.close()


@pytest.mark.parametrize("mode,kind", [("plain", "socks5"), ("explicit", "http"), ("implicit", "socks4")])
def test_through_a_proxy(ftp_env, workdir, proxy_server, mode, kind):
    """Control and (passive) data connections go through the proxy, to the
    server's own host name (not the address in the PASV/EPSV reply)."""
    local, remote = workdir
    proxy = proxy_server()
    port = ftp_env["servers"][mode].port
    fs = connect(ftp_env, mode, host="localhost", proxy_type=kind, proxy_host="127.0.0.1",
                 proxy_port=proxy.port)
    try:
        payload = os.urandom(300_000)
        with fs.open_write(f"{remote}/proxied.bin", size=len(payload)) as fh:
            fh.write(payload)
        assert (local / "proxied.bin").read_bytes() == payload
        with fs.open_read(f"{remote}/proxied.bin") as fh:
            assert fh.read() == payload
        assert [e.name for e in fs.listdir(remote)] == ["proxied.bin"]
    finally:
        fs.close()
    hosts = {host for host, _port in proxy.targets}
    assert hosts == {"localhost"}
    assert ("localhost", port) in proxy.targets and len(proxy.targets) >= 4   # control + data connections


def test_active_mode_cannot_go_through_a_proxy(ftp_env, workdir, proxy_server):
    _local, remote = workdir
    proxy = proxy_server()
    fs = connect(ftp_env, "plain", ftp_passive=False, proxy_type="socks5", proxy_host="127.0.0.1",
                 proxy_port=proxy.port)
    try:
        with pytest.raises(NotSupported, match="passive mode"):
            fs.listdir(remote)
    finally:
        fs.close()


# --------------------------------------------------------------------------
# Listing parsers (no server)
# --------------------------------------------------------------------------

NOW = datetime.datetime(2026, 11, 15, 12, 0, tzinfo=datetime.timezone.utc)


def ts(*args) -> float:
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc).timestamp()


def test_parse_unix_lines():
    lines = [
        "total 48",
        "drwxr-xr-x    2 bruno    staff        4096 Nov 14 09:05 Documents",
        "-rw-r--r--    1 bruno    staff      123456 Jan  3  2024 report final.pdf",
        "-rw-------    1 1000     1000            7 Dec 24 18:30 old.txt",
        "lrwxrwxrwx    1 root     root           11 Oct  1 10:00 www -> /var/www/html",
        "crw-rw-rw-    1 root     root       1,   3 Nov  1 00:00 null",
        "-rwsr-xr-t    1 root     wheel        512 Feb 29  2024 suid",
        "drwxr-xr-x+   3 bruno    staff        4096 Nov 15 11:59 acl dir",
        "-rw-r--r--    1 ftp           42 Mar  5 08:00 nogroup.txt",
        "-rw-r--r--    1 bruno    staff          10 2025-06-01 13:45 iso.txt",
        "drwxr-xr-x    5 bruno    staff        4096 Nov 15 10:00 .",
        "drwxr-xr-x    5 bruno    staff        4096 Nov 15 10:00 ..",
        "garbage line",
    ]
    entries = {e.name: e for e in parse_list(lines, "/home", NOW)}
    assert set(entries) == {"Documents", "report final.pdf", "old.txt", "www", "null", "suid",
                            "acl dir", "nogroup.txt", "iso.txt"}
    d = entries["Documents"]
    assert d.is_dir and d.path == "/home/Documents" and d.mode == 0o755
    assert d.owner == "bruno" and d.group == "staff"
    assert d.mtime == ts(2026, 11, 14, 9, 5)
    f = entries["report final.pdf"]
    assert f.size == 123456 and f.mtime == ts(2024, 1, 3) and f.mode == 0o644
    assert entries["old.txt"].mtime == ts(2025, 12, 24, 18, 30)       # future -> last year
    assert entries["old.txt"].mode == 0o600 and entries["old.txt"].owner == "1000"
    link = entries["www"]
    assert link.is_link and not link.is_dir and link.link_target == "/var/www/html"
    assert entries["null"].size == 0 and not entries["null"].is_dir
    assert entries["suid"].mode == 0o5755 and entries["suid"].mtime == ts(2024, 2, 29)
    assert entries["acl dir"].is_dir
    assert entries["nogroup.txt"].owner == "ftp" and entries["nogroup.txt"].size == 42
    assert entries["iso.txt"].mtime == ts(2025, 6, 1, 13, 45)
    assert parse_list_line("total 0") is None


def test_parse_dos_lines():
    lines = [
        "11-15-26  12:30PM       <DIR>          My Folder",
        "01-02-25  09:15AM               1,234 notes.txt",
        "12-31-99  12:05AM                  10 y2k.txt",
        "11-15-2026  23:59                 77 24h.txt",
    ]
    entries = {e.name: e for e in parse_list(lines, "/", NOW)}
    assert entries["My Folder"].is_dir and entries["My Folder"].mtime == ts(2026, 11, 15, 12, 30)
    assert entries["notes.txt"].size == 1234 and entries["notes.txt"].mtime == ts(2025, 1, 2, 9, 15)
    assert entries["y2k.txt"].mtime == ts(1999, 12, 31, 0, 5)
    assert entries["24h.txt"].mtime == ts(2026, 11, 15, 23, 59) and entries["24h.txt"].size == 77


def test_parse_mlsx_lines():
    f = parse_mlsx_line("type=file;size=42;modify=20261115123045.5;UNIX.mode=0644;"
                        "UNIX.owner=bruno;UNIX.group=staff; my file.txt", "/d")
    assert f.name == "my file.txt" and f.path == "/d/my file.txt" and f.size == 42
    assert f.mtime == ts(2026, 11, 15, 12, 30, 45) + 0.5
    assert f.mode == 0o644 and f.owner == "bruno" and f.group == "staff"
    d = parse_mlsx_line("type=dir;modify=20260101000000; sub", "/")
    assert d.is_dir and d.path == "/sub"
    assert parse_mlsx_line("type=cdir;modify=20260101000000; .", "/") is None
    assert parse_mlsx_line("type=pdir;modify=20260101000000; ..", "/") is None
    link = parse_mlsx_line("type=OS.unix=slink:/etc/hosts;size=10; hosts", "/")
    assert link.is_link and link.link_target == "/etc/hosts"
    mlst = parse_mlsx_line(" type=file;size=3; /full/path/name.txt", "/full/path")
    assert mlst.name == "name.txt"


def test_time_inference_near_new_year():
    now = datetime.datetime(2027, 1, 2, tzinfo=datetime.timezone.utc)
    entry = parse_list_line("-rw-r--r-- 1 a b 1 Dec 31 23:00 x", "/", now)
    assert entry.mtime == ts(2026, 12, 31, 23, 0)
    assert time.gmtime(entry.mtime).tm_year == 2026
