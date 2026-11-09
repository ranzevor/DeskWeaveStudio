from __future__ import annotations

import ctypes as c
import os
import select
import socket
import struct
import threading
from pathlib import Path

from ._common import Resource, rejected

LIBC = c.CDLL(None)
LIBC.free.argtypes = [c.c_void_p]
LOGIN_LIBRARIES = []
for name in ("libsystemd.so.0", "libelogind.so.0"):
    try:
        library = c.CDLL(name)
    except OSError:
        continue
    library.sd_pid_get_session.argtypes = [c.c_int, c.POINTER(c.c_void_p)]
    library.sd_pid_get_session.restype = c.c_int
    LOGIN_LIBRARIES.append(library)


def current_executable():
    return Path("/proc/self/exe").resolve(strict=True)


class Process(Resource):
    platform = "linux"

    def __init__(self, pid):
        self.pid = pid
        self._lock = threading.RLock()
        self._fd = os.pidfd_open(pid)
        try:
            self.initial = self.snapshot()
        except BaseException:
            self.close()
            raise

    def snapshot(self):
        with self._lock:
            if self._fd is None or select.select([self._fd], [], [], 0)[0]:
                raise rejected()
            try:
                root = Path(f"/proc/{self.pid}")
                fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] in {"Z", "X"}:
                    raise rejected()
                uid = (root / "status").read_text().split("Uid:", 1)[1].splitlines()[0].split()
                if len(set(uid)) != 1:
                    raise rejected()
                session = self._session(root)
                start = (
                    Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
                )
                executable = (root / "exe").resolve(strict=True)
                image = tuple(fields[index] for index in (23, 24, 25, 45, 46))
                if select.select([self._fd], [], [], 0)[0]:
                    raise rejected()
                return uid[0], session, start, executable, image
            except (FileNotFoundError, ProcessLookupError) as error:
                raise rejected() from error

    def _session(self, root):
        audit = root / "sessionid"
        login = root / "loginuid"
        if audit.exists() and login.exists():
            session, user = audit.read_text().strip(), login.read_text().strip()
            if session.isdecimal() and user.isdecimal() and "4294967295" not in (session, user):
                return f"audit:{user}:{session}"
        for module in LOGIN_LIBRARIES:
            function = module.sd_pid_get_session
            value = c.c_void_p()
            result = function(self.pid, c.byref(value))
            if result >= 0:
                try:
                    return "login:" + c.string_at(value).decode("utf-8", errors="strict")
                finally:
                    LIBC.free(value)
            if result not in (-61, -3):
                raise OSError(-result, "无法读取进程登录会话。")
        raise rejected()

    def image_path(self):
        return Path(f"/proc/{self.pid}/exe")

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None


def peer(sock):
    pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return pid, str(uid)


def verify_peer(process, credential, _requirement):
    if process.snapshot()[0] != credential:
        raise rejected()
