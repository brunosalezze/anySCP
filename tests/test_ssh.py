# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""SFTP and SCP backends against a real (throwaway) OpenSSH server."""

import os
import posixpath
import time

import pytest

from conftest import AcceptAllPrompter
from anyscp.backends import create_fs
from anyscp.backends.base import AlreadyExists, AuthFailed, Cancelled, NotFound


@pytest.fixture(params=["sftp", "scp"])
def fs(request, sshd):
    site = {"protocol": request.param, "host": "127.0.0.1", "port": sshd["port"],
            "user": sshd["user"], "auth": "key", "key_file": sshd["key"], "timeout": 10}
    prompter = AcceptAllPrompter()
    fs = create_fs(site, prompter)
    fs.connect()
    base = posixpath.join(sshd["root"], request.param + str(time.time_ns()))
    fs.mkdir(base)
    fs.test_base = base
    yield fs
    try:
        fs.remove_tree(base)
    finally:
        fs.close()


def test_listing_and_stat(fs):
    base = fs.test_base
    fs.mkdir(base + "/sub dir")
    w = fs.open_write(base + "/hello.txt", size=5)
    w.write(b"hello")
    w.close()
    os.symlink(base + "/sub dir", base + "/link")  # server is local
    names = {e.name: e for e in fs.listdir(base)}
    assert set(names) == {"sub dir", "hello.txt", "link"}
    assert names["sub dir"].is_dir and not names["sub dir"].is_link
    assert names["link"].is_dir and names["link"].is_link
    assert names["hello.txt"].size == 5 and not names["hello.txt"].is_dir
    assert names["hello.txt"].owner
    assert abs(names["hello.txt"].mtime - time.time()) < 120
    st = fs.stat(base + "/hello.txt")
    assert st.size == 5 and st.name == "hello.txt"
    with pytest.raises(NotFound):
        fs.stat(base + "/missing")
    with pytest.raises(NotFound):
        fs.listdir(base + "/missing")


def test_roundtrip_large(fs):
    data = os.urandom(3 * 1024 * 1024 + 123)
    path = fs.test_base + "/big.bin"
    w = fs.open_write(path, size=len(data))
    for i in range(0, len(data), 32768):
        w.write(data[i:i + 32768])
    w.close()
    assert fs.stat(path).size == len(data)
    r = fs.open_read(path)
    chunks = []
    while True:
        chunk = r.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    r.close()
    assert b"".join(chunks) == data
    # partial read from offset
    r = fs.open_read(path, offset=1000)
    assert r.read(10) == data[1000:1010]
    r.close()


def test_modifications(fs):
    base = fs.test_base
    w = fs.open_write(base + "/a.txt", size=3)
    w.write(b"abc")
    w.close()
    fs.rename(base + "/a.txt", base + "/b.txt")
    assert not fs.exists(base + "/a.txt") and fs.exists(base + "/b.txt")
    fs.chmod(base + "/b.txt", 0o600)
    assert fs.stat(base + "/b.txt").mode == 0o600
    fs.utime(base + "/b.txt", 1_600_000_000)
    assert int(fs.stat(base + "/b.txt").mtime) == 1_600_000_000
    fs.mkdir(base + "/d")
    with pytest.raises(AlreadyExists):
        fs.mkdir(base + "/d")
    fs.rmdir(base + "/d")
    fs.remove(base + "/b.txt")
    assert fs.listdir(base) == []


def test_remove_tree_and_walk(fs):
    base = fs.test_base
    for d in ("t", "t/x", "t/x/y"):
        fs.mkdir(f"{base}/{d}")
    for f in ("t/1", "t/x/2", "t/x/y/3"):
        w = fs.open_write(f"{base}/{f}", size=1)
        w.write(b"!")
        w.close()
    walked = [(p, sorted(d.name for d in ds), sorted(f.name for f in fs_)) for p, ds, fs_ in fs.walk(base + "/t")]
    assert walked[0] == (base + "/t", ["x"], ["1"])
    assert len(walked) == 3
    fs.remove_tree(base + "/t")
    assert not fs.exists(base + "/t")


def test_clone_and_exec(fs):
    other = fs.clone()
    try:
        assert other.listdir(fs.test_base) == []
        status, out, _ = fs.exec("echo hi")
        assert status == 0 and out.strip() == "hi"
        assert fs.realpath(fs.test_base + "/../" + posixpath.basename(fs.test_base)) == fs.test_base
    finally:
        other.close()
    # original still alive after closing the clone
    assert fs.listdir(fs.test_base) == []


def test_encrypted_key_prompts_for_passphrase(sshd):
    site = {"protocol": "sftp", "host": "127.0.0.1", "port": sshd["port"], "user": sshd["user"],
            "auth": "key", "key_file": sshd["enc_key"]}
    prompter = AcceptAllPrompter(passphrase="secret pass")
    fs = create_fs(site, prompter)
    fs.connect()
    assert fs.home()
    fs.close()


def test_cancelled_passphrase(sshd):
    site = {"protocol": "sftp", "host": "127.0.0.1", "port": sshd["port"], "user": sshd["user"],
            "auth": "key", "key_file": sshd["enc_key"]}
    fs = create_fs(site, AcceptAllPrompter(passphrase=None))
    with pytest.raises(Cancelled):
        fs.connect()


def test_rejected_host_key(sshd):
    class Reject(AcceptAllPrompter):
        def verify_host_key(self, *a):
            return "reject"
    site = {"protocol": "sftp", "host": "127.0.0.1", "port": sshd["port"], "user": sshd["user"],
            "auth": "key", "key_file": sshd["key"]}
    with pytest.raises(AuthFailed):
        create_fs(site, Reject()).connect()


def test_saved_host_key_not_prompted_again(sshd):
    class Save(AcceptAllPrompter):
        def verify_host_key(self, *a):
            self.host_key_prompts += 1
            return "save"
    site = {"protocol": "sftp", "host": "127.0.0.1", "port": sshd["port"], "user": sshd["user"],
            "auth": "key", "key_file": sshd["key"]}
    p = Save()
    fs = create_fs(site, p)
    fs.connect()
    fs.close()
    fs = create_fs(site, p)
    fs.connect()
    fs.close()
    assert p.host_key_prompts == 1


def test_wrong_key_fails(sshd, tmp_path):
    import subprocess
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], check=True)
    site = {"protocol": "sftp", "host": "127.0.0.1", "port": sshd["port"], "user": sshd["user"],
            "auth": "key", "key_file": str(tmp_path / "k")}
    with pytest.raises(AuthFailed):
        create_fs(site, AcceptAllPrompter()).connect()


def test_ls_parser():
    from anyscp.backends.scp import parse_ls_line
    e = parse_ls_line("-rw-r--r-- 1 bob staff 1234 1731670000 my file.txt", "/d")
    assert e.name == "my file.txt" and e.size == 1234 and e.mode == 0o644 and e.mtime == 1731670000
    e = parse_ls_line("lrwxrwxrwx 1 root root 7 Jan  5  2024 bin -> usr/bin", "/")
    assert e.is_link and e.link_target == "usr/bin" and e.name == "bin"
    e = parse_ls_line("drwxr-sr-x 2 root root 4096 2024-01-05 10:22 share", "/usr")
    assert e.is_dir and e.mode == 0o2755
    e = parse_ls_line("crw-rw-rw- 1 root root 1, 3 Nov 15 12:30 null", "/dev")
    assert e.name == "null" and e.size == 0
    assert parse_ls_line("total 12", "/") is None


def test_many_symlinks_pipelined(sshd):
    site = {"protocol": "sftp", "host": "127.0.0.1", "port": sshd["port"], "user": sshd["user"],
            "auth": "key", "key_file": sshd["key"]}
    fs = create_fs(site, AcceptAllPrompter())
    fs.connect()
    base = posixpath.join(sshd["root"], f"links{time.time_ns()}")
    os.makedirs(base + "/d")
    with open(base + "/f", "w") as fh:
        fh.write("12345")
    for i in range(300):
        os.symlink(base + ("/d" if i % 2 else "/f"), f"{base}/l{i:03d}")
    os.symlink(base + "/missing", base + "/broken")
    try:
        entries = {e.name: e for e in fs.listdir(base)}
        assert len(entries) == 303
        assert entries["l001"].is_dir and entries["l001"].is_link
        assert not entries["l000"].is_dir and entries["l000"].size == 5
        assert entries["broken"].is_link and not entries["broken"].is_dir
        fs.listdir(base)  # the channel is still in a consistent state
    finally:
        fs.remove_tree(base)
        fs.close()


def test_sftp_rename_never_overwrites(sshd):
    site = {"protocol": "sftp", "host": "127.0.0.1", "port": sshd["port"], "user": sshd["user"],
            "auth": "key", "key_file": sshd["key"]}
    fs = create_fs(site, AcceptAllPrompter())
    fs.connect()
    base = posixpath.join(sshd["root"], f"ren{time.time_ns()}")
    fs.mkdir(base)
    try:
        for name, data in (("a.txt", b"A"), ("b.txt", b"B")):
            w = fs.open_write(f"{base}/{name}", size=1)
            w.write(data)
            w.close()
        with pytest.raises(AlreadyExists):
            fs.rename(f"{base}/a.txt", f"{base}/b.txt")
        r = fs.open_read(f"{base}/b.txt")
        assert r.read(10) == b"B"
        r.close()
    finally:
        fs.remove_tree(base)
        fs.close()


def test_ssh_config_alias_and_proxy(sshd, tmp_path, monkeypatch):
    """A Host alias from ~/.ssh/config with IdentityFile and a ProxyCommand
    that jumps through the (same) test server with OpenSSH's ssh -W."""
    import shutil
    from anyscp.backends import sshconfig
    if not shutil.which("ssh"):
        pytest.skip("ssh client not installed")
    cfg = tmp_path / "config"
    cfg.write_text(f"""
Host testalias
    HostName 127.0.0.1
    Port {sshd['port']}
    User {sshd['user']}
    IdentityFile {sshd['key']}
    ProxyCommand ssh -i {sshd['key']} -p {sshd['port']} -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p {sshd['user']}@127.0.0.1
""")
    monkeypatch.setattr(sshconfig, "CONFIG_PATH", cfg)
    prompter = AcceptAllPrompter()
    fs = create_fs({"protocol": "sftp", "host": "testalias", "port": 22, "user": "", "auth": "auto"}, prompter)
    fs.connect()
    try:
        assert fs.listdir(sshd["root"]) is not None
        assert any("via proxy" in m for m in prompter.messages)
    finally:
        fs.close()
