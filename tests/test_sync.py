# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Directory comparison used by Synchronize (local file systems on both sides)."""

import os

from anyscp.backends.local import LocalFileSystem
from anyscp.sync import DELETE_LOCAL, DELETE_REMOTE, DOWNLOAD, UPLOAD, SyncOptions, compare


def write(path, data="x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def setup_trees(tmp_path):
    a, b = tmp_path / "local", tmp_path / "remote"
    write(str(a / "same.txt"), "1", 1_000_000)
    write(str(b / "same.txt"), "1", 1_000_000)
    write(str(a / "newer_local.txt"), "new", 2_000_000)
    write(str(b / "newer_local.txt"), "old", 1_000_000)
    write(str(a / "newer_remote.txt"), "old", 1_000_000)
    write(str(b / "newer_remote.txt"), "new", 2_000_000)
    write(str(a / "only_local" / "deep" / "f.txt"))
    write(str(b / "only_remote.txt"))
    write(str(a / ".git" / "HEAD"))
    write(str(a / "sub" / "x.txt"), "a", 1_000_000)
    write(str(b / "sub" / "x.txt"), "abc", 1_000_000)
    return str(a), str(b)


def kinds(actions):
    return {(a.kind, a.rel) for a in actions}


def test_upload_direction(tmp_path):
    a, b = setup_trees(tmp_path)
    fs = LocalFileSystem()
    actions = compare(fs, fs, a, b, SyncOptions(direction="remote"))
    assert kinds(actions) == {(UPLOAD, "newer_local.txt"), (UPLOAD, "only_local")}
    # the folder is uploaded as a whole: its content is not listed separately
    assert not any(x.rel.startswith("only_local/") for x in actions)


def test_mirror_deletes(tmp_path):
    a, b = setup_trees(tmp_path)
    fs = LocalFileSystem()
    actions = compare(fs, fs, a, b, SyncOptions(direction="remote", delete=True))
    assert (DELETE_REMOTE, "only_remote.txt") in kinds(actions)
    actions = compare(fs, fs, a, b, SyncOptions(direction="local", delete=True))
    assert kinds(actions) == {(DOWNLOAD, "newer_remote.txt"), (DOWNLOAD, "only_remote.txt"),
                              (DELETE_LOCAL, "only_local")}


def test_both_ways_and_size(tmp_path):
    a, b = setup_trees(tmp_path)
    fs = LocalFileSystem()
    both = kinds(compare(fs, fs, a, b, SyncOptions(direction="both")))
    assert {(UPLOAD, "newer_local.txt"), (DOWNLOAD, "newer_remote.txt"),
            (UPLOAD, "only_local"), (DOWNLOAD, "only_remote.txt")} <= both
    by_size = kinds(compare(fs, fs, a, b, SyncOptions(direction="remote", criteria="size")))
    assert (UPLOAD, "sub/x.txt") in by_size and (UPLOAD, "same.txt") not in by_size


def test_non_recursive_and_excludes(tmp_path):
    a, b = setup_trees(tmp_path)
    fs = LocalFileSystem()
    actions = compare(fs, fs, a, b, SyncOptions(direction="remote", recursive=False, criteria="size"))
    assert not any("/" in x.rel for x in actions)
    assert not any(x.rel.startswith(".git") for x in actions)
    with_git = compare(fs, fs, a, b, SyncOptions(direction="remote", exclude=[]))
    assert any(x.rel == ".git" for x in with_git)


def test_symlinked_folder_is_not_mirrored_away(tmp_path):
    import pytest
    from anyscp.backends.base import FSError
    a, b = tmp_path / "local", tmp_path / "remote"
    write(str(tmp_path / "shared" / "lib.js"))
    os.makedirs(a)
    os.symlink(tmp_path / "shared", a / "shared")          # link locally
    write(str(b / "shared" / "lib.js"))                     # real folder on the server
    fs = LocalFileSystem()
    actions = compare(fs, fs, str(a), str(b), SyncOptions(direction="remote", delete=True))
    assert not any(x.rel.startswith("shared/") for x in actions)
    # a missing source folder must not schedule deleting everything
    with pytest.raises(FSError):
        compare(fs, fs, str(tmp_path / "typo"), str(b), SyncOptions(direction="remote", delete=True))
