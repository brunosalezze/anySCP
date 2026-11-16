# Microsoft Store listing (Partner Center)

Texts and settings for the anySCP submission (Store ID 9N6K0972GDK0). The packages
are the `anySCP-msix-<commit>` artifact of the Build workflow: `anySCP_<version>_x64.msix`
and `anySCP_<version>_arm64.msix` (unsigned; the Store signs them). Every new
submission needs a higher version number (`anyscp/__init__.py`).

## Packages

Upload both .msix files. Device family: Windows 10/11 Desktop.

## Properties

- Category: **Utilities & tools**, subcategory **File managers** (or Developer tools)
- Privacy policy URL: https://github.com/brunosalezze/anySCP/blob/main/PRIVACY.md
- Website: https://github.com/brunosalezze/anySCP
- Support contact: https://github.com/brunosalezze/anySCP/issues

## Submission options: restricted capability `runFullTrust`

> anySCP is a desktop (Win32) application packaged as MSIX. runFullTrust lets it run
> as a regular desktop app: it lists, transfers and edits files between this PC and
> the servers the user connects to.

## Store listing (English)

**Description**

> anySCP is a free, open-source file transfer client with a two-pane layout: your
> computer on one side, the server on the other. Copy files with F5 or by dragging
> them, synchronize folders with a preview, edit remote files in place, and let a
> queue of transfers run in the background.
>
> It connects over SFTP, SCP, FTP/FTPS and WebDAV, also through SSH jump hosts and
> SOCKS or HTTP proxies. It checks server keys and certificates, keeps passwords in
> the Windows Credential Manager, and can import your saved sites from WinSCP.
>
> anySCP collects no data and connects only to the servers you enter. It is free
> software under the GNU General Public License (version 3 or later); the source code
> is at https://github.com/brunosalezze/anySCP.

**Product features** (one per line)

- Two panes: this PC and the server side by side
- SFTP, SCP, FTP/FTPS and WebDAV/WebDAVS
- Drag and drop between the panes and to File Explorer
- Background transfers with progress, speed, cancel and retry
- Folder synchronization with a preview of every change
- Built-in editor for remote files, or open them in any program
- SSH jump hosts in series, SOCKS and HTTP proxies
- Saved sites with folders and colors; imports sites from WinSCP
- Passwords in the Windows Credential Manager
- Light and dark appearance

**Keywords**: sftp, scp, ftp, webdav, ssh, file transfer, file manager

**Screenshots** (desktop, at least 1366 × 768): `docs/screenshots/commander.png`,
`sites.png`, `sync.png`, `dark.png`, `welcome.png`.

**Store logos → 1:1 app tile icon** (300 × 300): `store-logos/app-tile-icon-300x300.png`
(the app icon; without it the Store shows the package's 50 × 50 logo, enlarged).

**Copyright**: © 2026 anySCP contributors. GPL-3.0-or-later.
