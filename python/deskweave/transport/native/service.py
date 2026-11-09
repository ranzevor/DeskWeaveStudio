from __future__ import annotations

import queue
import threading
import time

from deskweave.transport.connection import ClientConnection

from ._common import Budget, Resource, cleanup, closed, rejected


class NativeClient(Resource):
    def __init__(self, channel, ticket, *, timeout=10.0, cancel=None):
        self.channel = channel
        self._lock = threading.Lock()
        self.connection = None
        try:
            self.connection = ClientConnection(ticket, channel.local, channel.peer)
            budget = Budget(timeout, cancel)
            channel.send(self.connection.start(), budget=budget)
            response = self.connection.receive(channel.receive(budget=budget))
            channel.max_frame_bytes = self.connection.selected["limits"]["maxFrameBytes"]
            channel.send(response, budget=budget)
            self.connection.receive(channel.receive(budget=budget))
            budget.check()
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
            response = self.channel.receive(budget=budget)
            result = self.connection.response(response, request=request)
            budget.check()
            return result
        except BaseException as error:
            self._abort(error)
            raise
        finally:
            if acquired:
                self._lock.release()

    def close(self):
        actions = [self.channel.close]
        if self.connection is not None:
            actions.append(self.connection.close)
        cleanup(*actions)


def serve_connection(channel, authority, *, timeout=30.0, cancel=None):
    if channel.local != authority.identity:
        channel.close()
        raise rejected()
    channel.max_frame_bytes = authority.profile.max_frame_bytes
    budget = Budget(authority.handshake_lifetime, cancel)
    next_check = 0.0
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
        if now >= next_check:
            channel.check(budget.check_time)
            next_check = time.monotonic() + 0.1
        if connection is not None and connection.closed:
            raise closed()

    def maintain():
        try:
            while not stopped.wait(0.1):
                authority.sweep()
        except BaseException as error:
            failures.put(error)
            try:
                channel.close()
            except BaseException as closing:
                failures.put(closing)

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
                response = connection.receive(channel.receive(budget=budget))
                channel.max_frame_bytes = connection.selected["limits"]["maxFrameBytes"]
                channel.send(response, budget=budget)
            while True:
                budget = Budget(timeout, cancel, checkpoint)
                response = connection.receive(channel.receive(budget=budget))
                channel.send(response, budget=budget)
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
