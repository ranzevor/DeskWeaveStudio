from __future__ import annotations

import queue
import threading
import time
from collections import deque

from deskweave.contracts import validate
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps
from deskweave.transport.connection import ClientConnection

from ._common import Budget, Resource, cleanup, closed, rejected


class NativeClient(Resource):
    def __init__(self, channel, ticket, *, timeout=10.0, cancel=None):
        self.channel = channel
        self._lock = threading.Lock()
        self.connection = None
        self._received = threading.Condition()
        self._events, self._responses = deque(), deque()
        self._event_bytes, self._overflow = 0, False
        self._failure = None
        self._stopped = threading.Event()
        self._reader = None
        try:
            self.connection = ClientConnection(ticket, channel.local, channel.peer)
            budget = Budget(timeout, cancel)
            channel.send(self.connection.start(), budget=budget)
            response = self.connection.receive(channel.receive(budget=budget))
            channel.max_frame_bytes = self.connection.selected["limits"]["maxFrameBytes"]
            channel.send(response, budget=budget)
            self.connection.receive(channel.receive(budget=budget))
            budget.check()
            self._reader = threading.Thread(
                target=self._receive, name="deskweave-ipc-reader", daemon=True
            )
            self._reader.start()
        except BaseException as error:
            self._abort(error)
            raise

    def call(self, method, params, *, request_id, timeout=30.0, cancel=None):
        if type(request_id) is not str or not request_id:
            raise ValueError("调用需要由客户端保存的请求身份。")
        budget = Budget(timeout, cancel)
        acquired = False
        try:
            budget.acquire(self._lock)
            acquired = True
            request = self.connection.request(
                method,
                params,
                request_id=request_id,
                deadline_ms=max(1, min(30000, int((budget.deadline - time.monotonic()) * 1000))),
            )
            self.channel.send(request, budget=budget)
            with self._received:
                while not self._responses:
                    if self._failure is not None:
                        raise self._failure
                    if self._stopped.is_set():
                        raise closed()
                    self._received.wait(budget.check())
                response = self._responses.popleft()
            result = self.connection.response(response, request=request)
            budget.check()
            return result
        except BaseException as error:
            self._abort(error)
            raise
        finally:
            if acquired:
                self._lock.release()

    def _receive(self):
        try:
            while not self._stopped.is_set():
                value = self.channel.receive(timeout=86400, cancel=self._stopped)
                validate("urn:deskweave:message:1", value)
                with self._received:
                    if value["kind"] == "event":
                        length = len(dumps(value))
                        if len(self._events) >= 5000 or self._event_bytes + length > 8_388_608:
                            self._overflow = True
                            self._events.clear()
                            self._event_bytes = 0
                        self._events.append((value, length))
                        self._event_bytes += length
                    elif value["kind"] == "response" and not self._responses:
                        self._responses.append(value)
                    else:
                        raise DeskWeaveError("INVALID_ARGUMENT", "连接返回了意外消息。")
                    self._received.notify_all()
        except BaseException as error:
            with self._received:
                self._failure = error
                self._received.notify_all()

    def next_event(self, *, timeout=30.0, cancel=None):
        budget = Budget(timeout, cancel)
        with self._received:
            while True:
                budget.check()
                if self._overflow:
                    self._overflow = False
                    self._events.clear()
                    self._event_bytes = 0
                    raise DeskWeaveError(
                        "EVENT_RESYNC_REQUIRED", "事件缓冲已满，请重新获取运行快照。"
                    )
                if self._events:
                    value, length = self._events.popleft()
                    self._event_bytes -= length
                    return value
                if self._failure is not None:
                    raise self._failure
                if self._stopped.is_set():
                    raise closed()
                self._received.wait(budget.check())

    def close(self):
        self._stopped.set()
        with self._received:
            self._received.notify_all()
        actions = [self.channel.close]
        if self.connection is not None:
            actions.append(self.connection.close)
        try:
            cleanup(*actions)
        finally:
            if self._reader is not None and self._reader is not threading.current_thread():
                self._reader.join()


def serve_connection(channel, authority, *, timeout=30.0, cancel=None):
    if channel.local != authority.identity:
        channel.close()
        raise rejected()
    channel.max_frame_bytes = authority.profile.max_frame_bytes
    budget = Budget(authority.handshake_lifetime, cancel)
    state_lock = threading.Lock()
    next_check = 0.0
    processing = False
    connection = None
    stopped = threading.Event()
    failures = queue.Queue()
    maintenance = None
    maintenance_started = False

    def checkpoint():
        nonlocal next_check
        if not failures.empty():
            raise closed()
        now = time.monotonic()
        with state_lock:
            should_check = now >= next_check
            active_processing = processing
            current_budget = budget
        if should_check:
            if active_processing:
                channel.check(current_budget.check_time)
            else:
                channel._check_identity(current_budget.check_time)
            with state_lock:
                next_check = time.monotonic() + 0.1
        if connection is not None and connection.closed:
            raise closed()

    def maintain():
        try:
            while not stopped.wait(0.1):
                authority.sweep()
                session = None if connection is None else connection.session
                if session is None or connection.closed:
                    continue
                engine = authority.engine
                with state_lock:
                    frame_limit = channel.max_frame_bytes
                with (
                    engine._mutex,
                    engine.workspace.budget(time.monotonic() + 2, authorize=session.revalidate),
                ):
                    engine._frame_limit = frame_limit
                    engine._response_overhead = 0
                    pending = engine.events.pending(session)
                    subscribed = any(
                        item["owner"] == session.id and item["active"]
                        for item in engine.events.subscriptions.values()
                    )
                with state_lock:
                    if subscribed and not processing:
                        budget.deadline = time.monotonic() + timeout
                if pending is not None:
                    identity, messages = pending
                    for message in messages:
                        authority.check(
                            connection, session.client_id, session.authorization_version
                        )
                        channel.send(message, timeout=2, cancel=stopped)
                        with engine._mutex:
                            engine.events.acknowledge(session.id, identity, message["seq"])
        except BaseException as error:
            failures.put(error)
            try:
                channel.close()
            except BaseException as closing:
                failures.put(closing)

    def exchange(request_timeout=None):
        nonlocal budget, next_check, processing
        if request_timeout is not None:
            with state_lock:
                budget = Budget(request_timeout, cancel, checkpoint)
        current_budget = budget
        value = channel.receive(budget=current_budget)
        with state_lock:
            processing = True
            next_check = time.monotonic() + 0.1
        try:
            response = connection.receive(value)
            with state_lock:
                channel.max_frame_bytes = connection.selected["limits"]["maxFrameBytes"]
            channel.send(response, budget=current_budget)
            if value.get("method") == "event.subscribe" and response.get("ok"):
                session = connection.session
                if session is not None:
                    with authority.engine._mutex:
                        authority.engine.events.activate(
                            session.id, response["result"]["subscriptionId"]
                        )
        finally:
            with state_lock:
                processing = False

    budget.checkpoint = checkpoint
    errors = []
    try:
        connection = authority.accept(
            channel.peer, check_peer=lambda: budget.check(), close_transport=channel.close
        )
        maintenance = threading.Thread(target=maintain, name="deskweave-ipc-expiry")
        with connection:
            maintenance.start()
            maintenance_started = True
            for _ in range(2):
                exchange()
            while True:
                exchange(timeout)
    except BaseException as error:
        errors.append(error)
    finally:
        stopped.set()
        try:
            channel.close()
        except BaseException as error:
            errors.append(error)
        if maintenance_started:
            maintenance.join()
        while not failures.empty():
            errors.append(failures.get_nowait())
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("连接服务与回收失败。", errors)
