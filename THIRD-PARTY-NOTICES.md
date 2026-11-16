# Third-party notices

anySCP is licensed under the **GNU General Public License v3.0 or later**
(see [LICENSE](LICENSE)).

## Relationship to WinSCP

anySCP is an independent project. It is **not affiliated with, endorsed by or
sponsored by WinSCP** or its author, Martin Přikryl. It was written from
scratch, and WinSCP is named only to describe where the user interface takes its
inspiration from and which file format the importer reads.

- **Code:** WinSCP's source code is licensed under the GNU GPL version 3
  ([github.com/winscp/winscp](https://github.com/winscp/winscp), `license.txt`).
  anySCP contains no WinSCP source code except one small, attributed port that is
  needed for compatibility. In `anyscp/winscp_import.py`, the decoding of
  passwords stored in `WinSCP.ini` follows WinSCP's
  `source/core/Security.cpp` (`DecryptPassword`). Because anySCP is licensed
  under GPL-3.0-or-later, this is permitted.
- **File format:** the names of the `WinSCP.ini` keys and the protocol numbers
  (`FSProtocol`, `Ftps`) are WinSCP's file format and are read only for
  interoperability.
- **Artwork:** WinSCP's icon set is *not* under the GPL. It is licensed stock
  artwork that may not be reused outside WinSCP (section B of WinSCP's
  `license.txt`). anySCP uses **none** of it:
  - The anySCP application icon (`anyscp/data/icons/…`) is original artwork made
    for this project.
  - File and toolbar icons come from the system icon theme, normally GNOME's
    Adwaita (LGPL-3.0-or-later / CC-BY-SA-3.0).

## Runtime dependencies

The source tree contains only anySCP's own code. When run from source, it uses
the libraries installed on the system:

| Component | License |
|---|---|
| GTK 4, GLib, Pango, gdk-pixbuf | LGPL-2.1-or-later / LGPL-2.0-or-later |
| libadwaita | LGPL-2.1-or-later |
| PyGObject | LGPL-2.1-or-later |
| GtkSourceView 5 (optional) | LGPL-2.1-or-later |
| VTE (optional) | LGPL-3.0-or-later |
| libsecret (optional) | LGPL-2.1-or-later |
| paramiko | LGPL-2.1-or-later |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| pycairo | LGPL-2.1-only OR MPL-1.1 |

## Packaged builds (AppImage, Windows .exe)

The portable packages bundle Python and many unmodified open source
components. Where they come from:

| Package | Components |
|---|---|
| AppImage x86-64, Windows x64 | [conda-forge](https://conda-forge.org) |
| AppImage arm64 | conda-forge, plus GtkSourceView and VTE (no arm64 builds on conda-forge), which are compiled from their unmodified upstream releases by `packaging/appimage/build-arm64-extras.sh` |
| Windows arm64 | [MSYS2](https://www.msys2.org) (conda-forge has no GTK for Windows on ARM), plus paramiko from conda-forge |

The build scripts generate a complete list automatically, with each
component's version, license, homepage and source recipe or source archive
(`packaging/collect_licenses.py`). Only components that are actually in the
package are listed. They ship it in every package with the license texts:

- **AppImage:** `usr/share/doc/anyscp/THIRD-PARTY-LICENSES.txt` and `licenses/`
- **Windows:** `THIRD-PARTY-LICENSES.txt` next to `anySCP.exe`, plus
  `runtime\licenses-info\licenses\`

A few MSYS2 packages ship no license file of their own (PyGObject,
gobject-introspection, GStreamer). Their folders then contain the texts from
conda-forge's package of the same project or, for plain LGPL-2.1, the standard
license text, and the list says so.

All bundled licenses are compatible with distributing the whole under the
GPL-3.0:

- Most components are permissive (MIT, BSD, Apache-2.0, PSF, zlib) or LGPL.
- FreeType is used under its FTL option, and nettle and GMP under their
  LGPL-3.0-or-later option.
- readline and libgettextpo are GPL-3.0.
- The Microsoft Visual C++ runtime DLLs in the Windows x64 build are
  redistributable under Microsoft's terms. (The arm64 build uses LLVM's libc++
  and the Universal CRT that is part of Windows.)

If you **publish** the binary packages, the GPL/LGPL also require that the
corresponding source code of those components be available. The bundled list
links each component's conda-forge or MSYS2 recipe and the exact package used,
or the upstream source archive. The simplest way to comply is to keep a copy
of the matching source archives next to your release, or to link to them from
it.

*This file summarizes the licensing situation as understood by the project.
It is not legal advice.*
