# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Render the app SVG into a multi-resolution Windows .ico (PNG entries)."""
import struct
import sys

import gi
gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg
import cairo

svg, out = sys.argv[1], sys.argv[2]
handle = Rsvg.Handle.new_from_file(svg)
images = []
for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    vp = Rsvg.Rectangle()
    vp.x, vp.y, vp.width, vp.height = 0, 0, size, size
    handle.render_document(ctx, vp)
    import io
    buf = io.BytesIO()
    surface.write_to_png(buf)
    images.append((size, buf.getvalue()))
header = struct.pack("<HHH", 0, 1, len(images))
offset = 6 + 16 * len(images)
entries, data = b"", b""
for size, png in images:
    dim = 0 if size >= 256 else size
    entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
    data += png
with open(out, "wb") as fh:
    fh.write(header + entries + data)
