from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from deskweave.adapters.dependencies import Dependencies
from deskweave.adapters.receipts import find_receipt, replay_receipt
from deskweave.adapters.runs import RunStore
from deskweave.contracts import method_schema, validate
from deskweave.domain.runs import RunPlan
from deskweave.domain.values import matches_type
from deskweave.domain.workflow import validate_workflow
from deskweave.errors import DeskWeaveError
from deskweave.json import digest, dumps, json_pointer, loads

DEFAULT_LIMITS = {
    "activeMs": 600_000,
    "steps": 10_000,
    "callDepth": 8,
    "parallelCalls": 8,
    "inputEvents": 100_000,
}


@dataclass(frozen=True, slots=True)
class RunAccess:
    client_id: str
    authorization_ref: str
    authorization_version: int
    permissions: frozenset[str]
    workflow_ids: frozenset[str] | None = None
    revalidate: Callable[[], None] = field(default=lambda: None, repr=False, compare=False)
    authorize_target: Callable[[str, str, str], None] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self):
        if (
            type(self.client_id) is not str
            or not 1 <= len(self.client_id) <= 128
            or type(self.authorization_ref) is not str
            or not 1 <= len(self.authorization_ref) <= 256
            or type(self.authorization_version) is not int
            or not 1 <= self.authorization_version <= 9007199254740991
            or not callable(self.revalidate)
            or self.authorize_target is not None
            and not callable(self.authorize_target)
        ):
            raise ValueError("运行授权上下文无效。")
        object.__setattr__(self, "permissions", frozenset(self.permissions))
        if self.workflow_ids is not None:
            object.__setattr__(self, "workflow_ids", frozenset(self.workflow_ids))

    def require(self, *permissions):
        self.revalidate()
        if not set(permissions) <= self.permissions:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端缺少运行所需的权限。")

    def workflow(self, identity):
        self.require("workflow.read")
        if self.workflow_ids is not None and identity not in self.workflow_ids:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有此流程的访问权限。")

    def target(self, slot, token, kind):
        self.revalidate()
        if self.authorize_target is None:
            raise DeskWeaveError("METHOD_UNAVAILABLE", "目标授权服务未接入。")
        self.authorize_target(slot, token, kind)

    def check_snapshot(self, snapshot):
        self.revalidate()
        if snapshot["authorization"] != {
            "clientId": self.client_id,
            "ref": self.authorization_ref,
            "version": self.authorization_version,
        }:
            raise DeskWeaveError("ACCESS_DENIED", "运行计划的授权上下文已变化。")
        self.require(*snapshot["requiredPermissions"])
        for item in snapshot["workflows"]:
            self.workflow(item["workflowId"])
        for slot, token in snapshot["targets"].items():
            self.target(slot, token, snapshot["targetKinds"][slot])


def _invalid(message, *path):
    raise DeskWeaveError("INVALID_ARGUMENT", message, details={"fieldPath": json_pointer(path)})


class RunService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = RunStore(workspace)

    def _parameters(self, params):
        value = loads(
            dumps(params, checkpoint=self.workspace.check_deadline),
            checkpoint=self.workspace.check_deadline,
        )
        validate(
            method_schema("run.start", "params"), value, checkpoint=self.workspace.check_deadline
        )
        if any(key in value for key in ("startNode", "initialVariables", "debug", "planId")):
            raise DeskWeaveError("METHOD_UNAVAILABLE", "该运行入口尚未接入执行服务。")
        return {"interruptPolicy": "pause", **value}

    def _inputs(self, definition, supplied):
        if not supplied.keys() <= definition["parameters"].keys():
            key = next(key for key in supplied if key not in definition["parameters"])
            _invalid("输入参数未在流程中声明。", "inputs", key)
        inputs, sensitive = {}, []
        for key, spec in definition["parameters"].items():
            self.workspace.check_deadline()
            if key in supplied:
                value = supplied[key]
            elif "default" in spec:
                value = spec["default"]
            elif spec["required"]:
                _invalid("缺少必填输入参数。", "inputs", key)
            else:
                continue
            if not matches_type(value, spec["type"]):
                _invalid("输入参数类型不匹配。", "inputs", key)
            inputs[key] = value
            if spec["sensitive"]:
                sensitive.append(key)
        return inputs, sorted(sensitive)

    def _prepare(self, access, params):
        access.require("run.execute")
        root = None
        required = {"run.execute", "workflow.read"}

        def visit(identity, revision, definition):
            nonlocal root
            if validate_workflow(definition, checkpoint=self.workspace.check_deadline):
                raise DeskWeaveError("INVALID_WORKFLOW", "运行引用的流程存在结构或连接错误。")
            if (identity, revision) == (params["workflowId"], params["revision"]):
                root = definition
            for node in definition["nodes"]:
                self.workspace.check_deadline()
                action = node["action"]
                if action.startswith(("pointer.", "keyboard.")) or action == "input.trajectory":
                    required.add("desktop.input")
                elif action == "screen.capture":
                    required.add("capture.read")
                elif action == "vision.match":
                    required.update(("capture.read", "vision.local"))
                elif action == "vision.observe":
                    required.update(("capture.read", "vision.remote"))

        graph = Dependencies(
            self.workspace, access.workflow, lambda: access.require("asset.read")
        ).resolve(params["workflowId"], params["revision"], visitor=visit)
        if root.get("kind", "workflow") != "workflow":
            raise DeskWeaveError("INVALID_WORKFLOW", "循环体不能作为根流程启动。")
        if graph.assets:
            required.add("asset.read")
        access.require(*required)
        inputs, sensitive = self._inputs(root, params["inputs"])
        targets = params["targets"]
        if not targets.keys() <= root["targets"].keys():
            _invalid("目标槽未在流程中声明。", "targets")
        for slot, spec in root["targets"].items():
            if spec["required"] and slot not in targets:
                _invalid("缺少必填目标槽。", "targets", slot)
            if slot in targets:
                access.target(slot, targets[slot], spec["kind"])
        shared = {**root.get("runVariables", {}), **params.get("runVariables", {})}
        validate(
            "urn:deskweave:workflow:1#/properties/runVariables",
            shared,
            checkpoint=self.workspace.check_deadline,
        )
        limits = {
            key: min(int(root["limits"][key]), int(value))
            for key, value in params.get("limits", DEFAULT_LIMITS).items()
        }
        snapshot = {
            "formatVersion": "1.0",
            "workspaceId": self.workspace.id,
            "name": root["name"],
            "root": {
                "workflowId": params["workflowId"],
                "revision": params["revision"],
                "entry": root["entry"],
            },
            **graph.manifest(),
            "initialState": {
                "inputs": inputs,
                "variables": root["variables"],
                "runVariables": shared,
                "sensitiveInputs": sensitive,
            },
            "targets": targets,
            "targetKinds": {slot: root["targets"][slot]["kind"] for slot in targets},
            "limits": limits,
            "detached": params["detached"],
            "interruptPolicy": params["interruptPolicy"],
            "requiredPermissions": sorted(required),
            "authorization": {
                "clientId": access.client_id,
                "ref": access.authorization_ref,
                "version": access.authorization_version,
            },
        }
        validate("urn:deskweave:run-plan:1", snapshot, checkpoint=self.workspace.check_deadline)
        return RunPlan(
            dumps(snapshot, max_bytes=33_554_432, checkpoint=self.workspace.check_deadline),
            digest(
                {"method": "run.start", "params": params}, checkpoint=self.workspace.check_deadline
            ),
        )

    def prepare(self, access: RunAccess, params: dict) -> RunPlan:
        with self.workspace.budget(None, authorize=access.revalidate):
            return self._prepare(access, self._parameters(params))

    def accept(self, access: RunAccess, request_id: str, params: dict):
        if type(request_id) is not str or not 1 <= len(request_id) <= 128:
            _invalid("请求身份长度应为 1～128。", "requestId")
        with self.workspace.budget(None, authorize=access.revalidate):
            params = self._parameters(params)
            access.require("run.execute")
            payload_hash = digest(
                {"method": "run.start", "params": params}, checkpoint=self.workspace.check_deadline
            )
            with self.workspace.transaction() as connection:
                row = find_receipt(connection, access.client_id, request_id)
                replay = replay_receipt(
                    row, "run.start", payload_hash, self.workspace.check_deadline
                )
            if replay is not None:
                snapshot = self.store.read(row["subject_id"])["snapshot"]
                access.check_snapshot(snapshot)
                return replay
            plan = self._prepare(access, params)
            return self.store.create(plan, request_id, authorize=access.check_snapshot)
