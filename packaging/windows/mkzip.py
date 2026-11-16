# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Zip a directory deterministically.

usage: mkzip.py <base_dir> <subdir-or-empty> <out.zip>

With a subdir, entries are stored as "<subdir>/..." (portable zip); with
"" the *contents* of base_dir are stored at the top level (exe payload).
"""

import os
import sys
import zipfile

base, sub, out = sys.argv[1], sys.argv[2], sys.argv[3]
root = os.path.join(base, sub) if sub else base
tmp = out + ".tmp"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            arc = os.path.relpath(full, base if sub else root).replace(os.sep, "/")
            zf.write(full, arc)
os.replace(tmp, out)
