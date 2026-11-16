# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""A content provider whose data is made when it is asked for ("delayed
rendering"), for drags to other programs on Windows.

GTK's Windows backend advertises only the MIME types a drag's content
provider lists itself; unlike X11 and Wayland it does not add those its
values can be serialized to.  So on Windows a provider has to list the
format – and is then asked to write it itself, through the provider's
``write_mime_type_async`` vfunc.  PyGObject cannot implement that vfunc in
Python: the caller's ``user_data`` gets lost and its task is never completed.
LazyProvider therefore installs the two vfuncs in its class structure with
ctypes, where GTK's callback and user_data pass through untouched, and calls
a Python function for the data.
"""

from __future__ import annotations

import ctypes
import itertools

from gi.repository import Gdk

_LIBRARIES = {  # conda-forge (Windows), MSYS2 (Windows), Linux
    "gobject": ("gobject-2.0-0.dll", "libgobject-2.0-0.dll", "libgobject-2.0.so.0"),
    "gio": ("gio-2.0-0.dll", "libgio-2.0-0.dll", "libgio-2.0.so.0"),
    "glib": ("glib-2.0-0.dll", "libglib-2.0-0.dll", "libglib-2.0.so.0"),
}


def _load(names):
    for name in names:
        try:
            return ctypes.CDLL(name)      # (the libraries are already loaded: this finds them)
        except OSError:
            continue
    raise OSError(f"cannot load any of {names}")


_gobject, _gio, _glib = (_load(_LIBRARIES[n]) for n in ("gobject", "gio", "glib"))
_ptr = ctypes.c_void_p
_gobject.g_type_from_name.argtypes = [ctypes.c_char_p]
_gobject.g_type_from_name.restype = ctypes.c_size_t
_gobject.g_type_class_ref.argtypes = [ctypes.c_size_t]
_gobject.g_type_class_ref.restype = _ptr
_gobject.g_object_ref.argtypes = [_ptr]
_gobject.g_object_ref.restype = _ptr
_gobject.g_object_unref.argtypes = [_ptr]
_gobject.g_object_unref.restype = None
_gio.g_task_new.argtypes = [_ptr, _ptr, _ptr, _ptr]
_gio.g_task_new.restype = _ptr
_gio.g_task_return_boolean.argtypes = [_ptr, ctypes.c_int]
_gio.g_task_return_boolean.restype = None
_gio.g_task_return_error.argtypes = [_ptr, _ptr]
_gio.g_task_return_error.restype = None
_gio.g_task_propagate_boolean.argtypes = [_ptr, _ptr]
_gio.g_task_propagate_boolean.restype = ctypes.c_int
_gio.g_output_stream_write_all_async.argtypes = [_ptr, _ptr, ctypes.c_size_t, ctypes.c_int, _ptr, _ptr, _ptr]
_gio.g_output_stream_write_all_async.restype = None
_gio.g_output_stream_write_all_finish.argtypes = [_ptr, _ptr, ctypes.POINTER(ctypes.c_size_t),
                                                  ctypes.POINTER(_ptr)]
_gio.g_output_stream_write_all_finish.restype = ctypes.c_int
_gio.g_io_error_quark.argtypes = []
_gio.g_io_error_quark.restype = ctypes.c_uint32
_glib.g_error_new_literal.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_char_p]
_glib.g_error_new_literal.restype = _ptr

_instances: dict[int, LazyProvider] = {}    # C instance pointer -> provider
_writing: dict[int, tuple] = {}             # write in progress -> (task, stream, buffer)
_keys = itertools.count(1)


class LazyProvider(Gdk.ContentProvider):
    """Offers ``mime_types``; ``produce(mime_type, done)`` is called for the
    data when another program asks for it, and calls ``done(data)`` (bytes)
    or ``done(None, message)``, at once or later from the main loop."""

    __gtype_name__ = "AnyscpLazyProvider"

    def __init__(self, mime_types, produce):
        super().__init__()
        self._mime_types = list(mime_types)
        self._produce = produce
        _instances[hash(self)] = self       # (PyGObject's hash is the C pointer)

    def do_ref_formats(self):
        return Gdk.ContentFormats.new(self._mime_types)

    def forget(self):
        """The drag is over: no more requests are expected."""
        _instances.pop(hash(self), None)


def _return_error(task, message: str):
    error = _glib.g_error_new_literal(_gio.g_io_error_quark(), 0, message.encode("utf-8", "replace"))
    _gio.g_task_return_error(task, error)               # (takes the error)


@ctypes.CFUNCTYPE(None, _ptr, ctypes.c_char_p, _ptr, ctypes.c_int, _ptr, _ptr, _ptr)
def _write_mime_type_async(provider, mime_type, stream, io_priority, cancellable, callback, user_data):
    task = _gio.g_task_new(provider, cancellable, callback, user_data)
    _gobject.g_object_ref(stream)
    finished = []

    def done(data, message=None):
        if finished:
            return
        finished.append(True)
        if data is None:
            _return_error(task, message or "not available")
            _gobject.g_object_unref(stream)
            _gobject.g_object_unref(task)
            return
        # Asynchronously: the reader may be in this very thread (a pipe within
        # anySCP), and a synchronous write would wait for it forever.
        key = next(_keys)
        buffer = ctypes.create_string_buffer(bytes(data), len(data))
        _writing[key] = (task, stream, buffer)
        _gio.g_output_stream_write_all_async(stream, ctypes.cast(buffer, _ptr), len(data), io_priority, None,
                                            ctypes.cast(_write_done, _ptr), _ptr(key))

    owner = _instances.get(provider)
    try:
        if owner is None:
            done(None, "the drag is over")
        else:
            owner._produce(mime_type.decode(), done)
    except Exception as exc:  # noqa: BLE001 - never leave GTK's request open
        done(None, str(exc))


@ctypes.CFUNCTYPE(None, _ptr, _ptr, _ptr)
def _write_done(stream, result, key):
    task, stream, _buffer = _writing.pop(key)
    written, error = ctypes.c_size_t(0), _ptr()
    if _gio.g_output_stream_write_all_finish(stream, result, ctypes.byref(written), ctypes.byref(error)):
        _gio.g_task_return_boolean(task, 1)
    else:
        _gio.g_task_return_error(task, error)
    _gobject.g_object_unref(stream)
    _gobject.g_object_unref(task)


@ctypes.CFUNCTYPE(ctypes.c_int, _ptr, _ptr, _ptr)
def _write_mime_type_finish(provider, result, error):
    return _gio.g_task_propagate_boolean(result, error)


def _install():
    offsets = {f.get_name(): f.get_offset() for f in Gdk.ContentProviderClass.__info__.get_fields()}
    klass = _gobject.g_type_class_ref(_gobject.g_type_from_name(b"AnyscpLazyProvider"))
    for name, function in (("write_mime_type_async", _write_mime_type_async),
                           ("write_mime_type_finish", _write_mime_type_finish)):
        offset = offsets[name]
        if offset <= 0 or offset % ctypes.sizeof(_ptr):
            raise RuntimeError(f"GdkContentProviderClass.{name}: unexpected offset {offset}")
        ctypes.c_void_p.from_address(klass + offset).value = ctypes.cast(function, _ptr).value


_install()
