from __future__ import annotations

import copy
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from deskweave.adapters.workspace import Workspace, utc_time
from deskweave.contracts import method_schema, schema_contents, validate
from deskweave.domain.values import evaluate
from deskweave.domain.workflow import validate_workflow
from deskweave.engine.pagination import Pages
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, digest, dumps, loads


@dataclass(frozen=True)
class Session:
    id: str
    client_id: str
    permissions: frozenset[str]
    workflow_ids: frozenset[str] | None
    local: bool
    authorization_version: int = 1


@dataclass(frozen=True)
class Method:
    handler: Callable
    permissions: frozenset[str]
    defaults: dict
    local: bool = False
    params_bytes: int = MAX_FRAME
    result_bytes: int = MAX_FRAME


class Engine:
    def __init__(self, workspace: Workspace, *, clock=time.time, monotonic=time.monotonic):
        self.workspace = workspace
        self.clock = clock
        self.monotonic = monotonic
        self.pages = Pages(workspace, clock)
        self._mutex = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._collections: dict[str, dict] = {}
        self._methods: dict[str, Method] = {}
        self.register(
            "expression.preview",
            self._preview,
            {"workflow.read"},
            {"context": {}, "expectedType": "any"},
            local=True,
        )
        self.register("workflow.save", self._save, {"workflow.write"})
        self.register("workflow.read", self._read, {"workflow.read"}, {"contentMode": "auto"})
        self.register("workflow.validate", self._validate, {"workflow.read"})
        self.register(
            "workflow.revisions",
            self._revisions,
            {"workflow.read"},
            {"cursor": None, "pageSize": 50},
        )
        self.register(
            "workspace.catalog",
            self._catalog,
            set(),
            {
                "cursor": None,
                "pageSize": 50,
                "nameContains": "",
                "states": [],
                "order": "updatedDesc",
            },
        )
        self.register(
            "query.read",
            self._query,
            {"workflow.read"},
            {"filter": {}, "order": "asc", "cursor": None, "pageSize": 50, "includeTotal": False},
        )
        self.register("query.close", self._close_query, set())
        self.register("request.status", self._request_status, set())

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(sorted(self._methods))

    def register(
        self,
        name: str,
        handler: Callable,
        permissions: set[str],
        defaults: dict | None = None,
        *,
        local: bool = False,
    ) -> None:
        if name in self._methods:
            raise ValueError("服务方法重复：" + name)
        from deskweave.contracts import contract_registry

        schemas, _ = contract_registry()
        if name not in schemas["urn:deskweave:service:1"]["$defs"]:
            raise ValueError("服务方法不在公共契约中：" + name)
        self._methods[name] = Method(
            handler,
            frozenset(permissions),
            defaults or {},
            local,
            schema_contents(method_schema(name, "params")).get("x-maxUtf8Bytes", MAX_FRAME),
            schema_contents(method_schema(name, "result")).get("x-maxUtf8Bytes", MAX_FRAME),
        )

    def open_session(
        self,
        client_id: str,
        permissions: set[str],
        *,
        local: bool = False,
        workflow_ids: set[str] | None = None,
    ) -> Session:
        with self._mutex:
            if self.workspace.closed:
                raise DeskWeaveError("WORKSPACE_CLOSED", "工作空间已关闭。")
            if not client_id or len(client_id) > 128:
                raise ValueError("客户端身份长度应为 1～128。")
            session = Session(
                secrets.token_urlsafe(24),
                client_id,
                frozenset(permissions),
                None if workflow_ids is None else frozenset(workflow_ids),
                local,
            )
            self._sessions[session.id] = session
            return session

    def close(self) -> None:
        with self._mutex:
            self._sessions.clear()
            self._collections.clear()
            self.workspace.close()

    def revoke_client(self, client_id: str) -> None:
        with self._mutex:
            for identity, session in tuple(self._sessions.items()):
                if session.client_id == client_id:
                    self.close_session(identity)

    def close_session(self, session_id: str) -> None:
        with self._mutex:
            session = self._sessions.pop(session_id, None)
            if session:
                if not self.workspace.closed:
                    self.pages.release(session.id)
                self._collections = {
                    key: value
                    for key, value in self._collections.items()
                    if value["owner"] != session.id
                }

    def dispatch(self, session_id: str, request: dict) -> dict:
        reply_id = request.get("id", "invalid") if type(request) is dict else "invalid"
        if type(reply_id) is not str or not 1 <= len(reply_id) <= 128:
            reply_id = "invalid"
        try:
            reply_id.encode("utf-8")
        except UnicodeEncodeError:
            reply_id = "invalid"
        header = {"protocolVersion": "1.0", "kind": "response", "replyTo": reply_id}
        started = self.monotonic()
        try:
            dumps(request)
            validate("urn:deskweave:message:1#/$defs/requestEnvelope", request)
            with self._mutex:
                session = self._sessions.get(session_id)
                if session is None or request["session"] != session_id:
                    raise DeskWeaveError("ACCESS_DENIED", "客户端会话无效。")
                if request["workspace"] != self.workspace.id:
                    raise DeskWeaveError("ACCESS_DENIED", "请求不属于当前工作空间。")
                method = self._methods.get(request["method"])
                if method is None:
                    raise DeskWeaveError("METHOD_UNAVAILABLE", "此服务方法尚未实现。")
                if (
                    not method.permissions <= session.permissions
                    or method.local
                    and not session.local
                ):
                    raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有此操作权限。")
                deadline = started + request["deadlineMs"] / 1000
                if request["method"] == "expression.preview":
                    deadline = min(deadline, started + 1)
                if request["method"] in {"query.read", "workspace.catalog", "workflow.revisions"}:
                    deadline = min(
                        deadline, started + (5 if request["params"].get("includeTotal") else 2)
                    )
                with self.workspace.budget(deadline, self.monotonic):
                    dumps(
                        request["params"],
                        max_bytes=method.params_bytes,
                        checkpoint=self.workspace.check_deadline,
                    )
                    validate(method_schema(request["method"], "params"), request["params"])
                    self.workspace.check_deadline()
                    params = copy.deepcopy(method.defaults)
                    params.update(copy.deepcopy(request["params"]))
                    if "workflowId" in params:
                        self._authorize_workflow(session, params["workflowId"])
                    result = method.handler(session, params, request["id"])
                    self.workspace.check_deadline()
                    try:
                        validate(method_schema(request["method"], "result"), result)
                    except DeskWeaveError as error:
                        raise DeskWeaveError(
                            "INTERNAL_ERROR", "服务结果不符合接口要求。"
                        ) from error
                    dumps(
                        result,
                        max_bytes=method.result_bytes,
                        checkpoint=self.workspace.check_deadline,
                    )
                    response = {**header, "ok": True, "result": result}
                    dumps(response, checkpoint=self.workspace.check_deadline)
            return response
        except DeskWeaveError as error:
            response = {**header, "ok": False, "error": error.as_dict()}
        except (OSError, sqlite3.Error):
            response = {
                **header,
                "ok": False,
                "error": DeskWeaveError(
                    "STORAGE_UNAVAILABLE",
                    "工作空间存储不可用，请检查目录访问和剩余空间。",
                    retry="afterQuery",
                ).as_dict(),
            }
        try:
            dumps(response)
        except DeskWeaveError as error:
            response = {**header, "ok": False, "error": error.as_dict()}
        return response

    @staticmethod
    def _authorize_workflow(session: Session, workflow_id: str) -> None:
        if session.workflow_ids is not None and workflow_id not in session.workflow_ids:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有此流程的访问权限。")

    def _preview(self, _session, params, _request_id):
        return {
            "value": evaluate(
                params["value"],
                params["context"],
                params["expectedType"],
                checkpoint=self.workspace.check_deadline,
            )
        }

    def _save(self, session, params, request_id):
        if "modelRef" in params:
            raise DeskWeaveError("METHOD_UNAVAILABLE", "内容引用保存服务尚未实现。")
        if params["workflowId"] != params["definition"]["id"]:
            raise DeskWeaveError("INVALID_ARGUMENT", "流程定义与保存目标不一致。")
        diagnostics = validate_workflow(
            params["definition"], checkpoint=self.workspace.check_deadline
        )
        if diagnostics:
            raise DeskWeaveError("INVALID_WORKFLOW", "流程存在结构或连接错误，请先校验流程。")
        if params["definition"]["resources"] or params["dependencies"]:
            raise DeskWeaveError("METHOD_UNAVAILABLE", "资源版本绑定服务尚未实现。")
        return self.workspace.save(session.client_id, request_id, params)

    def _read(self, _session, params, _request_id):
        if params["contentMode"] == "resource":
            raise DeskWeaveError("METHOD_UNAVAILABLE", "资源内容读取服务尚未实现。")
        return self.workspace.read(params["workflowId"], params["revision"])

    def _validate(self, session, params, _request_id):
        definition = params.get("definition")
        workflow_id = params.get("workflowId")
        if definition is None:
            definition = self.workspace.read(workflow_id, params["revision"])["definition"]
        else:
            identity = definition.get("id")
            if type(identity) is str and 1 <= len(identity) <= 128:
                self._authorize_workflow(session, identity)
                workflow_id = identity
        diagnostics = validate_workflow(definition, checkpoint=self.workspace.check_deadline)
        version = digest(definition)
        self.workspace.check_deadline()
        now = self.clock()
        active = self.pages.active_collections()
        self._collections = {
            key: value for key, value in self._collections.items() if value["expires"] > now
        }
        owned = [key for key, value in self._collections.items() if value["owner"] == session.id]
        if len(owned) >= 8 or len(self._collections) >= 32:
            candidates = owned if len(owned) >= 8 else self._collections
            oldest = next((key for key in candidates if key not in active), None)
            if oldest is None:
                raise DeskWeaveError("LIMIT_EXCEEDED", "诊断集合数量已满，请关闭已有诊断集合。")
            del self._collections[oldest]
        identity = uuid.uuid4().hex
        self._collections[identity] = {
            "owner": session.id,
            "workflow": workflow_id,
            "version": version,
            "expires": now + 300,
            "items": diagnostics,
        }
        return {
            "valid": not diagnostics,
            "diagnosticCount": len(diagnostics),
            "diagnosticsQuery": {
                "collectionId": identity,
                "kind": "diagnostics",
                "itemCount": len(diagnostics),
                "expiresAt": utc_time(now + 300),
            },
        }

    def _revisions(self, session, params, _request_id):
        binding = digest({"method": "workflow.revisions", **params, "cursor": None})

        def factory(connection):
            workflow = connection.execute(
                "SELECT head_revision,sequence FROM workflow WHERE id=?", (params["workflowId"],)
            ).fetchone()
            if workflow is None:
                raise DeskWeaveError("RESOURCE_NOT_FOUND", "流程不存在。")
            return {"workflowId": params["workflowId"], "watermark": workflow["sequence"]}, {
                "headRevision": workflow["head_revision"]
            }

        def project(page):
            page.update(
                workflowId=params["workflowId"], headRevision=page.pop("extra")["headRevision"]
            )
            return page

        return self.pages.read(
            session.id,
            session.authorization_version,
            binding,
            params["pageSize"],
            factory=factory,
            reader=self.workspace.revision_rows,
            project=project,
            max_bytes=self._methods["workflow.revisions"].result_bytes,
            cursor=params["cursor"],
        )

    def _catalog(self, session, params, _request_id):
        if params["kind"] != "workflow":
            raise DeskWeaveError("METHOD_UNAVAILABLE", "此对象目录服务尚未实现。")
        if "workflow.read" not in session.permissions:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有流程读取权限。")
        params["nameContains"] = unicodedata.normalize("NFC", params["nameContains"]).casefold()
        binding = digest({"method": "workspace.catalog", **params, "cursor": None})

        def factory(connection):
            watermark = connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM revision"
            ).fetchone()[0]
            return {
                "watermark": watermark,
                "order": params["order"],
                "nameContains": params["nameContains"],
                "workflowIds": None
                if session.workflow_ids is None
                else sorted(session.workflow_ids),
            }, {}

        def project(page):
            page.pop("extra")
            return page

        return self.pages.read(
            session.id,
            session.authorization_version,
            binding,
            params["pageSize"],
            factory=factory,
            reader=self.workspace.catalog_rows,
            project=project,
            max_bytes=self._methods["workspace.catalog"].result_bytes,
            cursor=params["cursor"],
        )

    def _query(self, session, params, _request_id):
        collection = self._collections.get(params["collectionId"])
        if (
            collection is None
            or collection["owner"] != session.id
            or params["kind"] != "diagnostics"
        ):
            raise DeskWeaveError("COLLECTION_NOT_FOUND", "结果集合不存在。")
        if collection["workflow"] is not None:
            self._authorize_workflow(session, collection["workflow"])
        if collection["expires"] <= self.clock():
            raise DeskWeaveError("CURSOR_EXPIRED", "结果集合已过期，请重新校验流程。")
        filters = {"text": "", "codes": [], "nodeId": None, **params["filter"]}
        filters["text"] = unicodedata.normalize("NFC", filters["text"]).casefold()
        filters["codes"] = sorted(set(filters["codes"]))
        params["filter"] = filters
        binding = digest({"method": "query.read", **params, "cursor": None})

        def matches(row):
            self.workspace.check_deadline()
            return (
                (
                    not filters["text"]
                    or any(
                        filters["text"] in unicodedata.normalize("NFC", row[key]).casefold()
                        for key in ("message", "code")
                    )
                )
                and (not filters["codes"] or row["code"] in filters["codes"])
                and (filters["nodeId"] is None or row.get("nodeId") == filters["nodeId"])
            )

        def factory(_connection):
            count = (
                sum(matches(row) for row in collection["items"]) if params["includeTotal"] else None
            )
            return {}, {
                "count": count,
                "sourceVersion": collection["version"],
                "collectionId": params["collectionId"],
            }

        def reader(_connection, _source, position, _limit):
            if params["order"] == "desc":
                indices = range(
                    len(collection["items"]) - 1 if position is None else position - 1, -1, -1
                )
            else:
                indices = range(0 if position is None else position + 1, len(collection["items"]))
            for index in indices:
                row = collection["items"][index]
                if matches(row):
                    yield index, {"index": index, "value": row}

        def project(page):
            return {
                "collectionId": params["collectionId"],
                "kind": "diagnostics",
                "querySnapshot": {
                    key: page[key] for key in ("snapshotId", "snapshotAt", "expiresAt")
                }
                | {"sourceVersion": page["extra"]["sourceVersion"]},
                "items": page["items"],
                "nextCursor": page["nextCursor"],
                "matchedCount": page["extra"]["count"],
                "countState": "exact" if params["includeTotal"] else "notComputed",
            }

        return self.pages.read(
            session.id,
            session.authorization_version,
            binding,
            params["pageSize"],
            factory=factory,
            reader=reader,
            project=project,
            max_bytes=self._methods["query.read"].result_bytes,
            cursor=params["cursor"],
            expires_at=collection["expires"],
        )

    def _close_query(self, session, params, _request_id):
        if "snapshotId" in params:
            return self.pages.close(session.id, params["snapshotId"])
        identity = params["collectionId"]
        self.pages.release_collection(session.id, identity)
        collection = self._collections.get(identity)
        if collection is not None and collection["owner"] == session.id:
            del self._collections[identity]
        return {"closed": True}

    def _request_status(self, session, params, _request_id):
        with self.workspace.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM command_receipt WHERE client_id=? AND request_id=?",
                (session.client_id, params["requestId"]),
            ).fetchone()
        if row is not None:
            self._authorize_workflow(session, row["workflow_id"])
            if "workflow.write" not in session.permissions:
                raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有此请求的访问权限。")
        return {
            "requestId": params["requestId"],
            "state": "finished" if row else "notReceived",
            "operationId": None,
            "result": loads(row["result"]) if row else None,
        }
