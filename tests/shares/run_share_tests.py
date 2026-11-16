#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Run the packaged anySCP from network shares and check that it works.

    python3 tests/shares/run_share_tests.py [--extra NAME=/mounted/path] [--only NAME]
                                            [--no-windows] [--no-linux] [--keep]

Locations tested (each that is available):
  sim-rw / sim-ro   folders in build/share-tests mapped into Wine as the UNC
                    shares \\\\fileserver\\apps (writable) and \\\\fileserver\\readonly
  smb-rw / smb-ro   real SMB shares from tests/shares/setup-local-shares.sh
  nfs-rw / nfs-ro   real NFS exports from tests/shares/setup-local-shares.sh
  --extra NAME=DIR  any other mounted network folder (e.g. a NAS); the test
                    files are put in DIR and removed afterwards

For every location the dist/ packages are deployed and then
  Windows (Wine, through a UNC path and, if mapped, a drive letter):
    * single-file exe --self-test, first start (unpacks) and second start
    * portable folder anySCP.exe --self-test (copies runtime to the local cache)
    * three instances at the same time sharing the portable settings folder
    * GUI scenario: browse the share, upload from it over SFTP, download back,
      rename, delete (tests/shares/gui_share_scenario.py)
  Linux:
    * the AppImage started from the share with --self-test
    * the GUI scenario with the AppImage runtime on the mounted share

Each self-test must pass, and portable mode must be on for writable shares
and fall back to the per-user folders on read-only ones.  A report is
written to build/share-tests/report.json, screenshots to build/share-tests/shots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build" / "share-tests"
DIST = ROOT / "dist"
VERSION = re.search(r'__version__ = "(.+?)"', (ROOT / "anyscp" / "__init__.py").read_text()).group(1)
EXE = DIST / f"anySCP-{VERSION}-windows-x64.exe"
FOLDER = ROOT / "build" / "windows" / "stage-x64" / "anySCP"     # what the exe unpacks: anySCP.exe + runtime\
APPIMAGE = DIST / f"anySCP-{VERSION}-x86_64.AppImage"
WINEPREFIX = BUILD / "wineprefix"
WIN_USER = os.environ.get("USER", "user")
LOCALAPPDATA = WINEPREFIX / "drive_c" / "users" / WIN_USER / "AppData" / "Local" / "anySCP"
APPDATA = WINEPREFIX / "drive_c" / "users" / WIN_USER / "AppData" / "Roaming" / "anySCP"

GREEN, RED, YELLOW, BOLD, END = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"
RESULTS: list[dict] = []


# ---------------------------------------------------------------------------- helpers

def log(msg):
    print(msg, flush=True)


def record(location, test, ok, detail="", seconds=None, expected_fail=False):
    RESULTS.append({"location": location, "test": test, "ok": bool(ok), "detail": str(detail)[:800],
                    "seconds": None if seconds is None else round(seconds, 1)})
    mark = f"{GREEN}PASS{END}" if ok else f"{RED}FAIL{END}"
    t = "" if seconds is None else f" ({seconds:.1f} s)"
    log(f"   {mark}  {test}{t}" + (f" – {detail}" if detail and not ok else ""))


def win_path_unc(server, share, *parts):
    return "\\\\" + server + "\\" + share + "".join("\\" + p for p in parts)


def to_wine_z(path: Path) -> str:
    return "Z:" + str(path).replace("/", "\\")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_writable(path: Path):
    for p in [path, *path.rglob("*")]:
        try:
            p.chmod(p.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass


def rmtree(path: Path):
    if path.exists():
        make_writable(path)
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------- environment

class Xvfb:
    def __init__(self):
        self.proc = None
        self.display = None

    def __enter__(self):
        for n in range(90, 120):
            if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
                self.display = f":{n}"
                break
        self.proc = subprocess.Popen(["Xvfb", self.display, "-screen", "0", "1600x1000x24", "-nolisten", "tcp"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)


class SSHServer:
    """Throwaway OpenSSH server (key auth) for the GUI scenario."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="anyscp-share-sshd-", dir=BUILD))
        self.port = free_port()
        self.proc = None

    def __enter__(self):
        d = self.dir
        for name in ("host", "client"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / name)], check=True)
        (d / "authorized_keys").write_text((d / "client.pub").read_text())
        (d / "sshd_config").write_text(
            f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {d}/host\nPidFile {d}/sshd.pid\n"
            f"AuthorizedKeysFile {d}/authorized_keys\nPasswordAuthentication no\n"
            f"KbdInteractiveAuthentication no\nUsePAM no\nStrictModes no\nSubsystem sftp internal-sftp\n")
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        self.proc = subprocess.Popen([sshd, "-D", "-e", "-f", str(d / "sshd_config")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        self.host_key_line = f"[127.0.0.1]:{self.port} " + " ".join((d / "host.pub").read_text().split()[:2])
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)
        rmtree(self.dir)


def wine_env(display, extra=None):
    env = dict(os.environ)
    env.update({"WINEPREFIX": str(WINEPREFIX), "WINEDEBUG": "-all", "WINEDLLOVERRIDES": "mscoree,mshtml=",
                "DISPLAY": display, "ANYSCP_NO_DIALOGS": "1", "GSK_RENDERER": "cairo"})
    for k in ("ANYSCP_CONFIG_DIR", "ANYSCP_CACHE_DIR", "ANYSCP_LAUNCHER_DIR", "ANYSCP_NO_KEYRING"):
        env.pop(k, None)
    env.update(extra or {})
    return env


def ensure_wine_prefix(display):
    if not (WINEPREFIX / "drive_c").exists():
        log("   initialising Wine prefix …")
        subprocess.run(["wineboot", "-i"], env=wine_env(display), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=300)


def map_unc(server, share, target: Path):
    d = WINEPREFIX / "dosdevices" / "unc" / server
    d.mkdir(parents=True, exist_ok=True)
    link = d / share
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def map_drive(letter, target: Path):
    link = WINEPREFIX / "dosdevices" / f"{letter.lower()}:"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def stop_wine(display):
    subprocess.run(["wineserver", "-k"], env=wine_env(display), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def parse_selftest(text):
    m = re.search(r"ANYSCP-SELF-TEST (\{.*\})", text)
    return json.loads(m.group(1)) if m else None


# ---------------------------------------------------------------------------- locations

class Location:
    def __init__(self, name, path: Path, populate: Path | None = None, writable=True,
                 unc=None, drive=None, extra=False):
        self.name = name
        self.path = path                  # where the app sees the files
        self.populate = populate or path  # where the harness writes them (ro shares!)
        self.writable = writable
        self.unc = unc                    # (server, share) for Wine
        self.drive = drive
        self.extra = extra


def discover(args) -> list[Location]:
    locs = [Location("sim-rw", BUILD / "sim" / "rw", unc=("fileserver", "apps"), drive="S"),
            Location("sim-ro", BUILD / "sim" / "ro", populate=BUILD / "sim" / "ro", writable=False,
                     unc=("fileserver", "readonly"))]
    mnt, srv = Path("/mnt/anyscp-test"), Path("/srv/anyscp-share-test")
    for kind, server, drive in (("smb", "smbserver", "T"), ("nfs", "nfsserver", "U")):
        if os.path.ismount(mnt / kind):
            locs.append(Location(f"{kind}-rw", mnt / kind, unc=(server, "anyscp-rw"), drive=drive))
        if os.path.ismount(mnt / f"{kind}-ro"):
            locs.append(Location(f"{kind}-ro", mnt / f"{kind}-ro", populate=srv / f"{kind}-ro",
                                 writable=False, unc=(server, "anyscp-ro")))
    for item in args.extra or []:
        name, _, path = item.partition("=")
        locs.append(Location(name, Path(path), unc=("nas", name), drive="N", extra=True))
    if args.only:
        locs = [l for l in locs if l.name in args.only]
    return locs


def deploy(loc: Location):
    """Copy the packages and some test data to the share (via the populate path)."""
    base = loc.populate
    if loc.name == "sim-ro":
        make_writable(base) if base.exists() else None
    base.mkdir(parents=True, exist_ok=True)
    for item in ("anySCP", "anySCP-data", "data"):
        rmtree(base / item)
    for f in base.glob("anySCP-*"):
        f.unlink() if f.is_file() else rmtree(f)
    shutil.copy2(EXE, base / EXE.name)
    shutil.copytree(FOLDER, base / "anySCP", symlinks=True)
    if APPIMAGE.exists():
        shutil.copy2(APPIMAGE, base / APPIMAGE.name)
        (base / APPIMAGE.name).chmod(0o755)
    data = base / "data"
    (data / "Ünïcode folder").mkdir(parents=True)
    payload = os.urandom(2 * 1024 * 1024 + 4321)
    (data / "payload.bin").write_bytes(payload)
    (data / "Ünïcode folder" / "nötes.txt").write_text("hello from the share\n", encoding="utf-8")
    if loc.name == "sim-ro":                       # simulate a read-only share
        for p in [base, *base.rglob("*")]:
            if not p.is_symlink():
                p.chmod(p.stat().st_mode & ~0o222)
    return hashlib.sha256(payload).hexdigest()


def cleanup(loc: Location, keep):
    if keep:
        return
    base = loc.populate
    make_writable(base)
    for item in ("anySCP", "anySCP-data", "data"):
        rmtree(base / item)
    for f in list(base.glob("anySCP-*")):
        f.unlink() if f.is_file() else rmtree(f)
    if loc.extra:
        try:
            base.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------- Windows tests

def run_wine(display, exe_win_path, args, log_file: Path, timeout=420):
    env = wine_env(display, {"ANYSCP_LOG_FILE": to_wine_z(log_file)})
    t0 = time.monotonic()
    try:
        proc = subprocess.run(["wine", exe_win_path, *args], env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    elapsed = time.monotonic() - t0
    text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
    return code, elapsed, text


def check_selftest(loc, label, code, text, expect_portable):
    rep = parse_selftest(text)
    if rep is None:
        tail = "\n".join(l for l in text.splitlines() if "gdk_frame_clock" not in l)[-600:]
        return False, f"exit {code}, no self-test report: {tail.strip() or '(empty log)'}"
    failed = [c for c in rep["checks"] if not c["ok"]]
    if failed:
        return False, "; ".join(f"{c['name']}: {c['detail']}" for c in failed)
    if bool(rep["info"]["portable"]) != expect_portable:
        return False, f"portable={rep['info']['portable']} (expected {expect_portable}), " \
                      f"config in {rep['info']['config_dir']}"
    return True, f"config: {rep['info']['config_dir']}"


def windows_tests(loc: Location, display, sshd: SSHServer, payload_sha):
    logs = BUILD / "logs" / loc.name
    rmtree(logs)
    logs.mkdir(parents=True)
    server, share = loc.unc
    map_unc(server, share, loc.path)
    unc_exe = win_path_unc(server, share, EXE.name)
    unc_folder_exe = win_path_unc(server, share, "anySCP", "anySCP.exe")

    # single-file exe: cold (unpack) and warm start
    rmtree(LOCALAPPDATA)
    rmtree(APPDATA)
    code, t, text = run_wine(display, unc_exe, ["--self-test"], logs / "exe-cold.log")
    ok, detail = check_selftest(loc, "exe", code, text, loc.writable)
    record(loc.name, "Windows single exe from UNC, first start", ok and code == 0, detail, t)
    code, t, text = run_wine(display, unc_exe, ["--self-test"], logs / "exe-warm.log")
    ok, detail = check_selftest(loc, "exe", code, text, loc.writable)
    record(loc.name, "Windows single exe from UNC, second start", ok and code == 0, detail, t)

    # portable folder: copies its runtime to the local cache
    rmtree(LOCALAPPDATA)
    code, t, text = run_wine(display, unc_folder_exe, ["--self-test"], logs / "folder-cold.log")
    ok, detail = check_selftest(loc, "folder", code, text, loc.writable)
    rep = parse_selftest(text)
    if ok and rep and "AppData" not in rep["info"]["executable"]:
        ok, detail = False, f"runtime not cached locally: {rep['info']['executable']}"
    record(loc.name, "Windows portable folder from UNC (runtime cached locally)", ok and code == 0, detail, t)

    if loc.drive:
        map_drive(loc.drive, loc.path)
        code, t, text = run_wine(display, f"{loc.drive}:\\anySCP\\anySCP.exe", ["--self-test"],
                                 logs / "folder-drive.log")
        ok, detail = check_selftest(loc, "drive", code, text, loc.writable)
        record(loc.name, f"Windows portable folder from mapped drive {loc.drive}:", ok and code == 0, detail, t)

    # three instances at once, same portable settings folder on the share
    env_procs = []
    t0 = time.monotonic()
    for i in range(3):
        lf = logs / f"concurrent-{i}.log"
        env = wine_env(display, {"ANYSCP_LOG_FILE": to_wine_z(lf)})
        env_procs.append((subprocess.Popen(["wine", unc_folder_exe, "--self-test"], env=env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), lf))
    problems = []
    for proc, lf in env_procs:
        try:
            code = proc.wait(420)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = "timeout"
        text = lf.read_text(encoding="utf-8", errors="replace") if lf.exists() else ""
        ok, detail = check_selftest(loc, "concurrent", code, text, loc.writable)
        if not ok or code != 0:
            problems.append(detail)
    data_dir = loc.populate / "anySCP" / "anySCP-data" / "config"
    if loc.writable:
        leftovers = list(data_dir.glob(".*.tmp")) if data_dir.exists() else []
        if leftovers:
            problems.append(f"temporary files left: {[p.name for p in leftovers[:3]]}")
        for jf in data_dir.glob("*.json") if data_dir.exists() else []:
            try:
                json.loads(jf.read_text())
            except ValueError:
                problems.append(f"{jf.name} is corrupt")
    record(loc.name, "Windows: 3 instances at once from the share", not problems, "; ".join(problems),
           time.monotonic() - t0)

    # GUI scenario inside the (locally cached) Windows runtime
    runtime = next((p for p in LOCALAPPDATA.glob("runtime-*") if (p / "anyscp-runtime.ok").exists()), None)
    if runtime is None:
        record(loc.name, "Windows GUI scenario", False, "no local runtime cache to run it with")
        return
    remote_dir = BUILD / "remote" / loc.name
    rmtree(remote_dir)
    remote_dir.mkdir(parents=True)
    run_gui_scenario(loc, "Windows", display, payload_sha, sshd, remote_dir,
                     cmd=["wine", to_wine_z(runtime / "python.exe"), "-s",
                          to_wine_z(Path(__file__).with_name("gui_share_scenario.py"))],
                     share_dir=win_path_unc(server, share, "data"),
                     key=to_wine_z(sshd.dir / "client"),
                     cfg_dir=None, env=wine_env(display, {
                         "ANYSCP_LAUNCHER_DIR": win_path_unc(server, share, "anySCP"),
                         "PYTHONUTF8": "1"}),
                     out_path=to_wine_z(BUILD / "out" / f"{loc.name}-windows.json"),
                     shot_path=to_wine_z(BUILD / "shots" / f"{loc.name}-windows.png"))


# ---------------------------------------------------------------------------- GUI scenario (both)

def run_gui_scenario(loc, platform, display, payload_sha, sshd, remote_dir, cmd, share_dir, key,
                     cfg_dir, env, out_path, shot_path):
    out_local = BUILD / "out" / f"{loc.name}-{platform.lower()}.json"
    out_local.parent.mkdir(parents=True, exist_ok=True)
    (BUILD / "shots").mkdir(parents=True, exist_ok=True)
    out_local.unlink(missing_ok=True)
    # trust the test server's host key in whichever config folder the app will use
    params = {"share_dir": share_dir, "writable": loc.writable, "remote_dir": str(remote_dir),
              "ssh": {"host": "127.0.0.1", "port": sshd.port, "user": WIN_USER if platform == "Windows"
                      else os.environ.get("USER", ""), "key": key},
              "out": out_path, "shot": shot_path}
    env = dict(env)
    if os.environ.get("ANYSCP_SHARE_TEST_DEBUG"):
        env["ANYSCP_SHARE_TEST_DEBUG"] = os.environ["ANYSCP_SHARE_TEST_DEBUG"]
    env["ANYSCP_SHARE_TEST"] = json.dumps(params)
    env["ANYSCP_NO_KEYRING"] = "1"
    known_hosts_dirs = []
    if loc.writable:
        known_hosts_dirs.append(loc.populate / ("anySCP" if platform == "Windows" else ".") / "anySCP-data" / "config")
    known_hosts_dirs.append(APPDATA if platform == "Windows" else Path(cfg_dir))
    for d in known_hosts_dirs:
        d.mkdir(parents=True, exist_ok=True)
        (d / "known_hosts").write_text(sshd.host_key_line + "\n")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        output = proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", "replace") + "\n(timeout)"
    elapsed = time.monotonic() - t0
    log_dir = BUILD / "logs" / loc.name
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"gui-{platform.lower()}.log").write_text(output, encoding="utf-8")
    if not out_local.exists():
        tail = "\n".join(l for l in output.splitlines() if "gdk_frame_clock" not in l)[-800:]
        record(loc.name, f"{platform} GUI scenario", False, f"no result: {tail}", elapsed)
        return
    res = json.loads(out_local.read_text())
    bad = [f"{s['name']}: {s['detail']}" for s in res["steps"] if not s["ok"]]
    uploaded = remote_dir / "payload.bin"
    if not uploaded.exists() or hashlib.sha256(uploaded.read_bytes()).hexdigest() != payload_sha:
        bad.append("uploaded file differs or is missing on the server")
    if loc.writable and res.get("portable") is not True:
        bad.append(f"settings not portable: {res.get('config_dir')}")
    record(loc.name, f"{platform} GUI scenario (browse share, upload, download, rename, delete)",
           not bad, "; ".join(bad), elapsed)


# ---------------------------------------------------------------------------- Linux tests

def linux_tests(loc: Location, display, sshd, payload_sha):
    if not APPIMAGE.exists():
        record(loc.name, "Linux AppImage", False, f"{APPIMAGE} missing")
        return
    image = loc.path / APPIMAGE.name
    home = BUILD / "linux-home" / loc.name
    rmtree(home)
    home.mkdir(parents=True)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "DISPLAY": display, "GDK_BACKEND": "x11",
           "XDG_RUNTIME_DIR": str(home), "GDK_DEBUG": "no-portals", "ANYSCP_NO_KEYRING": "1"}
    fuse = os.path.exists("/dev/fuse") and shutil.which("fusermount3") or shutil.which("fusermount")
    if not fuse:
        env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    t0 = time.monotonic()
    try:
        proc = subprocess.run([str(image), "--self-test"], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=300)
        out, code = proc.stdout.decode("utf-8", "replace"), proc.returncode
    except subprocess.TimeoutExpired:
        out, code = "", "timeout"
    except PermissionError as exc:
        out, code = f"cannot execute from this share ({exc}) – is it mounted noexec?", "noexec"
    ok, detail = check_selftest(loc, "appimage", code, out, loc.writable)
    record(loc.name, f"Linux AppImage from share --self-test ({'FUSE' if fuse else 'extract'})",
           ok and code == 0, detail if not ok else detail, time.monotonic() - t0)

    # GUI scenario with the AppImage runtime (extracted once to the local disk)
    root = BUILD / "appimage-root"
    stamp = root / ".source-sha"
    sha = hashlib.sha256(APPIMAGE.read_bytes()).hexdigest()
    if not stamp.exists() or stamp.read_text() != sha:
        rmtree(root)
        root.mkdir(parents=True)
        subprocess.run([str(APPIMAGE), "--appimage-extract"], cwd=root, stdout=subprocess.DEVNULL, check=True)
        stamp.write_text(sha)
    apprun = root / "squashfs-root" / "AppRun"
    test_runner = root / "squashfs-root" / "AppRun.sharetest"
    text = apprun.read_text()
    text = re.sub(r'^exec .*$', 'exec "$U/bin/python3" -s "$ANYSCP_TEST_SCRIPT"', text, flags=re.M)
    test_runner.write_text(text)
    test_runner.chmod(0o755)
    remote_dir = BUILD / "remote" / f"{loc.name}-linux"
    rmtree(remote_dir)
    remote_dir.mkdir(parents=True)
    cfg = home / ".config" / "anyscp"
    env2 = dict(env, ANYSCP_TEST_SCRIPT=str(Path(__file__).with_name("gui_share_scenario.py")),
                ANYSCP_LAUNCHER_DIR=str(loc.path))
    run_gui_scenario(loc, "Linux", display, payload_sha, sshd, remote_dir, cmd=[str(test_runner)],
                     share_dir=str(loc.path / "data"), key=str(sshd.dir / "client"), cfg_dir=cfg,
                     env=env2, out_path=str(BUILD / "out" / f"{loc.name}-linux.json"),
                     shot_path=str(BUILD / "shots" / f"{loc.name}-linux.png"))


# ---------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extra", action="append", metavar="NAME=DIR", help="another mounted share to test")
    ap.add_argument("--only", action="append", metavar="NAME", help="only test these locations")
    ap.add_argument("--no-windows", action="store_true")
    ap.add_argument("--no-linux", action="store_true")
    ap.add_argument("--keep", action="store_true", help="leave the deployed files on the shares")
    args = ap.parse_args()

    for f in (EXE, FOLDER):
        if not args.no_windows and not f.exists():
            sys.exit(f"missing {f} – run packaging/windows/build.sh first")
    if not args.no_windows and not shutil.which("wine"):
        sys.exit("wine is not installed (run packaging/setup-build-deps.sh)")
    BUILD.mkdir(parents=True, exist_ok=True)
    locs = discover(args)
    log(f"{BOLD}anySCP {VERSION} – share tests on: {', '.join(l.name for l in locs)}{END}")

    with Xvfb() as x, SSHServer() as sshd:
        if not args.no_windows:
            ensure_wine_prefix(x.display)
        for loc in locs:
            log(f"\n{BOLD}[{loc.name}] {loc.path} ({'writable' if loc.writable else 'read-only'}){END}")
            try:
                sha = deploy(loc)
            except OSError as exc:
                record(loc.name, "deploy test files", False, exc)
                continue
            try:
                if not args.no_windows:
                    windows_tests(loc, x.display, sshd, sha)
                    stop_wine(x.display)
                if not args.no_linux:
                    linux_tests(loc, x.display, sshd, sha)
            finally:
                cleanup(loc, args.keep)

    failed = [r for r in RESULTS if not r["ok"]]
    (BUILD / "report.json").write_text(json.dumps({"version": VERSION, "results": RESULTS}, indent=2))
    log(f"\n{BOLD}{len(RESULTS) - len(failed)}/{len(RESULTS)} passed{END}"
        + (f" – {RED}{len(failed)} failed{END}" if failed else f" – {GREEN}all good{END}"))
    log(f"report: {BUILD / 'report.json'}   screenshots: {BUILD / 'shots'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    main()
