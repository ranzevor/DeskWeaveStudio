from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field

from deskweave.contracts import validate
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps
from deskweave.transport.native import Installation, ProcessBinding
from deskweave.transport.native._common import Budget
from deskweave.transport.protocol import ClientCredential, ConnectionPolicy, PeerIdentity

from .directory import WorkspaceIdentity

REFERENCE = "urn:deskweave:connection:1#/$defs/"
MAX_HOSTS = 32


def policy_from(value):
    if type(value) is not dict or set(value) != {"permissions", "workflowIds", "local"}:
        raise DeskWeaveError("INVALID_ARGUMENT", "宿主权限字段无效。")
    return ConnectionPolicy(value["permissions"], value["workflowIds"], value["local"])


def within(requested, ceiling):
    return (
        requested.permissions <= ceiling.permissions
        and (not requested.local or ceiling.local)
        and (
            ceiling.workflow_ids is None
            or requested.workflow_ids is not None
            and requested.workflow_ids <= ceiling.workflow_ids
        )
    )


def check_expiry(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("宿主授权期限必须为有限正数。")


def proof(key, role, value, checkpoint=lambda: None):
    message = (
        b"DeskWeaveStudio/workspace-host/v1/"
        + role.encode("ascii")
        + b"\0"
        + dumps(value, checkpoint=checkpoint)
    )
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_proof(actual, expected):
    if (
        type(actual) is not str
        or len(actual) != 64
        or any(letter not in "0123456789abcdef" for letter in actual)
        or not hmac.compare_digest(actual, expected)
    ):
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主接入认证失败。")


@dataclass(frozen=True)
class HostBinding:
    authority: str
    grant_id: str
    directory: tuple[str, str]
    workspace: str
    engine_installation: str
    peer: PeerIdentity
    policy: ConnectionPolicy
    expires_at: float
    manager: bool
    epoch: str | None = None
    version: int = 1
    digest: str = field(init=False, repr=False)

    def __post_init__(self):
        for value in (self.authority, self.grant_id):
            validate(REFERENCE + "identity", value)
        validate(REFERENCE + "identity", self.workspace)
        if self.epoch is not None:
            validate(REFERENCE + "identity", self.epoch)
        validate(REFERENCE + "digest", self.engine_installation)
        if type(self.peer) is not PeerIdentity or type(self.policy) is not ConnectionPolicy:
            raise TypeError("宿主授权需要实际进程身份与连接策略。")
        if type(self.manager) is not bool or type(self.version) is not int or self.version != 1:
            raise ValueError("宿主管理角色或授权版本无效。")
        if (
            type(self.directory) is not tuple
            or len(self.directory) != 2
            or any(type(value) is not str or not 1 <= len(value) <= 128 for value in self.directory)
        ):
            raise ValueError("工作空间目录身份无效。")
        check_expiry(self.expires_at)
        object.__setattr__(self, "digest", hashlib.sha256(dumps(self.as_dict())).hexdigest())

    def as_dict(self):
        return {
            "authority": self.authority,
            "grantId": self.grant_id,
            "directory": list(self.directory),
            "workspace": self.workspace,
            "engineInstallation": self.engine_installation,
            "peer": self.peer.as_dict(),
            "policy": self.policy.as_dict(),
            "expiresAt": self.expires_at,
            "manager": self.manager,
            "epoch": self.epoch,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value):
        from deskweave.process import _wire as wire

        wire.fields(
            value,
            (
                "authority",
                "grantId",
                "directory",
                "workspace",
                "engineInstallation",
                "peer",
                "policy",
                "expiresAt",
                "manager",
                "epoch",
                "version",
            ),
        )
        if type(value["directory"]) is not list:
            raise ValueError("工作空间目录身份无效。")
        return cls(
            value["authority"],
            value["grantId"],
            tuple(value["directory"]),
            value["workspace"],
            value["engineInstallation"],
            wire.peer_value(value["peer"]),
            policy_from(value["policy"]),
            value["expiresAt"],
            value["manager"],
            value["epoch"],
            value["version"],
        )


@dataclass(frozen=True)
class HostGrant:
    binding: HostBinding
    secret: bytes = field(repr=False)

    def __post_init__(self):
        if type(self.binding) is not HostBinding:
            raise TypeError("宿主接入需要完整授权绑定。")
        ClientCredential(self.binding.grant_id, self.secret)

    @property
    def verification_key(self):
        return hashlib.sha256(self.secret).digest()

    def as_dict(self):
        from deskweave.process import _wire as wire

        return {
            "binding": self.binding.as_dict(),
            "credential": wire.credential_value(
                ClientCredential(self.binding.grant_id, self.secret)
            ),
        }

    @classmethod
    def from_dict(cls, value):
        from deskweave.process import _wire as wire

        wire.fields(value, ("binding", "credential"))
        binding = HostBinding.from_dict(value["binding"])
        credential = wire.credential(value["credential"])
        if binding.grant_id != credential.client_id:
            raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主授权身份不匹配。")
        return cls(binding, credential.secret)


@dataclass(frozen=True)
class HostRegistration:
    binding: HostBinding
    installation: Installation
    verification_key: bytes = field(repr=False)

    def __post_init__(self):
        if type(self.binding) is not HostBinding or type(self.installation) is not Installation:
            raise TypeError("宿主登记需要授权绑定及安装策略。")
        if type(self.verification_key) is not bytes or len(self.verification_key) != 32:
            raise ValueError("宿主验证材料长度无效。")


@dataclass(frozen=True)
class StartupPlan:
    authority: str
    directory: tuple[str, str]
    workspace: str
    engine_installation: str
    hosts: tuple[HostRegistration, ...] = field(repr=False)

    def __post_init__(self):
        if type(self.hosts) is not tuple or not 1 <= len(self.hosts) <= MAX_HOSTS:
            raise ValueError("启动计划需要 1～32 个宿主。")
        identities, processes = set(), set()
        for item in self.hosts:
            if type(item) is not HostRegistration:
                raise TypeError("启动计划宿主登记无效。")
            value = item.binding
            if (value.authority, value.directory, value.workspace, value.engine_installation) != (
                self.authority,
                self.directory,
                self.workspace,
                self.engine_installation,
            ) or value.epoch is not None:
                raise ValueError("启动计划的授权范围不一致。")
            if value.grant_id in identities or value.peer.process_id in processes:
                raise ValueError("启动计划包含重复宿主。")
            identities.add(value.grant_id)
            processes.add(value.peer.process_id)

    def select(self, grant, local, checkpoint=lambda: None):
        checkpoint()
        if grant.binding.peer != local.identity or not grant.binding.manager:
            raise DeskWeaveError("ACCESS_DENIED", "当前宿主没有工作空间启动管理权限。")
        for item in self.hosts:
            checkpoint()
            if item.binding == grant.binding and hmac.compare_digest(
                item.verification_key, grant.verification_key
            ):
                if item.binding.expires_at <= time.monotonic():
                    raise DeskWeaveError("ACCESS_DENIED", "宿主授权已到期。")
                return item
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主不属于当前启动计划。")


class StartupAuthority:
    def __init__(self, identity: WorkspaceIdentity, engine_installation: Installation):
        if type(identity) is not WorkspaceIdentity or type(engine_installation) is not Installation:
            raise TypeError("启动权威需要工作空间目录与 Engine 安装策略。")
        identity.check()
        self.identity = identity
        self.workspace = identity.workspace or uuid.uuid4().hex
        self.engine_installation = engine_installation.digest
        self.authority = secrets.token_urlsafe(24)
        self._hosts = {}
        self._sealed = False
        self._lock = threading.RLock()

    def register_host(
        self,
        peer: ProcessBinding,
        policy: ConnectionPolicy,
        *,
        lifetime,
        manager=False,
        timeout=30.0,
        cancel=None,
    ):
        budget = Budget(timeout, cancel)
        budget.acquire(self._lock)
        try:
            if self._sealed:
                raise DeskWeaveError("ACCESS_DENIED", "启动计划已经封存。")
            if type(peer) is not ProcessBinding or type(policy) is not ConnectionPolicy:
                raise TypeError("宿主登记需要实际进程与连接策略。")
            if len(self._hosts) >= MAX_HOSTS:
                raise DeskWeaveError("LIMIT_EXCEEDED", "启动宿主数量已满。")
            if peer.identity.process_id in self._hosts:
                raise ValueError("宿主进程已登记。")
            check_expiry(lifetime)
            peer.check(budget.check_time)
            self.identity.check()
            expires = time.monotonic() + lifetime
            check_expiry(expires)
            binding = HostBinding(
                self.authority,
                secrets.token_urlsafe(24),
                self.identity.object,
                self.workspace,
                self.engine_installation,
                peer.identity,
                policy,
                expires,
                manager,
            )
            grant = HostGrant(binding, secrets.token_bytes(32))
            self._hosts[peer.identity.process_id] = HostRegistration(
                binding, peer.installation, grant.verification_key
            )
            return grant

        finally:
            self._lock.release()

    def seal(self):
        with self._lock:
            value = StartupPlan(
                self.authority,
                self.identity.object,
                self.workspace,
                self.engine_installation,
                tuple(self._hosts.values()),
            )
            if not any(item.binding.manager for item in value.hosts):
                raise ValueError("启动计划需要管理宿主。")
            self._sealed = True
            return value
