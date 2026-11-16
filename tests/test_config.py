# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Config helpers, quick-connect URL parsing and WinSCP import."""

from anyscp import winscp_import
from anyscp.config import SiteStore, default_port, site_from_url


def test_quick_connect_urls():
    s = site_from_url("bob@example.com")
    assert (s["protocol"], s["host"], s["user"], s["port"]) == ("sftp", "example.com", "bob", 22)
    s = site_from_url("ftps://alice:pw%40x@ftp.example.org/pub/files")
    assert s["protocol"] == "ftp" and s["ftp_encryption"] == "implicit" and s["port"] == 990
    assert s["_password"] == "pw@x" and s["remote_dir"] == "/pub/files"
    s = site_from_url("davs://cloud.example.org:8443/remote.php/dav")
    assert s["protocol"] == "webdav" and s["tls"] and s["port"] == 8443
    assert site_from_url("gopher://x") is None
    assert site_from_url("") is None


def test_default_ports():
    assert default_port({"protocol": "ftp", "ftp_encryption": "implicit"}) == 990
    assert default_port({"protocol": "webdav", "tls": True}) == 443
    assert default_port({"protocol": "scp"}) == 22


def test_password_roundtrip():
    enc = winscp_import.encrypt_password("s3cr3t ünï", "bobexample.com", shift=7)
    assert winscp_import.decrypt_password(enc, "bobexample.com") == "s3cr3t ünï"
    assert winscp_import.decrypt_password(enc, "wrongkey") is None


def test_parse_ini(tmp_path):
    pw = winscp_import.encrypt_password("hunter2", "rootweb.example.com", shift=3)
    ini = f"""[Sessions\\Production/Web%20Server]
HostName=web.example.com
UserName=root
PortNumber=2222
FSProtocol=2
Password={pw}
RemoteDirectory=/var/www
Color=255

[Sessions\\Old%20FTP]
HostName=ftp.example.com
UserName=anon
FSProtocol=5
Ftps=3

[Sessions\\Bucket]
HostName=s3.amazonaws.com
FSProtocol=7

[Sessions\\Default%20Settings]
HostName=ignored

[Configuration\\Interface]
Foo=1
"""
    sites, warnings = winscp_import.parse_text(ini)
    assert [s["name"] for s in sites] == ["Web Server", "Old FTP"]
    web, ftp = sites
    assert web["folder"] == "Production" and web["port"] == 2222 and web["protocol"] == "sftp"
    assert web["_password"] == "hunter2" and web["remote_dir"] == "/var/www" and web["color"] == "#ff0000"
    assert ftp["protocol"] == "ftp" and ftp["ftp_encryption"] == "explicit" and ftp["port"] == 21
    assert any("S3" in w for w in warnings)

    store = SiteStore(tmp_path / "sites.json")
    assert winscp_import.store_sites(store, sites, with_passwords=False) == 2
    assert winscp_import.store_sites(store, sites, with_passwords=False) == 0   # no duplicates
    reloaded = SiteStore(tmp_path / "sites.json")
    assert len(reloaded.sites) == 2 and not any("_password" in s for s in reloaded.sites)


def test_parse_ini_proxy_and_tunnel():
    ini = """[Sessions\\Behind%20proxy]
HostName=inner.example.com
UserName=me
FSProtocol=2
ProxyMethod=2
ProxyHost=socks.example.com
ProxyPort=9050
ProxyUsername=proxyme
Tunnel=1
TunnelHostName=bastion.example.com
TunnelUserName=jumper
TunnelPortNumber=2200

[Sessions\\HTTP%20proxy]
HostName=web.example.com
FSProtocol=6
ProxyMethod=3
ProxyHost=proxy.example.com

[Sessions\\Telnet%20proxy%20and%20FTP%20tunnel]
HostName=ftp.example.com
FSProtocol=5
ProxyMethod=4
Tunnel=1
TunnelHostName=bastion.example.com
"""
    sites, warnings = winscp_import.parse_text(ini)
    inner, web, ftp = sites
    assert (inner["proxy_type"], inner["proxy_host"], inner["proxy_port"], inner["proxy_user"]) == \
        ("socks5", "socks.example.com", 9050, "proxyme")
    assert inner["jump_hosts"] == "jumper@bastion.example.com:2200"
    assert (web["proxy_type"], web["proxy_port"]) == ("http", 8080)
    assert ftp.get("proxy_type", "none") == "none" and not ftp.get("jump_hosts")
    assert any("proxy password" in w for w in warnings)
    assert any("Telnet" in w for w in warnings) and any("SFTP and SCP sites only" in w for w in warnings)


def test_portable_folder_is_created_next_to_the_launcher(tmp_path, monkeypatch):
    import os
    import sys
    from anyscp import config
    monkeypatch.setenv("ANYSCP_LAUNCHER_DIR", str(tmp_path))
    monkeypatch.delenv("ANYSCP_PORTABLE", raising=False)
    if sys.platform == "darwin":
        # next to an app (usually in /Applications) only a folder made by hand counts
        assert config._portable_dir() is None and not (tmp_path / "anySCP-data").exists()
        (tmp_path / "anySCP-data").mkdir()
    assert config._portable_dir() == tmp_path / "anySCP-data"
    assert (tmp_path / "anySCP-data").is_dir()
    # read-only location: fall back to the per-user folders, leave nothing behind
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o555)
    try:
        monkeypatch.setenv("ANYSCP_LAUNCHER_DIR", str(ro))
        if os.access(ro, os.W_OK):      # running as root: permissions not enforced
            return
        assert config._portable_dir() is None
        assert not (ro / "anySCP-data").exists()
    finally:
        os.chmod(ro, 0o755)
    # opt-out
    monkeypatch.setenv("ANYSCP_LAUNCHER_DIR", str(tmp_path / "other"))
    (tmp_path / "other").mkdir()
    monkeypatch.setenv("ANYSCP_PORTABLE", "0")
    assert config._portable_dir() is None
    assert not (tmp_path / "other" / "anySCP-data").exists()
