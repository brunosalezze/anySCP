# Privacy policy

anySCP is a file transfer program that runs on your own computer. It has no
servers or accounts of its own, collects no data, has no telemetry or
advertising, and does not check for updates.

## Network connections

anySCP connects only to the servers you enter (SFTP, SCP, FTP/FTPS, WebDAV)
and to the proxies and SSH jump hosts you set up for them. It uses these
connections only to show and transfer the files you choose. The links in its
About dialog open in your web browser, and only when you click them.

## What it keeps on your computer

- **Settings, saved sites, bookmarks, server host keys and trusted
  certificates** are stored in anySCP's settings folder. In portable mode
  that is the `anySCP-data` folder next to the program. Otherwise it is
  `~/.config/anyscp` on Linux and `%APPDATA%\anySCP` on Windows. A saved
  site holds the server's address, your user name and folders, but never
  your password.
- **Passwords** are stored only if you choose "Remember password". They go
  into your system's keyring (GNOME Keyring, KWallet, ...) or the Windows
  Credential Manager, never into a file of anySCP's.
- **Copies of remote files you open in another program** are stored in
  anySCP's cache folder, under `edit`. That is `anySCP-data/cache` in
  portable mode, `~/.cache/anyscp` on Linux, and
  `%LOCALAPPDATA%\anySCP\cache` on Windows. They stay there until you delete
  them.
- **Copies of files you drag out of the window to another program** are kept
  in the system's temporary folder (`anySCP-drag`), and anySCP removes them
  later.
- **On Windows only:** the program unpacks its runtime to
  `%LOCALAPPDATA%\anySCP`, and writes its error output to
  `%LOCALAPPDATA%\anySCP\anyscp.log`.

anySCP keeps a session's log in memory only. The log is gone when you close
the session.

None of this is sent anywhere. To remove it all, delete the folders above
and any passwords anySCP saved in your keyring.

## Questions

Open an issue at <https://github.com/brunosalezze/anySCP/issues>.
