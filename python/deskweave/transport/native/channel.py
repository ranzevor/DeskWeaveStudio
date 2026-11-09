from __future__ import annotations

import os
import secrets
import struct
import sys
import threading
from collections.abc import Mapping

from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, dumps, loads

from ._common import Budget, Resource, closed, rejected
from .identity import ProcessBinding

if sys.platform == "win32":
    from . import _windows as backend
else:
    from . import _unix as backend


def local_binding(binding, checkpoint=lambda: None):
    if type(binding) is not ProcessBinding or binding.identity.process_id != os.getpid():
        raise rejected()
    binding.check(checkpoint)


class NativeChannel(Resource):
    def __init__(self, raw, local, peer, *, budget=None):
        budget = budget or Budget(30.0)
        self._raw = raw
        self._local_binding, self._peer_binding = local, peer
        self._read_lock, self._write_lock = threading.Lock(), threading.Lock()
        self._closed = False
        self.max_frame_bytes = MAX_FRAME
        if (
            type(local) is not ProcessBinding
            or type(peer) is not ProcessBinding
            or local.identity.process_id != os.getpid()
        ):
            raise rejected()
        self.local, self.peer = local.identity, peer.identity
        self.check(budget.check_time)
        if (self.local.platform, self.local.user_id, self.local.os_session_id) != (
            self.peer.platform,
            self.peer.user_id,
            self.peer.os_session_id,
        ):
            raise rejected()

    def check(self, checkpoint=lambda: None):
        if self._closed:
            raise closed()
        local, peer = self._local_binding, self._peer_binding
        peer._verify_peer(self._raw, checkpoint)
        local.check(checkpoint)
        if peer is local:
            return
        if peer.identity.installation_digest == local.identity.installation_digest:
            peer._check_process(checkpoint)
            peer._verify_image(checkpoint)
            if peer._stamps != local._stamps:
                raise rejected()
            peer._check_process(checkpoint)
        else:
            peer.check(checkpoint)

    def _check_identity(self, checkpoint=lambda: None):
        if self._closed:
            raise closed()
        self._local_binding._check_process(checkpoint)
        self._peer_binding._check_process(checkpoint)

    def _exact(self, size, budget, *, prefix=False):
        data = bytearray()
        while len(data) < size:
            piece = self._raw.read(size - len(data), budget)
            if not piece:
                if prefix and not data:
                    raise closed()
                raise DeskWeaveError("TRANSPORT_TRUNCATED", "连接在完整消息到达前结束。")
            data.extend(piece)
        return bytes(data)

    def receive(self, *, timeout=30.0, cancel=None, budget=None):
        budget = budget or Budget(timeout, cancel)
        acquired = False
        try:
            budget.acquire(self._read_lock)
            acquired = True
            self._check_identity(budget.check_time)
            size = struct.unpack("<I", self._exact(4, budget, prefix=True))[0]
            if not 1 <= size <= self.max_frame_bytes:
                raise DeskWeaveError("LIMIT_EXCEEDED", "消息帧长度无效。")
            payload = self._exact(size, budget)
            self.check(budget.check_time)
            value = loads(payload, checkpoint=budget.check)
            if type(value) is not dict:
                raise DeskWeaveError("INVALID_ARGUMENT", "消息帧必须是 JSON 对象。")
            budget.check()
            return value
        except ConnectionError as error:
            failure = closed()
            failure.__cause__ = error
            self._abort(failure)
            raise failure from error
        except BaseException as error:
            self._abort(error)
            raise
        finally:
            if acquired:
                self._read_lock.release()

    def send(self, value, *, timeout=30.0, cancel=None, budget=None):
        budget = budget or Budget(timeout, cancel)
        acquired = False
        try:
            budget.acquire(self._write_lock)
            acquired = True
            self.check(budget.check_time)
            payload = dumps(value, max_bytes=self.max_frame_bytes, checkpoint=budget.check)
            data = struct.pack("<I", len(payload)) + payload
            offset = 0
            while offset < len(data):
                count = self._raw.write(data[offset:], budget)
                if not count:
                    raise closed()
                offset += count
            budget.check()
        except ConnectionError as error:
            failure = closed()
            failure.__cause__ = error
            self._abort(failure)
            raise failure from error
        except BaseException as error:
            self._abort(error)
            raise
        finally:
            if acquired:
                self._write_lock.release()

    def close(self):
        self._closed = True
        self._raw.close()


class NativeListener(Resource):
    def __init__(self, local: ProcessBinding, *, directory=None, backlog=64):
        local_binding(local)
        if type(backlog) is not int or not 1 <= backlog <= 4096:
            raise ValueError("监听队列容量应为 1～4096。")
        self.local = local
        self._raw = backend.Listener(
            backend.address_name(secrets.token_hex(24)), local.process, directory, backlog
        )
        self.address = self._raw.address

    def accept(self, peers: Mapping[int, ProcessBinding], *, timeout=30.0, cancel=None):
        budget = Budget(timeout, cancel)
        self.local.check(budget.check_time)
        raw = self._raw.accept(budget)
        try:
            pid, _credential = raw.peer()
            peer = peers.get(pid)
            if type(peer) is not ProcessBinding:
                raise rejected()
            channel = NativeChannel(raw, self.local, peer, budget=budget)
            budget.check()
            return channel
        except BaseException:
            raw.close()
            raise

    def close(self):
        self._raw.close()


def connect(
    address: str, local: ProcessBinding, peer: ProcessBinding, *, timeout=30.0, cancel=None
):
    budget = Budget(timeout, cancel)
    local_binding(local, budget.check_time)
    if type(address) is not str or type(peer) is not ProcessBinding:
        raise ValueError("连接需要端点地址和受控对端登记。")
    peer.check(budget.check_time)
    raw = backend.connect(address, budget)
    try:
        channel = NativeChannel(raw, local, peer, budget=budget)
        budget.check()
        return channel
    except BaseException:
        raw.close()
        raise
