from __future__ import annotations

import os
import threading
import time

from deskweave.errors import DeskWeaveError
from deskweave.process import EngineProcess, PythonLaunch
from deskweave.process import _wire as wire
from deskweave.process.control import ControlSession, RemoteFailure, remaining
from deskweave.transport.native import ProcessBinding, connect
from deskweave.transport.native._common import (
    Budget,
    Cancellation,
    InitializationError,
    Resource,
    cleanup,
    closed,
)
from deskweave.transport.native.identity import platform

from . import handshake
from .authority import HostGrant, StartupPlan
from .directory import WorkspaceIdentity, backend, runtime_directory
from .startup import Discovery, InvalidDiscovery, LaunchContext, check_idle


class BindingOwner(Resource):
    def __init__(self, binding):
        self.binding = binding
        self.lock = threading.RLock()
        self.references = 0
        self.closed = False

    def acquire(self):
        with self.lock:
            if self.closed:
                raise closed()
            self.references += 1
        released = False

        def release():
            nonlocal released
            with self.lock:
                if not released:
                    self.references -= 1
                    released = True
                self.retry()

        return self.binding, release

    def retry(self):
        with self.lock:
            if not self.references and not self.closed:
                self.binding.close()
                self.closed = True

    def close(self):
        self.retry()


class AttachedControl(ControlSession):
    def __init__(self, local, owner, channel, address, workspace):
        self.owner = owner
        super().__init__(local, owner.binding, channel, address, workspace)

    def _acquire_peer(self):
        if self._closed:
            raise closed()
        return self.owner.acquire()

    def close(self):
        def join():
            thread = getattr(self, "_heartbeat", None)
            if (
                thread is not None
                and thread.ident is not None
                and thread is not threading.current_thread()
            ):
                thread.join()

        cleanup(super().close, join, self.owner.retry)


class HostSession(Resource):
    def __init__(self, coordinator, control, grant, identity):
        self.coordinator, self.control, self.grant, self.identity = (
            coordinator,
            control,
            grant,
            identity,
        )
        self.local = coordinator.local
        self.workspace = control.workspace
        self.peer = control.peer
        self.created = isinstance(control, EngineProcess)

    def connect(self, credential=None, *, timeout=30.0, cancel=None):
        budget = Budget(timeout, cancel)
        if credential is None:
            credential = wire.credential(
                self.control._call("host.client", {}, timeout=remaining(budget), cancel=cancel)
            )
        return self.control.connect(
            credential, self.local, timeout=remaining(budget), cancel=cancel
        )

    def authorize(self, peer, policy, *, lifetime, timeout=30.0, cancel=None):
        return self.control.authorize(
            peer, policy, lifetime=lifetime, timeout=timeout, cancel=cancel
        )

    def issue(self, credential, *, timeout=30.0, cancel=None):
        return self.control.issue(credential, timeout=timeout, cancel=cancel)

    def revise(self, credential, policy, *, expected_version, lifetime, timeout=30.0, cancel=None):
        return self.control.revise(
            credential,
            policy,
            expected_version=expected_version,
            lifetime=lifetime,
            timeout=timeout,
            cancel=cancel,
        )

    def revoke(self, credential, *, timeout=30.0, cancel=None):
        return self.control.revoke(credential, timeout=timeout, cancel=cancel)

    def register_host(self, peer, policy, *, lifetime, manager=False, timeout=30.0, cancel=None):
        budget = Budget(timeout, cancel)
        budget.acquire(self.control._lock)
        begun = False
        try:
            peer.check(budget.check_time)
            result = self.control._exchange(
                "host.register.begin",
                {
                    "peer": peer.identity.as_dict(),
                    "installation": wire.manifest_header(peer.installation),
                    "policy": policy.as_dict(),
                    "lifetime": lifetime,
                    "manager": manager,
                },
                budget,
            )
            begun = True
            wire.fields(result, ("needFiles",))
            if type(result["needFiles"]) is not bool:
                raise DeskWeaveError("INVALID_ARGUMENT", "宿主登记响应无效。")
            if result["needFiles"]:
                for values in wire.chunks(peer.installation.components):
                    self.control._exchange("host.register.files", {"files": values}, budget)
            return HostGrant.from_dict(self.control._exchange("host.register.commit", {}, budget))
        except RemoteFailure:
            raise
        except BaseException:
            if begun:
                self.control.close()
            raise
        finally:
            self.control._lock.release()

    def revoke_host(self, grant_id, *, timeout=30.0, cancel=None):
        return self.control._call(
            "host.revoke", {"grantId": grant_id}, timeout=timeout, cancel=cancel
        )

    def status(self, *, timeout=30.0):
        return self.control.status(timeout=timeout)

    def diagnostics(self, *, timeout=30.0):
        return self.control._call("host.diagnostics", {}, timeout=timeout)["text"]

    def close(self):
        cleanup(self.control.close, self.identity.close)
        with self.coordinator.lock:
            self.coordinator.sessions.discard(self)


class WorkspaceCoordinator(Resource):
    def __init__(
        self,
        launch: PythonLaunch,
        local: ProcessBinding,
        *,
        directory=None,
        idle_grace=2.0,
        max_sessions=64,
    ):
        if (
            type(launch) is not PythonLaunch
            or type(local) is not ProcessBinding
            or local.identity.process_id != os.getpid()
        ):
            raise TypeError("工作空间协调需要当前宿主与受信启动配置。")
        if type(max_sessions) is not int or not 1 <= max_sessions <= 256:
            raise ValueError("宿主连接容量应为 1～256。")
        check_idle(idle_grace)
        self.launch, self.local = launch, local
        self.idle_grace, self.max_sessions = idle_grace, max_sessions
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.stopped = threading.Event()
        self.sessions, self.failed_closes = set(), set()
        self.pending = 0
        self.closed = False
        local.check()
        self.runtime = runtime_directory(local, directory)

    def dispose(self, resource):
        if resource is None:
            return
        try:
            resource.close()
        except BaseException:
            with self.lock:
                self.failed_closes.add(resource)
            raise

    def retry_close(self):
        errors = []
        for resource in tuple(self.failed_closes):
            try:
                resource.close()
            except BaseException as error:
                errors.append(error)
            else:
                self.failed_closes.discard(resource)
        return errors

    def attach(self, record, grant, budget):
        if record["authority"] != grant.binding.authority:
            raise DeskWeaveError("AUTHENTICATION_FAILED", "发现实例不属于宿主授权。")
        peer = channel = owner = None
        try:
            peer = ProcessBinding(
                record["peer"].process_id,
                self.launch.installation,
                instance_epoch=record["peer"].instance_epoch,
                timeout=remaining(budget),
                cancel=budget.cancel,
            )
            if peer.identity != record["peer"]:
                raise DeskWeaveError("AUTHENTICATION_FAILED", "执行服务实例已改变。")
            owner = BindingOwner(peer)
            channel = connect(
                record["control"], self.local, peer, timeout=remaining(budget), cancel=budget.cancel
            )
            _, channel._release = owner.acquire()
            ready = handshake.client(channel, grant, budget)
            if ready["workspace"] != record["workspace"] or ready["address"] != record["address"]:
                raise DeskWeaveError("AUTHENTICATION_FAILED", "执行服务就绪位置不匹配。")
            result = AttachedControl(
                self.local, owner, channel, ready["address"], ready["workspace"]
            )
            return result
        except BaseException as error:
            if isinstance(error, InitializationError):
                with self.lock:
                    self.failed_closes.add(error.resource)
            try:
                cleanup(lambda: self.dispose(channel), lambda: self.dispose(owner or peer))
            except BaseException as closing:
                raise BaseExceptionGroup("宿主接入与清理失败。", [error, closing]) from None
            raise

    @staticmethod
    def read_discovery(discovery):
        try:
            return discovery.read()
        except InvalidDiscovery:
            return None

    def instance_running(self, peer):
        process = None
        try:
            process = platform.Process(peer.process_id)
            user, session, started = process.initial[:3]
            return (user, session, started) == (
                peer.user_id,
                peer.os_session_id,
                peer.process_started,
            )
        except (ProcessLookupError, FileNotFoundError):
            return False
        except DeskWeaveError as error:
            if error.code != "AUTHENTICATION_FAILED":
                raise
            return False
        except OSError as error:
            if getattr(error, "winerror", None) != 87:
                raise
            return False
        finally:
            self.dispose(process)

    @staticmethod
    def writer_busy(identity):
        try:
            stream = identity.directory.open(".engine.lock", write=True)
        except FileNotFoundError:
            return False
        with stream:
            if not backend.acquire(stream):
                return True
            backend.release(stream)
            return False

    def open(
        self,
        identity: WorkspaceIdentity,
        grant: HostGrant,
        *,
        plan: StartupPlan | None = None,
        create=False,
        timeout=30.0,
        cancel=None,
    ):
        if type(identity) is not WorkspaceIdentity or type(grant) is not HostGrant:
            raise TypeError("工作空间接入需要目录对象与宿主授权。")
        cancel = Cancellation(cancel, self.stopped)
        budget = Budget(timeout, cancel)
        if (
            grant.binding.peer != self.local.identity
            or grant.binding.directory != identity.object
            or grant.binding.engine_installation
            != self.launch.installation.calculate_digest(budget.check_time)
        ):
            raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主授权与本次工作空间不匹配。")
        if grant.binding.expires_at <= time.monotonic():
            raise DeskWeaveError("ACCESS_DENIED", "宿主授权已到期。")
        if plan is not None:
            if type(plan) is not StartupPlan:
                raise TypeError("工作空间启动计划无效。")
            plan.select(grant, self.local, budget.check_time)
        with self.lock:
            if self.closed:
                raise closed()
            self.retry_close()
            if len(self.sessions) + self.pending + len(self.failed_closes) >= self.max_sessions:
                raise DeskWeaveError("LIMIT_EXCEEDED", "宿主连接容量已满。")
            self.pending += 1
        owned = control = None
        try:
            owned = WorkspaceIdentity(identity.path, create=create)
            if owned.object != identity.object:
                raise DeskWeaveError("RESOURCE_DENIED", "工作空间目录已改变。")
            discovery = Discovery(owned, self.runtime)
            wake = cancel or threading.Event()
            while True:
                budget.check_time()
                owned.check(grant.binding.workspace, allow_missing=create)
                record = self.read_discovery(discovery)
                failure = None
                if record is not None:
                    try:
                        control = self.attach(record, grant, budget)
                    except (DeskWeaveError, OSError) as error:
                        failure = error
                        if isinstance(error, DeskWeaveError) and error.code in {
                            "CANCELLED",
                            "DEADLINE_EXCEEDED",
                            "PROTOCOL_UNSUPPORTED",
                            "SCHEMA_UNSUPPORTED",
                        }:
                            raise
                    else:
                        owned.check(control.workspace)
                        break
                with owned.lock(budget):
                    current = self.read_discovery(discovery)
                    if current != record:
                        continue
                    busy = self.writer_busy(owned)
                    living = record is not None and self.instance_running(record["peer"])
                    if (
                        (busy or living)
                        and failure is not None
                        and isinstance(failure, DeskWeaveError)
                        and failure.code in {"AUTHENTICATION_FAILED", "ACCESS_DENIED"}
                    ):
                        raise failure
                    if busy or living:
                        selected = None
                    elif plan is None:
                        if failure is not None:
                            raise failure
                        raise DeskWeaveError("TRANSPORT_CLOSED", "工作空间执行服务已结束。")
                    else:
                        plan.select(grant, self.local, budget.check_time)
                        owned.check(plan.workspace, allow_missing=create)
                        selected = EngineProcess(
                            self.launch,
                            self.local,
                            owned.path,
                            create=create,
                            timeout=remaining(budget),
                            cancel=cancel,
                            _coordination=LaunchContext(plan, owned, self.runtime, self.idle_grace),
                        )
                        control = selected
                        advertised = False
                        try:
                            budget.check_time()
                            owned.check(selected.workspace)
                            advertised = True
                            discovery.publish(
                                selected.workspace,
                                selected.peer,
                                selected.host_address,
                                selected.address,
                                plan.authority,
                            )
                        except BaseException:
                            if not advertised and selected.poll() is None:
                                selected._process.kill()
                                selected.wait(10)
                            raise
                if selected is not None:
                    control = selected
                    control._call("host.publish", {}, timeout=remaining(budget), cancel=cancel)
                    owned.check(control.workspace)
                    break
                wake.wait(budget.check_time())
            session = HostSession(self, control, grant, owned)
            with self.lock:
                if self.closed:
                    raise closed()
                self.sessions.add(session)
            return session
        except BaseException as error:
            if isinstance(error, InitializationError):
                with self.lock:
                    self.failed_closes.add(error.resource)
            try:
                cleanup(lambda: self.dispose(control), lambda: self.dispose(owned))
            except BaseException as closing:
                raise BaseExceptionGroup("工作空间接入与清理失败。", [error, closing]) from None
            raise
        finally:
            with self.condition:
                self.pending -= 1
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.stopped.set()
            while self.pending:
                self.condition.wait()
            sessions = tuple(self.sessions)
        errors = []
        for session in sessions:
            try:
                session.close()
            except BaseException as error:
                errors.append(error)
        with self.lock:
            errors.extend(self.retry_close())
            if not self.pending:
                try:
                    self.runtime.close()
                except BaseException as error:
                    errors.append(error)
        if errors:
            raise BaseExceptionGroup("工作空间协调资源关闭失败。", errors)
