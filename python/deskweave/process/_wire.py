from __future__ import annotations

import base64
import struct
from collections.abc import Iterable
from pathlib import Path

from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, dumps, loads
from deskweave.transport.native import Installation, InstalledFile
from deskweave.transport.protocol import ClientCredential, ConnectionTicket, PeerIdentity

VERSION = "1.0"
MAX_COMPONENTS = 10000


def fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise DeskWeaveError("INVALID_ARGUMENT", "进程控制消息字段无效。")
    return value


def read_frame(stream):
    def exact(size):
        value = bytearray()
        while len(value) < size:
            part = stream.read(size - len(value))
            if not part:
                raise DeskWeaveError("TRANSPORT_TRUNCATED", "启动通道在完整消息到达前关闭。")
            value.extend(part)
        return bytes(value)

    size = struct.unpack("<I", exact(4))[0]
    if not 1 <= size <= MAX_FRAME:
        raise DeskWeaveError("LIMIT_EXCEEDED", "启动消息超过帧上限。")
    return loads(exact(size))


def write_frame(stream, value):
    data = dumps(value)
    view = memoryview(struct.pack("<I", len(data)) + data)
    while view:
        count = stream.write(view)
        if not count:
            raise BrokenPipeError("启动通道已关闭。")
        view = view[count:]
    stream.flush()


def file_value(item: InstalledFile):
    return {"path": str(item.path), "sha256": item.sha256}


def parse_file(value):
    fields(value, ("path", "sha256"))
    if type(value["path"]) is not str or not Path(value["path"]).is_absolute():
        raise DeskWeaveError("INVALID_ARGUMENT", "安装路径无效。")
    try:
        return InstalledFile(value["path"], value["sha256"])
    except (OSError, RuntimeError) as error:
        raise DeskWeaveError("AUTHENTICATION_FAILED", "安装文件路径无法解析。") from error


def manifest_header(value: Installation):
    return {
        "executable": file_value(value.executable),
        "componentCount": len(value.components),
        "macosRequirement": value.macos_requirement,
    }


def header_count(value):
    fields(value, ("executable", "componentCount", "macosRequirement"))
    count = value["componentCount"]
    if type(count) is not int or not 1 <= count <= MAX_COMPONENTS:
        raise DeskWeaveError("LIMIT_EXCEEDED", "安装组件数量无效。")
    return count


def chunks(items: Iterable[InstalledFile]):
    batch, size = [], 0
    for item in items:
        value = file_value(item)
        length = len(dumps(value))
        if batch and (len(batch) >= 128 or size + length > MAX_FRAME // 2):
            yield batch
            batch, size = [], 0
        batch.append(value)
        size += length + 1
    if batch:
        yield batch


class ManifestReader:
    def __init__(self, header, *, byte_limit=16777216):
        self.count = header_count(header)
        self.executable = parse_file(header["executable"])
        self.requirement = header["macosRequirement"]
        self.items = []
        self.bytes = len(
            dumps(
                {
                    "executable": file_value(self.executable),
                    "componentCount": self.count,
                    "macosRequirement": self.requirement,
                }
            )
        )
        self.byte_limit = byte_limit
        if self.bytes > byte_limit:
            raise DeskWeaveError("LIMIT_EXCEEDED", "安装登记元数据额度已满。")

    def append(self, values, checkpoint=lambda: None):
        if type(values) is not list or not 1 <= len(values) <= 128:
            raise DeskWeaveError("INVALID_ARGUMENT", "安装清单分块无效。")
        if len(self.items) + len(values) > self.count:
            raise DeskWeaveError("LIMIT_EXCEEDED", "安装清单分块超过声明数量。")
        for value in values:
            checkpoint()
            item = parse_file(value)
            length = len(dumps(file_value(item))) + 1
            if self.bytes + length > self.byte_limit:
                raise DeskWeaveError("LIMIT_EXCEEDED", "安装登记元数据额度已满。")
            self.items.append(item)
            self.bytes += length

    def finish(self):
        if len(self.items) != self.count:
            raise DeskWeaveError("INVALID_ARGUMENT", "安装清单分块不完整。")
        return Installation(self.executable, tuple(self.items), self.requirement)


def peer_value(value):
    fields(
        value,
        (
            "platform",
            "userId",
            "osSessionId",
            "processId",
            "processStarted",
            "installationDigest",
            "instanceEpoch",
        ),
    )
    return PeerIdentity(
        value["platform"],
        value["userId"],
        value["osSessionId"],
        value["processId"],
        value["processStarted"],
        value["installationDigest"],
        value["instanceEpoch"],
    )


def secret(value):
    if type(value) is not str or len(value) != 44:
        raise DeskWeaveError("INVALID_ARGUMENT", "进程控制凭据编码无效。")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise DeskWeaveError("INVALID_ARGUMENT", "进程控制凭据编码无效。") from error
    if len(decoded) != 32:
        raise DeskWeaveError("INVALID_ARGUMENT", "进程控制凭据长度无效。")
    return decoded


def credential_value(value):
    return {"clientId": value.client_id, "secret": base64.b64encode(value.secret).decode("ascii")}


def credential(value):
    fields(value, ("clientId", "secret"))
    return ClientCredential(value["clientId"], secret(value["secret"]))


def ticket_value(value):
    return {**value.binding(), "secret": base64.b64encode(value.secret).decode("ascii")}


def ticket(value):
    fields(
        value,
        (
            "ticketId",
            "purpose",
            "workspace",
            "clientId",
            "authorizationVersion",
            "clientPeer",
            "serverPeer",
            "secret",
        ),
    )
    if value["purpose"] != "engine.control":
        raise DeskWeaveError("AUTHENTICATION_FAILED", "连接票据用途不匹配。")
    return ConnectionTicket(
        value["ticketId"],
        secret(value["secret"]),
        value["clientId"],
        value["authorizationVersion"],
        value["workspace"],
        peer_value(value["clientPeer"]),
        peer_value(value["serverPeer"]),
    )


def manifest_size(installation, checkpoint=lambda: None):
    return len(dumps(manifest_header(installation), checkpoint=checkpoint)) + sum(
        len(dumps(file_value(item), checkpoint=checkpoint)) + 1 for item in installation.components
    )
