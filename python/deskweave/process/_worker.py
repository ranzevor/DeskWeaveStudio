from __future__ import annotations

import heapq
import os
import sys
import threading
import time
import traceback
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from deskweave.adapters.workspace import Workspace
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.transport.authority import ConnectionAuthority
from deskweave.transport.native import NativeListener, ProcessBinding, serve_connection
from deskweave.transport.native._common import Budget, InitializationError, cleanup
from deskweave.transport.protocol import ConnectionPolicy

from . import _wire as wire


@dataclass
class Peer:
    binding: ProcessBinding
    process_id: int
    installation_digest: str
    references: int = 0
    owned: bool = True
    users: int = 0
    close_failed: bool = False


@dataclass
class Grant:
    peer: Peer
    deadline: float
    version: int


class Registry:
    def __init__(self, authority, bindings, *, max_states, max_bytes):
        self.authority = authority
        self.peers = {}
        self.grants = {}
        self.expirations = []
        self.closing = set()
        self.retry_authority = False
        self.active_users = 0
        self.accepting = True
        self.on_forget = lambda _client: None
        self.lock = threading.RLock()
        self.known = {binding.identity.process_id: binding for binding in bindings}
        self.installations = {
            binding.identity.installation_digest: binding.installation for binding in bindings
        }
        self.fixed_installations = frozenset(self.installations)
        self.installation_references = {}
        self.sizes = {
            digest: wire.manifest_size(value) for digest, value in self.installations.items()
        }
        self.bytes = sum(self.sizes.values())
        self.states = sum(len(binding.installation.components) + 1 for binding in bindings)
        self.max_states, self.max_bytes = max_states, max_bytes
        if self.states > max_states or self.bytes > max_bytes:
            raise DeskWeaveError("LIMIT_EXCEEDED", "启动安装登记超过进程容量。")

    def get(self, pid):
        with self.lock:
            value = self.peers.get(pid)
            return value.binding if value is not None and pid not in self.closing else None

    def available_states(self, identity):
        known = self.known.get(identity.process_id)
        if known is not None and known.identity == identity:
            return self.max_states
        return self.max_states - self.states

    def remember(self, credential, peer, value):
        self.grants[credential.client_id] = Grant(peer, value.expires_at, value.version)
        heapq.heappush(self.expirations, (value.expires_at, value.version, credential.client_id))
        if len(self.expirations) > 2 * len(self.grants) + 32:
            self.expirations = [
                (grant.deadline, grant.version, client) for client, grant in self.grants.items()
            ]
            heapq.heapify(self.expirations)

    def retry_closing(self):
        errors = []
        for pid in tuple(self.closing):
            with self.lock:
                peer = self.peers[pid]
                if peer.users or self.retry_authority:
                    continue
            try:
                if peer.owned:
                    peer.binding.close()
            except BaseException as error:
                peer.close_failed = True
                errors.append(error)
                continue
            with self.lock:
                self.peers.pop(pid)
                self.closing.remove(pid)
            if peer.owned:
                self.states -= len(peer.binding.installation.components) + 1
            digest = peer.installation_digest
            self.installation_references[digest] -= 1
            if not self.installation_references[digest]:
                del self.installation_references[digest]
                if digest not in self.fixed_installations:
                    self.installations.pop(digest)
                    self.bytes -= self.sizes.pop(digest)
        return errors

    def forget(self, client):
        grant = self.grants.pop(client, None)
        if grant is not None:
            with self.lock:
                grant.peer.references -= 1
                if not grant.peer.references:
                    self.closing.add(grant.peer.process_id)
            self.on_forget(client)
        errors = self.retry_closing()
        if errors:
            raise BaseExceptionGroup("客户端进程引用关闭失败。", errors)

    def prune(self):
        now = self.authority.now()
        if (
            self.retry_authority
            or self.authority._closing_snapshot()
            or self.expirations
            and self.expirations[0][0] <= now
        ):
            try:
                self.authority.sweep()
                self.retry_authority = False
            except BaseException:
                self.retry_authority = True
                raise
        errors = self.retry_closing()
        while self.expirations and self.expirations[0][0] <= now:
            deadline, version, client = heapq.heappop(self.expirations)
            current = self.grants.get(client)
            if current is not None and (current.deadline, current.version) == (deadline, version):
                try:
                    self.forget(client)
                except BaseException as error:
                    errors.append(error)
        return errors

    def retain(self, binding, pid, digest, size, *, owned):
        peer = Peer(binding, pid, digest, owned=owned)
        if owned:
            self.states += len(binding.installation.components) + 1
        if digest not in self.installations:
            self.bytes += size
            self.sizes[digest] = size
            self.installations[digest] = binding.installation
        self.installation_references[digest] = self.installation_references.get(digest, 0) + 1
        with self.lock:
            self.closing.add(pid)
            self.peers[pid] = peer
        return peer

    def add(self, identity, installation, policy, lifetime, budget, *, not_after=None):
        self.authority._peer(identity)
        self.prune()
        if len(self.grants) >= self.authority.max_clients:
            raise DeskWeaveError("LIMIT_EXCEEDED", "客户端登记数量已满。")
        if identity.process_id in self.closing and self.peers[identity.process_id].close_failed:
            raise DeskWeaveError("RESOURCE_CLOSE_FAILED", "客户端进程引用正在释放。")
        with self.lock:
            peer = self.peers.get(identity.process_id)
        created = peer is None
        if created:
            if len(self.peers) >= self.authority.max_clients:
                raise DeskWeaveError("LIMIT_EXCEEDED", "进程登记数量已满。")
            known = self.known.get(identity.process_id)
            if known is not None and known.identity != identity:
                known = None
            actual = known.installation if known is not None else installation
            digest = (
                known.identity.installation_digest
                if known is not None
                else actual.calculate_digest(budget.check_time)
            )
            actual = self.installations.get(digest, actual)
            if known is None and len(actual.components) + 1 > self.available_states(identity):
                raise DeskWeaveError("LIMIT_EXCEEDED", "进程文件状态登记额度已满。")
            size = self.sizes.get(digest)
            if size is None:
                size = wire.manifest_size(actual, budget.check_time)
                if self.bytes + size > self.max_bytes:
                    raise DeskWeaveError("LIMIT_EXCEEDED", "安装登记元数据额度已满。")
            binding = known
            if binding is None:
                try:
                    binding = ProcessBinding(
                        identity.process_id,
                        actual,
                        instance_epoch=identity.instance_epoch,
                        timeout=max(budget.deadline - time.monotonic(), 1e-9),
                        cancel=budget.cancel,
                    )
                except InitializationError as error:
                    peer = self.retain(
                        error.resource, identity.process_id, digest, size, owned=True
                    )
                    peer.close_failed = True
                    raise
            peer = self.retain(binding, identity.process_id, digest, size, owned=known is None)
        credential = None
        try:
            if peer.binding.identity != identity:
                raise DeskWeaveError("AUTHENTICATION_FAILED", "客户端进程登记不匹配。")
            peer.binding.check(budget.check_time)
            credential = self.authority.authorize(policy, lifetime=lifetime, not_after=not_after)
            snapshot = self.authority.describe(credential)
            peer.references += 1
            with self.lock:
                self.closing.discard(identity.process_id)
            self.remember(credential, peer, snapshot)
            return credential
        except BaseException as error:
            errors = []
            if credential is not None:
                try:
                    self.authority.revoke(credential)
                except BaseException as closing:
                    errors.append(closing)
            if created and not peer.references:
                errors.extend(self.retry_closing())
            if errors:
                raise BaseExceptionGroup("客户端登记与清理失败。", [error, *errors]) from None
            raise

    def selected(self, credential):
        self.authority.describe(credential)
        grant = self.grants.get(credential.client_id)
        if grant is None:
            raise DeskWeaveError("ACCESS_DENIED", "客户端登记已结束。")
        return grant

    def revise(self, credential, policy, expected_version, lifetime, *, not_after=None):
        grant = self.selected(credential)
        failure = None
        try:
            version = self.authority.revise(
                credential,
                policy,
                expected_version=expected_version,
                lifetime=lifetime,
                not_after=not_after,
            )
        except BaseExceptionGroup as error:
            self.retry_authority = True
            failure = error
            version = None
        try:
            snapshot = self.authority.describe(credential)
        except DeskWeaveError as error:
            try:
                self.forget(credential.client_id)
            except BaseException as closing:
                raise BaseExceptionGroup(
                    "授权更新与登记清理失败。", [failure or error, closing]
                ) from None
            if failure is not None:
                raise failure from None
            raise
        self.remember(credential, grant.peer, snapshot)
        if failure is not None:
            raise failure
        return version

    def revoke(self, credential):
        return self._revoke(credential.client_id, lambda: self.authority.revoke(credential))

    def revoke_clients(self, clients):
        selected = tuple(clients)
        failure = None
        try:
            self.authority.revoke_clients(selected)
        except BaseExceptionGroup as error:
            self.retry_authority = True
            failure = error
        try:
            cleanup(*(lambda client=client: self.forget(client) for client in selected))
        except BaseException as error:
            if failure is not None:
                raise BaseExceptionGroup("授权撤销与登记清理失败。", [failure, error]) from None
            raise
        if failure is not None:
            raise failure

    def _revoke(self, client, revoke):
        failure = None
        try:
            revoke()
        except BaseExceptionGroup as error:
            self.retry_authority = True
            failure = error
        try:
            self.forget(client)
        except BaseException as error:
            if failure is not None:
                raise BaseExceptionGroup("授权撤销与登记清理失败。", [failure, error]) from None
            raise
        if failure is not None:
            raise failure

    def acquire_peer(self, pid):
        with self.lock:
            peer = self.peers.get(pid)
            if peer is None or pid in self.closing or not self.accepting:
                return None
            peer.users += 1
            self.active_users += 1
            return peer

    def release_peer(self, peer):
        with self.lock:
            peer.users -= 1
            self.active_users -= 1

    def stop_when_idle(self):
        with self.lock:
            if self.active_users:
                return False
            self.accepting = False
            return True

    def close(self):
        self.retry_authority = False
        self.grants.clear()
        self.expirations.clear()
        self.closing.update(self.peers)
        errors = self.retry_closing()
        if errors:
            raise BaseExceptionGroup("进程登记资源关闭失败。", errors)


class Admission:
    def __init__(self, registry):
        self.registry = registry
        self.peer = None

    def get(self, pid):
        self.peer = self.registry.acquire_peer(pid)
        return self.peer.binding if self.peer is not None else None

    def close(self):
        if self.peer is not None:
            peer, self.peer = self.peer, None
            self.registry.release_peer(peer)


def policy(value):
    wire.fields(value, ("permissions", "workflowIds", "local"))
    return ConnectionPolicy(value["permissions"], value["workflowIds"], value["local"])


class Runtime:
    def __init__(self, listener, authority, parent, *, max_states, max_bytes):
        self.listener = listener
        self.authority = authority
        self.registry = Registry(
            authority, (listener.local, parent), max_states=max_states, max_bytes=max_bytes
        )
        self.cancel = threading.Event()
        self.condition = threading.Condition()
        self.workers = set()
        self.failed_channels = {}
        self.capacity = threading.BoundedSemaphore(authority.max_connections)
        self.failures = 0
        self.pending = None
        self.acceptor = None

    def record_failure(self, error):
        with self.condition:
            self.failures = min(self.failures + 1, 2147483647)
        traceback.print_exception(error, file=sys.stderr)

    def serve(self, channel, admission):
        try:
            serve_connection(channel, self.authority)
        except DeskWeaveError:
            pass
        except BaseException as error:
            self.record_failure(error)
        finally:
            for error in self.finish_connection(channel, admission):
                self.record_failure(error)
            with self.condition:
                self.workers.discard(threading.current_thread())
                self.condition.notify_all()

    def finish_connection(self, channel, admission):
        if channel is not None:
            try:
                channel.close()
            except BaseException as error:
                with self.condition:
                    self.failed_channels[channel] = admission
                return [error]
        with self.condition:
            self.failed_channels.pop(channel, None)
        admission.close()
        self.capacity.release()
        return []

    def retry_connections(self):
        with self.condition:
            pending = tuple(self.failed_channels.items())
        errors = []
        for channel, admission in pending:
            errors.extend(self.finish_connection(channel, admission))
        return errors

    def accept(self):
        while not self.cancel.is_set():
            if not self.capacity.acquire(timeout=0.1):
                continue
            transferred = False
            channel = None
            admission = Admission(self.registry)
            try:
                channel = self.listener.accept(admission, cancel=self.cancel)
                worker = threading.Thread(
                    target=self.serve, args=(channel, admission), name="deskweave-client"
                )
                try:
                    with self.condition:
                        self.workers.add(worker)
                    worker.start()
                except BaseException:
                    with self.condition:
                        self.workers.discard(worker)
                    channel.close()
                    raise
                transferred = True
            except DeskWeaveError as error:
                if error.code not in {
                    "DEADLINE_EXCEEDED",
                    "CANCELLED",
                    "AUTHENTICATION_FAILED",
                    "TRANSPORT_CLOSED",
                }:
                    self.record_failure(error)
            except InitializationError as error:
                channel = error.resource
                self.record_failure(error)
            except BaseException as error:
                if not self.cancel.is_set():
                    self.record_failure(error)
                    self.cancel.set()
            finally:
                if not transferred:
                    for error in self.finish_connection(channel, admission):
                        self.record_failure(error)

    def dispatch(self, method, params, budget, state=None):
        state = self if state is None else state
        self.registry.prune()
        if state.pending is not None and method not in {"register.files", "register.commit"}:
            raise DeskWeaveError("INVALID_ARGUMENT", "客户端登记分块尚未结束。")
        if method == "ping":
            wire.fields(params, ())
            return {}
        if method == "status":
            wire.fields(params, ())
            with self.condition:
                count = len(self.workers)
            return {
                "connections": count,
                "clients": len(self.registry.grants),
                "componentStates": self.registry.states,
                "manifestBytes": self.registry.bytes,
            }
        if method == "register.begin":
            wire.fields(params, ("peer", "installation", "policy", "lifetime"))
            identity = wire.peer_value(params["peer"])
            selected_policy = policy(params["policy"])
            self.authority._lifetime(params["lifetime"])
            if len(self.registry.grants) >= self.authority.max_clients:
                raise DeskWeaveError("LIMIT_EXCEEDED", "客户端登记数量已满。")
            count = wire.header_count(params["installation"])
            if (
                identity.process_id in self.registry.closing
                and self.registry.peers[identity.process_id].close_failed
            ):
                raise DeskWeaveError("RESOURCE_CLOSE_FAILED", "客户端进程引用正在释放。")
            record = self.registry.peers.get(identity.process_id)
            existing = record.binding if record is not None else None
            if existing is None and count + 1 > self.registry.available_states(identity) - getattr(
                state, "other_states", 0
            ):
                raise DeskWeaveError("LIMIT_EXCEEDED", "进程文件状态登记额度已满。")
            if existing is not None:
                if existing.identity != identity:
                    raise DeskWeaveError("AUTHENTICATION_FAILED", "客户端实例已改变。")
                installation = existing.installation
                reader = None
            else:
                installation = self.registry.installations.get(identity.installation_digest)
                reader = (
                    wire.ManifestReader(
                        params["installation"],
                        byte_limit=self.registry.max_bytes
                        - self.registry.bytes
                        - getattr(state, "other_manifest_bytes", 0),
                    )
                    if installation is None
                    else None
                )
            if installation is not None and params["installation"] != wire.manifest_header(
                installation
            ):
                raise DeskWeaveError("INVALID_ARGUMENT", "安装清单头与已登记内容不一致。")
            state.pending = (identity, reader, installation, selected_policy, params["lifetime"])
            return {"needFiles": reader is not None}
        if method == "register.files":
            wire.fields(params, ("files",))
            if state.pending is None or state.pending[1] is None:
                raise DeskWeaveError("INVALID_ARGUMENT", "客户端登记没有等待安装分块。")
            state.pending[1].append(params["files"], budget.check_time)
            return {}
        if method == "register.commit":
            wire.fields(params, ())
            if state.pending is None:
                raise DeskWeaveError("INVALID_ARGUMENT", "客户端登记尚未开始。")
            identity, reader, installation, selected_policy, lifetime = state.pending
            state.pending = None
            if reader is not None:
                installation = reader.finish()
            return wire.credential_value(
                self.registry.add(
                    identity,
                    installation,
                    selected_policy,
                    lifetime,
                    budget,
                    not_after=getattr(state, "not_after", None),
                )
            )
        if method == "ticket":
            wire.fields(params, ("credential",))
            credential = wire.credential(params["credential"])
            grant = self.registry.selected(credential)
            grant.peer.binding.check(budget.check_time)
            return wire.ticket_value(self.authority.issue(credential, grant.peer.binding.identity))
        if method == "revoke":
            wire.fields(params, ("credential",))
            credential = wire.credential(params["credential"])
            self.registry.revoke(credential)
            return {}
        if method == "revise":
            wire.fields(params, ("credential", "policy", "version", "lifetime"))
            credential = wire.credential(params["credential"])
            version = self.registry.revise(
                credential,
                policy(params["policy"]),
                params["version"],
                params["lifetime"],
                not_after=getattr(state, "not_after", None),
            )
            return {"authorizationVersion": version}
        raise DeskWeaveError("METHOD_UNAVAILABLE", "未知进程控制方法。")

    def start(self):
        if self.acceptor is None:
            self.acceptor = threading.Thread(target=self.accept, name="deskweave-accept")
            self.acceptor.start()

    def clear_state(self, state):
        state.pending = None

    def control_messages(self, control, state, dispatch):
        previous = 0
        transaction = None
        while not self.cancel.is_set():
            budget = transaction or Budget(30)
            request = control.receive(budget=budget)
            if self.cancel.is_set():
                break
            wire.fields(request, ("id", "method", "params"))
            identity = request["id"]
            if type(identity) is not int or identity != previous + 1:
                raise DeskWeaveError("INVALID_ARGUMENT", "进程控制请求顺序无效。")
            previous = identity
            try:
                if type(request["method"]) is not str or type(request["params"]) is not dict:
                    raise DeskWeaveError("INVALID_ARGUMENT", "进程控制方法或参数无效。")
                result = dispatch(request["method"], request["params"], budget)
                response = {"id": identity, "ok": True, "result": result}
            except DeskWeaveError as error:
                self.clear_state(state)
                response = {"id": identity, "ok": False, "error": error.as_dict()}
            except BaseExceptionGroup:
                self.clear_state(state)
                response = {
                    "id": identity,
                    "ok": False,
                    "error": {
                        "code": "RESOURCE_CLOSE_FAILED",
                        "message": "进程引用释放失败，可重试释放。",
                        "retry": "safe",
                    },
                }
            except (ValueError, TypeError, KeyError):
                self.clear_state(state)
                response = {
                    "id": identity,
                    "ok": False,
                    "error": {
                        "code": "INVALID_ARGUMENT",
                        "message": "进程控制参数无效。",
                        "retry": "never",
                    },
                }
            control.send(response, budget=budget)
            transaction = budget if state.pending is not None else None
            if getattr(state, "stop", False):
                break

    def run(self, control):
        self.start()
        try:
            self.control_messages(control, self, self.dispatch)
        except DeskWeaveError as error:
            if error.code not in {"TRANSPORT_CLOSED", "TRANSPORT_TRUNCATED", "DEADLINE_EXCEEDED"}:
                self.record_failure(error)
        finally:
            self.close()
        return 1 if self.failures else 0

    def close(self):
        self.pending = None
        self.cancel.set()

        def join_acceptor():
            if self.acceptor is not None and self.acceptor.ident is not None:
                self.acceptor.join()

        def drain_workers():
            failure = None
            while True:
                with self.condition:
                    if not self.workers:
                        break
                    self.condition.wait(0.1)
                try:
                    self.registry.prune()
                except BaseException as error:
                    if failure is None:
                        failure = error
            errors = self.retry_connections()
            if failure is not None:
                errors.insert(0, failure)
            if errors:
                raise BaseExceptionGroup("业务连接排空失败。", errors)

        cleanup(
            self.listener.close,
            join_acceptor,
            drain_workers,
            self.authority.close,
            self.registry.close,
            self.listener.close,
        )


class ParentExit:
    def __init__(self, parent):
        self.parent = parent

    def is_set(self):
        try:
            self.parent._check_process()
        except DeskWeaveError as error:
            if error.code != "AUTHENTICATION_FAILED":
                raise
            return True
        return False


def main(config, engine_files, parent_files):
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    ready = False
    try:
        wire.fields(
            config,
            (
                "version",
                "workspace",
                "create",
                "parent",
                "engineEpoch",
                "engineInstallation",
                "parentInstallation",
                "directory",
                "maxConnections",
                "maxClients",
                "maxComponentStates",
                "maxManifestBytes",
            )
            + (("coordination",) if "coordination" in config else ()),
        )
        if config["version"] != wire.VERSION:
            raise DeskWeaveError("PROTOCOL_UNSUPPORTED", "启动协议版本不兼容。")
        if type(config["create"]) is not bool:
            raise DeskWeaveError("INVALID_ARGUMENT", "工作空间创建选项无效。")
        for key, maximum in (
            ("maxConnections", 64),
            ("maxClients", 256),
            ("maxComponentStates", 1000000),
            ("maxManifestBytes", 67108864),
        ):
            if type(config[key]) is not int or not 1 <= config[key] <= maximum:
                raise DeskWeaveError("INVALID_ARGUMENT", "进程服务容量无效。")

        def installation(header, values):
            reader = wire.ManifestReader(header, byte_limit=config["maxManifestBytes"])
            for offset in range(0, len(values), 128):
                reader.append(values[offset : offset + 128])
            values.clear()
            return reader.finish()

        engine_install = installation(config["engineInstallation"], engine_files)
        parent_install = installation(config["parentInstallation"], parent_files)
        parent_identity = wire.peer_value(config["parent"])
        with ExitStack() as stack:
            local = stack.enter_context(
                ProcessBinding(os.getpid(), engine_install, instance_epoch=config["engineEpoch"])
            )
            parent = stack.enter_context(
                ProcessBinding(
                    parent_identity.process_id,
                    parent_install,
                    instance_epoch=parent_identity.instance_epoch,
                )
            )
            if parent.identity != parent_identity:
                raise DeskWeaveError("AUTHENTICATION_FAILED", "启动器身份不匹配。")
            coordination = config.get("coordination")
            if coordination is not None:
                from deskweave.coordination.directory import Directory, WorkspaceIdentity
                from deskweave.coordination.server import SharedRuntime
                from deskweave.coordination.startup import read_plan

                selected_identity = stack.enter_context(
                    WorkspaceIdentity(Path(config["workspace"]), create=config["create"])
                )
                if list(selected_identity.object) != coordination[
                    "directory"
                ] or selected_identity.workspace not in (None, coordination["workspace"]):
                    raise DeskWeaveError("AUTHENTICATION_FAILED", "工作空间启动对象已改变。")
                runtime_directory = stack.enter_context(
                    Directory(coordination["runtimePath"], principal=local)
                )
                if list(runtime_directory.identity) != coordination["runtimeDirectory"]:
                    raise DeskWeaveError("AUTHENTICATION_FAILED", "运行目录对象已改变。")
                budget = Budget(max(coordination["deadline"] - time.monotonic(), 1e-9))
                plan = read_plan(
                    source,
                    coordination,
                    (engine_install, parent_install),
                    config["maxManifestBytes"],
                    budget,
                )
                if plan.engine_installation != local.identity.installation_digest:
                    raise DeskWeaveError("AUTHENTICATION_FAILED", "启动计划与执行服务安装不匹配。")
                selected_identity.check()
            engine = Engine(
                Workspace(
                    Path(config["workspace"]),
                    create=config["create"],
                    _identity=coordination["workspace"] if coordination is not None else None,
                )
            )
            stack.callback(engine.close)
            authority = stack.enter_context(
                ConnectionAuthority(
                    engine,
                    local.identity,
                    max_connections=config["maxConnections"],
                    max_clients=config["maxClients"],
                )
            )
            business = stack.enter_context(NativeListener(local, directory=config["directory"]))
            listener = stack.enter_context(NativeListener(local, directory=config["directory"]))
            if coordination is None:
                runtime = Runtime(
                    business,
                    authority,
                    parent,
                    max_states=config["maxComponentStates"],
                    max_bytes=config["maxManifestBytes"],
                )
            else:
                selected_identity.check(engine.workspace.id)
                runtime = SharedRuntime(
                    business,
                    authority,
                    parent,
                    max_states=config["maxComponentStates"],
                    max_bytes=config["maxManifestBytes"],
                    identity=selected_identity,
                    runtime_directory=runtime_directory,
                    plan=plan,
                    config=coordination,
                )
            stack.callback(runtime.close)
            runtime.start()
            wire.write_frame(
                sink,
                {
                    "ok": True,
                    "version": wire.VERSION,
                    "peer": local.identity.as_dict(),
                    "control": listener.address,
                    "address": business.address,
                    "workspace": engine.workspace.id,
                },
            )
            ready = True
            source.close()
            sink.close()
            deadline = time.monotonic() + 30
            if coordination is not None:
                deadline = min(deadline, coordination["deadline"])
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeskWeaveError("DEADLINE_EXCEEDED", "启动控制连接已超时。")
                if coordination is not None:
                    parent._check_process()
                try:
                    control = listener.accept(
                        {parent.identity.process_id: parent},
                        timeout=remaining,
                        cancel=ParentExit(parent) if coordination is not None else None,
                    )
                    break
                except DeskWeaveError as error:
                    if error.code != "AUTHENTICATION_FAILED":
                        raise
            with control:
                hello = control.receive(timeout=10)
                if hello != {"id": 0, "method": "hello", "params": {"version": wire.VERSION}}:
                    raise DeskWeaveError("PROTOCOL_UNSUPPORTED", "启动控制协议不兼容。")
                response = (
                    {"workspace": engine.workspace.id}
                    if coordination is None
                    else runtime.root_response()
                )
                control.send({"id": 0, "ok": True, "result": response})
                listener.close()
                return runtime.run(control)
    except BaseException as error:
        if not ready:
            try:
                detail = (
                    error.as_dict()
                    if isinstance(error, DeskWeaveError)
                    else {
                        "code": "ENGINE_START_FAILED",
                        "message": "执行服务启动失败。",
                        "retry": "never",
                    }
                )
                wire.write_frame(sink, {"ok": False, "error": detail})
            except BaseException:
                pass
        traceback.print_exception(error, file=sys.stderr)
        return 1
