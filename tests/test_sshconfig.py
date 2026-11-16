# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""~/.ssh/config resolution."""

from anyscp.backends.sshconfig import resolve


def test_alias_and_jump(tmp_path):
    key = tmp_path / "id_test"
    key.write_text("dummy")
    cfg = tmp_path / "config"
    cfg.write_text(f"""
Host web
    HostName web.internal.example.com
    User deploy
    Port 2200
    IdentityFile {key}
    ProxyJump bastion1,bastion2

Host plain
    HostName 10.0.0.5
""")
    r = resolve({"host": "web", "port": 22, "user": ""}, cfg)
    assert (r.hostname, r.port, r.user) == ("web.internal.example.com", 2200, "deploy")
    assert r.identity_files == [str(key)]
    assert r.proxy_command is None
    assert r.jump_hosts == ["bastion1", "bastion2"]     # connected through natively, in this order

    # explicit site values win over the config
    r = resolve({"host": "web", "port": 2222, "user": "me"}, cfg)
    assert (r.port, r.user) == (2222, "me")

    r = resolve({"host": "plain", "port": 22}, cfg)
    assert r.hostname == "10.0.0.5" and r.proxy_command is None

    r = resolve({"host": "unlisted.example.org", "port": 22}, cfg)
    assert r.hostname == "unlisted.example.org"


def test_missing_config(tmp_path):
    r = resolve({"host": "h", "port": 22, "user": "u"}, tmp_path / "nope")
    assert (r.hostname, r.port, r.user, r.proxy_command) == ("h", 22, "u", None)
