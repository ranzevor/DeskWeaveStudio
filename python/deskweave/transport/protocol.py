from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from importlib.metadata import version

from deskweave.contracts import contract_digest, validate
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, dumps, loads

REFERENCE = "urn:deskweave:connection:1#/$defs/"
CAPABILITIES = frozenset({"contentRefsV1"})


def packet(value: object, kind: str) -> dict:
    snapshot = loads(dumps(value))
    validate(REFERENCE + kind, snapshot)
    return snapshot


def proof(secret: bytes, role: str, value: dict) -> str:
    data = b"DeskWeaveStudio/connection/v1/" + role.encode("ascii") + b"\0" + dumps(value)
    return hmac.new(secret, data, hashlib.sha256).hexdigest()


def verify(actual: str, expected: str) -> None:
    if not hmac.compare_digest(actual, expected):
        raise DeskWeaveError("AUTHENTICATION_FAILED", "连接认证失败。")


@dataclass(frozen=True)
class PeerIdentity:
    platform: str
    user_id: str
    os_session_id: str
    process_id: int
    process_started: str
    installation_digest: str
    instance_epoch: str

    def __post_init__(self):
        validate(REFERENCE + "peerIdentity", self.as_dict())

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "userId": self.user_id,
            "osSessionId": self.os_session_id,
            "processId": self.process_id,
            "processStarted": self.process_started,
            "installationDigest": self.installation_digest,
            "instanceEpoch": self.instance_epoch,
        }


@dataclass(frozen=True)
class ConnectionProfile:
    protocol_version: str
    component_version: str
    instance_epoch: str
    schema_digest: str
    capabilities: frozenset[str]
    max_frame_bytes: int = MAX_FRAME

    def __post_init__(self):
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        validate(REFERENCE + "profile", self.as_dict())

    @classmethod
    def current(cls, peer: PeerIdentity, *, max_frame_bytes: int = MAX_FRAME):
        return cls(
            "1.0",
            version("deskweave"),
            peer.instance_epoch,
            contract_digest(),
            CAPABILITIES,
            max_frame_bytes,
        )

    @classmethod
    def from_dict(cls, value: dict):
        validate(REFERENCE + "profile", value)
        return cls(
            value["protocolVersion"],
            value["componentVersion"],
            value["instanceEpoch"],
            value["schemaDigest"],
            frozenset(value["capabilities"]),
            value["limits"]["maxFrameBytes"],
        )

    def as_dict(self) -> dict:
        return {
            "protocolVersion": self.protocol_version,
            "componentVersion": self.component_version,
            "instanceEpoch": self.instance_epoch,
            "schemaDigest": self.schema_digest,
            "capabilities": sorted(self.capabilities),
            "limits": {"maxFrameBytes": self.max_frame_bytes},
        }


def negotiate(client: ConnectionProfile, server: ConnectionProfile) -> dict:
    client_major, client_minor = map(int, client.protocol_version.split("."))
    server_major, server_minor = map(int, server.protocol_version.split("."))
    if client_major != server_major:
        raise DeskWeaveError("PROTOCOL_UNSUPPORTED", "本机协议主版本不兼容。")
    if client.schema_digest != server.schema_digest:
        raise DeskWeaveError("SCHEMA_UNSUPPORTED", "连接两端的公共契约不一致。")
    return {
        "protocolVersion": f"{server_major}.{min(client_minor, server_minor)}",
        "capabilities": sorted(client.capabilities & server.capabilities),
        "limits": {"maxFrameBytes": min(client.max_frame_bytes, server.max_frame_bytes)},
    }


@dataclass(frozen=True)
class ConnectionPolicy:
    permissions: frozenset[str]
    workflow_ids: frozenset[str] | None = None
    local: bool = False

    def __post_init__(self):
        object.__setattr__(self, "permissions", frozenset(self.permissions))
        if self.workflow_ids is not None:
            object.__setattr__(self, "workflow_ids", frozenset(self.workflow_ids))
        validate(REFERENCE + "policy", self.as_dict())

    def as_dict(self) -> dict:
        return {
            "permissions": sorted(self.permissions),
            "workflowIds": None if self.workflow_ids is None else sorted(self.workflow_ids),
            "local": self.local,
        }


@dataclass(frozen=True)
class ClientCredential:
    client_id: str
    secret: bytes = field(repr=False)

    def __post_init__(self):
        validate(REFERENCE + "identity", self.client_id)
        if type(self.secret) is not bytes or len(self.secret) != 32:
            raise ValueError("身份恢复凭据必须为 32 字节。")


@dataclass(frozen=True)
class ConnectionTicket:
    ticket_id: str
    secret: bytes = field(repr=False)
    client_id: str
    authorization_version: int
    workspace: str
    client_peer: PeerIdentity
    server_peer: PeerIdentity

    def __post_init__(self):
        if type(self.client_peer) is not PeerIdentity or type(self.server_peer) is not PeerIdentity:
            raise TypeError("连接票据需要双方原生身份。")
        for identity in (self.ticket_id, self.client_id, self.workspace):
            validate(REFERENCE + "identity", identity)
        if type(self.secret) is not bytes or len(self.secret) != 32:
            raise ValueError("连接票据密钥必须为 32 字节。")
        if (
            type(self.authorization_version) is not int
            or not 1 <= self.authorization_version <= 9007199254740991
        ):
            raise ValueError("授权版本无效。")

    def binding(self) -> dict:
        return {
            "ticketId": self.ticket_id,
            "purpose": "engine.control",
            "workspace": self.workspace,
            "clientId": self.client_id,
            "authorizationVersion": self.authorization_version,
            "clientPeer": self.client_peer.as_dict(),
            "serverPeer": self.server_peer.as_dict(),
        }


def transcript(ticket: ConnectionTicket, hello: dict, challenge: dict) -> dict:
    return {"binding": ticket.binding(), "hello": hello, "challenge": challenge}
