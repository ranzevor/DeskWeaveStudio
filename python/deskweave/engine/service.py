from __future__ import annotations

import copy
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

import rfc8785

from deskweave.adapters.assets import Assets
from deskweave.adapters.content import MAX_CONTENT, Content, ContentStore
from deskweave.adapters.dependencies import Dependencies
from deskweave.adapters.execution import ExecutionStore
from deskweave.adapters.receipts import find_receipt
from deskweave.adapters.run_commands import RunCommand
from deskweave.adapters.workspace import Workspace, utc_time
from deskweave.contracts import method_schema, schema_contents, validate
from deskweave.domain.platform import current_platform_tag
from deskweave.domain.values import evaluate
from deskweave.domain.workflow import validate_workflow
from deskweave.engine.inspection import Inspection
from deskweave.engine.pagination import Pages
from deskweave.engine.run_api import RunAPI
from deskweave.engine.run_events import RunEvents
from deskweave.engine.run_manager import RunManager
from deskweave.engine.run_planning import DEFAULT_LIMITS, RunAccess
from deskweave.engine.run_queries import RunQueries
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, digest, dumps, loads


def _engine_version() -> str:
    try:
        return package_version("deskweave")
    except PackageNotFoundError:
        return "0.1.0"


@dataclass(frozen=True, slots=True)
class _PendingCapabilities:
    base: dict


@dataclass(frozen=True)
class Session:
    id: str
    client_id: str
    permissions: frozenset[str]
    workflow_ids: frozenset[str] | None
    local: bool
    authorization_version: int = 1
    capabilities: frozenset[str] = frozenset()
    revalidate: Callable = field(default=lambda: None, repr=False, compare=False)


@dataclass(frozen=True)
class Method:
    handler: Callable
    permissions: frozenset[str]
    defaults: dict
    local: bool = False
    params_bytes: int = MAX_FRAME
    result_bytes: int = MAX_FRAME


class Engine:
    def __init__(
        self, workspace: Workspace, *, clock=time.time, monotonic=time.monotonic, threaded=True
    ):
        self.workspace = workspace
        self.clock = clock
        self.monotonic = monotonic
        self.pages = Pages(workspace, clock)
        self.contents = ContentStore(
            clock=clock, monotonic=lambda: self.monotonic(), checkpoint=workspace.check_deadline
        )
        self._mutex = threading.RLock()
        self._desktop_lock = threading.Lock()
        self._desktop_stopping = threading.Event()
        self._desktop_agent = None
        self._desktop_caps_future: Future | None = None
        self._desktop_caps_agent = None
        self._revocations = {}
        self._draining = False
        self._frame_limit = MAX_FRAME
        self._response_overhead = 256
        self.runs = RunManager(workspace, clock=monotonic, threaded=threaded)
        self.runs.lifecycle.recover()
        self.run_api = RunAPI(self)
        self.run_queries = RunQueries(self)
        self.inspection = Inspection(self)
        self.events = RunEvents(self)
        self._sessions: dict[str, Session] = {}
        self._closing_sessions: dict[str, Session] = {}
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
            set(),
            {"filter": {}, "order": "asc", "cursor": None, "pageSize": 50, "includeTotal": False},
        )
        self.register("query.close", self._close_query, set())
        self.register("request.status", self._request_status, set())
        self.register("content.begin", self._content_begin, set())
        self.register("content.write", self._content_write, set())
        self.register("content.seal", self._content_seal, set())
        self.register("content.discard", self._content_discard, set())
        self.register("resource.read", self._resource_read, set())
        self.register("asset.read", self._asset_read, {"asset.read"})
        self.register("run.prepare", self.run_api.prepare, {"run.execute"})
        self.register("run.start", self.run_api.start, {"run.execute"})
        for method in ("run.pause", "run.resume", "run.stop"):
            self.register(method, self.run_api.control, {"run.control"})
        self.register(
            "run.snapshot",
            lambda session, params, _id: self.run_queries.snapshot(session, params["runId"]),
            {"run.read"},
        )
        self.register(
            "debug.variables.read",
            lambda session, params, _id: self.inspection.read(session, params),
            {"run.read"},
            {"cursor": None, "pageSize": 50},
        )
        self.register("event.subscribe", self.events.subscribe, {"run.read"})
        self.register("event.read", self.events.read, {"run.read"})
        self.register("event.unsubscribe", self.events.unsubscribe, set())
        self.register("system.capabilities", self._system_capabilities, set())

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
        capabilities: set[str] | None = None,
        authorization_version: int = 1,
        revalidate: Callable | None = None,
    ) -> Session:
        with self._mutex:
            if self.workspace.closed or self._draining:
                raise DeskWeaveError("WORKSPACE_CLOSED", "工作空间已关闭。")
            if not client_id or len(client_id) > 128:
                raise ValueError("客户端身份长度应为 1～128。")
            if (
                type(authorization_version) is not int
                or not 1 <= authorization_version <= 9007199254740991
            ):
                raise ValueError("授权版本无效。")
            revoked = (
                self._revocations.setdefault(client_id, threading.Event())
                if revalidate is None
                else threading.Event()
            )

            def current():
                if revoked.is_set():
                    raise DeskWeaveError("ACCESS_DENIED", "客户端授权已撤销。")
                if revalidate is not None:
                    revalidate()

            current()
            session = Session(
                secrets.token_urlsafe(24),
                client_id,
                frozenset(permissions),
                None if workflow_ids is None else frozenset(workflow_ids),
                local,
                authorization_version=authorization_version,
                capabilities=frozenset(capabilities or ()),
                revalidate=current,
            )
            self._sessions[session.id] = session
            return session

    def run_access(self, session):
        return RunAccess(
            session.client_id,
            session.client_id,
            session.authorization_version,
            session.permissions,
            session.workflow_ids,
            revalidate=session.revalidate,
        )

    def result_limit(self, method):
        return min(self._methods[method].result_bytes, self._frame_limit - self._response_overhead)

    @property
    def has_work(self):
        return self.runs.busy.is_set()

    def begin_idle_shutdown(self):
        with self._mutex:
            if self.has_work:
                return False
            self._draining = True
            self._desktop_stopping.set()
            return True

    def start_desktop_agent(
        self, local, installation, authority, *, timeout=10, cancel=None, idle_grace=2.0
    ):
        from deskweave.agent import start_agent

        return start_agent(
            self,
            local,
            installation,
            authority,
            timeout=timeout,
            cancel=cancel,
            idle_grace=idle_grace,
        )

    def close(self) -> None:
        with self._mutex:
            self._draining = True
            self._desktop_stopping.set()
        failures = []
        with self._desktop_lock:
            self._desktop_caps_future = None
            self._desktop_caps_agent = None
            if self._desktop_agent is not None:
                try:
                    self._desktop_agent.close()
                except BaseException as error:
                    failures.append(error)
                else:
                    self._desktop_agent = None
        try:
            self.runs.close()
        except BaseException as error:
            failures.append(error)
        with self._mutex:
            for owner in tuple(self._sessions):
                try:
                    self.close_session(owner)
                except BaseException as error:
                    failures.append(error)
            self._collections.clear()
            self.run_api.plans.clear()
            self.run_queries.collections.clear()
            try:
                self.contents.close()
                self._closing_sessions.clear()
            except BaseException as error:
                failures.append(error)
            finally:
                self.workspace.close()
        self._raise_cleanup(failures)

    def client_exited(self, client_id):
        self.runs.client_exited(client_id)

    def revoke_client(self, client_id: str) -> None:
        with self._mutex:
            revoked = self._revocations.pop(client_id, None)
            if revoked is not None:
                revoked.set()
            self.runs.revoke_client(client_id)
            failures = []
            for session in tuple({**self._sessions, **self._closing_sessions}.values()):
                if session.client_id == client_id:
                    try:
                        self.close_session(session.id)
                    except BaseException as error:
                        failures.append(error)
            self._raise_cleanup(failures)

    @staticmethod
    def _raise_cleanup(failures) -> None:
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("会话资源关闭失败。", failures)

    def close_session(self, session_id: str) -> None:
        with self._mutex:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                self._closing_sessions[session.id] = session
            failures = []
            releases = [
                self.inspection.release,
                self.run_api.release,
                self.run_queries.release,
                self.events.release,
                self.contents.release,
            ]
            if not self.workspace.closed:
                releases.append(self.pages.release)
            for release in releases:
                try:
                    release(session_id)
                except BaseException as error:
                    failures.append(error)
            self._collections = {
                key: value
                for key, value in self._collections.items()
                if value["owner"] != session_id
            }
            self._raise_cleanup(failures)
            self._closing_sessions.pop(session_id, None)

    def dispatch(
        self,
        session_id: str,
        request: dict,
        *,
        authorize: Callable[[], None] | None = None,
        max_frame_bytes: int = MAX_FRAME,
    ) -> dict:
        if type(max_frame_bytes) is not int or not 1024 <= max_frame_bytes <= MAX_FRAME:
            raise ValueError("连接帧上限无效。")
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
            checkpoint = authorize or (lambda: None)
            checkpoint()
            dumps(request, max_bytes=max_frame_bytes, checkpoint=checkpoint)
            validate(
                "urn:deskweave:message:1#/$defs/requestEnvelope", request, checkpoint=checkpoint
            )
            deadline = started + request["deadlineMs"] / 1000
            name = request["method"]
            if name == "expression.preview":
                deadline = min(deadline, started + 1)
            is_query = name in {
                "query.read",
                "workspace.catalog",
                "workflow.revisions",
                "run.snapshot",
                "event.subscribe",
                "event.read",
                "debug.variables.read",
            }
            if is_query:
                deadline = min(
                    deadline, started + (5 if request["params"].get("includeTotal") else 2)
                )

            def current():
                checkpoint()
                if self._sessions.get(session_id) is not session:
                    raise DeskWeaveError("ACCESS_DENIED", "客户端会话已关闭。")
                session.revalidate()

            with self._mutex:
                session = self._sessions.get(session_id)
                if session is None or request["session"] != session_id:
                    raise DeskWeaveError("ACCESS_DENIED", "客户端会话无效。")
                current()
                if request["workspace"] != self.workspace.id:
                    raise DeskWeaveError("ACCESS_DENIED", "请求不属于当前工作空间。")
                method = self._methods.get(name)
                if method is None:
                    raise DeskWeaveError("METHOD_UNAVAILABLE", "此服务方法尚未实现。")
                if (
                    not method.permissions <= session.permissions
                    or method.local
                    and not session.local
                ):
                    raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有此操作权限。")
                if self._draining and name in {"run.start", "run.prepare"}:
                    raise DeskWeaveError("WORKSPACE_CLOSED", "执行服务正在关闭。")
                self._frame_limit = max_frame_bytes
                empty = rfc8785.dumps({**header, "ok": True, "result": {}})
                self._response_overhead = len(empty) - 2
                scope = (
                    self.workspace.transaction(
                        deadline=deadline, clock=self.monotonic, authorize=current
                    )
                    if is_query
                    else self.workspace.budget(deadline, self.monotonic, authorize=current)
                )
                with scope:
                    dumps(
                        request["params"],
                        max_bytes=method.params_bytes,
                        checkpoint=self.workspace.check_deadline,
                    )
                    validate(
                        method_schema(name, "params"),
                        request["params"],
                        checkpoint=self.workspace.check_deadline,
                    )
                    params = copy.deepcopy(method.defaults)
                    params.update(copy.deepcopy(request["params"]))
                    if "workflowId" in params:
                        self._authorize_workflow(session, params["workflowId"])
                    if name in {"run.pause", "run.resume", "run.stop"}:
                        result = self.run_api.control(
                            session, name, params, request["id"], deadline, current
                        )
                    else:
                        result = method.handler(session, params, request["id"])
                    if not isinstance(result, (Future, _PendingCapabilities)):
                        response = self._response(header, name, method, result, max_frame_bytes)
            if isinstance(result, _PendingCapabilities):
                payload = result.base
                desktop = self._query_desktop_capabilities(deadline, current)
                if desktop is not None:
                    versions = dict(payload["componentVersions"])
                    if 1 <= len(desktop["backendVersion"]) <= 64:
                        versions["agent"] = desktop["backendVersion"]
                    payload = {
                        **payload,
                        "componentVersions": versions,
                        "desktop": desktop,
                    }
                with (
                    self._mutex,
                    self.workspace.budget(deadline, self.monotonic, authorize=current),
                ):
                    response = self._response(header, name, method, payload, max_frame_bytes)
            elif isinstance(result, Future):
                try:
                    result = result.result(timeout=max(0, deadline - self.monotonic()))
                except FutureTimeout:
                    raise DeskWeaveError(
                        "DEADLINE_EXCEEDED", "运行控制请求已超时。", retry="afterQuery"
                    ) from None
                with (
                    self._mutex,
                    self.workspace.budget(deadline, self.monotonic, authorize=current),
                ):
                    response = self._response(
                        header, name, method, RunCommand.result(result), max_frame_bytes
                    )
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
            dumps(response, max_bytes=max_frame_bytes)
        except DeskWeaveError as error:
            response = {**header, "ok": False, "error": error.as_dict()}
        return response

    def _response(self, header, name, method, result, max_frame_bytes):
        self.workspace.check_deadline()
        try:
            validate(
                method_schema(name, "result"), result, checkpoint=self.workspace.check_deadline
            )
        except DeskWeaveError as error:
            if error.code != "INVALID_ARGUMENT":
                raise
            raise DeskWeaveError("INTERNAL_ERROR", "服务结果不符合接口要求。") from error
        dumps(result, max_bytes=method.result_bytes, checkpoint=self.workspace.check_deadline)
        response = {**header, "ok": True, "result": result}
        dumps(response, max_bytes=max_frame_bytes, checkpoint=self.workspace.check_deadline)
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
        request_params = None
        if "modelRef" in params:
            self._require_content(session)
            receipt = self.workspace.save_receipt(session.client_id, request_id, params)
            if receipt is not None:
                return receipt
            item = self.contents.resource(session.id, params["modelRef"]["resourceId"])
            self._authorize_content(session, item)
            model = self.contents.resolve(item, params["modelRef"], "workflowModel")
            request_params = params
            params = {key: value for key, value in params.items() if key != "modelRef"} | model
        if params["workflowId"] != params["definition"]["id"]:
            raise DeskWeaveError("INVALID_ARGUMENT", "流程定义与保存目标不一致。")
        diagnostics = validate_workflow(
            params["definition"], checkpoint=self.workspace.check_deadline
        )
        if diagnostics:
            raise DeskWeaveError("INVALID_WORKFLOW", "流程存在结构或连接错误，请先校验流程。")
        return self.workspace.save(
            session.client_id,
            request_id,
            params,
            request_params=request_params,
            authorize_workflow=lambda identity: self._authorize_dependency_workflow(
                session, identity
            ),
            authorize_asset=lambda: self._authorize_asset(session),
        )

    def _read(self, session, params, _request_id):
        mode = params["contentMode"]
        if mode == "resource":
            self._require_content(session)
        result = self.workspace.read(params["workflowId"], params["revision"])
        Dependencies(
            self.workspace,
            lambda identity: self._authorize_dependency_workflow(session, identity),
            lambda: self._authorize_asset(session),
        ).bind(result["definition"], result["dependencies"])
        if mode == "inline" or "contentRefsV1" not in session.capabilities:
            return result
        model = {key: result[key] for key in ("definition", "dependencies")}
        data = dumps(model, max_bytes=MAX_CONTENT, checkpoint=self.workspace.check_deadline)
        if mode == "auto" and len(data) <= 262144:
            return result
        resource = self.contents.export(session.id, params["workflowId"], data)
        return {key: result[key] for key in ("workflowId", "revision", "definitionHash")} | {
            "modelResource": resource
        }

    def _authorize_dependency_workflow(self, session, identity):
        if "workflow.read" not in session.permissions:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有子流程读取权限。")
        self._authorize_workflow(session, identity)

    @staticmethod
    def _authorize_asset(session):
        if "asset.read" not in session.permissions:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有资产读取权限。")

    def _asset_read(self, session, params, _request_id):
        self._require_content(session)
        row, data = Assets(self.workspace).read_range(
            params["assetVersion"], int(params["offset"]), int(params["length"])
        )
        return self.contents.export(
            session.id,
            None,
            data,
            permission="asset.read",
            purpose="asset",
            media_type=row["media_type"],
            asset_version=params["assetVersion"],
        )

    @staticmethod
    def _require_content(session: Session) -> None:
        if "contentRefsV1" not in session.capabilities:
            raise DeskWeaveError("CAPABILITY_REQUIRED", "此会话未协商内容引用功能。")

    @staticmethod
    def _content_permission(session: Session, purpose: str) -> str:
        permission = "workflow.write" if purpose == "workflowModel" else "asset.write"
        if permission not in session.permissions:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有上传此类内容的权限。")
        return permission

    def _authorize_content(self, session: Session, item: Content) -> None:
        self._require_content(session)
        if item.permission not in session.permissions:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有访问此内容的权限。")
        if item.inspection_id is not None:
            self.inspection.check(session, item.inspection_id)
        if item.workflow_id is not None:
            self._authorize_workflow(session, item.workflow_id)

    def _content_begin(self, session, params, _request_id):
        self._require_content(session)
        permission = self._content_permission(session, params["purpose"])
        return self.contents.begin(session.id, params, permission)

    def _content_write(self, session, params, _request_id):
        self._require_content(session)
        item = self.contents.upload(session.id, params["uploadId"])
        self._authorize_content(session, item)
        return self.contents.write(item, params)

    def _content_seal(self, session, params, _request_id):
        self._require_content(session)
        item = self.contents.upload(session.id, params["uploadId"])
        self._authorize_content(session, item)

        def validate_content(purpose, value):
            reference = (
                "urn:deskweave:content:1#/$defs/workflowModel"
                if purpose == "workflowModel"
                else "urn:deskweave:trajectory:1"
            )
            try:
                validate(reference, value, checkpoint=self.workspace.check_deadline)
            except DeskWeaveError as error:
                if error.code != "INVALID_ARGUMENT":
                    raise
                details = {"contentPath": error.details["fieldPath"]} if error.details else None
                raise DeskWeaveError(
                    "INVALID_CONTENT", "上传内容不符合所选格式。", details=details
                ) from error
            if purpose == "workflowModel":
                identity = value["definition"]["id"]
                self._authorize_workflow(session, identity)
                return identity
            return None

        return self.contents.seal(item, validate_content)

    def _content_discard(self, session, params, _request_id):
        self._require_content(session)
        item = self.contents.discard_target(session.id, params)
        if item is not None:
            self._authorize_content(session, item)
        self.contents.discard(session.id, params)
        return {"discarded": True}

    def _resource_read(self, session, params, _request_id):
        self._require_content(session)
        item = self.contents.resource(session.id, params["resourceId"])
        self._authorize_content(session, item)
        return self.contents.read(item, params["offset"], params["length"])

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
        version = digest(definition, checkpoint=self.workspace.check_deadline)
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
        if params["kind"] == "run":
            return self.run_queries.catalog(session, params)
        if params["kind"] not in {"workflow", "asset"}:
            raise DeskWeaveError("METHOD_UNAVAILABLE", "此对象目录服务尚未实现。")
        permission = "asset.read" if params["kind"] == "asset" else "workflow.read"
        if permission not in session.permissions:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有此目录的读取权限。")
        params["nameContains"] = unicodedata.normalize("NFC", params["nameContains"]).casefold()
        binding = digest({"method": "workspace.catalog", **params, "cursor": None})

        def factory(connection):
            table = "asset_version" if params["kind"] == "asset" else "revision"
            watermark = connection.execute(
                f"SELECT COALESCE(MAX(sequence),0) FROM {table}"
            ).fetchone()[0]
            return {
                "kind": params["kind"],
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
        if not ({"workflow.read", "run.read", "run.execute"} & session.permissions):
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端缺少所需权限。")
        if params["collectionId"] in self.run_queries.collections:
            return self.run_queries.read(session, params)
        collection = self._collections.get(params["collectionId"])
        if (
            collection is None
            or collection["owner"] != session.id
            or params["kind"] != "diagnostics"
        ):
            raise DeskWeaveError("COLLECTION_NOT_FOUND", "结果集合不存在。")
        if "workflow.read" not in session.permissions:
            raise DeskWeaveError("ACCESS_DENIED", "当前客户端缺少所需权限。")
        if collection["workflow"] is not None:
            self._authorize_workflow(session, collection["workflow"])
        if collection["expires"] <= self.clock():
            raise DeskWeaveError("CURSOR_EXPIRED", "结果集合已过期，请重新校验流程。")
        filters = {"text": "", "codes": [], **params["filter"]}
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
                and ("nodeId" not in filters or row.get("nodeId") == filters["nodeId"])
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
        self.run_queries.close(session.id, identity)
        self.pages.release_collection(session.id, identity)
        collection = self._collections.get(identity)
        if collection is not None and collection["owner"] == session.id:
            del self._collections[identity]
        return {"closed": True}

    def _run_desktop_capabilities(self, desktop_agent, future: Future) -> None:
        try:
            result = desktop_agent.desktop_capabilities(timeout=5, cancel=self._desktop_stopping)
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)
        finally:
            with self._desktop_lock:
                if self._desktop_caps_future is future:
                    self._desktop_caps_future = None
                    self._desktop_caps_agent = None

    def _query_desktop_capabilities(
        self, deadline: float, authorize: Callable[[], None]
    ) -> dict | None:
        authorize()
        if self._desktop_stopping.is_set():
            raise DeskWeaveError("WORKSPACE_CLOSED", "执行服务正在关闭。")
        if deadline - self.monotonic() <= 0:
            raise DeskWeaveError("DEADLINE_EXCEEDED", "请求已超过截止时间。")
        with self._desktop_lock:
            desktop_agent = (
                self._desktop_agent
                if self._desktop_agent is not None and self._desktop_agent.running
                else None
            )
            if desktop_agent is None:
                return None
            if self._desktop_stopping.is_set():
                raise DeskWeaveError("WORKSPACE_CLOSED", "执行服务正在关闭。")
            future = self._desktop_caps_future
            if future is None or self._desktop_caps_agent is not desktop_agent:
                future = Future()
                self._desktop_caps_future = future
                self._desktop_caps_agent = desktop_agent
                try:
                    threading.Thread(
                        target=self._run_desktop_capabilities,
                        args=(desktop_agent, future),
                        name="deskweave-agent-capabilities",
                        daemon=True,
                    ).start()
                except BaseException:
                    self._desktop_caps_future = None
                    self._desktop_caps_agent = None
                    raise
        while True:
            authorize()
            if self._desktop_stopping.is_set():
                raise DeskWeaveError("WORKSPACE_CLOSED", "执行服务正在关闭。")
            left = deadline - self.monotonic()
            if left <= 0:
                raise DeskWeaveError("DEADLINE_EXCEEDED", "请求已超过截止时间。")
            try:
                return copy.deepcopy(future.result(timeout=min(left, 0.05)))
            except FutureTimeout:
                continue
            except DeskWeaveError:
                if self._desktop_stopping.is_set():
                    raise DeskWeaveError("WORKSPACE_CLOSED", "执行服务正在关闭。") from None
                raise

    def _system_capabilities(self, session, _params, _request_id):
        base = {
            "platform": current_platform_tag(),
            "componentVersions": {"engine": _engine_version()},
            "actions": sorted(self.runs.ports.actions),
            "limits": {
                **DEFAULT_LIMITS,
                "maxFrameBytes": self._frame_limit,
                "maxContentBytes": MAX_CONTENT,
            },
        }
        if "platform-capabilities-v1" in session.capabilities:
            return _PendingCapabilities(base)
        return base

    def _request_status(self, session, params, _request_id):
        with self.workspace.transaction() as connection:
            row = find_receipt(connection, session.client_id, params["requestId"])
        if row is not None:
            if row["subject_kind"] == "run" and row["method"] in {
                "run.start",
                "run.pause",
                "run.resume",
                "run.stop",
            }:
                access = self.run_access(session)
                permission = "run.execute" if row["method"] == "run.start" else "run.control"
                ExecutionStore(self.workspace).check_access(row["subject_id"], access, permission)
                if (row["authorization_ref"], row["authorization_version"]) != (
                    access.authorization_ref,
                    access.authorization_version,
                ):
                    raise DeskWeaveError("ACCESS_DENIED", "当前授权不能读取此请求回执。")
            elif row["subject_kind"] == "workflow" and row["method"] == "workflow.save":
                self._authorize_workflow(session, row["subject_id"])
                if "workflow.write" not in session.permissions:
                    raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有此请求的访问权限。")
            else:
                raise DeskWeaveError("ACCESS_DENIED", "当前客户端没有此请求的访问权限。")
        return {
            "requestId": params["requestId"],
            "state": "finished" if row else "notReceived",
            "operationId": None,
            "result": loads(row["result"], checkpoint=self.workspace.check_deadline)
            if row
            else None,
        }
