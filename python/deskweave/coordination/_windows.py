from __future__ import annotations

import ctypes as c
import msvcrt
import os
import uuid
from ctypes import wintypes as w
from pathlib import Path

from deskweave.errors import DeskWeaveError
from deskweave.transport.native import _windows as win
from deskweave.transport.native._common import Resource

NTDLL = c.WinDLL("ntdll", use_last_error=True)
PTR, DWORD, HANDLE = c.c_void_p, w.DWORD, w.HANDLE


class UnicodeString(c.Structure):
    _fields_ = [("length", w.USHORT), ("maximum", w.USHORT), ("buffer", w.LPWSTR)]


class ObjectAttributes(c.Structure):
    _fields_ = [
        ("length", DWORD),
        ("root", HANDLE),
        ("name", PTR),
        ("attributes", DWORD),
        ("security", PTR),
        ("quality", PTR),
    ]


class IoStatus(c.Structure):
    _fields_ = [("status", PTR), ("information", c.c_size_t)]


class FileId(c.Structure):
    _fields_ = [("volume", c.c_uint64), ("identity", c.c_ubyte * 16)]


class FileStandard(c.Structure):
    _fields_ = [
        ("allocated", c.c_int64),
        ("size", c.c_int64),
        ("links", DWORD),
        ("pending", c.c_ubyte),
        ("directory", c.c_ubyte),
    ]


class FileTag(c.Structure):
    _fields_ = [("attributes", DWORD), ("tag", DWORD)]


class Rename(c.Structure):
    _fields_ = [("replace", DWORD), ("root", HANDLE), ("length", DWORD), ("name", w.WCHAR * 1)]


class Acl(c.Structure):
    _fields_ = [
        ("revision", c.c_ubyte),
        ("reserved", c.c_ubyte),
        ("size", w.USHORT),
        ("count", w.USHORT),
        ("reserved2", w.USHORT),
    ]


NtCreateFile = win.api(
    NTDLL, "NtCreateFile", w.LONG, PTR, DWORD, PTR, PTR, PTR, DWORD, DWORD, DWORD, DWORD, PTR, DWORD
)
NtSetInformationFile = win.api(
    NTDLL, "NtSetInformationFile", w.LONG, HANDLE, PTR, PTR, DWORD, c.c_int
)
NtStatusError = win.api(NTDLL, "RtlNtStatusToDosError", DWORD, w.LONG)
GetInfo = win.api(win.KERNEL, "GetFileInformationByHandleEx", w.BOOL, HANDLE, c.c_int, PTR, DWORD)
SetInfo = win.api(win.KERNEL, "SetFileInformationByHandle", w.BOOL, HANDLE, c.c_int, PTR, DWORD)
Lock = win.api(win.KERNEL, "LockFileEx", w.BOOL, HANDLE, DWORD, DWORD, DWORD, DWORD, PTR)
Unlock = win.api(win.KERNEL, "UnlockFileEx", w.BOOL, HANDLE, DWORD, DWORD, DWORD, PTR)
CreateDirectory = win.api(win.KERNEL, "CreateDirectoryW", w.BOOL, w.LPCWSTR, PTR)
GetSecurity = win.api(
    win.ADVAPI, "GetSecurityInfo", DWORD, HANDLE, c.c_int, DWORD, PTR, PTR, PTR, PTR, PTR
)
GetAce = win.api(win.ADVAPI, "GetAce", w.BOOL, PTR, DWORD, PTR)


def denied():
    return DeskWeaveError("RESOURCE_DENIED", "目录或协调文件的身份与权限无效。")


def information(handle, kind, structure):
    value = structure()
    win.checked(GetInfo(handle, kind, c.byref(value), c.sizeof(value)))
    return value


def identity(handle):
    value = information(handle, 18, FileId)
    return str(value.volume), bytes(value.identity).hex()


def sid_text(sid):
    value = w.LPWSTR()
    win.checked(win.ConvertSidToStringSid(sid, c.byref(value)))
    try:
        return value.value
    finally:
        win.LocalFree(value)


def private(handle, principal):
    owner, dacl, descriptor = PTR(), PTR(), PTR()
    result = GetSecurity(
        handle, 1, 5, c.byref(owner), None, c.byref(dacl), None, c.byref(descriptor)
    )
    if result:
        raise c.WinError(result)
    try:
        if not owner or sid_text(owner) != principal.identity.user_id or not dacl:
            raise denied()
        allowed = {
            principal.identity.user_id,
            principal.process.logon_sid,
            "S-1-5-18",
            "S-1-5-32-544",
        }
        for index in range(c.cast(dacl, c.POINTER(Acl)).contents.count):
            ace = PTR()
            win.checked(GetAce(dacl, index, c.byref(ace)))
            kind = c.c_ubyte.from_address(ace.value).value
            if kind == 1:
                continue
            if kind != 0 or sid_text(PTR(ace.value + 8)) not in allowed:
                raise denied()
    finally:
        win.LocalFree(descriptor)


def make_private(path, principal):
    descriptor = PTR()
    sid = principal.process.logon_sid
    win.checked(
        win.ConvertSDDL(
            f"O:{principal.identity.user_id}D:P(A;OICI;FA;;;{sid})", 1, c.byref(descriptor), None
        )
    )
    try:
        attributes = win.SecurityAttributes(c.sizeof(win.SecurityAttributes), descriptor, False)
        if not CreateDirectory(str(path), c.byref(attributes)):
            error = c.get_last_error()
            if error != 183:
                raise c.WinError(error)
    finally:
        win.LocalFree(descriptor)


def runtime_base():
    shell = c.WinDLL("shell32", use_last_error=True)
    ole = c.WinDLL("ole32", use_last_error=True)
    get_folder = win.api(shell, "SHGetKnownFolderPath", w.LONG, PTR, DWORD, HANDLE, PTR)
    release = win.api(ole, "CoTaskMemFree", None, PTR)
    folder = c.create_string_buffer(uuid.UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le)
    value = w.LPWSTR()
    result = get_folder(folder, 0, None, c.byref(value))
    if result < 0:
        raise OSError("无法读取本地应用目录。")
    try:
        return Path(value.value)
    finally:
        release(value)


class File(Resource):
    def __init__(self, handle):
        self.handle, self.fd = handle, None

    def close(self):
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            os.close(descriptor)
        elif self.handle is not None:
            win.checked(win.CloseHandle(self.handle))
            self.handle = None


class Directory(Resource):
    def __init__(self, path, *, principal=None):
        self.handle = win.CreateFile(str(path), 0x001200A1, 7, None, 3, 0x02200000, None)
        if self.handle == win.INVALID:
            raise c.WinError(c.get_last_error())
        try:
            attributes = information(self.handle, 9, FileTag).attributes
            if not attributes & 0x10 or attributes & 0x400:
                raise denied()
            self.identity = identity(self.handle)
            if principal is not None:
                private(self.handle, principal)
        except BaseException as error:
            self._abort_initialization(error)
            raise

    def open(self, name, *, write=False, create=False, exclusive=False, delete=False):
        buffer = c.create_unicode_buffer(name)
        size = len(name.encode("utf-16-le"))
        text = UnicodeString(size, size + 2, c.cast(buffer, w.LPWSTR))
        attributes = ObjectAttributes(
            c.sizeof(ObjectAttributes), self.handle, c.addressof(text), 0x40, None, None
        )
        handle, status = HANDLE(), IoStatus()
        access = (0x40000000 if write else 0) | (0x10000 if delete else 0) | 0x80000000 | 0x100000
        disposition = 2 if exclusive else 3 if create else 1
        result = NtCreateFile(
            c.byref(handle),
            access,
            c.byref(attributes),
            c.byref(status),
            None,
            0x80,
            7,
            disposition,
            0x200060,
            None,
            0,
        )
        if result < 0:
            raise c.WinError(NtStatusError(result))
        owned = File(handle)
        try:
            tags = information(handle, 9, FileTag).attributes
            info = information(handle, 1, FileStandard)
            if tags & 0x400 or info.directory or info.links > 1:
                raise denied()
            fd = msvcrt.open_osfhandle(
                handle.value, os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY)
            )
            owned.handle, owned.fd = None, fd
            return os.fdopen(fd, "r+b" if write else "rb", buffering=0)
        except BaseException as error:
            owned._abort_initialization(error)
            raise

    def replace(self, stream, _source, target):
        encoded = target.encode("utf-16-le")
        data = c.create_string_buffer(max(c.sizeof(Rename), Rename.name.offset + len(encoded)))
        value = Rename.from_buffer(data)
        value.replace, value.root, value.length = 1, self.handle, len(encoded)
        c.memmove(c.addressof(data) + Rename.name.offset, encoded, len(encoded))
        status = IoStatus()
        result = NtSetInformationFile(
            msvcrt.get_osfhandle(stream.fileno()),
            c.byref(status),
            data,
            Rename.name.offset + len(encoded),
            10,
        )
        if result < 0:
            raise c.WinError(NtStatusError(result))

    def unlink(self, stream, _name):
        value = c.c_ubyte(1)
        win.checked(SetInfo(msvcrt.get_osfhandle(stream.fileno()), 4, c.byref(value), 1))

    def close(self):
        if self.handle is not None:
            win.checked(win.CloseHandle(self.handle))
            self.handle = None


def file_identity(stream):
    return identity(msvcrt.get_osfhandle(stream.fileno()))


def acquire(stream):
    overlapped = win.Overlapped()
    if Lock(msvcrt.get_osfhandle(stream.fileno()), 3, 0, 1, 0, c.byref(overlapped)):
        return True
    error = c.get_last_error()
    if error == 33:
        return False
    raise c.WinError(error)


def release(stream):
    overlapped = win.Overlapped()
    win.checked(Unlock(msvcrt.get_osfhandle(stream.fileno()), 0, 1, 0, c.byref(overlapped)))
