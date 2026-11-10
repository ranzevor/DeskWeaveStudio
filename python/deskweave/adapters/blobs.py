from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from deskweave.errors import DeskWeaveError

MAX_BLOB = 1_073_741_824
BLOCK = 196_608


class Blobs:
    def __init__(self, workspace):
        self.workspace = workspace
        self.directory = workspace.path / "objects/sha256"

    def _path(self, identity: str) -> Path:
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise DeskWeaveError("STORAGE_CORRUPT", "资产内容引用无效。")
        path = self.directory / identity
        if path.is_symlink() or not path.resolve().is_relative_to(self.workspace.path):
            raise DeskWeaveError("RESOURCE_DENIED", "资产内容路径越界。")
        return path

    def put(self, source, *, maximum=MAX_BLOB) -> tuple[str, int]:
        self.workspace.check_deadline()
        if not self.directory.resolve().is_relative_to(self.workspace.path):
            raise DeskWeaveError("RESOURCE_DENIED", "资产内容路径越界。")
        temporary = None
        try:
            with NamedTemporaryFile(dir=self.directory, prefix=".writing-", delete=False) as out:
                temporary = Path(out.name)
                identity, size = self.copy(source, out, maximum=maximum)
                os.fsync(out.fileno())
            path = self._path(identity)
            self.workspace.check_deadline()
            if path.exists():
                with self.open(identity, size):
                    pass
            else:
                os.replace(temporary, path)
                if os.name != "nt":
                    descriptor = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            return identity, size
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def copy(self, source, destination, *, maximum=MAX_BLOB):
        size, digest = 0, hashlib.sha256()
        while True:
            self.workspace.check_deadline()
            block = source.read(min(BLOCK, maximum - size + 1))
            if not block:
                break
            size += len(block)
            if size > maximum:
                raise DeskWeaveError("LIMIT_EXCEEDED", "资产内容超过大小限制。")
            digest.update(block)
            view = memoryview(block)
            while view:
                self.workspace.check_deadline()
                written = destination.write(view)
                if not written:
                    raise OSError("asset write made no progress")
                view = view[written:]
        destination.flush()
        return digest.hexdigest(), size

    @contextmanager
    def open(self, identity: str, length: int):
        path = self._path(identity)
        try:
            stream = path.open("rb")
        except FileNotFoundError as error:
            raise DeskWeaveError("STORAGE_CORRUPT", "资产引用的内容缺失。") from error
        with stream:
            if os.fstat(stream.fileno()).st_size != length or not 0 <= length <= MAX_BLOB:
                raise DeskWeaveError("STORAGE_CORRUPT", "资产内容长度不匹配。")
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                self.workspace.check_deadline()
                block = stream.read(min(BLOCK, remaining))
                if not block:
                    raise DeskWeaveError("STORAGE_CORRUPT", "资产内容不完整。")
                digest.update(block)
                remaining -= len(block)
            if stream.read(1) or digest.hexdigest() != identity:
                raise DeskWeaveError("STORAGE_CORRUPT", "资产内容摘要不匹配。")
            self.workspace.check_deadline()
            stream.seek(0)
            yield stream

    def checksums(self, stream):
        stream.seek(0)
        result = []
        while True:
            self.workspace.check_deadline()
            block = stream.read(BLOCK)
            if not block:
                return result
            result.append(hashlib.sha256(block).hexdigest())

    def read_range(self, identity, total, offset, length):
        if not 0 <= offset <= total or not 0 <= length <= 4_194_304:
            raise DeskWeaveError("INVALID_ARGUMENT", "资产读取范围无效。")
        length = min(length, total - offset)
        first, last = offset // BLOCK, (offset + length - 1) // BLOCK
        with self.workspace.transaction() as connection:
            rows = (
                connection.execute(
                    "SELECT position,sha256 FROM blob_chunk WHERE content_hash=? "
                    "AND position>=? AND position<=? ORDER BY position",
                    (identity, first, last),
                ).fetchall()
                if length
                else []
            )
        if len(rows) != (last - first + 1 if length else 0):
            raise DeskWeaveError("STORAGE_CORRUPT", "资产内容分块索引缺失。")
        try:
            with self._path(identity).open("rb") as stream:
                if os.fstat(stream.fileno()).st_size != total:
                    raise DeskWeaveError("STORAGE_CORRUPT", "资产内容长度不匹配。")
                result = bytearray()
                stream.seek(first * BLOCK)
                for row in rows:
                    self.workspace.check_deadline()
                    start = row["position"] * BLOCK
                    block = stream.read(min(BLOCK, total - start))
                    if hashlib.sha256(block).hexdigest() != row["sha256"]:
                        raise DeskWeaveError("STORAGE_CORRUPT", "资产内容分块损坏。")
                    result.extend(
                        block[max(0, offset - start) : min(len(block), offset + length - start)]
                    )
                return bytes(result)
        except FileNotFoundError as error:
            raise DeskWeaveError("STORAGE_CORRUPT", "资产引用的内容缺失。") from error
