# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Runs one job in the interactive desktop of a real Windows PC.

GUI tests need the desktop the monitors show (an SSH login has none), so a
scheduled task of the logged-on user ("run only when the user is logged on")
starts this script, with pythonw.exe so that no console window covers the
test.  It reads job.json next to itself:

    {"cmd": [...], "env": {...}, "cwd": "...", "timeout": 900}

runs the command without a console window, writes its output to job.log and
then its exit code to job.done.
"""

import json
import os
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    with open(os.path.join(BASE, "job.json"), encoding="utf-8") as fh:
        job = json.load(fh)
    env = dict(os.environ)
    env.update(job.get("env", {}))
    with open(os.path.join(BASE, "job.log"), "wb") as log:
        try:
            code = subprocess.run(job["cmd"], cwd=job.get("cwd", BASE), env=env, stdout=log,
                                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  timeout=job.get("timeout", 900),
                                  creationflags=0x08000000).returncode     # CREATE_NO_WINDOW
        except subprocess.TimeoutExpired:
            code = "timeout"
        except OSError as exc:
            log.write(f"cannot start {job['cmd']}: {exc}\n".encode())
            code = "error"
    with open(os.path.join(BASE, "job.done"), "w", encoding="utf-8") as fh:
        fh.write(str(code))


main()
