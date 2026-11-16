/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 anySCP contributors */
/*
 * anySCP.exe – native launcher for the Windows build.
 *
 * Two layouts are supported by the same binary:
 *
 *  - Portable folder:  anySCP.exe next to a "runtime" folder (the zip
 *    distribution).
 *  - Single file:      the runtime is appended to anySCP.exe as a zip
 *    archive followed by a 32-byte trailer:
 *        uint64 payload offset | 16 ASCII chars build id | "ANYSCPZ1"
 *    On first start it is extracted to %LOCALAPPDATA%\anySCP\runtime-<id>
 *    (atomically, so an interrupted extraction is simply redone).
 *
 * Two runtime layouts are understood:
 *
 *  - conda-forge (x64):  runtime\pythonw.exe, the GTK DLLs and data in
 *    runtime\Library\ (bin, lib, share, etc).
 *  - MSYS2 (arm64):      a Unix-like tree: runtime\bin\pythonw.exe with every
 *    DLL next to it, runtime\lib, runtime\share, runtime\etc.
 *
 * The launcher then points Python/GTK at the runtime, remembers the values it
 * replaced (ANYSCP_ORIG_* / ANYSCP_UNSET_VARS, used by anySCP when it starts
 * other programs) and runs  pythonw.exe -s -m anyscp <arguments>.
 * stdout/stderr go to %LOCALAPPDATA%\anySCP\anyscp.log; if Python exits with
 * an error within a few seconds the log is shown in a message box.
 */
#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <commctrl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <wchar.h>

#include "miniz.h"

#define APP_TITLE L"anySCP"
#define TRAILER_MAGIC "ANYSCPZ1"
#define TRAILER_SIZE 32

static wchar_t g_exe_path[MAX_PATH * 2];
static wchar_t g_exe_dir[MAX_PATH * 2];

/* ------------------------------------------------------------------ helpers */

static int no_dialogs(void)
{
    /* ANYSCP_NO_DIALOGS=1: automated tests – report through the exit code */
    wchar_t v[8];
    return GetEnvironmentVariableW(L"ANYSCP_NO_DIALOGS", v, 8) > 0 && v[0] != L'0';
}

static void fail(const wchar_t *fmt, ...)
{
    wchar_t msg[4096];
    va_list ap;
    va_start(ap, fmt);
    _vsnwprintf(msg, sizeof msg / sizeof msg[0] - 1, fmt, ap);
    va_end(ap);
    msg[sizeof msg / sizeof msg[0] - 1] = 0;
    if (no_dialogs())
        fwprintf(stderr, L"anySCP launcher: %ls\n", msg);
    else
        MessageBoxW(NULL, msg, APP_TITLE, MB_OK | MB_ICONERROR);
    ExitProcess(1);
}

static int dir_exists(const wchar_t *path)
{
    DWORD a = GetFileAttributesW(path);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}

static int file_exists(const wchar_t *path)
{
    DWORD a = GetFileAttributesW(path);
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

/* Create every missing directory of a path (backslash separated). */
static int make_dirs(const wchar_t *path)
{
    wchar_t buf[MAX_PATH * 4];
    size_t n = wcslen(path);
    if (n >= sizeof buf / sizeof buf[0])
        return 0;
    wcscpy(buf, path);
    for (size_t i = 3; i <= n; i++) {
        if (buf[i] == L'\\' || buf[i] == 0) {
            wchar_t c = buf[i];
            buf[i] = 0;
            if (!dir_exists(buf) && !CreateDirectoryW(buf, NULL) && GetLastError() != ERROR_ALREADY_EXISTS)
                return 0;
            buf[i] = c;
        }
    }
    return 1;
}

static void save_and_set(const wchar_t *name, const wchar_t *value, wchar_t *unset_list, size_t unset_cap)
{
    wchar_t old[32767];
    DWORD n = GetEnvironmentVariableW(name, old, 32767);
    if (n > 0 && n < 32767) {
        wchar_t key[256];
        _snwprintf(key, 255, L"ANYSCP_ORIG_%ls", name);
        key[255] = 0;
        SetEnvironmentVariableW(key, old);
    } else {
        if (wcslen(unset_list) + wcslen(name) + 2 < unset_cap) {
            wcscat(unset_list, L";");
            wcscat(unset_list, name);
        }
    }
    SetEnvironmentVariableW(name, value);   /* value NULL = unset */
}

/* ------------------------------------------------------------------ progress window */

static HWND g_progress_wnd, g_progress_bar;

static LRESULT CALLBACK progress_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_CLOSE)
        return 0;                 /* cannot be cancelled */
    return DefWindowProcW(h, m, w, l);
}

static void progress_open(void)
{
    INITCOMMONCONTROLSEX icc = { sizeof icc, ICC_PROGRESS_CLASS };
    InitCommonControlsEx(&icc);
    WNDCLASSW wc = { 0 };
    wc.lpfnWndProc = progress_proc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    wc.lpszClassName = L"AnyscpProgress";
    wc.hIcon = LoadIconW(wc.hInstance, MAKEINTRESOURCEW(1));
    wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_WAIT);
    RegisterClassW(&wc);
    int w = 420, h = 130;
    int x = (GetSystemMetrics(SM_CXSCREEN) - w) / 2, y = (GetSystemMetrics(SM_CYSCREEN) - h) / 2;
    g_progress_wnd = CreateWindowExW(WS_EX_TOPMOST, L"AnyscpProgress", APP_TITLE,
                                     WS_CAPTION | WS_VISIBLE, x, y, w, h, NULL, NULL, wc.hInstance, NULL);
    HFONT font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);
    HWND label = CreateWindowExW(0, L"STATIC", L"Preparing anySCP for its first start\x2026",
                                 WS_CHILD | WS_VISIBLE, 16, 14, w - 40, 20, g_progress_wnd, NULL, wc.hInstance, NULL);
    SendMessageW(label, WM_SETFONT, (WPARAM)font, TRUE);
    g_progress_bar = CreateWindowExW(0, PROGRESS_CLASSW, NULL, WS_CHILD | WS_VISIBLE,
                                     16, 44, w - 48, 18, g_progress_wnd, NULL, wc.hInstance, NULL);
    SendMessageW(g_progress_bar, PBM_SETRANGE32, 0, 1000);
    UpdateWindow(g_progress_wnd);
}

static void progress_set(double fraction)
{
    if (!g_progress_wnd)
        return;
    SendMessageW(g_progress_bar, PBM_SETPOS, (WPARAM)(fraction * 1000), 0);
    MSG msg;
    while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE)) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
}

static void progress_close(void)
{
    if (g_progress_wnd)
        DestroyWindow(g_progress_wnd);
    g_progress_wnd = NULL;
}

/* ------------------------------------------------------------------ payload */

/* Returns 1 and fills offset/size/id if this exe carries a runtime payload. */
static int read_trailer(uint64_t *offset, uint64_t *size, char id[17])
{
    FILE *f = _wfopen(g_exe_path, L"rb");
    if (!f)
        return 0;
    unsigned char t[TRAILER_SIZE];
    int ok = 0;
    if (_fseeki64(f, -(int64_t)TRAILER_SIZE, SEEK_END) == 0 && fread(t, 1, TRAILER_SIZE, f) == TRAILER_SIZE
        && memcmp(t + 24, TRAILER_MAGIC, 8) == 0) {
        int64_t end = _ftelli64(f);
        memcpy(offset, t, 8);
        memcpy(id, t + 8, 16);
        id[16] = 0;
        *size = (uint64_t)end - TRAILER_SIZE - *offset;
        ok = *offset > 0 && *size > 0;
    }
    fclose(f);
    return ok;
}

static void extract_payload(uint64_t offset, uint64_t size, const wchar_t *target)
{
    wchar_t tmp[MAX_PATH * 2], marker[MAX_PATH * 2];
    _snwprintf(tmp, MAX_PATH * 2 - 1, L"%ls.partial-%lu", target, GetCurrentProcessId());
    tmp[MAX_PATH * 2 - 1] = 0;
    if (!make_dirs(tmp))
        fail(L"Cannot create the folder\n%ls", tmp);

    FILE *f = _wfopen(g_exe_path, L"rb");
    if (!f || _fseeki64(f, (int64_t)offset, SEEK_SET) != 0)
        fail(L"Cannot read %ls", g_exe_path);
    mz_zip_archive zip;
    memset(&zip, 0, sizeof zip);
    if (!mz_zip_reader_init_cfile(&zip, f, size, 0))
        fail(L"The runtime embedded in anySCP.exe is damaged. Please download it again.");

    mz_uint count = mz_zip_reader_get_num_files(&zip);
    uint64_t total = 0, done = 0;
    for (mz_uint i = 0; i < count; i++) {
        mz_zip_archive_file_stat st;
        if (mz_zip_reader_file_stat(&zip, i, &st))
            total += st.m_uncomp_size;
    }
    progress_open();
    for (mz_uint i = 0; i < count; i++) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&zip, i, &st))
            fail(L"Corrupt runtime entry %u", i);
        wchar_t rel[MAX_PATH * 2], out[MAX_PATH * 4];
        int n = MultiByteToWideChar(CP_UTF8, 0, st.m_filename, -1, rel, MAX_PATH * 2);
        if (n <= 0)
            fail(L"Bad file name in runtime");
        for (wchar_t *p = rel; *p; p++)
            if (*p == L'/')
                *p = L'\\';
        _snwprintf(out, MAX_PATH * 4 - 1, L"%ls\\%ls", tmp, rel);
        out[MAX_PATH * 4 - 1] = 0;
        if (st.m_is_directory) {
            make_dirs(out);
            continue;
        }
        wchar_t *slash = wcsrchr(out, L'\\');
        *slash = 0;
        make_dirs(out);
        *slash = L'\\';
        FILE *o = _wfopen(out, L"wb");
        if (!o)
            fail(L"Cannot write\n%ls", out);
        if (!mz_zip_reader_extract_to_cfile(&zip, i, o, 0))
            fail(L"Cannot extract\n%ls\n(disk full?)", out);
        fclose(o);
        done += st.m_uncomp_size;
        if ((i & 31) == 0)
            progress_set(total ? (double)done / total : 0);
    }
    mz_zip_reader_end(&zip);
    fclose(f);

    _snwprintf(marker, MAX_PATH * 2 - 1, L"%ls\\anyscp-runtime.ok", tmp);
    marker[MAX_PATH * 2 - 1] = 0;
    HANDLE m = CreateFileW(marker, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (m != INVALID_HANDLE_VALUE)
        CloseHandle(m);
    progress_set(1.0);
    if (!MoveFileExW(tmp, target, 0)) {
        /* Another instance finished first: use its copy. */
        if (!dir_exists(target))
            fail(L"Cannot finish installing the runtime to\n%ls", target);
    }
    progress_close();
}

/* ------------------------------------------------------------------ network locations */

/* UNC path (\\server\share, \\?\UNC\...) or a mapped network drive. */
static int is_network_path(const wchar_t *path)
{
    if (wcsncmp(path, L"\\\\?\\UNC\\", 8) == 0)
        return 1;
    if (wcsncmp(path, L"\\\\?\\", 4) == 0)
        path += 4;
    if (path[0] == L'\\' && path[1] == L'\\')
        return 1;
    if (path[0] && path[1] == L':') {
        wchar_t root[4] = { path[0], L':', L'\\', 0 };
        return GetDriveTypeW(root) == DRIVE_REMOTE;
    }
    return 0;
}

static uint64_t g_copy_done, g_copy_total;
static unsigned g_copy_files;

static int copy_tree(const wchar_t *src, const wchar_t *dst)
{
    if (!CreateDirectoryW(dst, NULL) && GetLastError() != ERROR_ALREADY_EXISTS)
        return 0;
    wchar_t pattern[MAX_PATH * 4];
    _snwprintf(pattern, MAX_PATH * 4 - 1, L"%ls\\*", src);
    pattern[MAX_PATH * 4 - 1] = 0;
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    int ok = 1;
    do {
        if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L".."))
            continue;
        wchar_t s2[MAX_PATH * 4], d2[MAX_PATH * 4];
        _snwprintf(s2, MAX_PATH * 4 - 1, L"%ls\\%ls", src, fd.cFileName);
        _snwprintf(d2, MAX_PATH * 4 - 1, L"%ls\\%ls", dst, fd.cFileName);
        s2[MAX_PATH * 4 - 1] = d2[MAX_PATH * 4 - 1] = 0;
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            ok = copy_tree(s2, d2);
        } else {
            ok = CopyFileW(s2, d2, FALSE);
            g_copy_done += ((uint64_t)fd.nFileSizeHigh << 32) | fd.nFileSizeLow;
            if ((++g_copy_files & 15) == 0)
                progress_set(g_copy_total ? (double)g_copy_done / g_copy_total : 0);
        }
    } while (ok && FindNextFileW(h, &fd));
    FindClose(h);
    return ok;
}

/* Copy the runtime from a network share to the local cache (once per build). */
static int cache_runtime(const wchar_t *src, const wchar_t *target, uint64_t total_bytes)
{
    wchar_t tmp[MAX_PATH * 2], marker[MAX_PATH * 2];
    _snwprintf(tmp, MAX_PATH * 2 - 1, L"%ls.partial-%lu", target, GetCurrentProcessId());
    tmp[MAX_PATH * 2 - 1] = 0;
    wchar_t parent[MAX_PATH * 2];
    wcscpy(parent, target);
    wchar_t *sl = wcsrchr(parent, L'\\');
    if (sl)
        *sl = 0;
    if (!make_dirs(parent))
        return 0;
    g_copy_total = total_bytes;
    g_copy_done = 0;
    g_copy_files = 0;
    progress_open();
    int ok = copy_tree(src, tmp);
    if (ok) {
        _snwprintf(marker, MAX_PATH * 2 - 1, L"%ls\\anyscp-runtime.ok", tmp);
        marker[MAX_PATH * 2 - 1] = 0;
        HANDLE m = CreateFileW(marker, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (m != INVALID_HANDLE_VALUE)
            CloseHandle(m);
        progress_set(1.0);
        if (!MoveFileExW(tmp, target, 0) && !dir_exists(target))
            ok = 0;
    }
    progress_close();
    return ok;
}

/* ------------------------------------------------------------------ main */

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR cmdline, int show)
{
    (void)inst; (void)prev; (void)show;
    if (!GetModuleFileNameW(NULL, g_exe_path, MAX_PATH * 2))
        fail(L"Cannot determine the program location");
    wcscpy(g_exe_dir, g_exe_path);
    wchar_t *slash = wcsrchr(g_exe_dir, L'\\');
    if (slash)
        *slash = 0;

    wchar_t localapp[MAX_PATH * 2], appdata_dir[MAX_PATH * 2], runtime[MAX_PATH * 2];
    if (!GetEnvironmentVariableW(L"LOCALAPPDATA", localapp, MAX_PATH * 2))
        wcscpy(localapp, g_exe_dir);
    _snwprintf(appdata_dir, MAX_PATH * 2 - 1, L"%ls\\anySCP", localapp);
    appdata_dir[MAX_PATH * 2 - 1] = 0;

    uint64_t off = 0, size = 0;
    char id[17];
    if (read_trailer(&off, &size, id)) {
        wchar_t wid[17];
        MultiByteToWideChar(CP_ACP, 0, id, -1, wid, 17);
        _snwprintf(runtime, MAX_PATH * 2 - 1, L"%ls\\runtime-%ls", appdata_dir, wid);
        runtime[MAX_PATH * 2 - 1] = 0;
        wchar_t marker[MAX_PATH * 2];
        _snwprintf(marker, MAX_PATH * 2 - 1, L"%ls\\anyscp-runtime.ok", runtime);
        marker[MAX_PATH * 2 - 1] = 0;
        if (!file_exists(marker))
            extract_payload(off, size, runtime);
    } else {
        _snwprintf(runtime, MAX_PATH * 2 - 1, L"%ls\\runtime", g_exe_dir);
        runtime[MAX_PATH * 2 - 1] = 0;
        /* Started from a network share: run from a local copy of the runtime.
           Windows does not resolve DLL dependencies from UNC folders added
           with AddDllDirectory reliably, thousands of small reads over SMB/NFS
           make every start slow, and running in place would lock the files on
           the share.  The copy is shared with the single-file exe (same id). */
        wchar_t idfile[MAX_PATH * 2];
        _snwprintf(idfile, MAX_PATH * 2 - 1, L"%ls\\anyscp-runtime.id", runtime);
        idfile[MAX_PATH * 2 - 1] = 0;
        FILE *idf = is_network_path(g_exe_dir) ? _wfopen(idfile, L"rb") : NULL;
        if (idf) {
            char line[128] = { 0 };
            unsigned long long bytes = 0;
            if (fgets(line, sizeof line, idf)) {
                char *nl = strpbrk(line, "\r\n");
                if (nl)
                    *nl = 0;
                if (fscanf(idf, "%llu", &bytes) != 1)
                    bytes = 0;
            }
            fclose(idf);
            if (strlen(line) == 16) {
                wchar_t wid[17], cached[MAX_PATH * 2], marker[MAX_PATH * 2];
                MultiByteToWideChar(CP_ACP, 0, line, -1, wid, 17);
                _snwprintf(cached, MAX_PATH * 2 - 1, L"%ls\\runtime-%ls", appdata_dir, wid);
                cached[MAX_PATH * 2 - 1] = 0;
                _snwprintf(marker, MAX_PATH * 2 - 1, L"%ls\\anyscp-runtime.ok", cached);
                marker[MAX_PATH * 2 - 1] = 0;
                if (file_exists(marker) || cache_runtime(runtime, cached, bytes))
                    wcscpy(runtime, cached);
                /* else: could not copy (disk full…) – try running from the share */
            }
        }
    }

    /* conda-forge layout, else MSYS2 layout (see the top of this file) */
    wchar_t python[MAX_PATH * 2], tree[MAX_PATH * 2];
    int msys2 = 0;
    _snwprintf(python, MAX_PATH * 2 - 1, L"%ls\\pythonw.exe", runtime);
    python[MAX_PATH * 2 - 1] = 0;
    if (!file_exists(python)) {
        wchar_t alt[MAX_PATH * 2];
        _snwprintf(alt, MAX_PATH * 2 - 1, L"%ls\\bin\\pythonw.exe", runtime);
        alt[MAX_PATH * 2 - 1] = 0;
        if (!file_exists(alt))
            fail(L"The anySCP runtime is missing:\n%ls\n\nKeep anySCP.exe next to its \"runtime\" folder.", python);
        wcscpy(python, alt);
        msys2 = 1;
    }
    /* where bin, lib, share and etc are */
    _snwprintf(tree, MAX_PATH * 2 - 1, msys2 ? L"%ls" : L"%ls\\Library", runtime);
    tree[MAX_PATH * 2 - 1] = 0;

    /* ---- environment ---- */
    static wchar_t unset_list[2048] = L"";
    static wchar_t buf[32767], oldpath[32767];
    DWORD n = GetEnvironmentVariableW(L"PATH", oldpath, 32767);
    if (n == 0 || n >= 32767)
        oldpath[0] = 0;
    if (msys2)
        _snwprintf(buf, 32766, L"%ls\\bin;%ls", runtime, oldpath);
    else
        _snwprintf(buf, 32766, L"%ls\\Library\\bin;%ls;%ls\\DLLs;%ls", runtime, runtime, runtime, oldpath);
    save_and_set(L"PATH", buf, unset_list, 2048);
    save_and_set(L"PYTHONHOME", runtime, unset_list, 2048);
    save_and_set(L"PYTHONNOUSERSITE", L"1", unset_list, 2048);
    save_and_set(L"PYTHONPATH", NULL, unset_list, 2048);
    save_and_set(L"PYTHONSTARTUP", NULL, unset_list, 2048);
    save_and_set(L"PYTHONUTF8", L"1", unset_list, 2048);       /* UTF-8 log, whatever the code page */
    _snwprintf(buf, 32766, L"%ls\\lib\\girepository-1.0", tree);
    save_and_set(L"GI_TYPELIB_PATH", buf, unset_list, 2048);
    _snwprintf(buf, 32766, L"%ls\\share", tree);
    save_and_set(L"XDG_DATA_DIRS", buf, unset_list, 2048);
    _snwprintf(buf, 32766, L"%ls\\share\\glib-2.0\\schemas", tree);
    save_and_set(L"GSETTINGS_SCHEMA_DIR", buf, unset_list, 2048);
    save_and_set(L"GSETTINGS_BACKEND", L"memory", unset_list, 2048);   /* no registry writes */
    _snwprintf(buf, 32766, L"%ls\\lib\\gdk-pixbuf-2.0\\2.10.0\\loaders.cache", tree);
    save_and_set(L"GDK_PIXBUF_MODULE_FILE", buf, unset_list, 2048);
    _snwprintf(buf, 32766, L"%ls\\etc\\fonts\\fonts.conf", tree);
    save_and_set(L"FONTCONFIG_FILE", buf, unset_list, 2048);
    SetEnvironmentVariableW(L"ANYSCP_UNSET_VARS", unset_list[0] ? unset_list + 1 : L"");
    SetEnvironmentVariableW(L"ANYSCP_LAUNCHER_DIR", g_exe_dir);

    /* ---- log file ---- */
    make_dirs(appdata_dir);
    wchar_t logpath[MAX_PATH * 2];
    if (!GetEnvironmentVariableW(L"ANYSCP_LOG_FILE", logpath, MAX_PATH * 2))   /* tests: one log per instance */
        _snwprintf(logpath, MAX_PATH * 2 - 1, L"%ls\\anyscp.log", appdata_dir);
    logpath[MAX_PATH * 2 - 1] = 0;
    SECURITY_ATTRIBUTES sa = { sizeof sa, NULL, TRUE };
    HANDLE log = CreateFileW(logpath, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                             CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);

    /* ---- command line: "pythonw.exe" -s -P -m anyscp <our arguments> ----
       (-P: never import an "anyscp" folder from the current directory) */
    static wchar_t cmd[32767];
    _snwprintf(cmd, 32766, L"\"%ls\" -s -P -m anyscp %ls", python, cmdline ? cmdline : L"");

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    if (log != INVALID_HANDLE_VALUE) {
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdOutput = log;
        si.hStdError = log;
        si.hStdInput = NULL;
    }
    if (!CreateProcessW(python, cmd, NULL, NULL, log != INVALID_HANDLE_VALUE, 0, NULL, NULL, &si, &pi))
        fail(L"Could not start Python (error %lu):\n%ls", GetLastError(), python);

    /* --self-test: wait for the report and show it.  Otherwise, if Python
       dies right away, show why instead of failing silently. */
    int self_test = cmdline && wcsstr(cmdline, L"--self-test") != NULL;
    if (WaitForSingleObject(pi.hProcess, self_test ? 300000 : 6000) == WAIT_OBJECT_0) {
        DWORD code = 0;
        GetExitCodeProcess(pi.hProcess, &code);
        if ((code != 0 || self_test) && log != INVALID_HANDLE_VALUE) {
            CloseHandle(log);
            log = INVALID_HANDLE_VALUE;
            static char text[8192];
            static wchar_t wtext[8192], msg[9000];
            text[0] = 0;
            FILE *lf = _wfopen(logpath, L"rb");
            if (lf) {
                _fseeki64(lf, 0, SEEK_END);
                int64_t len = _ftelli64(lf);
                _fseeki64(lf, len > 6000 ? len - 6000 : 0, SEEK_SET);
                size_t got = fread(text, 1, sizeof text - 1, lf);
                text[got] = 0;
                fclose(lf);
            }
            MultiByteToWideChar(CP_UTF8, 0, text, -1, wtext, 8192);
            /* the report is everything before the machine readable line */
            wchar_t *json = wcsstr(wtext, L"ANYSCP-SELF-TEST {");
            if (json)
                *json = 0;
            if (self_test)
                _snwprintf(msg, 8999, L"%ls\nLog: %ls", wtext, logpath);
            else
                _snwprintf(msg, 8999, L"anySCP stopped unexpectedly (exit code %lu).\n\n%ls\n\nLog: %ls",
                           code, wtext, logpath);
            msg[8999] = 0;
            if (!no_dialogs())
                MessageBoxW(NULL, msg, APP_TITLE, MB_OK | (code == 0 ? MB_ICONINFORMATION : MB_ICONERROR));
        }
    }
    DWORD final_code = 0;
    if (GetExitCodeProcess(pi.hProcess, &final_code) && final_code == STILL_ACTIVE)
        final_code = 0;
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    if (log != INVALID_HANDLE_VALUE)
        CloseHandle(log);
    return (int)final_code;
}
