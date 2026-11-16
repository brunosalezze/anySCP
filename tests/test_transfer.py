# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Transfer engine: recursive upload/download, move, overwrite policies, resume."""

import os
import time

import pytest
from gi.repository import GLib

from conftest import AcceptAllPrompter
from anyscp.backends import create_fs
from anyscp.backends.local import LocalFileSystem
from anyscp.session import LocalEndpoint
from anyscp.transfer import DONE, AutoDecider, TransferJob, TransferManager


class FSEndpoint:
    is_local = False

    def __init__(self, fs):
        self.fs = fs
        self.label = "remote"

    def acquire_fs(self):
        return self.fs.clone()

    def release_fs(self, fs, ok=True):
        fs.close()


@pytest.fixture
def remote(sshd):
    site = {"protocol": "sftp", "host": "127.0.0.1", "port": sshd["port"], "user": sshd["user"],
            "auth": "key", "key_file": sshd["key"]}
    fs = create_fs(site, AcceptAllPrompter())
    fs.connect()
    base = os.path.join(sshd["root"], f"xfer{time.time_ns()}")
    fs.mkdir(base)
    yield fs, base
    fs.remove_tree(base)
    fs.close()


def make_tree(root):
    os.makedirs(os.path.join(root, "proj", "src", "deep"))
    with open(os.path.join(root, "proj", "readme.md"), "w") as f:
        f.write("hello\n")
    with open(os.path.join(root, "proj", "src", "main.py"), "wb") as f:
        f.write(os.urandom(700_000))
    with open(os.path.join(root, "proj", "src", "deep", "ünïcode file.txt"), "w") as f:
        f.write("x" * 10)
    os.utime(os.path.join(root, "proj", "readme.md"), (1_500_000_000, 1_500_000_000))


def tree(path):
    out = {}
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = f.read()
    return out


def test_upload_download_roundtrip(tmp_path, remote):
    fs, base = remote
    make_tree(tmp_path / "a")
    local = LocalFileSystem()
    src = local.stat(str(tmp_path / "a" / "proj"))
    job = TransferJob("up", LocalEndpoint(), FSEndpoint(fs), [(src, base + "/proj")])
    job.execute(AutoDecider())
    assert job.total_files == 3 and job.done_bytes == job.total_bytes
    assert int(fs.stat(base + "/proj/readme.md").mtime) == 1_500_000_000

    remote_info = fs.stat(base + "/proj")
    job = TransferJob("down", FSEndpoint(fs), LocalEndpoint(), [(remote_info, str(tmp_path / "b" / "proj"))])
    os.makedirs(tmp_path / "b")
    job.execute(AutoDecider())
    assert tree(tmp_path / "a" / "proj") == tree(tmp_path / "b" / "proj")


def test_move_removes_sources(tmp_path, remote):
    fs, base = remote
    make_tree(tmp_path / "a")
    local = LocalFileSystem()
    job = TransferJob("mv", LocalEndpoint(), FSEndpoint(fs),
                      [(local.stat(str(tmp_path / "a" / "proj")), base + "/proj")], move=True)
    job.execute(AutoDecider())
    assert not (tmp_path / "a" / "proj").exists()
    assert fs.stat(base + "/proj/src/main.py").size == 700_000


def test_skip_and_resume(tmp_path, remote):
    fs, base = remote
    data = os.urandom(1_000_000)
    (tmp_path / "f.bin").write_bytes(data)
    w = fs.open_write(base + "/f.bin")
    w.write(data[:400_000])
    w.close()
    local = LocalFileSystem()
    info = local.stat(str(tmp_path / "f.bin"))

    job = TransferJob("skip", LocalEndpoint(), FSEndpoint(fs), [(info, base + "/f.bin")])
    job.execute(AutoDecider(overwrite="skip"))
    assert fs.stat(base + "/f.bin").size == 400_000

    job = TransferJob("resume", LocalEndpoint(), FSEndpoint(fs), [(info, base + "/f.bin")])
    job.execute(AutoDecider(overwrite="resume"))
    r = fs.open_read(base + "/f.bin")
    got = b""
    while chunk := r.read(65536):
        got += chunk
    r.close()
    assert got == data


def test_manager_runs_jobs(tmp_path, remote):
    fs, base = remote
    for i in range(5):
        (tmp_path / f"{i}.txt").write_text(str(i) * 1000)
    local = LocalFileSystem()
    manager = TransferManager(AutoDecider(), parallel=2)
    finished = []
    manager.connect("job-finished", lambda _m, job: finished.append(job))
    for i in range(5):
        info = local.stat(str(tmp_path / f"{i}.txt"))
        manager.add(TransferJob(f"{i}", LocalEndpoint(), FSEndpoint(fs), [(info, f"{base}/{i}.txt")]))
    ctx = GLib.MainContext.default()
    deadline = time.time() + 20
    while len(finished) < 5 and time.time() < deadline:
        ctx.iteration(False)
        time.sleep(0.01)
    assert len(finished) == 5
    assert all(j.state == DONE for j in finished), [j.error for j in finished]
    assert sorted(e.name for e in fs.listdir(base)) == [f"{i}.txt" for i in range(5)]


def test_retry_keeps_skips_and_resumes_partials(tmp_path):
    """A retry must not touch files the user skipped and must resume only
    what the job itself left half-written."""
    local = LocalFileSystem()
    (tmp_path / "src").mkdir()
    (tmp_path / "dst").mkdir()
    (tmp_path / "src" / "config.ini").write_text("NEW-CONFIG" * 10)
    (tmp_path / "dst" / "config.ini").write_text("MY-EDITS")
    (tmp_path / "src" / "big.bin").write_bytes(b"A" * 1000)
    pairs = [(local.stat(str(tmp_path / "src" / n)), str(tmp_path / "dst" / n)) for n in ("big.bin", "config.ini")]
    job = TransferJob("t", LocalEndpoint(), LocalEndpoint(), pairs)
    job.execute(AutoDecider(overwrite="skip"))
    assert (tmp_path / "dst" / "config.ini").read_text() == "MY-EDITS"
    # simulate an interrupted write of big.bin, then retry
    (tmp_path / "dst" / "big.bin").write_bytes(b"A" * 300)
    job.partial_dsts.add(str(tmp_path / "dst" / "big.bin"))
    job.done_dsts.discard(str(tmp_path / "dst" / "big.bin"))
    job.reset()
    job.retrying = True
    job.execute(AutoDecider(overwrite="overwrite"))
    assert (tmp_path / "dst" / "config.ini").read_text() == "MY-EDITS"
    assert (tmp_path / "dst" / "big.bin").read_bytes() == b"A" * 1000


def test_copy_onto_itself_is_refused(tmp_path):
    local = LocalFileSystem()
    f = tmp_path / "a.txt"
    f.write_text("precious")
    job = TransferJob("t", LocalEndpoint(), LocalEndpoint(), [(local.stat(str(f)), str(f))])
    job.execute(AutoDecider(overwrite="overwrite", on_error="skip"))
    assert f.read_text() == "precious" and job.warnings
