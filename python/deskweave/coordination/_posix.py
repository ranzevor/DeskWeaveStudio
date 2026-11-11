from __future__ import annotations

import errno
import fcntl
import os
import stat
import sys
import tempfile
from pathlib import Path

from deskweave.errors import DeskWeaveError
from deskweave.transport.native._common import Resource


def denied():
    return DeskWeaveError("RESOURCE_DENIED", "目录或协调文件的身份与权限无效。")


def identity(info):
    return str(info.st_dev), str(info.st_ino)


def make_private(path, _principal):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass


def runtime_base():
    if sys.platform == "darwin":
        value = os.confstr(os.confstr_names.get("CS_DARWIN_USER_TEMP_DIR", 65537))
        if not value:
            raise OSError("无法读取用户运行目录。")
        return Path(value)
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value:
        return Path(value)
    selected = Path("/run/user") / str(os.geteuid())
    if selected.is_dir():
        return selected
    selected = Path(tempfile.gettempdir()) / ("deskweave-" + str(os.geteuid()))
    make_private(selected, None)
    return selected


class File(Resource):
    def __init__(self, fd):
        self.fd = fd

    def close(self):
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            os.close(descriptor)


class Directory(Resource):
    def __init__(self, path, *, principal=None):
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISDIR(info.st_mode):
                raise denied()
            self.identity = identity(info)
            if principal is not None and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise denied()
        except BaseException as error:
            self._abort_initialization(error)
            raise

    def open(self, name, *, write=False, create=False, exclusive=False, delete=False):
        flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        if create or exclusive:
            try:
                fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.fd)
            except FileExistsError:
                if exclusive:
                    raise
                fd = os.open(name, flags, dir_fd=self.fd)
        else:
            fd = os.open(name, flags, dir_fd=self.fd)
        owned = File(fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise denied()
            stream = os.fdopen(fd, "r+b" if write else "rb", buffering=0)
        except BaseException as error:
            owned._abort_initialization(error)
            raise
        return stream

    def replace(self, stream, source, target):
        if identity(os.stat(source, dir_fd=self.fd, follow_symlinks=False)) != file_identity(
            stream
        ):
            raise denied()
        os.replace(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)

    def unlink(self, stream, name):
        if identity(os.stat(name, dir_fd=self.fd, follow_symlinks=False)) != file_identity(stream):
            raise denied()
        os.unlink(name, dir_fd=self.fd)

    def close(self):
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            os.close(descriptor)


def file_identity(stream):
    return identity(os.fstat(stream.fileno()))


def acquire(stream):
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


def release(stream):
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
