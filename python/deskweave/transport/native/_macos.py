from __future__ import annotations

import ctypes as c
import os
import select
import socket
import struct
import threading
from pathlib import Path

from ._common import Resource, rejected

LIBPROC = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
LIBPROC.proc_pidinfo.argtypes = [c.c_int, c.c_int, c.c_uint64, c.c_void_p, c.c_int]
LIBPROC.proc_pidinfo.restype = c.c_int
LIBPROC.proc_pidpath.argtypes = [c.c_int, c.c_void_p, c.c_uint32]
LIBPROC.proc_pidpath.restype = c.c_int


CF = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
SECURITY = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
pointer = c.c_void_p
for name, result, args in (
    ("CFDataCreate", pointer, [pointer, pointer, c.c_long]),
    ("CFStringCreateWithCString", pointer, [pointer, c.c_char_p, c.c_uint32]),
    ("CFDictionaryCreate", pointer, [pointer, pointer, pointer, c.c_long, pointer, pointer]),
    ("CFRelease", None, [pointer]),
):
    function = getattr(CF, name)
    function.restype, function.argtypes = result, args
for name, args in (
    ("SecRequirementCreateWithString", [pointer, c.c_uint32, pointer]),
    ("SecCodeCopyGuestWithAttributes", [pointer, pointer, c.c_uint32, pointer]),
    ("SecCodeCheckValidity", [pointer, c.c_uint32, pointer]),
):
    function = getattr(SECURITY, name)
    function.restype, function.argtypes = c.c_int32, args


class BsdInfo(c.Structure):
    _fields_ = [
        (name, c.c_uint32)
        for name in (
            "flags",
            "status",
            "xstatus",
            "pid",
            "ppid",
            "uid",
            "gid",
            "ruid",
            "rgid",
            "svuid",
            "svgid",
            "reserved",
        )
    ]
    _fields_ += [("comm", c.c_char * 16), ("name", c.c_char * 32)]
    _fields_ += [(name, c.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid", "nice")]
    _fields_ += [("seconds", c.c_uint64), ("microseconds", c.c_uint64)]


def process_executable(pid):
    path = c.create_string_buffer(4096)
    if LIBPROC.proc_pidpath(pid, path, len(path)) <= 0:
        raise rejected()
    return Path(path.value.decode()).resolve(strict=True)


def current_executable():
    return process_executable(os.getpid())


def local_session():
    first, second = socket.socketpair()
    with first, second:
        token = first.getsockopt(0, 6, 32)
    return "audit:" + str(struct.unpack("8I", token)[6])


class Process(Resource):
    platform = "macos"

    def __init__(self, pid):
        self.pid = pid
        self._lock = threading.RLock()
        self._queue = select.kqueue()
        self._changed = False
        try:
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_EXIT | select.KQ_NOTE_EXEC,
            )
            self._queue.control([event], 0, 0)
            self.initial = self.snapshot()
        except BaseException:
            self.close()
            raise

    def snapshot(self):
        with self._lock:
            if self._queue is None or self._changed:
                raise rejected()
            self._changed = bool(self._queue.control(None, 1, 0))
            if self._changed:
                raise rejected()
            info = BsdInfo()
            if LIBPROC.proc_pidinfo(self.pid, 3, 0, c.byref(info), c.sizeof(info)) != c.sizeof(
                info
            ):
                raise rejected()
            if len({info.uid, info.ruid, info.svuid}) != 1 or info.status == 5:
                raise rejected()
            return (
                str(info.uid),
                local_session(),
                f"{info.seconds}:{info.microseconds}",
                process_executable(self.pid),
            )

    def close(self):
        with self._lock:
            if self._queue is not None:
                self._queue.close()
                self._queue = None


def peer(sock):
    token = sock.getsockopt(0, 6, 32)
    return struct.unpack("8I", token)[5], token


def verify_peer(process, token, requirement):
    values = struct.unpack("8I", token)
    snapshot = process.snapshot()
    if (values[5], str(values[1]), "audit:" + str(values[6])) != (
        process.pid,
        snapshot[0],
        snapshot[1],
    ):
        raise rejected()
    if not requirement:
        raise rejected()
    cf, security = CF, SECURITY
    pointer = c.c_void_p
    objects = []
    try:
        data = cf.CFDataCreate(None, c.create_string_buffer(token), len(token))
        text = cf.CFStringCreateWithCString(None, requirement.encode(), 0x08000100)
        objects.extend([data, text])
        if not data or not text:
            raise rejected()
        keys = (pointer * 1)(pointer.in_dll(security, "kSecGuestAttributeAudit").value)
        values = (pointer * 1)(data)
        attributes = cf.CFDictionaryCreate(
            None,
            keys,
            values,
            1,
            c.addressof(c.c_byte.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
            c.addressof(c.c_byte.in_dll(cf, "kCFTypeDictionaryValueCallBacks")),
        )
        objects.append(attributes)
        rule, guest = pointer(), pointer()
        if security.SecRequirementCreateWithString(text, 0, c.byref(rule)) != 0:
            raise rejected()
        objects.append(rule)
        if security.SecCodeCopyGuestWithAttributes(None, attributes, 0, c.byref(guest)) != 0:
            raise rejected()
        objects.append(guest)
        if security.SecCodeCheckValidity(guest, 0, rule) != 0:
            raise rejected()
    finally:
        for item in reversed(objects):
            if item:
                cf.CFRelease(item)
