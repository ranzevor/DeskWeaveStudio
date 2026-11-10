from __future__ import annotations

import hashlib
import os
import re
import secrets
import sys
import threading
from pathlib import Path

from deskweave.errors import DeskWeaveError
from deskweave.json import dumps, loads
from deskweave.transport.native._common import InitializationError, Resource, closed

if sys.platform == "win32":
    from . import _windows as backend
else:
    from . import _posix as backend


def read_bounded(stream, limit):
    data = bytearray()
    while len(data) <= limit:
        block = stream.read(min(8192, limit + 1 - len(data)))
        if not block:
            return bytes(data)
        data.extend(block)
    raise DeskWeaveError("LIMIT_EXCEEDED", "协调记录超过大小限制。")


class OwnedFile(Resource):
    def __init__(self, owner, stream):
        self.owner, self.stream = owner, stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def close(self):
        with self.owner._lock:
            try:
                self.stream.close()
            except BaseException:
                self.owner._failed.add(self)
                raise
            self.owner._failed.discard(self)
            self.owner._streams.discard(self)


class Directory(Resource):
    def __init__(self, path, *, principal=None):
        self.path = Path(path).resolve(strict=True)
        self._lock = threading.RLock()
        self._closed = False
        self.principal = principal
        self._raw = None
        self._failed, self._streams = set(), set()
        self._pending_removals = {}
        try:
            self._raw = backend.Directory(self.path, principal=principal)
            self.identity = self._raw.identity
        except InitializationError as error:
            self._raw = error.resource
            self._abort_initialization(error)
            raise

    @staticmethod
    def _name(value):
        if (
            type(value) is not str
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value)
            or value in {".", ".."}
        ):
            raise ValueError("协调文件名无效。")
        return value

    def remove_pending(self, stream):
        name = self._pending_removals[stream]
        try:
            self._raw.unlink(stream, name)
        except FileNotFoundError:
            pass
        del self._pending_removals[stream]
        stream.close()

    def retry(self):
        for stream in tuple(self._pending_removals):
            self.remove_pending(stream)
        for resource in tuple(self._failed):
            resource.close()
            self._failed.discard(resource)

    def check(self):
        with self._lock:
            if self._closed:
                raise closed()
            self.retry()
            try:
                current = backend.Directory(self.path, principal=self.principal)
            except InitializationError as error:
                self._failed.add(error.resource)
                raise
            except (OSError, RuntimeError) as error:
                raise DeskWeaveError("RESOURCE_DENIED", "目录对象已改变。") from error
            self._failed.add(current)
            try:
                if current.identity != self.identity:
                    raise DeskWeaveError("RESOURCE_DENIED", "目录对象已改变。")
            except BaseException as error:
                current._abort(error)
                self._failed.discard(current)
                raise
            else:
                current.close()
                self._failed.discard(current)

    def open(self, name, *, write=False, create=False, exclusive=False, delete=False):
        with self._lock:
            self.check()
            try:
                stream = self._raw.open(
                    self._name(name), write=write, create=create, exclusive=exclusive, delete=delete
                )
            except InitializationError as error:
                self._failed.add(error.resource)
                raise
            result = OwnedFile(self, stream)
            self._streams.add(result)
            return result

    def read(self, name, *, limit=16384):
        try:
            stream = self.open(name)
        except FileNotFoundError:
            return None
        with stream:
            data = read_bounded(stream, limit)
        self.check()
        return data

    def write(self, name, data):
        if type(data) is not bytes or len(data) > 16384:
            raise ValueError("协调记录应为不超过 16KiB 的字节。")
        name = self._name(name)
        temporary = ".tmp-" + secrets.token_hex(16)
        stream = self.open(temporary, write=True, exclusive=True, delete=True)
        published = False
        try:
            offset = 0
            while offset < len(data):
                count = stream.write(data[offset:])
                if not count:
                    raise OSError("协调记录写入中断。")
                offset += count
            stream.flush()
            os.fsync(stream.fileno())
            with self._lock:
                self.check()
                self._raw.replace(stream, temporary, name)
                published = True
                self.check()
        except BaseException as error:
            try:
                with self._lock:
                    if not published:
                        self._pending_removals[stream] = temporary
                        self.remove_pending(stream)
                    else:
                        stream.close()
            except BaseException as closing:
                raise BaseExceptionGroup("协调记录写入与清理失败。", [error, closing]) from None
            raise
        else:
            stream.close()

    def remove(self, name, *, matches):
        try:
            stream = self.open(name, write=True, delete=True)
        except FileNotFoundError:
            return False
        with stream:
            try:
                data = read_bounded(stream, 16384)
            except DeskWeaveError:
                return False
            if not matches(data):
                return False
            with self._lock:
                self.check()
                self._raw.unlink(stream, self._name(name))
            return True

    def close(self):
        with self._lock:
            self._closed = True
            errors = []
            for stream in tuple(self._pending_removals):
                try:
                    self.remove_pending(stream)
                except BaseException as error:
                    errors.append(error)
            for resource in self._streams | self._failed:
                if resource in self._pending_removals:
                    continue
                try:
                    resource.close()
                    self._failed.discard(resource)
                except BaseException as error:
                    errors.append(error)
            if self._raw is not None and not self._pending_removals:
                try:
                    self._raw.close()
                except BaseException as error:
                    errors.append(error)
            if errors:
                raise BaseExceptionGroup("目录资源关闭失败。", errors)


class StartupLock(Resource):
    def __init__(self, directory, budget):
        self.directory = directory
        self.stream = directory.open(".engine-start.lock", write=True, create=True)
        self.acquired = False
        wake = budget.cancel or threading.Event()
        try:
            while True:
                budget.check_time()
                if backend.acquire(self.stream):
                    self.acquired = True
                    break
                wake.wait(budget.check_time())
            directory.check()
            with directory.open(".engine-start.lock") as current:
                if backend.file_identity(current) != backend.file_identity(self.stream):
                    raise DeskWeaveError("RESOURCE_DENIED", "启动锁对象已改变。")
            budget.check_time()
        except BaseException as error:
            self._abort_initialization(error)
            raise

    def close(self):
        if self.stream is not None:
            if self.acquired:
                backend.release(self.stream)
                self.acquired = False
            self.stream.close()
            self.stream = None


class WorkspaceIdentity(Resource):
    def __init__(self, path, *, create=False):
        if type(create) is not bool:
            raise ValueError("工作空间创建选项无效。")
        path = Path(path)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        self.directory = Directory(path)
        self.path = self.directory.path
        self.object = self.directory.identity
        self.workspace = None
        try:
            self.workspace = self.read_workspace()
            if self.workspace is None and not create:
                raise DeskWeaveError("WORKSPACE_NOT_FOUND", "目录中没有工作空间。")
            self.key = hashlib.sha256(dumps(list(self.object))).hexdigest()
        except BaseException as error:
            self._abort_initialization(error)
            raise

    def read_workspace(self):
        data = self.directory.read("workspace.json", limit=1048576)
        if data is None:
            return None
        value = loads(data)
        if type(value) is not dict or value.get("formatVersion") != "1.0":
            raise DeskWeaveError("SCHEMA_UNSUPPORTED", "工作空间格式不受支持。")
        identity = value.get("id")
        if type(identity) is not str or not 1 <= len(identity) <= 128:
            raise DeskWeaveError("INVALID_ARGUMENT", "工作空间身份无效。")
        return identity

    def check(self, expected=None, *, allow_missing=False):
        self.directory.check()
        identity = self.read_workspace()
        selected = self.workspace if expected is None else expected
        if identity is None and self.workspace is None and allow_missing:
            return None
        if selected is not None and identity != selected:
            raise DeskWeaveError("RESOURCE_DENIED", "工作空间身份已改变。")
        return identity

    def lock(self, budget):
        self.check()
        return StartupLock(self.directory, budget)

    def close(self):
        self.directory.close()


def runtime_directory(local, path=None):
    if path is None:
        key = hashlib.sha256(
            dumps([local.identity.user_id, local.identity.os_session_id])
        ).hexdigest()[:12]
        base = backend.runtime_base()
        if sys.platform != "win32":
            selected = Directory(base, principal=local)
            try:
                selected.close()
            except BaseException as error:
                raise InitializationError(selected, [error]) from None
        path = base / ("dw-" + key)
    path = Path(path).absolute()
    backend.make_private(path, local)
    return Directory(path, principal=local)
