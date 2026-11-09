from __future__ import annotations

import ctypes as c
import threading
from ctypes import wintypes as w
from pathlib import Path

from ._common import Resource, closed, rejected

KERNEL = c.WinDLL("kernel32", use_last_error=True)
ADVAPI = c.WinDLL("advapi32", use_last_error=True)
PTR = c.c_void_p
DWORD = w.DWORD
HANDLE = w.HANDLE
INVALID = c.c_void_p(-1).value


def api(library, name, result, *args):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = args
    return function


class Overlapped(c.Structure):
    _fields_ = [
        ("internal", c.c_size_t),
        ("high", c.c_size_t),
        ("offset", DWORD),
        ("offset_high", DWORD),
        ("event", HANDLE),
    ]


class SecurityAttributes(c.Structure):
    _fields_ = [("length", DWORD), ("descriptor", PTR), ("inherit", w.BOOL)]


class Luid(c.Structure):
    _fields_ = [("low", DWORD), ("high", w.LONG)]


class SidAndAttributes(c.Structure):
    _fields_ = [("sid", PTR), ("attributes", DWORD)]


class TokenGroups(c.Structure):
    _fields_ = [("count", DWORD), ("groups", SidAndAttributes * 1)]


class TokenStatistics(c.Structure):
    _fields_ = [
        ("token", Luid),
        ("authentication", Luid),
        ("expiration", c.c_int64),
        ("type", DWORD),
        ("level", DWORD),
        ("charged", DWORD),
        ("available", DWORD),
        ("groups", DWORD),
        ("privileges", DWORD),
        ("modified", Luid),
    ]


GetCurrentProcess = api(KERNEL, "GetCurrentProcess", HANDLE)
CloseHandle = api(KERNEL, "CloseHandle", w.BOOL, HANDLE)
OpenProcess = api(KERNEL, "OpenProcess", HANDLE, DWORD, w.BOOL, DWORD)
GetProcessTimes = api(KERNEL, "GetProcessTimes", w.BOOL, HANDLE, PTR, PTR, PTR, PTR)
QueryFullProcessImageName = api(
    KERNEL, "QueryFullProcessImageNameW", w.BOOL, HANDLE, DWORD, w.LPWSTR, PTR
)
WaitForSingleObject = api(KERNEL, "WaitForSingleObject", DWORD, HANDLE, DWORD)
OpenProcessToken = api(ADVAPI, "OpenProcessToken", w.BOOL, HANDLE, DWORD, PTR)
GetTokenInformation = api(ADVAPI, "GetTokenInformation", w.BOOL, HANDLE, DWORD, PTR, DWORD, PTR)
ConvertSidToStringSid = api(ADVAPI, "ConvertSidToStringSidW", w.BOOL, PTR, PTR)
LocalFree = api(KERNEL, "LocalFree", PTR, PTR)
CreateNamedPipe = api(
    KERNEL, "CreateNamedPipeW", HANDLE, w.LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, PTR
)
CreateFile = api(KERNEL, "CreateFileW", HANDLE, w.LPCWSTR, DWORD, DWORD, PTR, DWORD, DWORD, HANDLE)
ConnectNamedPipe = api(KERNEL, "ConnectNamedPipe", w.BOOL, HANDLE, PTR)
ReadFile = api(KERNEL, "ReadFile", w.BOOL, HANDLE, PTR, DWORD, PTR, PTR)
WriteFile = api(KERNEL, "WriteFile", w.BOOL, HANDLE, PTR, DWORD, PTR, PTR)
CreateEvent = api(KERNEL, "CreateEventW", HANDLE, PTR, w.BOOL, w.BOOL, w.LPCWSTR)
CancelIoEx = api(KERNEL, "CancelIoEx", w.BOOL, HANDLE, PTR)
GetOverlappedResult = api(KERNEL, "GetOverlappedResult", w.BOOL, HANDLE, PTR, PTR, w.BOOL)
GetClientPID = api(KERNEL, "GetNamedPipeClientProcessId", w.BOOL, HANDLE, PTR)
GetServerPID = api(KERNEL, "GetNamedPipeServerProcessId", w.BOOL, HANDLE, PTR)
ConvertSDDL = api(
    ADVAPI,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    w.BOOL,
    w.LPCWSTR,
    DWORD,
    PTR,
    PTR,
)


def checked(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


def token_info(token, kind):
    size = DWORD()
    GetTokenInformation(token, kind, None, 0, c.byref(size))
    if c.get_last_error() != 122:
        raise c.WinError(c.get_last_error())
    data = c.create_string_buffer(size.value)
    checked(GetTokenInformation(token, kind, data, size, c.byref(size)))
    return data


def token_identity(handle):
    token = HANDLE()
    checked(OpenProcessToken(handle, 8, c.byref(token)))
    try:
        user = token_info(token, 1)
        sid = w.LPWSTR()
        checked(ConvertSidToStringSid(c.cast(user, c.POINTER(PTR))[0], c.byref(sid)))
        try:
            user_id = sid.value
        finally:
            LocalFree(sid)
        session = c.cast(token_info(token, 12), c.POINTER(DWORD)).contents.value
        stats = c.cast(token_info(token, 10), c.POINTER(TokenStatistics)).contents
        high, low = stats.authentication.high & 0xFFFFFFFF, stats.authentication.low
        groups = token_info(token, 2)
        count = c.cast(groups, c.POINTER(DWORD)).contents.value
        entries = c.cast(
            c.addressof(groups) + TokenGroups.groups.offset, c.POINTER(SidAndAttributes)
        )
        for index in range(count):
            if entries[index].attributes & 0xC0000000 == 0xC0000000:
                logon = w.LPWSTR()
                checked(ConvertSidToStringSid(entries[index].sid, c.byref(logon)))
                try:
                    return user_id, f"{session}:{high}:{low}", logon.value
                finally:
                    LocalFree(logon)
        raise rejected()
    finally:
        checked(CloseHandle(token))


def process_executable(handle):
    path = c.create_unicode_buffer(32768)
    size = DWORD(len(path))
    checked(QueryFullProcessImageName(handle, 0, path, c.byref(size)))
    return Path(path.value).resolve(strict=True)


def current_executable():
    return process_executable(GetCurrentProcess())


class Process(Resource):
    platform = "windows"

    def __init__(self, pid):
        self.pid = pid
        self._handle = checked(OpenProcess(0x100000 | 0x1000, False, pid))
        self._lock = threading.RLock()
        try:
            self.initial = self.snapshot()
        except BaseException:
            self.close()
            raise

    def snapshot(self):
        with self._lock:
            if self._handle is None or WaitForSingleObject(self._handle, 0) != 258:
                raise rejected()
            times = [c.c_uint64() for _ in range(4)]
            checked(GetProcessTimes(self._handle, *(c.byref(item) for item in times)))
            executable = process_executable(self._handle)
            user, session, self.logon_sid = token_identity(self._handle)
            return user, session, str(times[0].value), executable

    def close(self):
        with self._lock:
            if self._handle is not None:
                checked(CloseHandle(self._handle))
                self._handle = None


class Stream(Resource):
    def __init__(self, handle, *, server):
        self.handle = handle
        self.server = server
        self._condition = threading.Condition()
        self._active = 0
        self._closed = False

    def _io(self, kind, data, budget):
        event = checked(CreateEvent(None, True, False, None))
        overlapped = Overlapped(event=event)
        count = DWORD()
        buffer = c.create_string_buffer(data if isinstance(data, bytes) else data or 1)
        registered = False
        pending = False
        try:
            budget.check()
            with self._condition:
                if self._closed:
                    raise closed()
                self._active += 1
                registered = True
                if kind == "connect":
                    result = ConnectNamedPipe(self.handle, c.byref(overlapped))
                else:
                    function = ReadFile if kind == "read" else WriteFile
                    result = function(
                        self.handle,
                        buffer,
                        len(data) if isinstance(data, bytes) else data,
                        c.byref(count),
                        c.byref(overlapped),
                    )
                error = c.get_last_error() if not result else 0
                if kind == "connect" and error == 535:
                    return b""
                if error == 109 and kind == "read":
                    return b""
                if not result and error != 997:
                    if error in (109, 232, 995):
                        raise closed()
                    raise c.WinError(error)
                pending = True
            while WaitForSingleObject(event, 0) == 258:
                delay = budget.check()
                with self._condition:
                    if self._closed:
                        raise closed()
                wait = WaitForSingleObject(event, max(1, int(delay * 1000)))
                if wait not in (0, 258):
                    raise c.WinError(c.get_last_error())
            result = GetOverlappedResult(self.handle, c.byref(overlapped), c.byref(count), False)
            pending = False
            if not result:
                error = c.get_last_error()
                if kind == "read" and error in (109, 232):
                    return b""
                if error in (109, 232, 995):
                    raise closed()
                raise c.WinError(error)
            budget.check()
            return buffer.raw[: count.value] if kind == "read" else count.value
        finally:
            try:
                if pending:
                    CancelIoEx(self.handle, c.byref(overlapped))
                    # OVERLAPPED 与缓冲区必须存活到内核完成取消或原操作。
                    GetOverlappedResult(self.handle, c.byref(overlapped), c.byref(count), True)
            finally:
                CloseHandle(event)
                if registered:
                    with self._condition:
                        self._active -= 1
                        self._condition.notify_all()

    def read(self, size, budget):
        return self._io("read", size, budget)

    def write(self, data, budget):
        return self._io("write", data, budget)

    def peer(self):
        with self._condition:
            if self._closed:
                raise closed()
            pid = DWORD()
            checked((GetClientPID if self.server else GetServerPID)(self.handle, c.byref(pid)))
            return pid.value, None

    def close(self):
        with self._condition:
            self._closed = True
            if self.handle is None:
                return
            CancelIoEx(self.handle, None)
            while self._active:
                self._condition.wait()
            if self.handle is not None:
                checked(CloseHandle(self.handle))
                self.handle = None


class Listener(Resource):
    def __init__(self, address, process, _directory, _backlog):
        self.address = address
        self._lock = threading.Lock()
        self._accept_lock = threading.Lock()
        self._closed = False
        self._sid = process.logon_sid
        self._pending = self._create(first=True)

    def _create(self, *, first=False):
        descriptor = PTR()
        checked(ConvertSDDL(f"D:P(A;;GA;;;{self._sid})", 1, c.byref(descriptor), None))
        try:
            attributes = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
            handle = CreateNamedPipe(
                self.address,
                3 | 0x40000000 | (0x80000 if first else 0),
                8,
                255,
                65536,
                65536,
                0,
                c.byref(attributes),
            )
            if handle == INVALID:
                raise c.WinError(c.get_last_error())
            return Stream(handle, server=True)
        finally:
            LocalFree(descriptor)

    def accept(self, budget):
        budget.acquire(self._accept_lock)
        current = None
        try:
            with self._lock:
                if self._closed:
                    raise closed()
                current = self._pending
            current._io("connect", None, budget)
            with self._lock:
                if self._closed:
                    raise closed()
                self._pending = self._create()
            return current
        except BaseException:
            if current is not None:
                with self._lock:
                    if not self._closed and self._pending is current:
                        self._pending = self._create()
                current.close()
            raise
        finally:
            self._accept_lock.release()

    def close(self):
        with self._lock:
            self._closed = True
            pending = self._pending
        pending.close()


def connect(address, budget):
    if not address.startswith("\\\\.\\pipe\\DeskWeaveStudio-") or "\\" in address[9:]:
        raise ValueError("命名管道地址无效。")
    while True:
        delay = budget.check()
        handle = CreateFile(
            address, 0x80000000 | 0x40000000, 0, None, 3, 0x40000000 | 0x100000 | 0x10000, None
        )
        if handle != INVALID:
            return Stream(handle, server=False)
        error = c.get_last_error()
        if error != 231:
            raise c.WinError(error)
        threading.Event().wait(delay)


def address_name(token):
    return "\\\\.\\pipe\\DeskWeaveStudio-" + token
