#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Run the GUI scenario (tests/gui/ui_scenario.py) in the packaged builds.

  run_ui_tests.py                      the x86-64 AppImage, and the Windows x64 build (Wine)
  run_ui_tests.py --linux FILE         an AppImage (an arm64 one runs in --docker IMAGE)
  run_ui_tests.py --windows DIR        a Windows build's folder, build/windows/stage-<arch>/anySCP
  run_ui_tests.py --selftest FILE.exe  also run "anySCP.exe --self-test" of a single-file exe

Everything runs against a throwaway OpenSSH server on 127.0.0.1, under
Xvfb, with a fresh settings folder.  Results: build/ui-tests/<label>/out,
screenshots: build/ui-tests/<label>/shots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tests" / "shares"))
import run_share_tests as share  # noqa: E402  (SSH server and Wine helpers)

BUILD = ROOT / "build" / "ui-tests"
DIST = ROOT / "dist"
VERSION = share.VERSION
GREEN, RED, BOLD, END = "\033[32m", "\033[31m", "\033[1m", "\033[0m"
SCREEN = "2400x1400x24"
RESULTS: list[tuple[str, bool, str]] = []


def log(msg):
    print(msg, flush=True)


def record(label, name, ok, detail=""):
    RESULTS.append((label, bool(ok), name))
    log(f"   {GREEN + 'PASS' if ok else RED + 'FAIL'}{END}  {name}" + (f" – {detail}" if detail and not ok else ""))


class Xvfb:
    def __init__(self):
        self.proc = None
        self.display = None

    def __enter__(self):
        for n in range(140, 170):
            if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
                self.display = f":{n}"
                break
        self.proc = subprocess.Popen(["Xvfb", self.display, "-screen", "0", SCREEN, "-nolisten", "tcp"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)


# ---------------------------------------------------------------------------- test data

def prepare(label):
    base = BUILD / label
    share.rmtree(base)
    d = {k: base / k for k in ("remote", "local", "readonly", "config", "cache", "home", "tmp", "out", "shots")}
    for path in d.values():
        path.mkdir(parents=True)
    d["home"].chmod(0o700)
    sha = {}

    def put(rel, data):
        path = d["remote"] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        sha[path.name] = hashlib.sha256(data).hexdigest()

    put("big.bin", os.urandom(64 << 20))
    put("small.txt", b"a small file\n" * 200)
    put("docs/a.txt", b"first document\n" * 50)
    put("docs/b.txt", b"second document\n" * 80)
    put("pointer.txt", b"dragged with the pointer\n" * 100)
    put("dragged.bin", os.urandom(24 << 20))
    put("notyet.txt", b"not downloaded\n")
    put("button-down.txt", b"downloaded with the pane's copy button\n" * 20)
    put("highlight.txt", b"dropped into a highlighted folder\n" * 20)
    with open(d["remote"] / "huge.bin", "wb") as fh:
        fh.truncate(16 << 30)               # sparse: costs nothing, never finishes in time
    d["readonly"].chmod(0o555)
    return base, d, sha


def password_server(d):
    """tests/gui/sftp_server.py on the run's server folder: (process, info)"""
    info_file = d["out"] / "sftp-server.json"
    proc = subprocess.Popen([sys.executable, str(HERE / "sftp_server.py"), str(d["remote"]), "tester",
                             "s3cret pässwörd", str(info_file), os.environ.get("UI_SERVER_DELAY", "0")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        if info_file.exists():
            break
        time.sleep(0.05)
    info = json.loads(info_file.read_text())
    with open(d["config"] / "known_hosts", "a", encoding="utf-8") as fh:
        fh.write(info["known_hosts"] + "\n")
    return proc, {"host": "127.0.0.1", "port": info["port"], "user": "tester", "password": "s3cret pässwörd"}


def proxy_server(d):
    """tests/proxy_server.py (SOCKS5 and more): (process, {"port", "targets"})"""
    info_file = d["out"] / "proxy.json"
    proc = subprocess.Popen([sys.executable, str(HERE.parent / "proxy_server.py"), str(info_file)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        if info_file.exists():
            break
        time.sleep(0.05)
    info = json.loads(info_file.read_text())
    return proc, {"port": info["port"], "targets": str(info_file) + ".targets"}


def params(d, sha, sshd, native, pointer, slow, drop_target_cmd, only=None):
    return {"only": only,
        "ssh": {"host": "127.0.0.1", "port": sshd.port, "user": os.environ.get("USER", ""),
                "key": native(sshd.dir / "client")},
        "remote_dir": str(d["remote"]), "local_dir": native(d["local"]), "ro_dir": native(d["readonly"]),
        "ssh_home": os.path.expanduser("~"),          # (the test server's user: its SFTP start folder)
        "sha": sha, "docs": ["a.txt", "b.txt"], "out": native(d["out"] / "result.json"),
        "shots": native(d["shots"]), "drop_out": native(d["out"] / "drop.json"),
        "drop_target_cmd": drop_target_cmd, "pointer": pointer, "slow": slow,
    }


def report(label, d, proc_output, seconds):
    (d["out"] / "scenario.log").write_text(proc_output, encoding="utf-8")
    result_file = d["out"] / "result.json"
    if not result_file.exists():
        tail = "\n".join(line for line in proc_output.splitlines() if "gdk_frame_clock" not in line)[-1500:]
        record(label, "GUI scenario", False, f"no result:\n{tail}")
        return
    res = json.loads(result_file.read_text())
    for s in res["steps"]:
        record(label, s["name"], s["ok"], s["detail"])
    log(f"   ({len(res['steps'])} steps in {seconds:.0f} s; log: {d['out'] / 'scenario.log'})")


LIVE_LOG = BUILD / f"running-{os.getpid()}.log"   # the output of whatever runs now, as it comes


STEP = re.compile(rb"^ +[\d.]+s (OK|FAIL) ", re.M)


def run(cmd, env, timeout, first_step_within=None):
    """Run cmd, its output going to LIVE_LOG as it comes.  With
    first_step_within, give up early if the scenario has not reported a
    first step by then (it did not start).  -> (output, seconds, started)"""
    t0 = time.monotonic()
    BUILD.mkdir(parents=True, exist_ok=True)
    started = first_step_within is None
    with open(LIVE_LOG, "wb") as live:
        proc = subprocess.Popen(cmd, env=env, stdout=live, stderr=subprocess.STDOUT)
        timed_out = False
        while proc.poll() is None:
            elapsed = time.monotonic() - t0
            if not started:
                started = bool(STEP.search(LIVE_LOG.read_bytes()))
                if not started and elapsed > first_step_within:
                    timed_out = True
            if elapsed > timeout:
                timed_out = True
            if timed_out:
                proc.kill()
                proc.wait()
                break
            time.sleep(1)
    data = LIVE_LOG.read_bytes()
    started = started or bool(STEP.search(data))
    out = data.decode("utf-8", "replace") + ("\n(timeout)" if timed_out else "")
    return out, time.monotonic() - t0, started


# ---------------------------------------------------------------------------- Linux

def linux(appimage: Path, sshd, args):
    arch = "aarch64" if "aarch64" in appimage.name else "x86_64"
    label = f"linux-{arch}" + (f"-{args.docker.split(':')[-1]}" if args.docker else "")
    log(f"\n{BOLD}[{label}] {appimage.name}{' in ' + args.docker if args.docker else ''}{END}")
    base, d, sha = prepare(label)
    root = base / "appimage"
    root.mkdir()
    # (An AppImage of another architecture needs qemu called by hand: its
    # "AI" marker in the ELF header keeps qemu's binfmt entry from matching.)
    emulate = [f"qemu-{arch}-static"] if arch != os.uname().machine else []
    subprocess.run(emulate + [str(appimage), "--appimage-extract"], cwd=root, stdout=subprocess.DEVNULL,
                   check=True)
    apprun = root / "squashfs-root" / "AppRun"
    runner = root / "squashfs-root" / "AppRun.uitest"
    runner.write_text(re.sub(r'^exec .*$', 'exec "$U/bin/python3" -s "$@"', apprun.read_text(), flags=re.M))
    runner.chmod(0o755)
    (d["config"] / "known_hosts").write_text(sshd.host_key_line + "\n")
    p = params(d, sha, sshd, str, not args.no_pointer, args.slow,
               [str(runner), str(HERE / "drop_target.py")], args.only)
    pw_server, p["pw_ssh"] = password_server(d)
    proxy_proc, p["proxy"] = proxy_server(d)
    env = {"HOME": str(d["home"]), "PATH": "/usr/bin:/bin", "GDK_BACKEND": "x11", "LANG": "C.UTF-8",
           "XDG_RUNTIME_DIR": str(d["home"]), "GDK_DEBUG": "no-portals", "GTK_A11Y": "none",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path={d['tmp']}/no-bus", "TMPDIR": str(d["tmp"]),
           "ANYSCP_CONFIG_DIR": str(d["config"]),
           "ANYSCP_CACHE_DIR": str(d["cache"]), "ANYSCP_UI_TEST": json.dumps(p)}
    if not args.keyring:
        env["ANYSCP_NO_KEYRING"] = "1"
    scenario = [str(runner), str(HERE / "ui_scenario.py")]
    if args.docker:
        display = ":177"
        inner = (f"Xvfb {display} -screen 0 {SCREEN} -nolisten tcp >/dev/null 2>&1 & sleep 3; "
                 + " ".join(f"'{c}'" for c in scenario) + "; code=$?; kill %1; exit $code")
        cmd = ["docker", "run", "--rm", "--platform", "linux/arm64" if arch == "aarch64" else "linux/amd64",
               "--network", "host", "-v", f"{ROOT}:{ROOT}", "-w", str(ROOT), "-u", f"{os.getuid()}:{os.getgid()}"]
        for k, v in dict(env, DISPLAY=display).items():
            cmd += ["-e", f"{k}={v}"]
        cmd += args.docker_arg + [args.docker, "sh", "-c", inner]
        out, seconds, _ = run(cmd, os.environ, 3600)
    elif args.display:
        out, seconds, _ = run(scenario, dict(env, DISPLAY=args.display), max(1200, 600 * args.slow))
    else:
        with Xvfb() as x:
            out, seconds, _ = run(scenario, dict(env, DISPLAY=x.display), 1200)
    pw_server.terminate()
    proxy_proc.terminate()
    report(label, d, out, seconds)


# ---------------------------------------------------------------------------- Windows (Wine)

def windows(package: Path, display, sshd, args):
    label = "windows-" + (package.parent.name.removeprefix("stage-") if package.is_dir()
                          else re.sub(r"^anySCP-[\d.]+-windows-|-portable(\.zip)?$", "", package.name))
    log(f"\n{BOLD}[{label}] {package.name} (Wine){END}")
    base, d, sha = prepare(label)
    if package.is_dir():
        app = package if (package / "anySCP.exe").exists() else package / "anySCP"
    else:
        with zipfile.ZipFile(package) as zf:
            zf.extractall(base / "app")
        app = base / "app" / "anySCP"
    runtime = app / "runtime"
    python = runtime / "python.exe" if (runtime / "python.exe").exists() else runtime / "bin" / "python.exe"
    z = share.to_wine_z
    (d["config"] / "known_hosts").write_text(sshd.host_key_line + "\n")
    p = params(d, sha, sshd, z, not args.no_pointer, args.slow, [], args.only)
    pw_server, p["pw_ssh"] = password_server(d)
    proxy_proc, p["proxy"] = proxy_server(d)
    extra = {"ANYSCP_UI_TEST": json.dumps(p), "ANYSCP_CONFIG_DIR": z(d["config"]),
             "ANYSCP_CACHE_DIR": z(d["cache"]), "PYTHONUTF8": "1"}
    if not args.keyring:
        extra["ANYSCP_NO_KEYRING"] = "1"
    if os.environ.get("UI_WINEDEBUG"):                  # e.g. +loaddll,+seh to find a crash
        extra["WINEDEBUG"] = os.environ["UI_WINEDEBUG"]
    env = share.wine_env(display, extra)
    if os.environ.get("UI_GSK_RENDERER"):               # "default": GTK's own choice, as on Windows
        env.pop("GSK_RENDERER", None)
        if os.environ["UI_GSK_RENDERER"] != "default":
            env["GSK_RENDERER"] = os.environ["UI_GSK_RENDERER"]
    # (Wine sometimes deadlocks while the GTK DLLs load: if the scenario has
    # not started after 3 minutes, try once more with a fresh wineserver.)
    for attempt in (1, 2):
        out, seconds, started = run(["wine", z(python), "-s", z(HERE / "ui_scenario.py")], env, 1200,
                                    first_step_within=180)
        if started:
            break
        log(f"   (attempt {attempt}: the scenario did not start under Wine{', retrying' if attempt == 1 else ''})")
        share.stop_wine(display)
    pw_server.terminate()
    proxy_proc.terminate()
    report(label, d, out, seconds)

    # the launcher of the folder version
    selftest(label + " folder", app / "anySCP.exe", display, d)
    share.stop_wine(display)


def signed_copy(exe: Path, folder: Path) -> Path | None:
    """A copy of exe with an Authenticode signature (a throwaway self-signed
    certificate), made with osslsigncode; None without osslsigncode."""
    tool = shutil.which("osslsigncode") or os.environ.get("OSSLSIGNCODE")
    if not tool:
        log("   (osslsigncode not found: the signed exe is not tested)")
        return None
    share.rmtree(folder)
    folder.mkdir(parents=True)
    key, cert = folder / "test.key", folder / "test.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-days", "2", "-nodes",
                    "-subj", "/CN=anySCP test signature", "-addext", "extendedKeyUsage=codeSigning",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    out = folder / f"{exe.stem}-signed{exe.suffix}"
    subprocess.run([tool, "sign", "-certs", str(cert), "-key", str(key), "-h", "sha256", "-n", "anySCP",
                    "-in", str(exe), "-out", str(out)], check=True, capture_output=True)
    return out


def selftest(label, exe: Path, display, d):
    logf = d["out"] / f"selftest-{exe.stem}.log"
    env = share.wine_env(display, {"ANYSCP_LOG_FILE": share.to_wine_z(logf)})
    out, seconds, _ = run(["wine", share.to_wine_z(exe), "--self-test"], env, 600)
    text = (logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else "") or out
    rep = share.parse_selftest(text)
    if rep is None:
        record(label, f"{exe.name} --self-test", False, text[-800:])
        return
    bad = [f"{c['name']}: {c['detail']}" for c in rep["checks"] if not c["ok"]]
    record(label, f"{exe.name} --self-test ({seconds:.0f} s; {rep['info'].get('python')}, "
                  f"GTK {rep['info'].get('gtk')}, optional: "
                  f"{next((c['detail'] for c in rep['checks'] if c['name'] == 'optional libraries'), '?')})",
           not bad, "; ".join(bad))


# ---------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--linux", action="append", type=Path, default=[], metavar="APPIMAGE")
    ap.add_argument("--windows", action="append", type=Path, default=[], metavar="ZIP|DIR")
    ap.add_argument("--selftest", action="append", type=Path, default=[], metavar="EXE")
    ap.add_argument("--docker", metavar="IMAGE", help="run the Linux scenario in this container image")
    ap.add_argument("--docker-arg", action="append", default=[], metavar="ARG",
                    help="extra argument for docker run, e.g. --docker-arg=--cpuset-cpus=7")
    ap.add_argument("--display", metavar=":N", help="use this X display instead of starting Xvfb")
    ap.add_argument("--slow", type=float, default=1.0, help="scale the scenario's timeouts (emulation)")
    ap.add_argument("--no-pointer", action="store_true", help="skip the drags with a real pointer")
    ap.add_argument("--only", type=lambda v: v.split(","), metavar="PARTS",
                    help="after logging in, run only these parts: saved,proxy,queue,drag,pointer")
    ap.add_argument("--keyring", action="store_true",
                    help="Windows: use the real password store (Credential Manager) instead of memory")
    args = ap.parse_args()
    if not (args.linux or args.windows or args.selftest):
        args.linux = [DIST / f"anySCP-{VERSION}-x86_64.AppImage"]
        args.windows = [ROOT / "build" / "windows" / "stage-x64" / "anySCP"]
        args.selftest = [DIST / f"anySCP-{VERSION}-windows-x64.exe"]
    for path in args.linux + args.windows + args.selftest:
        if not path.exists():
            sys.exit(f"{path} does not exist")
    BUILD.mkdir(parents=True, exist_ok=True)
    share.BUILD.mkdir(parents=True, exist_ok=True)      # (the SSH server's folder)

    with share.SSHServer() as sshd:
        for appimage in args.linux:
            linux(appimage.resolve(), sshd, args)
        if args.windows or args.selftest:
            with Xvfb() as x:
                share.ensure_wine_prefix(x.display)
                for package in args.windows:
                    windows(package.resolve(), x.display, sshd, args)
                for exe in args.selftest:
                    # (a copy: in portable mode it makes anySCP-data next to itself)
                    d = {"out": BUILD / "selftest" / exe.stem}
                    share.rmtree(d["out"])
                    d["out"].mkdir(parents=True)
                    shutil.copy(exe, d["out"] / exe.name)
                    selftest(exe.stem, d["out"] / exe.name, x.display, d)
                    share.stop_wine(x.display)
                    # and signed: the launcher finds its runtime before the signature
                    signed = signed_copy(exe, BUILD / "selftest" / f"{exe.stem}-signed")
                    if signed is not None:
                        selftest(f"{exe.stem} signed", signed, x.display, {"out": signed.parent})
                        share.stop_wine(x.display)

    failed = [r for r in RESULTS if not r[1]]
    log(f"\n{BOLD}{len(RESULTS) - len(failed)}/{len(RESULTS)} passed{END}"
        + (f" – {RED}{len(failed)} failed{END}" if failed else f" – {GREEN}all good{END}"))
    for label, _ok, name in failed:
        log(f"   {RED}FAIL{END} [{label}] {name}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
