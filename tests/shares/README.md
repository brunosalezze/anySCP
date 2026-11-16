# Network-share tests

These tests check that the packaged anySCP (the Windows `.exe`, the Windows
portable folder and the Linux AppImage) works when it is **started from an SMB
or NFS share**. That is a common setup: one copy on a file server, used by
several PCs. The Windows builds run under Wine on Linux.

## What is tested

The harness copies the three packages to every share it finds, then checks:

| Check | Windows (Wine) | Linux |
|---|---|---|
| Start from `\\server\share\…` with `--self-test`, first and second start | ✓ single exe, portable folder | ✓ AppImage via FUSE |
| Start from a mapped drive letter | ✓ portable folder | — |
| Portable settings go to `anySCP-data` on writable shares, and fall back to the per-user folder on read-only ones | ✓ | ✓ |
| Three instances started at the same time, sharing one settings folder | ✓ | — |
| GUI scenario: browse the share in the local pane, upload a file from it over SFTP, download it back, rename and delete it, Unicode names | ✓ | ✓ |

A test passes only if every `--self-test` check passes. The self-test checks:
- the settings folder is writable, including concurrent writers;
- the home folder and the launch folder can be listed;
- libraries, icons and fonts load;
- the password store works;
- the window shows.

## Locations

| Name | Where | Needs |
|---|---|---|
| `sim-rw`, `sim-ro` | Folders under `build/share-tests/sim`, mapped into Wine as `\\fileserver\apps` and `\\fileserver\readonly` | nothing |
| `smb-rw`, `smb-ro` | Real SMB 3.1.1 shares on 127.0.0.1 | `setup-local-shares.sh` |
| `nfs-rw`, `nfs-ro` | Real NFS 4.2 exports on 127.0.0.1 | `setup-local-shares.sh` |
| `--extra NAME=DIR` | Any mounted network folder, for example a NAS | a writable test folder; it is emptied and removed afterwards |

## Running

```sh
./packaging/appimage/build.sh && ./packaging/windows/build.sh   # build what is tested
./tests/shares/setup-local-shares.sh       # once (sudo): local SMB + NFS servers, see the script
python3 tests/shares/run_share_tests.py    # every available location
python3 tests/shares/run_share_tests.py --only sim-rw --no-linux
mkdir /mnt/nas/anySCP-share-test && \
  python3 tests/shares/run_share_tests.py --only nas --extra nas=/mnt/nas/anySCP-share-test
./tests/shares/teardown-local-shares.sh [--purge]   # remove the servers and mounts again
```

The results go to these places:
- `build/share-tests/report.json`: the full results;
- `build/share-tests/shots/`: screenshots of the GUI scenario;
- `build/share-tests/logs/`: the self-test logs and the GUI scenario output for each location.

Set `ANYSCP_SHARE_TEST_DEBUG=30` to dump all thread stacks of the GUI scenario after 30 seconds.
