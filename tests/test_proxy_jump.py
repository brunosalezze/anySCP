# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""SSH connections through a proxy (SOCKS5, SOCKS4a, HTTP CONNECT) and
through jump hosts in series, against the throwaway OpenSSH server (which
is also the jump host: it forwards to itself)."""

import pytest
from conftest import AcceptAllPrompter

from anyscp import config
from anyscp.backends import create_fs
from anyscp.backends.base import FSError
from anyscp.backends.proxy import ProxyError
from anyscp.backends.sshconfig import parse_host_spec


def ssh_site(sshd, protocol="sftp", **extra):
    site = {"protocol": protocol, "host": "127.0.0.1", "port": sshd["port"], "user": sshd["user"],
            "auth": "key", "key_file": sshd["key"]}
    site.update(extra)
    return site


def connect(site):
    prompter = AcceptAllPrompter()
    fs = create_fs(site, prompter)
    fs.connect()
    return fs, prompter


@pytest.mark.parametrize("protocol", ["sftp", "scp"])
def test_socks5_proxy(sshd, proxy_server, protocol):
    proxy = proxy_server()
    fs, prompter = connect(ssh_site(sshd, protocol, proxy_type="socks5", proxy_host="127.0.0.1",
                                    proxy_port=proxy.port))
    try:
        assert fs.listdir(sshd["root"]) is not None
    finally:
        fs.close()
    assert proxy.targets == [("127.0.0.1", sshd["port"])]
    assert any("through the SOCKS5 proxy" in m for m in prompter.messages)


def test_socks5_proxy_login(sshd, proxy_server):
    proxy = proxy_server("proxyuser", "proxy pass")
    site = ssh_site(sshd, proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=proxy.port,
                    proxy_user="proxyuser", _proxy_password="proxy pass")
    fs, _prompter = connect(site)
    fs.close()
    assert len(proxy.targets) == 1

    with pytest.raises(ProxyError, match="rejected the user name or password"):
        connect(dict(site, _proxy_password="wrong"))
    with pytest.raises(ProxyError, match="needs a user name and password"):
        connect(dict(site, proxy_user="", _proxy_password=None))


def test_proxy_password_is_asked_when_missing(sshd, proxy_server):
    proxy = proxy_server("proxyuser", "asked")
    site = ssh_site(sshd, proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=proxy.port,
                    proxy_user="proxyuser")
    prompter = AcceptAllPrompter(password="asked")
    fs = create_fs(site, prompter)
    fs.connect()
    fs.close()
    assert any("for the SOCKS5 proxy" in m for m in prompter.messages)


def test_socks4a_proxy_with_host_name(sshd, proxy_server):
    proxy = proxy_server()
    fs, _prompter = connect(ssh_site(sshd, host="localhost", proxy_type="socks4", proxy_host="127.0.0.1",
                                     proxy_port=proxy.port))
    fs.close()
    assert proxy.targets == [("localhost", sshd["port"])]       # the proxy resolved the name


def test_http_connect_proxy(sshd, proxy_server):
    proxy = proxy_server("web", "secret")
    site = ssh_site(sshd, proxy_type="http", proxy_host="127.0.0.1", proxy_port=proxy.port,
                    proxy_user="web", _proxy_password="secret")
    fs, _prompter = connect(site)
    fs.close()
    assert proxy.targets == [("127.0.0.1", sshd["port"])]
    with pytest.raises(ProxyError, match="rejected the user name or password"):
        connect(dict(site, _proxy_password="nope"))


def test_unreachable_proxy():
    port = __import__("conftest").free_port()
    with pytest.raises(ProxyError, match="Could not connect to the proxy"):
        connect({"protocol": "sftp", "host": "example.invalid", "port": 22, "user": "x",
                 "proxy_type": "socks5", "proxy_host": "127.0.0.1", "proxy_port": port})


def test_parse_host_spec():
    assert parse_host_spec("alice@jump.example.com:2222") == ("alice", "jump.example.com", 2222)
    assert parse_host_spec("jump") == ("", "jump", 0)
    assert parse_host_spec("ssh://bob@[2001:db8::1]:22") == ("bob", "2001:db8::1", 22)
    assert parse_host_spec("[::1]") == ("", "::1", 0)
    assert parse_host_spec("me@host") == ("me", "host", 0)


def test_jump_host_given_by_address(sshd):
    """A jump host given as user@host:port logs in like the site (its key)."""
    site = ssh_site(sshd, jump_hosts=f"{sshd['user']}@127.0.0.1:{sshd['port']}")
    site["_jumps"] = config.jump_hops(site, sites=[])
    fs, prompter = connect(site)
    try:
        assert fs.listdir(sshd["root"]) is not None
        jump = fs.conn.jumps[0]
        assert jump.active
    finally:
        fs.close()
    assert sum("Opening a tunnel through" in m for m in prompter.messages) == 1
    assert not jump.active                          # closed with the session


def test_jump_hosts_in_series_through_saved_sites(sshd):
    """Two saved sites as jump hosts, one after the other; each logs in with
    its own settings (here: its own key file)."""
    first = config.new_site(name="Bastion", protocol="sftp", host="127.0.0.1", port=sshd["port"],
                            user=sshd["user"], auth="key", key_file=sshd["key"])
    second = config.new_site(name="Inner jump", protocol="sftp", host="localhost", port=sshd["port"],
                             user=sshd["user"], auth="key", key_file=sshd["key"])
    site = ssh_site(sshd, "scp", key_file=sshd["key"], jump_hosts="bastion, Inner Jump")
    site["_jumps"] = config.jump_hops(site, sites=[first, second])
    assert [h["host"] for h in site["_jumps"]] == ["127.0.0.1", "localhost"]
    fs, prompter = connect(site)
    try:
        assert fs.listdir(sshd["root"]) is not None
        assert len(fs.conn.jumps) == 2
    finally:
        fs.close()
    tunnels = [m for m in prompter.messages if "Opening a tunnel through" in m]
    assert len(tunnels) == 2 and "through 127.0.0.1 to localhost" in tunnels[0]


def test_saved_jump_host_brings_its_own_jump_hosts(sshd):
    outer = config.new_site(name="Outer", protocol="sftp", host="127.0.0.1", port=sshd["port"],
                            user=sshd["user"], auth="key", key_file=sshd["key"])
    inner = config.new_site(name="Inner", protocol="sftp", host="localhost", port=sshd["port"],
                            user=sshd["user"], auth="key", key_file=sshd["key"], jump_hosts="Outer")
    site = ssh_site(sshd, jump_hosts="Inner")
    hops = config.jump_hops(site, sites=[outer, inner])
    assert [h["name"] for h in hops] == ["Outer", "Inner"]
    inner_loop = dict(outer, jump_hosts="Inner")
    with pytest.raises(ValueError, match="circle"):
        config.jump_hops(site, sites=[inner_loop, inner])


def test_jump_host_behind_a_proxy(sshd, proxy_server):
    """The site's proxy carries the connection to the first jump host only."""
    proxy = proxy_server()
    site = ssh_site(sshd, jump_hosts=f"127.0.0.1:{sshd['port']}", proxy_type="socks5",
                    proxy_host="127.0.0.1", proxy_port=proxy.port, user=sshd["user"])
    site["_jumps"] = config.jump_hops(site, sites=[])
    site["_jumps"][0]["user"] = sshd["user"]
    fs, _prompter = connect(site)
    fs.close()
    assert proxy.targets == [("127.0.0.1", sshd["port"])]


def test_jump_host_that_cannot_reach_the_target(sshd):
    port = __import__("conftest").free_port()
    site = ssh_site(sshd, port=port, jump_hosts=f"{sshd['user']}@127.0.0.1:{sshd['port']}")
    site["_jumps"] = config.jump_hops(site, sites=[])
    with pytest.raises(FSError, match="could not connect to 127.0.0.1"):
        connect(site)


def test_ssh_config_proxyjump_is_native(sshd, tmp_path, monkeypatch):
    """ProxyJump in ~/.ssh/config goes through anySCP's own connections (no
    ssh program needed); the jump host is an alias of that config too."""
    from anyscp.backends import sshconfig
    cfg = tmp_path / "config"
    cfg.write_text(f"""
Host bastion
    HostName 127.0.0.1
    Port {sshd['port']}
    User {sshd['user']}
    IdentityFile {sshd['key']}

Host target
    HostName localhost
    Port {sshd['port']}
    User {sshd['user']}
    IdentityFile {sshd['key']}
    ProxyJump bastion
""")
    monkeypatch.setattr(sshconfig, "CONFIG_PATH", cfg)
    fs, prompter = connect({"protocol": "sftp", "host": "target", "port": 22, "user": "", "auth": "auto"})
    try:
        assert fs.listdir(sshd["root"]) is not None
    finally:
        fs.close()
    assert any("through 127.0.0.1 to localhost" in m for m in prompter.messages)
