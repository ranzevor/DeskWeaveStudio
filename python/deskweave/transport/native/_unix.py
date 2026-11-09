from __future__ import annotations

import errno
import os
import select
import socket
import stat
import sys
import tempfile
import threading
from pathlib import Path

from ._common import Resource, closed, rejected

if sys.platform == "darwin":
    from . import _macos as platform
else:
    from . import _linux as platform


def identity(path):
    info = path.lstat()
    return info.st_dev, info.st_ino


def private_directory(path):
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise rejected()


class Stream(Resource):
    def __init__(self, sock):
        self.sock = sock
        self.sock.setblocking(False)
        self.sock.set_inheritable(False)
        self._lock = threading.RLock()
        self._closed = False

    def read(self, size, budget):
        while True:
            delay = budget.check()
            with self._lock:
                if self._closed:
                    raise closed()
                try:
                    return self.sock.recv(size)
                except BlockingIOError:
                    pass
            select.select([self.sock], [], [], delay)

    def write(self, data, budget):
        while True:
            delay = budget.check()
            with self._lock:
                if self._closed:
                    raise closed()
                try:
                    return self.sock.send(data)
                except BlockingIOError:
                    pass
            select.select([], [self.sock], [], delay)

    def peer(self):
        with self._lock:
            if self._closed:
                raise closed()
            return platform.peer(self.sock)

    def close(self):
        with self._lock:
            if not self._closed:
                self.sock.close()
                self._closed = True


class Listener(Resource):
    def __init__(self, _address, _process, directory, backlog):
        base = Path(directory or tempfile.gettempdir()).resolve(strict=True)
        self.directory = Path(tempfile.mkdtemp(prefix="dw-", dir=base))
        self.address = str(self.directory / "control.sock")
        self._directory_id = identity(self.directory)
        self._socket_id = None
        self._lock = threading.RLock()
        self._accept_lock = threading.Lock()
        self._closed = False
        self._directory_fd = None
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            private_directory(self.directory)
            self._directory_fd = os.open(
                self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            info = os.fstat(self._directory_fd)
            if (info.st_dev, info.st_ino) != self._directory_id:
                raise rejected()
            if len(os.fsencode(self.address)) >= (104 if sys.platform == "darwin" else 108):
                raise ValueError("Unix socket 路径过长。")
            self.sock.setblocking(False)
            self.sock.bind(self.address)
            self._socket_id = identity(Path(self.address))
            os.chmod(self.address, 0o600)
            self.sock.listen(backlog)
        except BaseException:
            self.close()
            raise

    def _check(self):
        private_directory(self.directory)
        if (
            identity(self.directory) != self._directory_id
            or identity(Path(self.address)) != self._socket_id
        ):
            raise rejected()

    def accept(self, budget):
        budget.acquire(self._accept_lock)
        try:
            while True:
                delay = budget.check()
                with self._lock:
                    if self._closed:
                        raise closed()
                    self._check()
                    try:
                        sock, _ = self.sock.accept()
                        return Stream(sock)
                    except BlockingIOError:
                        pass
                select.select([self.sock], [], [], delay)
        finally:
            self._accept_lock.release()

    def close(self):
        with self._lock:
            self._closed = True
            self.sock.close()
            if self._directory_fd is not None:
                try:
                    info = os.stat("control.sock", dir_fd=self._directory_fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == self._socket_id:
                        os.unlink("control.sock", dir_fd=self._directory_fd)
                except FileNotFoundError:
                    pass
                os.close(self._directory_fd)
                self._directory_fd = None
            try:
                if identity(self.directory) == self._directory_id:
                    self.directory.rmdir()
            except FileNotFoundError:
                pass


def connect(address, budget):
    path = Path(address)
    if not path.is_absolute():
        raise ValueError("Unix socket 地址必须为绝对路径。")
    parent = path.parent.resolve(strict=True)
    private_directory(parent)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise rejected()
    stream = Stream(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
    try:
        while True:
            delay = budget.check()
            result = stream.sock.connect_ex(str(path))
            if result != errno.EAGAIN:
                break
            threading.Event().wait(delay)
        if result not in (0, errno.EINPROGRESS):
            raise OSError(result, "无法连接本机端点。")
        if result:
            while not select.select([], [stream.sock], [], budget.check())[1]:
                pass
            error = stream.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                raise OSError(error, "无法连接本机端点。")
        if identity(path) != (info.st_dev, info.st_ino):
            raise rejected()
        budget.check()
        return stream
    except BaseException:
        stream.close()
        raise


def address_name(_token):
    return None
