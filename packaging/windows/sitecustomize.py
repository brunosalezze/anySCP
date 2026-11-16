# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# Added by the anySCP Windows build.  Makes the GTK DLLs in Library\bin
# loadable however Python is started:
#  - os.add_dll_directory() for extension modules (.pyd), and
#  - PATH for libraries that GObject-introspection opens with LoadLibrary.
import os
import sys

_anyscp_dll_dirs = []
if sys.platform == "win32":
    _dirs = [os.path.join(sys.prefix, "Library", "bin"), os.path.join(sys.prefix, "DLLs")]
    _path = os.environ.get("PATH", "")
    _parts = [p.lower().rstrip("\\") for p in _path.split(os.pathsep) if p]
    _missing = [d for d in _dirs if os.path.isdir(d) and d.lower() not in _parts]
    if _missing:
        os.environ["PATH"] = os.pathsep.join(_missing + ([_path] if _path else []))
    for _d in _dirs:
        if os.path.isdir(_d):
            try:
                _anyscp_dll_dirs.append(os.add_dll_directory(_d))
            except OSError:
                pass
