from __future__ import annotations

import threading
import time

from deskweave.errors import DeskWeaveError
from deskweave.transport.native import NativeClient, ProcessBinding, connect
from deskweave.transport.native._common import (
    Budget,
    Cancellation,
    InitializationError,
    Resource,
    cleanup,
    closed,
)
from deskweave.transport.protocol import ClientCredential, ConnectionPolicy

from . import _wire as wire


class RemoteFailure(DeskWeaveError):
    pass


def remaining(budget):
    budget.check_time()
    return max(budget.deadline - time.monotonic(), 1e-9)


class Connecting(Resource):
    def __init__(self, release):
        self.resource = None
        self.release = release

    def close(self):
        if self.resource is not None:
            self.resource.close()
            self.resource = None
        if self.release is not None:
            self.release()
            self.release = None


class ControlSession(Resource):
    def __init__(self, local, peer, channel, address, workspace):
        self.local, self._peer, self._control = local, peer, channel
        self.address, self.workspace = address, workspace
        self._lock = threading.Lock()
        self._initialize_connections()
        self._stopped = threading.Event()
        self._failures = []
        self._closed = False
        self._sequence = 0
        self._heartbeat = threading.Thread(
            target=self._keep_alive, name="deskweave-control", daemon=True
        )
        try:
            self._heartbeat.start()
        except BaseException as error:
            self._abort_initialization(error)
            raise

    def _initialize_connections(self):
        self._connect_lock = threading.RLock()
        self._failed_connections = set()

    def _retry_connections(self):
        errors = []
        for pending in tuple(self._failed_connections):
            try:
                pending.close()
            except BaseException as error:
                errors.append(error)
            else:
                self._failed_connections.discard(pending)
        if errors:
            raise BaseExceptionGroup("客户端连接资源释放失败。", errors)

    @property
    def peer(self):
        return self._peer.identity

    def _acquire_peer(self):
        return self._peer, None

    def _keep_alive(self):
        while not self._stopped.wait(5):
            if not self._lock.acquire(blocking=False):
                continue
            try:
                self._exchange("ping", {}, Budget(10))
            except BaseException as error:
                if not self._stopped.is_set():
                    self._failures.append(error)
                try:
                    self.close()
                except BaseException as closing:
                    self._failures.append(closing)
                return
            finally:
                self._lock.release()

    @staticmethod
    def _remote_error(value):
        if type(value) is not dict or not {"code", "message", "retry"} <= value.keys():
            raise DeskWeaveError("INVALID_ARGUMENT", "进程控制错误响应无效。")
        raise RemoteFailure(
            value["code"], value["message"], retry=value["retry"], details=value.get("details")
        )

    def _exchange(self, method, params, budget):
        if self._closed:
            raise closed()
        budget.check_time()
        request = {"id": self._sequence + 1, "method": method, "params": params}
        wire.dumps(request, checkpoint=budget.check_time)
        self._sequence += 1
        try:
            self._control.send(request, budget=budget)
            value = self._control.receive(budget=budget)
            if (
                type(value.get("id")) is not int
                or value["id"] != self._sequence
                or type(value.get("ok")) is not bool
            ):
                raise DeskWeaveError("INVALID_ARGUMENT", "进程控制响应不匹配。")
            wire.fields(value, ("id", "ok", "result") if value["ok"] else ("id", "ok", "error"))
        except BaseException:
            self.close()
            raise
        if not value["ok"]:
            self._remote_error(value["error"])
        return value["result"]

    def _call(self, method, params, *, timeout=30.0, cancel=None):
        budget = Budget(timeout, cancel)
        budget.acquire(self._lock)
        try:
            return self._exchange(method, params, budget)
        finally:
            self._lock.release()

    def authorize(
        self,
        peer: ProcessBinding,
        policy: ConnectionPolicy,
        *,
        lifetime: float,
        timeout=30.0,
        cancel=None,
    ) -> ClientCredential:
        if type(peer) is not ProcessBinding or type(policy) is not ConnectionPolicy:
            raise TypeError("客户端授权需要受控进程登记和连接策略。")
        budget = Budget(timeout, cancel)
        budget.acquire(self._lock)
        begun = False
        try:
            peer.check(budget.check_time)
            result = self._exchange(
                "register.begin",
                {
                    "peer": peer.identity.as_dict(),
                    "installation": wire.manifest_header(peer.installation),
                    "policy": policy.as_dict(),
                    "lifetime": lifetime,
                },
                budget,
            )
            begun = True
            wire.fields(result, ("needFiles",))
            if type(result["needFiles"]) is not bool:
                raise DeskWeaveError("INVALID_ARGUMENT", "客户端登记响应无效。")
            if result["needFiles"]:
                for batch in wire.chunks(peer.installation.components):
                    self._exchange("register.files", {"files": batch}, budget)
            return wire.credential(self._exchange("register.commit", {}, budget))
        except RemoteFailure:
            raise
        except BaseException:
            if begun:
                self.close()
            raise
        finally:
            self._lock.release()

    def issue(self, credential: ClientCredential, *, timeout=30.0, cancel=None):
        return wire.ticket(
            self._call(
                "ticket",
                {"credential": wire.credential_value(credential)},
                timeout=timeout,
                cancel=cancel,
            )
        )

    def connect(
        self, credential: ClientCredential, local: ProcessBinding, *, timeout=30.0, cancel=None
    ):
        cancel = Cancellation(cancel, self._stopped)
        budget = Budget(timeout, cancel)
        ticket = self.issue(credential, timeout=remaining(budget), cancel=cancel)
        budget.acquire(self._connect_lock)
        pending = None
        try:
            if self._closed:
                raise closed()
            self._retry_connections()
            peer, release = self._acquire_peer()
            pending = Connecting(release)
            try:
                channel = connect(
                    self.address, local, peer, timeout=remaining(budget), cancel=cancel
                )
            except InitializationError as error:
                pending.resource = error.resource
                raise
            pending.resource = channel
            client = NativeClient(channel, ticket, timeout=remaining(budget), cancel=cancel)
            channel._release = release
            pending.release = None
            return client
        except BaseException as error:
            if pending is not None:
                try:
                    pending.close()
                except BaseException as closing:
                    self._failed_connections.add(pending)
                    raise InitializationError(pending, [error, closing]) from None
            raise
        finally:
            self._connect_lock.release()

    def revoke(self, credential: ClientCredential, *, timeout=30.0, cancel=None):
        self._call(
            "revoke",
            {"credential": wire.credential_value(credential)},
            timeout=timeout,
            cancel=cancel,
        )

    def revise(
        self,
        credential: ClientCredential,
        policy: ConnectionPolicy,
        *,
        expected_version: int,
        lifetime: float,
        timeout=30.0,
        cancel=None,
    ):
        return self._call(
            "revise",
            {
                "credential": wire.credential_value(credential),
                "policy": policy.as_dict(),
                "version": expected_version,
                "lifetime": lifetime,
            },
            timeout=timeout,
            cancel=cancel,
        )["authorizationVersion"]

    def status(self, *, timeout=30.0):
        return self._call("status", {}, timeout=timeout)

    def close(self):
        self._closed = True
        self._stopped.set()

        def pending():
            with self._connect_lock:
                self._retry_connections()

        actions = [pending]
        if self._control is not None:
            actions.insert(0, self._control.close)
        cleanup(*actions)
