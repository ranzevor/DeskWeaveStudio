from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from deskweave.adapters.assets import Assets
from deskweave.contracts import validate
from deskweave.errors import DeskWeaveError
from deskweave.json import digest, dumps

MAX_VERSIONS = 10_000
MAX_BINDINGS = 100_000


@dataclass(frozen=True, slots=True)
class WorkflowVersion:
    workflow_id: str
    revision: str
    definition_hash: str
    definition_length: int
    dependencies_hash: str
    dependencies_length: int

    def manifest(self):
        return {
            "workflowId": self.workflow_id,
            "revision": self.revision,
            "definitionHash": self.definition_hash,
            "definitionLength": self.definition_length,
            "dependenciesHash": self.dependencies_hash,
            "dependenciesLength": self.dependencies_length,
        }


@dataclass(frozen=True, slots=True)
class AssetVersion:
    asset_id: str
    version: str
    kind: str
    sha256: str
    length: int
    media_type: str

    def manifest(self):
        return {
            "assetId": self.asset_id,
            "assetVersion": self.version,
            "kind": self.kind,
            "sha256": self.sha256,
            "length": self.length,
            "mediaType": self.media_type,
        }


@dataclass(frozen=True, slots=True)
class ResolvedGraph:
    root: tuple[str, str]
    workflows: Mapping[tuple[str, str], WorkflowVersion]
    assets: Mapping[str, AssetVersion]

    def manifest(self):
        return {
            "workflows": [self.workflows[key].manifest() for key in sorted(self.workflows)],
            "assets": [self.assets[key].manifest() for key in sorted(self.assets)],
        }


class Dependencies:
    def __init__(self, workspace, authorize_workflow, authorize_asset):
        self.workspace = workspace
        self.authorize_workflow = authorize_workflow
        self.authorize_asset = authorize_asset
        self.assets = Assets(workspace)
        self._reset()

    def _reset(self, visitor=None):
        self.visitor = visitor
        self.captured = {}
        self.records = {}
        self.versions = {}
        self.checked_blobs = set()
        self.pending = deque()
        self.bindings = 0

    def _capacity(self):
        if len(self.records) + len(self.versions) >= MAX_VERSIONS:
            raise DeskWeaveError("LIMIT_EXCEEDED", "依赖版本数量超过限制。")

    def _workflow(self, identity, revision):
        self.authorize_workflow(identity)
        key = (identity, revision)
        if key not in self.records:
            self._capacity()
            record = self.workspace.read(identity, revision)
            self.records[key] = {
                "resources": record["definition"]["resources"],
                "definitionHash": record["definitionHash"],
                "dependencies": record["dependencies"],
            }
            if self.visitor is not None:
                definition = dumps(
                    record["definition"],
                    max_bytes=33_554_432,
                    checkpoint=self.workspace.check_deadline,
                )
                dependencies = dumps(
                    record["dependencies"],
                    max_bytes=33_554_432,
                    checkpoint=self.workspace.check_deadline,
                )
                self.captured[key] = WorkflowVersion(
                    identity,
                    revision,
                    record["definitionHash"],
                    len(definition),
                    hashlib.sha256(dependencies).hexdigest(),
                    len(dependencies),
                )
                self.visitor(identity, revision, record["definition"])
            self.pending.append(key)
        return self.records[key]

    def _asset(self, binding):
        self.authorize_asset()
        version = binding["assetVersion"]
        if version not in self.versions:
            self._capacity()
            record = self.assets.get(version)
            self.versions[version] = record
            blob = (record["content_hash"], record["byte_length"])
            if blob not in self.checked_blobs:
                with self.assets.blobs.open(record["content_hash"], record["byte_length"]):
                    pass
                self.checked_blobs.add(blob)
        record = self.versions[version]
        if (binding["assetId"], binding["kind"], binding["sha256"]) != (
            record["asset_id"],
            record["kind"],
            record["content_hash"],
        ):
            raise DeskWeaveError("DEPENDENCY_MISMATCH", "资产依赖与指定版本不一致。")

    def _normalize(self, resources, dependencies):
        self.workspace.check_deadline()
        if resources.keys() != dependencies.keys():
            raise DeskWeaveError("DEPENDENCY_MISMATCH", "依赖绑定必须准确覆盖流程资源。")
        result = {}
        for key, binding in dependencies.items():
            self.workspace.check_deadline()
            self.bindings += 1
            if self.bindings > MAX_BINDINGS:
                raise DeskWeaveError("LIMIT_EXCEEDED", "依赖关系数量超过限制。")
            if resources[key]["kind"] != binding["kind"]:
                raise DeskWeaveError("DEPENDENCY_MISMATCH", "依赖绑定的类型与流程资源不一致。")
            if binding["kind"] != "workflow":
                self._asset(binding)
                result[key] = dict(binding)
                continue
            record = self._workflow(binding["workflowId"], binding["revision"])
            if "definition" in binding:
                if (
                    digest(binding["definition"], checkpoint=self.workspace.check_deadline)
                    != (record["definitionHash"])
                ):
                    raise DeskWeaveError("DEPENDENCY_MISMATCH", "子流程内容与指定版本不一致。")
                embedded = self._normalize(record["resources"], binding["dependencies"])
                if embedded != record["dependencies"]:
                    raise DeskWeaveError("DEPENDENCY_MISMATCH", "子流程依赖与指定版本不一致。")
            result[key] = {field: binding[field] for field in ("kind", "workflowId", "revision")}
        return result

    def bind(self, definition, dependencies):
        self._reset()
        validate(
            "urn:deskweave:dependencies:1", dependencies, checkpoint=self.workspace.check_deadline
        )
        result = self._normalize(definition["resources"], dependencies)
        self._finish()
        return result

    def resolve(self, identity, revision, *, visitor=lambda *_args: None):
        with self.workspace.budget(None):
            self._reset(visitor)
            self._workflow(identity, revision)
            self._finish()
            assets = {
                version: AssetVersion(
                    row["asset_id"],
                    version,
                    row["kind"],
                    row["content_hash"],
                    row["byte_length"],
                    row["media_type"],
                )
                for version, row in self.versions.items()
            }
            return ResolvedGraph(
                (identity, revision),
                MappingProxyType(dict(self.captured)),
                MappingProxyType(assets),
            )

    def _finish(self):
        while self.pending:
            self.workspace.check_deadline()
            record = self.records[self.pending.popleft()]
            validate(
                "urn:deskweave:dependencies:1",
                record["dependencies"],
                checkpoint=self.workspace.check_deadline,
            )
            canonical = self._normalize(record["resources"], record["dependencies"])
            if canonical != record["dependencies"]:
                raise DeskWeaveError("STORAGE_CORRUPT", "已保存的依赖格式无效。")
        degrees = dict.fromkeys(self.records, 0)
        for record in self.records.values():
            for binding in record["dependencies"].values():
                self.workspace.check_deadline()
                if binding["kind"] == "workflow":
                    degrees[(binding["workflowId"], binding["revision"])] += 1
        ready = deque(key for key, degree in degrees.items() if not degree)
        visited = 0
        while ready:
            key = ready.popleft()
            visited += 1
            for binding in self.records[key]["dependencies"].values():
                self.workspace.check_deadline()
                if binding["kind"] == "workflow":
                    child = (binding["workflowId"], binding["revision"])
                    degrees[child] -= 1
                    if not degrees[child]:
                        ready.append(child)
        if visited != len(degrees):
            raise DeskWeaveError("DEPENDENCY_CYCLE", "流程版本依赖存在循环。")
