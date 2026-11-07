from __future__ import annotations

import base64
import binascii
import hashlib
import math
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

from deskweave.adapters.workspace import utc_time
from deskweave.errors import DeskWeaveError
from deskweave.json import loads

MAX_CONTENT = 33_554_432
MAX_CHUNK = 196_608
MAX_ENTRIES = 16
MAX_RESERVED = 67_108_864
LIFETIME = 60


@dataclass(eq=False)
class Content:
    owner: str
    purpose: str
    permission: str
    length: int
    sha256: str
    stream: BinaryIO
    expires: float
    deadline: float
    resource_id: str
    upload_id: str | None = None
    workflow_id: str | None = None
    received: int = 0
    boundaries: bytearray | None = None
    sealed: bool = False

    @property
    def expires_at(self) -> str:
        return utc_time(self.expires)


class ContentStore:
    def __init__(self, *, clock: Callable, monotonic: Callable, checkpoint: Callable):
        self.clock = clock
        self.monotonic = monotonic
        self.checkpoint = checkpoint
        self.uploads: dict[str, Content] = {}
        self.resources: dict[str, Content] = {}

    def _drop(self, item: Content) -> None:
        if item.upload_id is not None:
            self.uploads.pop(item.upload_id, None)
        self.resources.pop(item.resource_id, None)
        item.stream.close()

    def _expired(self, item: Content) -> bool:
        return self.clock() >= item.expires or self.monotonic() >= item.deadline

    def expire(self) -> None:
        for item in set(self.uploads.values()) | set(self.resources.values()):
            self.checkpoint()
            if self._expired(item):
                self._drop(item)

    @staticmethod
    def _capacity(items, length: int) -> None:
        if len(items) >= MAX_ENTRIES or sum(item.length for item in items) + length > MAX_RESERVED:
            raise DeskWeaveError("LIMIT_EXCEEDED", "临时内容容量已满，请释放已有内容。")

    def _expiry(self) -> tuple[float, float]:
        now = self.clock()
        expires = math.floor(now) + LIFETIME
        return expires, self.monotonic() + (expires - now)

    def _new(self, owner, purpose, permission, length, sha256, **kwargs) -> Content:
        self.checkpoint()
        stream = tempfile.TemporaryFile(mode="w+b")
        try:
            expires, deadline = self._expiry()
            return Content(
                owner,
                purpose,
                permission,
                length,
                sha256,
                stream,
                expires,
                deadline,
                secrets.token_urlsafe(24),
                **kwargs,
            )
        except BaseException:
            stream.close()
            raise

    def begin(self, owner: str, params: dict, permission: str) -> dict:
        self.expire()
        length = int(params["length"])
        self._capacity(list(self.uploads.values()), length)
        item = self._new(
            owner,
            params["purpose"],
            permission,
            length,
            params["sha256"],
            upload_id=secrets.token_urlsafe(24),
            boundaries=bytearray((length + 8) // 8),
        )
        item.boundaries[0] = 1
        self.uploads[item.upload_id] = item
        return {
            "uploadId": item.upload_id,
            "nextOffset": 0,
            "maxChunkBytes": MAX_CHUNK,
            "expiresAt": item.expires_at,
        }

    def _get(self, records: dict[str, Content], owner: str, identity: str, code: str) -> Content:
        item = records.get(identity)
        if item is None or item.owner != owner:
            raise DeskWeaveError(code, "临时内容不存在或已过期，请重新取得内容。")
        if self._expired(item):
            self._drop(item)
            raise DeskWeaveError(code, "临时内容不存在或已过期，请重新取得内容。")
        return item

    def upload(self, owner: str, identity: str) -> Content:
        return self._get(self.uploads, owner, identity, "CONTENT_NOT_FOUND")

    def resource(self, owner: str, identity: str) -> Content:
        return self._get(self.resources, owner, identity, "RESOURCE_NOT_FOUND")

    @staticmethod
    def _block_boundary(bits: bytearray, start: int, end: int) -> bool:
        if not bits[start // 8] & (1 << (start % 8)) or not bits[end // 8] & (1 << (end % 8)):
            return False
        first, last = start + 1, end
        if first == last:
            return True
        left, right = first // 8, last // 8
        if left == right:
            mask = ((1 << (last % 8)) - 1) & (255 << (first % 8))
            return not bits[left] & mask
        return not (
            bits[left] & (255 << (first % 8))
            or any(memoryview(bits)[left + 1 : right])
            or bits[right] & ((1 << (last % 8)) - 1)
        )

    def write(self, item: Content, params: dict) -> dict:
        try:
            block = base64.b64decode(params["chunk"], validate=True)
        except (ValueError, binascii.Error) as error:
            raise DeskWeaveError("INVALID_ARGUMENT", "内容块不是有效的 Base64。") from error
        if (
            not block
            or len(block) > MAX_CHUNK
            or base64.b64encode(block).decode() != params["chunk"]
        ):
            raise DeskWeaveError("INVALID_ARGUMENT", "内容块的编码或大小无效。")
        if hashlib.sha256(block).hexdigest() != params["chunkSha256"]:
            raise DeskWeaveError("CONTENT_HASH_MISMATCH", "内容块摘要不匹配，请重新发送该块。")
        start = int(params["offset"])
        end = start + len(block)
        if start > item.received or end > item.length:
            raise DeskWeaveError("CONTENT_OFFSET_CONFLICT", "内容块偏移或长度与上传进度不符。")
        if start < item.received:
            if end > item.received or not self._block_boundary(item.boundaries, start, end):
                raise DeskWeaveError("CONTENT_OFFSET_CONFLICT", "重传内容块的范围与已接收块不符。")
            item.stream.seek(start)
            if item.stream.read(len(block)) != block:
                raise DeskWeaveError("CONTENT_OFFSET_CONFLICT", "重传内容块与已接收内容不同。")
            return {"uploadId": item.upload_id, "nextOffset": item.received, "replayed": True}
        if item.sealed:
            raise DeskWeaveError("CONTENT_SEALED", "内容已封存，请创建新的上传。")
        self.checkpoint()
        item.stream.seek(start)
        try:
            remaining = memoryview(block)
            while remaining:
                self.checkpoint()
                written = item.stream.write(remaining)
                if not written:
                    raise OSError("content write made no progress")
                remaining = remaining[written:]
            item.stream.flush()
            self.checkpoint()
        except BaseException:
            try:
                item.stream.truncate(start)
                item.stream.seek(start)
            except OSError:
                self._drop(item)
            raise
        item.received = end
        item.boundaries[end // 8] |= 1 << (end % 8)
        return {"uploadId": item.upload_id, "nextOffset": end, "replayed": False}

    def _bytes(self, item: Content) -> bytes:
        self.checkpoint()
        item.stream.seek(0)
        parts = []
        actual = hashlib.sha256()
        remaining = item.length
        while remaining:
            self.checkpoint()
            block = item.stream.read(min(MAX_CHUNK, remaining))
            if not block:
                raise DeskWeaveError("CONTENT_INCOMPLETE", "内容尚未完整接收。")
            actual.update(block)
            parts.append(block)
            remaining -= len(block)
        if item.stream.read(1) or actual.hexdigest() != item.sha256:
            raise DeskWeaveError("CONTENT_HASH_MISMATCH", "内容长度或摘要不匹配，请重新上传。")
        self.checkpoint()
        return b"".join(parts)

    @staticmethod
    def reference(item: Content) -> dict:
        return {
            "kind": "resource",
            "resourceId": item.resource_id,
            "length": item.length,
            "sha256": item.sha256,
        }

    @staticmethod
    def descriptor(item: Content) -> dict:
        return {
            "resourceId": item.resource_id,
            "mediaType": "application/json",
            "length": item.length,
            "sha256": item.sha256,
            "expiresAt": item.expires_at,
        }

    def seal(self, item: Content, validate_content: Callable) -> dict:
        if not item.sealed:
            if item.received != item.length:
                raise DeskWeaveError("CONTENT_INCOMPLETE", "内容尚未完整接收。")
            value = loads(self._bytes(item), max_bytes=MAX_CONTENT)
            self.checkpoint()
            workflow_id = validate_content(item.purpose, value)
            self.checkpoint()
            item.workflow_id = workflow_id
            item.sealed = True
            item.expires, item.deadline = self._expiry()
            self.resources[item.resource_id] = item
        return {"reference": self.reference(item), "expiresAt": item.expires_at}

    def resolve(self, item: Content, reference: dict, purpose: str) -> dict:
        if item.purpose != purpose or self.reference(item) != reference:
            raise DeskWeaveError("CONTENT_REFERENCE_INVALID", "内容引用的用途、长度或摘要不匹配。")
        value = loads(self._bytes(item), max_bytes=MAX_CONTENT)
        self.checkpoint()
        return value

    def export(self, owner: str, workflow_id: str, data: bytes) -> dict:
        self.expire()
        sha256 = hashlib.sha256(data).hexdigest()
        outputs = [item for item in self.resources.values() if item.upload_id is None]
        for item in outputs:
            if (
                item.owner == owner
                and item.workflow_id == workflow_id
                and item.sha256 == sha256
                and item.length == len(data)
            ):
                return self.descriptor(item)
        self._capacity(outputs, len(data))
        item = self._new(
            owner,
            "workflowModel",
            "workflow.read",
            len(data),
            sha256,
            workflow_id=workflow_id,
            sealed=True,
        )
        try:
            for offset in range(0, len(data), MAX_CHUNK):
                self.checkpoint()
                block = data[offset : offset + MAX_CHUNK]
                if item.stream.write(block) != len(block):
                    raise OSError("content write incomplete")
            item.stream.flush()
            self.checkpoint()
        except BaseException:
            item.stream.close()
            raise
        self.resources[item.resource_id] = item
        return self.descriptor(item)

    def read(self, item: Content, offset: int, length: int) -> dict:
        offset, length = int(offset), int(length)
        if offset > item.length:
            raise DeskWeaveError("INVALID_ARGUMENT", "读取偏移超过内容长度。")
        self.checkpoint()
        expected = min(length, item.length - offset)
        item.stream.seek(offset)
        data = item.stream.read(expected)
        if len(data) != expected:
            raise DeskWeaveError("CONTENT_INCOMPLETE", "内容读取不完整，请重新取得内容。")
        return {
            "resourceId": item.resource_id,
            "mediaType": "application/json",
            "offset": offset,
            "length": len(data),
            "totalLength": item.length,
            "sha256": item.sha256,
            "chunk": base64.b64encode(data).decode(),
        }

    def discard_target(self, owner: str, params: dict) -> Content | None:
        if "uploadId" in params:
            item = self.uploads.get(params["uploadId"])
        else:
            item = self.resources.get(params["resourceId"])
        return item if item is not None and item.owner == owner else None

    def discard(self, owner: str, params: dict) -> None:
        item = self.discard_target(owner, params)
        if item is not None:
            self._drop(item)

    def _drop_all(self, items) -> None:
        failure = None
        for item in items:
            try:
                self._drop(item)
            except OSError as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure

    def release(self, owner: str) -> None:
        self._drop_all(
            item
            for item in set(self.uploads.values()) | set(self.resources.values())
            if item.owner == owner
        )

    def close(self) -> None:
        self._drop_all(set(self.uploads.values()) | set(self.resources.values()))
