/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 anySCP contributors
 *
 * anySCP.app/Contents/MacOS/anySCP: the program of the macOS app bundle.
 *
 * It runs Python inside this very process (linked against the runtime's
 * libpython in Contents/Resources/lib), so that macOS sees anySCP.app and not
 * a python3 binary: menu bar name, Dock icon, Keychain prompts.  First it
 * points Python, GObject introspection and GTK at the runtime in
 * Contents/Resources.  Like the AppImage and Windows launchers it remembers
 * the values it replaces (ANYSCP_ORIG_<NAME>, ANYSCP_UNSET_VARS), so that the
 * programs anySCP starts get the original environment.  Then it runs
 *     python -s -P -m anyscp <arguments>
 */
#include <Python.h>

#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Every variable this launcher sets or removes. */
static const char *CHANGED[] = {
    "PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONSTARTUP", "PYTHONSAFEPATH",
    "PYTHONDONTWRITEBYTECODE", "GI_TYPELIB_PATH", "XDG_DATA_DIRS", "GSETTINGS_SCHEMA_DIR",
    "GSETTINGS_BACKEND", "GIO_MODULE_DIR", "GIO_EXTRA_MODULES", "GDK_PIXBUF_MODULE_FILE",
    "GTK_PATH", "GTK_EXE_PREFIX", "GTK_DATA_PREFIX", "LC_CTYPE", NULL,
};

static void remember_environment(void)
{
    char unset[4096] = "", key[256];
    for (const char **name = CHANGED; *name; name++) {
        const char *value = getenv(*name);
        if (value) {
            snprintf(key, sizeof key, "ANYSCP_ORIG_%s", *name);
            setenv(key, value, 1);
        } else {
            if (unset[0])
                strlcat(unset, ":", sizeof unset);
            strlcat(unset, *name, sizeof unset);
        }
    }
    if (unset[0])
        setenv("ANYSCP_UNSET_VARS", unset, 1);
}

static void set_path(const char *name, const char *base, const char *relative)
{
    char value[PATH_MAX * 2];
    snprintf(value, sizeof value, "%s/%s", base, relative);
    setenv(name, value, 1);
}

/* path without its last `levels` components */
static void strip(char *path, int levels)
{
    while (levels-- > 0) {
        char *slash = strrchr(path, '/');
        if (slash)
            *slash = 0;
    }
}

int main(int argc, char **argv)
{
    char exe[PATH_MAX], self[PATH_MAX], resources[PATH_MAX], folder[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, self)) {
        fprintf(stderr, "anySCP: cannot find where the program is\n");
        return 1;
    }
    /* self: <folder>/anySCP.app/Contents/MacOS/anySCP */
    strlcpy(resources, self, sizeof resources);
    strip(resources, 2);
    strlcat(resources, "/Resources", sizeof resources);
    strlcpy(folder, self, sizeof folder);
    strip(folder, 4);

    remember_environment();
    setenv("PYTHONHOME", resources, 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);     /* never write into the signed bundle */
    unsetenv("PYTHONPATH");
    unsetenv("PYTHONSTARTUP");
    unsetenv("PYTHONSAFEPATH");
    unsetenv("GIO_EXTRA_MODULES");
    unsetenv("GTK_PATH");
    unsetenv("GTK_EXE_PREFIX");
    unsetenv("GTK_DATA_PREFIX");
    set_path("GI_TYPELIB_PATH", resources, "lib/girepository-1.0");
    set_path("XDG_DATA_DIRS", resources, "share");
    set_path("GSETTINGS_SCHEMA_DIR", resources, "share/glib-2.0/schemas");
    setenv("GSETTINGS_BACKEND", "memory", 1);       /* GTK's own settings: nothing written */
    set_path("GIO_MODULE_DIR", resources, "lib/gio/modules");
    set_path("GDK_PIXBUF_MODULE_FILE", resources, "lib/gdk-pixbuf-2.0/2.10.0/loaders.cache");
    /* Apps started from Finder have no locale: GLib then assumes ASCII. */
    if (!getenv("LC_ALL") && !getenv("LC_CTYPE") && !getenv("LANG"))
        setenv("LC_CTYPE", "UTF-8", 1);
    /* the folder holding anySCP.app: portable mode if anySCP-data is there */
    setenv("ANYSCP_LAUNCHER_DIR", folder, 1);

    /* python -s -P -m anyscp <arguments>, without the -psn_… argument that
       older macOS versions add when the app is opened from Finder */
    char **args = calloc((size_t)argc + 5, sizeof *args);
    if (!args)
        return 1;
    int n = 0;
    args[n++] = argv[0];
    args[n++] = "-s";
    args[n++] = "-P";
    args[n++] = "-m";
    args[n++] = "anyscp";
    for (int i = 1; i < argc; i++)
        if (strncmp(argv[i], "-psn_", 5) != 0)
            args[n++] = argv[i];
    return Py_BytesMain(n, args);
}
