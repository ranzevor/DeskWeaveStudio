from __future__ import annotations

import struct

from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, dumps, loads


def encode_frame(message: dict) -> bytes:
    payload = dumps(message)
    return struct.pack("<I", len(payload)) + payload


class FrameDecoder:
    def __init__(self):
        self._buffer = bytearray()
        self._length: int | None = None
        self._failed = False

    def feed(self, data: bytes) -> list[dict]:
        if self._failed:
            raise DeskWeaveError("TRANSPORT_CLOSED", "消息连接已关闭。")
        messages = []
        remaining = memoryview(data)
        try:
            while remaining:
                limit = 4 if self._length is None else self._length
                size = min(len(remaining), limit - len(self._buffer))
                self._buffer.extend(remaining[:size])
                remaining = remaining[size:]
                if len(self._buffer) < limit:
                    continue
                if self._length is None:
                    self._length = struct.unpack("<I", self._buffer)[0]
                    self._buffer.clear()
                    if not 1 <= self._length <= MAX_FRAME:
                        raise DeskWeaveError("LIMIT_EXCEEDED", "消息帧长度无效。")
                else:
                    value = loads(bytes(self._buffer))
                    if type(value) is not dict:
                        raise DeskWeaveError("INVALID_ARGUMENT", "消息帧必须是 JSON 对象。")
                    messages.append(value)
                    self._buffer.clear()
                    self._length = None
        except DeskWeaveError:
            self._failed = True
            self._buffer.clear()
            raise
        return messages

    def finish(self) -> None:
        partial = bool(self._buffer) or self._length is not None
        self._failed = True
        self._buffer.clear()
        if partial:
            raise DeskWeaveError("TRANSPORT_TRUNCATED", "连接在完整消息到达前结束。")
