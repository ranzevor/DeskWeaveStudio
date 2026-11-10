from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from deskweave.contracts import validate
from deskweave.errors import DeskWeaveError
from deskweave.json import loads


@dataclass(frozen=True, slots=True)
class RunPlan:
    content: bytes = field(repr=False)
    request_hash: str

    def __post_init__(self):
        if (
            type(self.content) is not bytes
            or type(self.request_hash) is not str
            or (
                len(self.request_hash) != 64
                or any(c not in "0123456789abcdef" for c in self.request_hash)
            )
        ):
            raise ValueError("运行计划内容或请求摘要无效。")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    def value(self, *, checkpoint=lambda: None) -> dict:
        value = loads(self.content, max_bytes=33_554_432, checkpoint=checkpoint)
        validate_snapshot(value, checkpoint=checkpoint)
        return value


def validate_snapshot(value, *, checkpoint=lambda: None):
    validate("urn:deskweave:run-plan:1", value, checkpoint=checkpoint)
    workflows, assets = set(), set()
    for item in value["workflows"]:
        checkpoint()
        key = (item["workflowId"], item["revision"])
        if key in workflows:
            raise DeskWeaveError("INVALID_ARGUMENT", "运行计划包含重复流程版本。")
        workflows.add(key)
    for item in value["assets"]:
        checkpoint()
        if item["assetVersion"] in assets:
            raise DeskWeaveError("INVALID_ARGUMENT", "运行计划包含重复资产版本。")
        assets.add(item["assetVersion"])
    root = value["root"]
    if (
        (root["workflowId"], root["revision"]) not in workflows
        or len(workflows) + len(assets) > 10_000
        or value["targets"].keys() != value["targetKinds"].keys()
        or not set(value["initialState"]["sensitiveInputs"])
        <= value["initialState"]["inputs"].keys()
    ):
        raise DeskWeaveError("INVALID_ARGUMENT", "运行计划的根版本、目标或初始状态不一致。")
