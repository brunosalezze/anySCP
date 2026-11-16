# Building the portable packages

Every package bundles its own Python, GTK 4.24, libadwaita 1.10, PyGObject,
GtkSourceView and paramiko, so users install nothing. Everything is built on
an x86-64 Linux machine (tested on Debian 13), the arm64 packages included.

| Output (in `dist/`) | What it is |
|---|---|
| `anySCP-<ver>-x86_64.AppImage` | Linux x86-64, single file. Needs glibc ≥ 2.28 (Debian 10, Ubuntu 20.04, RHEL 8 and later). Includes the embedded terminal (VTE). |
| `anySCP-<ver>-aarch64.AppImage` | Linux arm64 (Raspberry Pi 4/5 with a 64-bit OS, ARM servers and laptops), otherwise the same. |
| `anySCP-<ver>-windows-x64.exe` | Windows 10/11, single file. On first start it unpacks its runtime to `%LOCALAPPDATA%\anySCP\runtime-<id>` (once per version), then starts in about a second. |
| `anySCP-<ver>-windows-arm64.exe` | The same for Windows on ARM (Snapdragon PCs, Surface Pro X/11). |

The folder the exe is made from (`anySCP.exe` next to `runtime\`) stays in
`build/windows/stage-<arch>/anySCP`; the launcher runs that layout too.

Where the components come from:

| Package | Runtime |
|---|---|
| AppImage x86-64, Windows x64 | conda-forge (Python 3.12) |
| AppImage arm64 | conda-forge (Python 3.12). conda-forge has no arm64 GtkSourceView and VTE, so these two are compiled from their upstream releases, with conda-forge's build options. |
| Windows arm64 | MSYS2 (`clangarm64`, Python 3.14): conda-forge has no GTK for Windows on ARM. paramiko comes from conda-forge, so that all builds have the same version. |

## 1. One-time setup

```sh
./packaging/setup-build-deps.sh
```

It is safe to re-run; finished steps are skipped. It:

1. installs `gcc-mingw-w64-x86-64`, `wine` and `qemu-user-static` with apt;
2. downloads micromamba, appimagetool, the x86-64 and arm64 AppImage runtimes,
   miniz and [llvm-mingw](https://github.com/mstorsjo/llvm-mingw) (the compiler
   for the arm64 `anySCP.exe`) into `build/tools/`;
3. creates the conda environments `build/appimage/env` (linux-64),
   `build/windows/env` (win-64), `build/appimage/env-aarch64` (linux-aarch64,
   downloaded without running anything) and `build/appimage/buildenv-aarch64`
   (the same with compilers);
4. downloads the Windows-on-ARM runtime from MSYS2 into `build/windows/env-arm64`
   (`windows/msys2_fetch.py`: reads the repository index, resolves the
   dependencies and checks every package's SHA-256);
5. builds small Docker images (Ubuntu 22.04/24.04 with Xvfb and no GTK, x86-64
   and arm64) used to test the AppImages;
6. builds GtkSourceView and VTE for arm64 in an emulated arm64 container
   (`appimage/build-arm64-extras.sh`, about 10 minutes, once) and makes the
   arm64 runtime's gdk-pixbuf loader cache there.

Everything except the apt packages and Docker images stays in `build/` (about 6 GB).

## 2. Build

```sh
./packaging/appimage/build.sh           # ~5 s  -> dist/anySCP-<ver>-x86_64.AppImage
./packaging/appimage/build.sh aarch64   # ~5 s  -> dist/anySCP-<ver>-aarch64.AppImage
./packaging/windows/build.sh            # ~30 s -> dist/anySCP-<ver>-windows-x64.exe
./packaging/windows/build.sh arm64      # ~20 s -> dist/anySCP-<ver>-windows-arm64.exe
```

Re-run them after changing the app. They copy the runtime again, prune it and repack.

## macOS (Apple Silicon, experimental)

`packaging/macos/build.sh` runs on a Mac with Apple Silicon, not on the Linux build
machine; the `macOS` GitHub Actions workflow (`.github/workflows/macos.yml`) runs it on a
`macos-15` runner when the macOS packaging changes, every Monday, for version tags and by
hand (a workflow of its own: GitHub's Apple Silicon runners are sometimes unavailable for
a long while, which must not fail the main build). It needs only the
Xcode command line tools: it downloads micromamba and the conda-forge runtime itself
(the same packages as the other builds, with `keyring` for the Keychain; conda-forge has
no VTE for macOS, so the terminal opens in Terminal.app). It makes
`build/macos/anySCP.app` and `dist/anySCP-<ver>-macos-arm64.dmg`.

- The runtime goes into `anySCP.app/Contents/Resources`, pruned as in the AppImage.
- `Contents/MacOS/anySCP` is a small C program (`launcher.c`) that runs Python inside its
  own process, linked to the runtime's `libpython`. So macOS sees the app as anySCP (menu
  bar, Dock, Keychain prompts), not as `python3`. It sets up the environment as `AppRun`
  does, and remembers the values it replaces for the programs anySCP starts.
- The bytecode is "unchecked-hash" and `PYTHONDONTWRITEBYTECODE` is set: Python never
  writes into the bundle, which would break its signature.
- The bundle is signed ad hoc (Apple Silicon runs only signed code). Without a paid Apple
  Developer ID it cannot be notarized, so people approve its first start once (README).
- There is no build for Intel Macs, which Apple is phasing out; `build.sh` stops on them.
- That workflow also runs `--self-test` from the mounted disk image with
  `ANYSCP_SELFTEST_SCREENSHOT`, which saves a picture of the window as GTK draws it
  (`dist/macos-window.png`, in its artifact), and the backend tests.

## Microsoft Store (MSIX)

The Store takes the Windows programs as MSIX packages, and signs them itself: installed from
the Store, anySCP gets no SmartScreen or Smart App Control warning. The package holds the
folder layout of a Windows build (anySCP.exe next to `runtime\`), not the single-file exe.

```sh
./packaging/windows/build.sh arm64      # (or x64)
./packaging/windows/msix/make_layout.sh arm64 <Package/Identity/Name> "<Publisher CN=...>" "<Publisher display name>"
```

The three identity values come from Partner Center (the app's *Product identity*).
`make_layout.sh` writes `build/msix/<arch>/layout`: the build's files, `AppxManifest.xml`
(a full-trust desktop app, `runFullTrust`) and the logos made from the app icon. On Windows,
`makeappx pack /d layout /p anySCP.msix` packs it; the Store wants it unsigned.

`packaging/windows/msix/wack.ps1 -Layout <layout>` (as an administrator) packs the layout,
signs it with a throwaway certificate whose subject is the manifest's Publisher, runs the
**Windows App Certification Kit** on it and removes the certificate and the test install
again. It installs the Windows SDK's kit and tools first if needed. The SDK installer offers
the kit only on x86/x64 Windows; on Windows on ARM, its x64 MSIs (from
`winsdksetup.exe /layout <dir> /features OptionId.WindowsSoftwareLogoToolkit`, folder
`Installers`) install with `msiexec` and run emulated.

First result (2026-11-16, arm64 package, kit 10.0.26100.7627): overall **PASS**; 23 of 24
tests passed. The optional "Blocked executables" test lists every file containing strings like
"cmd" or "reg" (Python's standard library, GTK, openssl.cnf...) and API names like
`ShellExecuteW`: informational, not a certification failure.

## How it works

**AppImage** (`appimage/`):
- `AppRun` points Python, GObject-introspection, GSettings schemas, gdk-pixbuf loaders, fontconfig and the XKB/X11 data at the bundle.
- The libraries use `$ORIGIN` RPATHs, so `LD_LIBRARY_PATH` is never set. Only glibc and the GPU drivers come from the host.
- Fonts come from the host's fontconfig setup. On systems without one (minimal containers), bundled fonts and aliases are used instead.
  `FONTCONFIG_PATH=/etc/fonts` matters: the host's `fonts.conf` includes `conf.d` (the `sans-serif`/`monospace` aliases)
  by a relative path, which the bundled fontconfig would otherwise look up in its own build folder; every font name then
  fell back to the first font installed (FreeSerif, in the editor and terminal). `--self-test` checks that `Monospace` is fixed-width.
- On X11, `AppRun` picks GTK's OpenGL renderer unless `GSK_RENDERER` is set: GTK's Vulkan renderer cannot present on X servers without DRI3 (Xvfb, VNC, xrdp) and then quits instead of falling back. Wayland sessions keep GTK's default.
- The arm64 image is assembled without running any arm64 code: the tools the build uses (`glib-compile-schemas`, `rsvg-convert`, the bytecode compiler) come from the x86-64 runtime, whose Python has the same version. Their output does not depend on the architecture.

**Windows** (`windows/`):
- `launcher.c` is a small native launcher, built with mingw-w64 (x64) or llvm-mingw (arm64) and miniz. It sets the environment and runs `pythonw.exe -m anyscp`. It understands both runtime layouts: conda-forge's (`runtime\pythonw.exe`, GTK in `runtime\Library\`) and MSYS2's (`runtime\bin\pythonw.exe` with every DLL next to it, `runtime\lib`, `runtime\share`).
- In the single-file build, the runtime is a zip appended to the exe, followed by a 32-byte trailer. Extraction goes to a temporary folder that is renamed when complete, so an interrupted first start is simply redone.
- When the folder version (`anySCP.exe` + `runtime\`) is started from a network location (a UNC path or a mapped network drive), it copies `runtime\` once to the same local cache the single exe uses. Both use the same build id, written to `runtime\anyscp-runtime.id`. Loading the GTK DLLs straight from a UNC folder fails, and running in place would be slow and would lock the share.
- `--self-test` makes the launcher wait for the report and show it in a message box. `ANYSCP_NO_DIALOGS=1` suppresses all message boxes (used by automated tests), and `ANYSCP_LOG_FILE` overrides the log path.
- Python's output goes to `%LOCALAPPDATA%\anySCP\anyscp.log`. If the app dies during startup, the launcher shows that log in a message box.
- Drag and drop: GTK's Windows backend advertises only the MIME types a drag's content provider lists itself (X11 and Wayland also add those its values can be serialized to). So drags between the panes carry a marker under its own MIME type, local files offered to Explorer carry their "Shell IDList Array" directly, and server files offered to Explorer come from `anyscp/ui/lazyprovider.py`, a provider that makes the data only when Explorer asks for it (after the drop, once the temporary download is complete).
- GTK on Windows converts drag and drop positions with the main monitor's scale, which is wrong for a window on a monitor with another scale. So drops are taken for the whole window and sent to the pane under the pointer's real position (`GetCursorPos`), and every drop is finished explicitly (GTK busy-waits for that).
- Monitors left of or above the main one have negative coordinates. GTK runs Windows' drag loop on its own thread and posts it the pointer position, packed from floating point numbers. Converting a negative number that way is undefined in C, and on ARM64 it becomes 0. The drag loop then looks for the drop target at the edge of the main monitor: drops on the other monitor did nothing, or moved the files to the desktop. `anyscp/ui/win32drag.py` hooks that thread (`WH_GETMESSAGE`) and puts in the real pointer position. It also subclasses the drag icon's window so the icon follows the pointer there. If either cannot be set up, the drag icon is left out; Windows still shows its drag cursor.
- The arm64 runtime is staged by `windows/stage_msys2.py`. It leaves out headers, static and import libraries, documentation, translations, developer tools, Tcl/Tk and GStreamer's plugins. Then it keeps only the DLLs something loads: it reads the imports of Python, its extension modules, the image loaders and the libraries named in the GObject typelibs, and fails the build if any import is missing. It also writes gdk-pixbuf's loader cache, which pacman's install hook would have made (MSYS2 packages are unpacked without pacman). 860 MB of MSYS2 packages become a 120 MB runtime.

**Both**: the launchers save every variable they change as `ANYSCP_ORIG_*` or `ANYSCP_UNSET_VARS`. anySCP uses these to start other programs (default apps, terminals) with the user's normal environment, so they don't load the bundled libraries.

**Portable mode** (automatic): on first start the packaged app creates `anySCP-data` next to the AppImage or the exe, and keeps its settings, saved sites and host keys there. If that location isn't writable, it falls back to `~/.config/anyscp` or `%APPDATA%\anySCP`. `ANYSCP_PORTABLE=0` disables portable mode. Passwords always go to the system keyring or Windows Credential Manager. The Windows runtime cache and log stay in `%LOCALAPPDATA%\anySCP`.

## Licenses in the packages

The build scripts run `packaging/collect_licenses.py`. It reads the metadata
of every bundled component (conda-forge's, MSYS2's, or `anyscp-extras.json`
for the two libraries built from source) and writes
`THIRD-PARTY-LICENSES.txt` (name, version, license, homepage, recipe, exact
package or source URL) and a `licenses/<component>/` folder with the license
texts. Only components that are in the package are listed. anySCP's own
GPL-3.0 `LICENSE` is included in every package. If you publish the binaries,
also make the matching sources available; see `THIRD-PARTY-NOTICES.md`.

## Testing

```sh
python3 tests/gui/run_ui_tests.py           # x86-64 AppImage + Windows x64 (Wine)
python3 tests/gui/run_ui_tests.py --linux dist/anySCP-<ver>-aarch64.AppImage \
        --docker anyscp-test:22.04-arm64 --slow 6               # arm64, emulated
./packaging/windows/build.sh x64-msys2      # the Windows arm64 package's x64 twin…
python3 tests/gui/run_ui_tests.py --windows build/windows/stage-x64-msys2/anySCP \
        --selftest build/windows/dist-x64-msys2/anySCP-<ver>-windows-x64-msys2.exe   # …under Wine
```

`tests/gui/run_ui_tests.py` runs `tests/gui/ui_scenario.py` inside a package's
own runtime, against a throwaway OpenSSH server (and a paramiko SFTP server for
password logins, `tests/gui/sftp_server.py`). It covers saving a new site on
**Log In**, reopening a site whose password is in the password store (with
`--keyring`, Windows Credential Manager under Wine), the transfer queue at the bottom of the window (opens with a
transfer, closes 2 s after the last one, stays open on errors, fits the
smallest window), dragging files out to other programs (the file list they
get, the temporary download of server files, its reuse, cancellation and
clean-up), and drags with real mouse input: on Linux under Xvfb, between the
panes and onto a stand-in file manager; on Windows with synthesized input
(`SetCursorPos`/`mouse_event`) through Windows' own drag and drop, between the
panes. On Windows it also runs the launchers' `--self-test`. `tests/shares/` tests starting from network shares.

Wine shows one screen, so drag and drop across monitors is tested on a real
Windows PC with two monitors: `tests/gui/multimon_scenario.py` runs in the
package's own Python, in the desktop the monitors show. An SSH login has no
desktop, so a scheduled task of the logged-on user ("run only when the user
is logged on") starts `tests/gui/win_runner.py` with `pythonw.exe`, which runs
the job described in `job.json` next to it. The scenario places anySCP and an
Explorer window on each pair of monitors and drags with synthesized input: pane
to pane both ways, a file dropped back on its own pane, local and server files
onto Explorer, and files from Explorer onto anySCP. It also opens sessions the
ways a user does (a new site with **Log In**, the saved site from the start page
and from the site dialog, quick connect) and checks the real screen pixels for
the server's files. `tests/gui/win_probe.py` prints the monitor layout and
takes screenshots. The Windows packages contain unchecked-hash `.pyc` files: to
try a changed `.py` file in an installed runtime, delete the `__pycache__` folders.

GTK on Windows (4.24 and main) releases every drag object once more than it
took it: in `gdk/win32/gdkdrag-win32.c`, `do_drag_drop_response()` clears the
data object's pointer with `g_clear_object()`, although `data_object_new()`
never took a reference. The object is freed early; under Wine that logged
`g_object_unref: assertion 'G_IS_OBJECT (object)' failed` and crashed now and
then in a later drag. `anyscp/ui/win32drag.py` takes one extra reference per
drag and watches the object: if GTK did not use it up (a fixed GTK), it is
given back five seconds after the drag, so nothing leaks either way. The
Windows pointer test checks both.

Testing notes:

- The arm64 AppImage passed the whole GUI scenario on real arm64 hardware (Debian 13, Mali-G720, headless with Xvfb): `run_ui_tests.py --linux … --display :N` with an X server from a container.
- The AppImages were also tested in `ubuntu:22.04` and `ubuntu:24.04` containers with no GTK, no Python GUI libraries and no fonts. The arm64 one runs there under qemu emulation: an AppImage of another architecture has to be started with `qemu-aarch64-static` explicitly, because the AppImage marker in its ELF header keeps qemu's binfmt rule from matching (not an issue on real arm64 machines).
- The Windows x64 build was tested under Wine 10: first-start extraction, the fast second start, an SFTP session, the drive-letter local pane, and Windows Credential Manager. Under Wine, text only renders with `PANGOCAIRO_BACKEND=fc`, because Wine's DirectWrite is incomplete. Real Windows uses its native DirectWrite path. That path has not been run on real Windows hardware by these scripts.
- Wine on x86-64 cannot run arm64 programs. The Windows arm64 package is tested through `build.sh x64-msys2`: the same MSYS2 packages and versions for x64 (`clang64`), staged by the same script into the same layout, with the launcher's same code path and Python 3.14. Only the CPU architecture differs, and the arm64 launcher is compiled but never run. Under Wine both Windows builds occasionally deadlock or crash while the GTK DLLs load, before any of anySCP's code runs (a Wine problem); the runner then tries once more.
