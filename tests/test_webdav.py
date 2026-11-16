# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Tests for the WebDAV backend against a real local wsgidav/cheroot server.

Needs the test-only packages ``pytest wsgidav cheroot cryptography``.
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
import logging  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("wsgidav")
pytest.importorskip("cheroot")

from cheroot import wsgi  # noqa: E402
from cheroot.ssl.builtin import BuiltinSSLAdapter  # noqa: E402
from wsgidav.wsgidav_app import WsgiDAVApp  # noqa: E402

from anyscp import config  # noqa: E402
from anyscp.backends import create_fs  # noqa: E402
from anyscp.backends.base import (  # noqa: E402
    CAP_RESUME,
    AlreadyExists,
    AuthFailed,
    Cancelled,
    ConnectionLost,
    FSError,
    NotFound,
    NotSupported,
)
from anyscp.backends import webdav as davmod  # noqa: E402
from anyscp.backends.webdav import (  # noqa: E402
    WebDAVFileSystem,
    parse_challenges,
    parse_multistatus,
)

# cheroot's own buffered writers complain (on garbage collection) about the
# sockets these tests deliberately abandon; that is server-side noise.
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")

USER, PASSWORD = "alice", "s3cret ü"     # non-ASCII password on purpose
MOUNT = "/dav files"                     # share path with a space in it


def test_config_dir_is_isolated():
    assert str(config.CONFIG_DIR).startswith(tempfile.gettempdir())


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def make_cert(directory: Path) -> tuple[Path, Path]:
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
    certfile, keyfile = directory / "cert.pem", directory / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.TraditionalOpenSSL,
                                          serialization.NoEncryption()))
    return certfile, keyfile


class Prompter:
    def __init__(self, passwords=(), cert="once", user=None):
        self.passwords = list(passwords)
        self.cert = cert
        self.user = user
        self.password_prompts: list[str] = []
        self.text_prompts: list[str] = []
        self.cert_prompts: list[tuple] = []
        self.logs: list[str] = []

    def ask_password(self, title, message):
        self.password_prompts.append(message)
        return self.passwords.pop(0) if self.passwords else None

    def ask_text(self, title, message, default=""):
        self.text_prompts.append(message)
        return self.user

    def ask_interactive(self, title, instructions, prompts):
        return None

    def verify_host_key(self, host, port, key_type, fingerprint, changed):
        return "reject"

    def verify_certificate(self, host, port, fingerprint, subject, issuer, error):
        self.cert_prompts.append((host, port, fingerprint, error))
        return self.cert

    def log(self, message):
        self.logs.append(message)


class DAVServer:
    def __init__(self, root: Path, auth: str, cert: tuple[Path, Path] | None = None):
        dav_config = {
            "host": "127.0.0.1",
            "port": 0,
            "provider_mapping": {MOUNT: str(root)},
            "simple_dc": {"user_mapping": {"*": {USER: {"password": PASSWORD}}}},
            "http_authenticator": {
                "domain_controller": None,
                "accept_basic": True,
                "accept_digest": auth == "digest",
                "default_to_digest": auth == "digest",
            },
            "verbose": 1,
            "logging": {"enable": False},
            "dir_browser": {"enable": False},
        }
        self.app = WsgiDAVApp(dav_config)
        self.server = wsgi.Server(("127.0.0.1", 0), self.app, numthreads=8)
        # quiet: some tests hang up mid-handshake / mid-request on purpose
        self.server.error_log = lambda *args, **kwargs: None
        if cert is not None:
            self.server.ssl_adapter = BuiltinSSLAdapter(str(cert[0]), str(cert[1]))
        self.server.prepare()
        self.port = self.server.bind_addr[1]
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.stop()
        self.thread.join(5)


@pytest.fixture(scope="module")
def dav_env(tmp_path_factory):
    logging.getLogger("wsgidav").setLevel(logging.ERROR)
    base = tmp_path_factory.mktemp("dav")
    root = base / "root"
    root.mkdir()
    cert = make_cert(base)
    servers = {
        "basic": DAVServer(root, "basic"),
        "digest": DAVServer(root, "digest"),
        "https": DAVServer(root, "basic", cert),
    }
    yield {"root": root, "servers": servers, "cert": cert}
    # cheroot leaves buffered writers of connections we abandoned on purpose;
    # collect them now, silently, instead of at some random later point.
    import gc
    previous_hook = sys.unraisablehook
    sys.unraisablehook = lambda unraisable: None
    try:
        for server in servers.values():
            server.stop()
        gc.collect()
    finally:
        sys.unraisablehook = previous_hook


def site_for(env, kind: str, **extra) -> dict:
    site = config.new_site(protocol="webdav", host="127.0.0.1", port=env["servers"][kind].port,
                           user=USER, timeout=10, tls=kind == "https", remote_dir=MOUNT)
    site["_password"] = PASSWORD
    site.update(extra)
    return site


def connect(env, kind, prompter=None, **extra) -> WebDAVFileSystem:
    fs = create_fs(site_for(env, kind, **extra), prompter or Prompter())
    assert isinstance(fs, WebDAVFileSystem)
    fs.connect()
    return fs


@pytest.fixture
def workdir(dav_env):
    name = "t ü-" + uuid.uuid4().hex[:8]
    local = dav_env["root"] / name
    local.mkdir()
    return local, f"{MOUNT}/{name}"


KINDS = ["basic", "digest", "https"]


# --------------------------------------------------------------------------
# Connecting and authentication
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_connect_and_info(dav_env, kind):
    prompter = Prompter()
    fs = connect(dav_env, kind, prompter)
    try:
        assert fs.home() == MOUNT
        info = dict(fs.info())
        assert info["Protocol"] == ("WebDAV over HTTPS" if kind == "https" else "WebDAV")
        assert info["Authentication"].startswith("Digest" if kind == "digest" else "Basic")
        assert "%20" in info["URL"]
        if kind == "https":
            assert info["Encryption"].startswith("TLS")
            assert len(prompter.cert_prompts) == 1
        else:
            assert info["Encryption"] == "None"
        assert prompter.password_prompts == []
        assert not fs.has(CAP_RESUME)
        fs.keepalive()
    finally:
        fs.close()


@pytest.mark.parametrize("kind", ["basic", "digest"])
def test_password_prompt_and_clone(dav_env, kind):
    prompter = Prompter(passwords=[PASSWORD])
    site = site_for(dav_env, kind)
    del site["_password"]
    fs = WebDAVFileSystem(site, prompter)
    fs.connect()
    try:
        assert len(prompter.password_prompts) == 1
        assert fs.site["_password"] == PASSWORD
        other = fs.clone()
        try:
            assert other is not fs and other._session_trust is fs._session_trust
            other.listdir(MOUNT)
        finally:
            other.close()
        assert len(prompter.password_prompts) == 1
    finally:
        fs.close()


def test_username_prompt(dav_env):
    prompter = Prompter(passwords=[PASSWORD], user=USER)
    site = site_for(dav_env, "basic", user="")
    del site["_password"]
    fs = WebDAVFileSystem(site, prompter)
    fs.connect()
    try:
        assert len(prompter.text_prompts) == 1 and fs.site["user"] == USER
    finally:
        fs.close()


@pytest.mark.parametrize("kind", ["basic", "digest"])
def test_wrong_password_then_cancel(dav_env, kind):
    prompter = Prompter(passwords=[])
    fs = WebDAVFileSystem(site_for(dav_env, kind, _password="wrong"), prompter)
    with pytest.raises(Cancelled):
        fs.connect()
    assert len(prompter.password_prompts) == 1
    assert "failed" in prompter.password_prompts[0]


@pytest.mark.parametrize("kind", ["basic", "digest"])
def test_wrong_password_three_times(dav_env, kind):
    prompter = Prompter(passwords=["bad1", "bad2", "bad3", PASSWORD])
    site = site_for(dav_env, kind)
    del site["_password"]
    with pytest.raises(AuthFailed):
        WebDAVFileSystem(site, prompter).connect()
    assert len(prompter.password_prompts) == 3


def test_wrong_password_then_right(dav_env):
    prompter = Prompter(passwords=[PASSWORD])
    fs = WebDAVFileSystem(site_for(dav_env, "https", _password="wrong"), prompter)
    fs.connect()
    try:
        assert len(prompter.password_prompts) == 1
        assert len(prompter.cert_prompts) == 1
        assert fs.listdir(MOUNT) is not None
    finally:
        fs.close()


def test_certificate_rejected(dav_env):
    with pytest.raises(AuthFailed):
        WebDAVFileSystem(site_for(dav_env, "https"), Prompter(cert="reject")).connect()


@pytest.mark.parametrize("kind,proxy_kind", [("basic", "socks5"), ("https", "http")])
def test_through_a_proxy(dav_env, workdir, proxy_server, kind, proxy_kind):
    local, remote = workdir
    # its own server: the connections the proxy relays must not tie up the
    # shared servers' worker threads for the tests after this one
    server = DAVServer(dav_env["root"], "basic", dav_env["cert"] if kind == "https" else None)
    env = dict(dav_env, servers={kind: server})
    proxy = proxy_server("davproxy", "pw") if proxy_kind == "http" else proxy_server()
    extra = {"proxy_type": proxy_kind, "proxy_host": "127.0.0.1", "proxy_port": proxy.port}
    if proxy_kind == "http":
        extra.update(proxy_user="davproxy", _proxy_password="pw")
    try:
        fs = connect(env, kind, **extra)
        try:
            payload = os.urandom(200_000)
            with fs.open_write(f"{remote}/p.bin", size=len(payload)) as fh:
                fh.write(payload)
            assert (local / "p.bin").read_bytes() == payload
            assert [e.name for e in fs.listdir(remote)] == ["p.bin"]
        finally:
            fs.close()
        assert proxy.targets and {t[1] for t in proxy.targets} == {server.port}
    finally:
        previous_hook = sys.unraisablehook
        sys.unraisablehook = lambda unraisable: None
        try:
            server.stop()
            import gc
            gc.collect()
        finally:
            sys.unraisablehook = previous_hook


def test_connection_refused():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    site = config.new_site(protocol="webdav", host="127.0.0.1", port=port, user=USER, timeout=3)
    with pytest.raises(ConnectionLost):
        WebDAVFileSystem(site, Prompter()).connect()


def test_not_a_webdav_server():
    """A plain HTTP server answering 501 to PROPFIND."""
    import http.server

    httpd = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        site = config.new_site(protocol="webdav", host="127.0.0.1",
                               port=httpd.server_address[1], timeout=3)
        with pytest.raises(NotSupported):
            WebDAVFileSystem(site, Prompter()).connect()
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# File system operations
# --------------------------------------------------------------------------

NAMES = {"plain.txt", "with space.txt", "ünïcødé ✓.txt", "per%cent&amp+plus#hash.txt", "sub dir"}


def _populate(local: Path) -> None:
    (local / "plain.txt").write_bytes(b"hello")
    (local / "with space.txt").write_bytes(b"x" * 10)
    (local / "ünïcødé ✓.txt").write_bytes(b"unicode")
    (local / "per%cent&amp+plus#hash.txt").write_bytes(b"odd")
    (local / "sub dir").mkdir()
    (local / "sub dir" / "inner.txt").write_bytes(b"inner")
    os.utime(local / "plain.txt", (1_600_000_000, 1_600_000_000))


@pytest.mark.parametrize("kind", KINDS)
def test_listdir_and_stat(dav_env, workdir, kind):
    local, remote = workdir
    _populate(local)
    fs = connect(dav_env, kind)
    try:
        entries = {e.name: e for e in fs.listdir(remote)}
        assert set(entries) == NAMES
        plain = entries["plain.txt"]
        assert plain.path == f"{remote}/plain.txt"
        assert plain.size == 5 and not plain.is_dir
        assert plain.mtime == 1_600_000_000
        sub = entries["sub dir"]
        assert sub.is_dir and sub.path == f"{remote}/sub dir" and sub.size == 0
        assert entries["ünïcødé ✓.txt"].size == 7
        assert entries["per%cent&amp+plus#hash.txt"].size == 3

        assert fs.stat(f"{remote}/ünïcødé ✓.txt").size == 7
        st = fs.stat(f"{remote}/sub dir")
        assert st.is_dir and st.name == "sub dir"
        assert fs.stat(remote + "/").is_dir                 # trailing slash normalised
        assert fs.stat(f"{remote}/per%cent&amp+plus#hash.txt").size == 3
        assert fs.exists(f"{remote}/with space.txt")
        assert not fs.exists(f"{remote}/missing")
        with pytest.raises(NotFound):
            fs.stat(f"{remote}/missing")
        with pytest.raises(NotFound):
            fs.listdir(f"{remote}/missing dir")
        assert [e.name for e in fs.listdir(f"{remote}/sub dir")] == ["inner.txt"]
        assert fs.listdir(f"{remote}/sub dir")[0].path == f"{remote}/sub dir/inner.txt"
    finally:
        fs.close()


@pytest.mark.parametrize("kind", KINDS)
def test_modifications(dav_env, workdir, kind):
    local, remote = workdir
    _populate(local)
    fs = connect(dav_env, kind)
    try:
        fs.mkdir(f"{remote}/new ✓ dir")
        assert (local / "new ✓ dir").is_dir()
        with pytest.raises(AlreadyExists):
            fs.mkdir(f"{remote}/new ✓ dir")
        with pytest.raises(NotFound):
            fs.mkdir(f"{remote}/no/such/parent")

        fs.rename(f"{remote}/plain.txt", f"{remote}/new ✓ dir/renamed ü.txt")       # MOVE
        assert (local / "new ✓ dir" / "renamed ü.txt").read_bytes() == b"hello"
        assert not (local / "plain.txt").exists()
        with pytest.raises(AlreadyExists):                                         # Overwrite: F
            fs.rename(f"{remote}/with space.txt", f"{remote}/new ✓ dir/renamed ü.txt")
        with pytest.raises(NotFound):
            fs.rename(f"{remote}/missing", f"{remote}/x")
        fs.rename(f"{remote}/sub dir", f"{remote}/moved dir")                       # collection
        assert (local / "moved dir" / "inner.txt").exists()

        with pytest.raises(FSError) as excinfo:
            fs.rmdir(f"{remote}/new ✓ dir")                # not empty: must not recurse
        assert not isinstance(excinfo.value, NotFound)
        assert (local / "new ✓ dir" / "renamed ü.txt").exists()
        fs.remove(f"{remote}/new ✓ dir/renamed ü.txt")
        with pytest.raises(NotFound):
            fs.remove(f"{remote}/new ✓ dir/renamed ü.txt")
        fs.rmdir(f"{remote}/new ✓ dir")
        assert not (local / "new ✓ dir").exists()
        with pytest.raises(NotFound):
            fs.rmdir(f"{remote}/new ✓ dir")

        removed = []
        fs.remove_tree(f"{remote}/moved dir", progress=removed.append)
        assert not (local / "moved dir").exists() and removed

        with pytest.raises(NotSupported):
            fs.chmod(f"{remote}/with space.txt", 0o644)
        with pytest.raises(NotSupported):
            fs.symlink("a", f"{remote}/link")
    finally:
        fs.close()


@pytest.mark.parametrize("kind", KINDS)
def test_read_write_roundtrip(dav_env, workdir, kind):
    local, remote = workdir
    payload = os.urandom(3 * 1024 * 1024 + 321)
    fs = connect(dav_env, kind)
    try:
        # streamed PUT with a known size
        with fs.open_write(f"{remote}/big ü.bin", size=len(payload)) as fh:
            for i in range(0, len(payload), 100_000):
                fh.write(payload[i:i + 100_000])
        assert (local / "big ü.bin").read_bytes() == payload
        assert fs.stat(f"{remote}/big ü.bin").size == len(payload)

        # spooled PUT (size unknown)
        fh = fs.open_write(f"{remote}/spooled.bin")
        fh.write(payload[:500_000])
        fh.write(payload[500_000:])
        fh.close()
        assert (local / "spooled.bin").read_bytes() == payload

        # empty file
        fs.open_write(f"{remote}/empty.txt", size=0).close()
        assert (local / "empty.txt").read_bytes() == b""

        fh = fs.open_read(f"{remote}/big ü.bin")
        chunks = []
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
        fh.close()
        assert b"".join(chunks) == payload

        with fs.open_read(f"{remote}/big ü.bin", offset=2_000_000) as fh:     # Range
            assert fh.read() == payload[2_000_000:]
        with fs.open_read(f"{remote}/big ü.bin", offset=len(payload)) as fh:
            assert fh.read() == b""
        with pytest.raises(NotFound):
            fs.open_read(f"{remote}/missing.bin")
        with pytest.raises(NotSupported):
            fs.open_write(f"{remote}/big ü.bin", offset=10, size=len(payload))
        with pytest.raises(NotFound):
            with fs.open_write(f"{remote}/no such dir/x.bin", size=3) as fh:
                fh.write(b"abc")
    finally:
        fs.close()


def test_incomplete_upload_is_reported(dav_env, workdir):
    local, remote = workdir
    fs = connect(dav_env, "basic")
    try:
        fh = fs.open_write(f"{remote}/partial.bin", size=1000)
        fh.write(b"x" * 10)
        with pytest.raises(FSError):
            fh.close()
        with pytest.raises(FSError):
            fh2 = fs.open_write(f"{remote}/toolong.bin", size=2)
            fh2.write(b"abc")
        fh2.abort()
        # the instance keeps working after abandoned uploads
        assert {e.name for e in fs.listdir(remote)} <= {"partial.bin", "toolong.bin"}
    finally:
        fs.close()


def test_early_close_and_concurrency(dav_env, workdir):
    local, remote = workdir
    payload = os.urandom(4 * 1024 * 1024)
    (local / "big.bin").write_bytes(payload)
    fs = connect(dav_env, "https")
    try:
        fh = fs.open_read(f"{remote}/big.bin")
        assert fh.read(1000) == payload[:1000]
        # a WebDAV instance can do other requests while a transfer is open
        assert [e.name for e in fs.listdir(remote)] == ["big.bin"]
        fh.close()                                          # abandon mid-body
        with fs.open_read(f"{remote}/big.bin") as fh:
            assert fh.read() == payload
        clone = fs.clone()
        try:
            with clone.open_read(f"{remote}/big.bin") as fh:
                assert fh.read(10) == payload[:10]
        finally:
            clone.close()
    finally:
        fs.close()


def test_reconnects_after_server_closed_keepalive(dav_env, workdir):
    local, remote = workdir
    (local / "f.txt").write_bytes(b"data")
    fs = connect(dav_env, "digest")
    try:
        fs.listdir(remote)
        assert fs._pool and any(c.sock is not None for c in fs._pool)
        for conn in fs._pool:                       # pretend the server dropped them
            if conn.sock is not None:
                conn.sock.shutdown(socket.SHUT_RDWR)
        assert [e.name for e in fs.listdir(remote)] == ["f.txt"]
        for conn in fs._pool:                       # and a silently dead socket
            if conn.sock is not None:
                conn.sock.close()
        assert fs.stat(f"{remote}/f.txt").size == 4
    finally:
        fs.close()


def test_retries_request_on_stale_keepalive(dav_env, workdir, monkeypatch):
    """The server closed an idle connection but it still looked usable:
    the first send fails and the request is retried on a new connection."""
    local, remote = workdir
    (local / "f.txt").write_bytes(b"data")
    fs = connect(dav_env, "https")
    try:
        fs.listdir(remote)
        monkeypatch.setattr(davmod, "_sock_unusable", lambda sock: False)
        for conn in fs._pool:
            if conn.sock is not None:
                conn.sock.shutdown(socket.SHUT_WR)
        assert [e.name for e in fs.listdir(remote)] == ["f.txt"]
        with pytest.raises(FSError):
            fs.listdir(f"{remote}/f.txt")           # a file is not a directory
    finally:
        fs.close()


# --------------------------------------------------------------------------
# Unit tests (no server)
# --------------------------------------------------------------------------

MULTISTATUS = b"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/me/docs/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
      <d:getlastmodified>Sun, 15 Nov 2026 12:30:00 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>https://cloud.example.com/remote.php/dav/files/me/docs/My%20Report%20%C3%BC.pdf</d:href>
    <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>1234</d:getcontentlength>
      <d:getlastmodified>Sun, 15 Nov 2026 12:30:00 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
    <d:propstat><d:prop><d:displayname/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>Sub%20Folder/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""


def test_parse_multistatus():
    entries = parse_multistatus(MULTISTATUS, "/remote.php/dav/files/me/docs/")
    paths = [e.path for e in entries]
    assert paths == ["/remote.php/dav/files/me/docs",
                     "/remote.php/dav/files/me/docs/My Report ü.pdf",
                     "/remote.php/dav/files/me/docs/Sub Folder"]
    docs, report, sub = entries
    assert docs.is_collection() and sub.is_collection() and not report.is_collection()
    info = report.to_info()
    assert info.name == "My Report ü.pdf" and info.size == 1234
    assert info.mtime == datetime.datetime(2026, 11, 15, 12, 30,
                                           tzinfo=datetime.timezone.utc).timestamp()
    assert davmod.DAV + "displayname" not in report.props      # 404 propstat ignored


def test_parse_challenges():
    challenges = parse_challenges([
        'Basic realm="files", Digest realm="files, inc", nonce="abc==", qop="auth,auth-int", '
        'algorithm=MD5, stale=TRUE',
        'Bearer'])
    assert [s for s, _ in challenges] == ["basic", "digest", "bearer"]
    assert challenges[0][1] == {"realm": "files"}
    digest = challenges[1][1]
    assert digest["realm"] == "files, inc" and digest["nonce"] == "abc=="
    assert digest["qop"] == "auth,auth-int" and digest["stale"] == "TRUE"
    assert davmod._choose_challenge(challenges)[0] == "digest"
    assert davmod._choose_challenge(challenges[:1])[0] == "basic"
    assert davmod._choose_challenge(challenges[2:]) is None


def test_digest_response_rfc2617_example(monkeypatch):
    """The worked example from RFC 2617, section 3.5."""
    auth = davmod._Auth("Mufasa")
    auth.password = "Circle Of Life"
    auth.set_challenge("digest", {"realm": "testrealm@host.com", "qop": "auth,auth-int",
                                  "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093",
                                  "opaque": "5ccc069c403ebaf9f0171e9517f40e41"})
    monkeypatch.setattr(davmod.os, "urandom", lambda n: bytes.fromhex("0a4f113b"))
    header = auth.header("GET", "/dir/index.html")
    assert header.startswith("Digest ")
    assert 'response="6629fae49393a05397450978507c4ef1"' in header
    assert "nc=00000001" in header and 'cnonce="0a4f113b"' in header
    assert 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in header
    assert "nc=00000002" in auth.header("GET", "/dir/index.html")


def test_basic_header():
    auth = davmod._Auth("user")
    auth.password = "pässword"
    auth.set_challenge("basic", {"realm": "x"})
    import base64
    assert auth.header("GET", "/") == "Basic " + base64.b64encode(
        "user:pässword".encode()).decode()
