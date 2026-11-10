from __future__ import annotations

import base64
import math
import time
from dataclasses import dataclass
from pathlib import Path

from deskweave.errors import DeskWeaveError
from deskweave.process import _wire as wire
from deskweave.transport.native._common import Resource

from .authority import MAX_HOSTS, HostBinding, HostRegistration, StartupPlan
from .directory import Directory, WorkspaceIdentity

HOST_BYTES = 1048576


def check_idle(idle_grace):
    if (
        type(idle_grace) not in (int, float)
        or not math.isfinite(idle_grace)
        or not 0.05 <= idle_grace <= 300
    ):
        raise ValueError("空闲宽限应为 0.05～300 秒。")


def header(plan, identity, runtime, idle_grace, budget):
    check_idle(idle_grace)
    identity.check(plan.workspace, allow_missing=True)
    runtime.check()
    if plan.directory != identity.object:
        raise DeskWeaveError("AUTHENTICATION_FAILED", "启动计划与工作空间不匹配。")
    budget.check_time()
    return {
        "version": "1.0",
        "authority": plan.authority,
        "directory": list(plan.directory),
        "workspace": plan.workspace,
        "engineInstallation": plan.engine_installation,
        "runtimePath": str(runtime.path),
        "runtimeDirectory": list(runtime.identity),
        "hostCount": len(plan.hosts),
        "idleGrace": idle_grace,
        "deadline": budget.deadline,
    }


def write_plan(stream, plan, installations, budget):
    known = {item.digest: wire.manifest_header(item) for item in installations}
    total = 0
    for host in plan.hosts:
        budget.check_time()
        digest = host.installation.calculate_digest(budget.check_time)
        if digest != host.binding.peer.installation_digest:
            raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主安装登记不匹配。")
        metadata = wire.manifest_header(host.installation)
        value = {
            "binding": host.binding.as_dict(),
            "installation": metadata,
            "verificationKey": base64.b64encode(host.verification_key).decode("ascii"),
        }
        total += len(wire.dumps(value, checkpoint=budget.check_time))
        if total > HOST_BYTES:
            raise DeskWeaveError("LIMIT_EXCEEDED", "宿主授权元数据额度已满。")
        wire.write_frame(stream, value)
        if digest in known:
            if known[digest] != metadata:
                raise DeskWeaveError("AUTHENTICATION_FAILED", "安装摘要与清单头不匹配。")
        else:
            for values in wire.chunks(host.installation.components):
                budget.check_time()
                wire.write_frame(stream, values)
            known[digest] = metadata


def read_plan(stream, config, installations, max_bytes, budget):
    wire.fields(
        config,
        (
            "version",
            "authority",
            "directory",
            "workspace",
            "engineInstallation",
            "runtimePath",
            "runtimeDirectory",
            "hostCount",
            "idleGrace",
            "deadline",
        ),
    )
    if config["version"] != "1.0":
        raise DeskWeaveError("PROTOCOL_UNSUPPORTED", "工作空间协调协议不兼容。")
    if type(config["hostCount"]) is not int or not 1 <= config["hostCount"] <= MAX_HOSTS:
        raise ValueError("启动宿主数量无效。")
    for key in ("directory", "runtimeDirectory"):
        value = config[key]
        if (
            type(value) is not list
            or len(value) != 2
            or any(type(item) is not str or not 1 <= len(item) <= 128 for item in value)
        ):
            raise ValueError("启动目录身份无效。")
    if type(config["runtimePath"]) is not str or not Path(config["runtimePath"]).is_absolute():
        raise ValueError("运行目录必须是绝对路径。")
    check_idle(config["idleGrace"])
    deadline = config["deadline"]
    if (
        type(deadline) not in (int, float)
        or not math.isfinite(deadline)
        or deadline <= time.monotonic()
    ):
        raise DeskWeaveError("DEADLINE_EXCEEDED", "工作空间启动已超时。")
    known = {value.digest: value for value in installations}
    used = sum(wire.manifest_size(value, budget.check_time) for value in known.values())
    hosts, total = [], 0
    for _ in range(config["hostCount"]):
        budget.check_time()
        value = wire.read_frame(stream)
        wire.fields(value, ("binding", "installation", "verificationKey"))
        total += len(wire.dumps(value, checkpoint=budget.check_time))
        if total > HOST_BYTES:
            raise DeskWeaveError("LIMIT_EXCEEDED", "宿主授权元数据额度已满。")
        binding = HostBinding.from_dict(value["binding"])
        key = wire.secret(value["verificationKey"])
        installation = known.get(binding.peer.installation_digest)
        if installation is not None:
            if wire.manifest_header(installation) != value["installation"]:
                raise DeskWeaveError("AUTHENTICATION_FAILED", "安装摘要与清单头不匹配。")
        else:
            reader = wire.ManifestReader(value["installation"], byte_limit=max_bytes - used)
            while len(reader.items) < reader.count:
                budget.check_time()
                reader.append(wire.read_frame(stream), budget.check_time)
            installation = reader.finish()
            if installation.calculate_digest(budget.check_time) != binding.peer.installation_digest:
                raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主安装摘要不匹配。")
            used += reader.bytes
            known[binding.peer.installation_digest] = installation
        hosts.append(HostRegistration(binding, installation, key))
    return StartupPlan(
        config["authority"],
        tuple(config["directory"]),
        config["workspace"],
        config["engineInstallation"],
        tuple(hosts),
    )


@dataclass(frozen=True)
class LaunchContext:
    plan: StartupPlan
    identity: WorkspaceIdentity
    runtime: Directory
    idle_grace: float = 2.0


class InvalidDiscovery(DeskWeaveError):
    def __init__(self):
        super().__init__("INVALID_ARGUMENT", "工作空间发现记录已损坏。")


class Discovery(Resource):
    def __init__(self, identity, runtime):
        self.identity, self.runtime = identity, runtime
        self.name = identity.key + ".json"

    def read(self):
        try:
            data = self.runtime.read(self.name)
        except DeskWeaveError as error:
            if error.code == "LIMIT_EXCEEDED":
                raise InvalidDiscovery() from error
            raise
        try:
            return self.decode(data)
        except (DeskWeaveError, ValueError, TypeError, KeyError) as error:
            raise InvalidDiscovery() from error

    def decode(self, data):
        if data is None:
            return None
        value = wire.loads(data)
        wire.fields(
            value, ("version", "directory", "workspace", "peer", "control", "address", "authority")
        )
        if value["version"] != "1.0" or value["directory"] != list(self.identity.object):
            raise DeskWeaveError("AUTHENTICATION_FAILED", "发现记录与工作空间目录不匹配。")
        peer = wire.peer_value(value["peer"])
        for name in ("control", "address", "workspace", "authority"):
            if type(value[name]) is not str or not 1 <= len(value[name]) <= 4096:
                raise DeskWeaveError("INVALID_ARGUMENT", "发现记录字段无效。")
        if self.identity.workspace is not None and value["workspace"] != self.identity.workspace:
            raise DeskWeaveError("AUTHENTICATION_FAILED", "发现记录的工作空间身份不匹配。")
        value["peer"] = peer
        return value

    def publish(self, workspace, peer, control, address, authority):
        self.identity.check(workspace)
        value = {
            "version": "1.0",
            "directory": list(self.identity.object),
            "workspace": workspace,
            "peer": peer.as_dict(),
            "control": control,
            "address": address,
            "authority": authority,
        }
        self.runtime.write(self.name, wire.dumps(value, max_bytes=16384))
        self.identity.check(workspace)

    def remove(self, peer):
        def matches(data):
            try:
                value = wire.loads(data)
            except DeskWeaveError:
                return False
            return (
                type(value) is dict
                and value.get("directory") == list(self.identity.object)
                and value.get("peer") == peer.as_dict()
            )

        return self.runtime.remove(self.name, matches=matches)
