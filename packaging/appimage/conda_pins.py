# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Print the exact package specs of a conda environment, one per line.

usage: conda_pins.py <conda-env> <platform>

Used to create a build environment with the very same libraries as a runtime
environment.  The last line pins the C library the compilers link against
(``sysroot_<platform>``) to the oldest glibc the runtime's own packages
accept, so that what is built against it runs wherever the runtime does.
"""

import glob
import json
import re
import sys

env, platform = sys.argv[1], sys.argv[2]
floor = (2, 17)
for path in sorted(glob.glob(env + "/conda-meta/*.json")):
    meta = json.load(open(path, encoding="utf-8"))
    print(f"{meta['name']}=={meta['version']}={meta['build']}")
    for dep in meta.get("depends", []):
        m = re.match(r"__glibc >=(\d+)\.(\d+)", dep)
        if m:
            floor = max(floor, (int(m.group(1)), int(m.group(2))))
print(f"sysroot_{platform}=={floor[0]}.{floor[1]}")
