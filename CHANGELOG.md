# Changelog

All notable changes to anySCP. Versions follow [Semantic Versioning](https://semver.org/).

## 1.1.1 — 2026-11-24

### Changed

- **The Sites dialog is now a window of its own** that can be resized and maximized. It
  opens at the size it had last time. Escape still closes it.

## 1.1.0 — 2026-11-18

### Added

- **Jump hosts with settings of their own.** In the site dialog, the jump hosts are now
  a list: each one has its own host name, port, user name, password, authentication,
  private key and key passphrase, and proxy, just like the site itself. Add them with
  **Add**, remove them with the trash button, and move them with the up arrow. The arrow
  next to **Add** adds a copy of a saved site instead.
- **Remembered passwords and key passphrases for each machine in the chain.** The site
  and each of its jump hosts have a *Key passphrase* field and a *Remember password and
  passphrase* switch. What is remembered is used at login without asking. A passphrase
  typed when asked is remembered too, if that machine's switch is on.
- **The connection, machine by machine.** While connecting, the session shows each
  jump host and then the server, from top to bottom: whether it is waiting, connecting,
  connected or has failed, and what happened (host key, login, tunnel to the next
  machine). If the connection fails, the error page shows where it stopped.
- **Proxies anywhere in the chain.** A jump host's proxy is used to reach that jump host.
  The site's proxy is used to reach the server, through the last jump host if there are
  any.
- **Double-click a saved site** in the Sites dialog to log in to it.
- **Click the empty space of a file list** to clear its selection. The other side keeps its
  own selection.

### Changed

- Sites saved by 1.0 with jump hosts (a list of names, separated by commas) keep
  working as before. Once opened in the site dialog, they are shown and saved in the new
  form, with the site's proxy moved to the first jump host, where it was used.
- Importing from WinSCP turns a session's SSH tunnel into a jump host, with its
  password and WinSCP's proxy.

### Fixed

- A login prompt that was answered twice as it closed could use the second answer.

## 1.0.0 — 2026-11-16

First release: two-pane file transfer client for SFTP, SCP, FTP/FTPS and WebDAV, with a
transfer queue, folder synchronization, a built-in editor, saved sites, SSH jump hosts,
SOCKS and HTTP proxies, and WinSCP import. Linux (x86-64, arm64) and Windows (x64,
arm64); macOS on Apple Silicon is experimental.
