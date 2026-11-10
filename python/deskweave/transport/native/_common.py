from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from deskweave.errors import DeskWeaveError


def rejected() -> DeskWeaveError:
    return DeskWeaveError("AUTHENTICATION_FAILED", "本机连接的进程身份不匹配。")


def closed() -> DeskWeaveError:
    return DeskWeaveError("TRANSPORT_CLOSED", "本机连接已关闭。")


class Cancellation:
    def __init__(self, caller, stopped):
        self.caller, self.stopped = caller, stopped

    def is_set(self):
        return self.stopped.is_set() or self.caller is not None and self.caller.is_set()

    def wait(self, timeout):
        if not self.is_set():
            self.stopped.wait(timeout)
        return self.is_set()


class Budget:
    def __init__(
        self,
        timeout: float,
        cancel: threading.Event | None = None,
        checkpoint: Callable[[], None] | None = None,
    ):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("传输期限必须为有限正数。")
        self.deadline = time.monotonic() + timeout
        self.cancel = cancel
        self.checkpoint = checkpoint

    def check_time(self) -> float:
        if self.cancel is not None and self.cancel.is_set():
            raise DeskWeaveError("CANCELLED", "本机通信已取消。")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeskWeaveError("DEADLINE_EXCEEDED", "本机通信已超时。")
        return min(remaining, 0.025)

    def check(self) -> float:
        self.check_time()
        if self.checkpoint is not None:
            self.checkpoint()
        return self.check_time()

    def acquire(self, lock) -> None:
        while not lock.acquire(timeout=self.check()):
            pass
        try:
            self.check()
        except BaseException:
            lock.release()
            raise


def cleanup(*actions) -> None:
    errors = []
    for action in actions:
        try:
            action()
        except BaseException as error:
            errors.append(error)
    if errors:
        raise BaseExceptionGroup("本机通信资源关闭失败。", errors)


class InitializationError(BaseExceptionGroup):
    def __new__(cls, resource, errors):
        value = super().__new__(cls, "资源初始化与关闭失败。", errors)
        value.resource = resource
        return value

    def __init__(self, resource, errors):
        super().__init__("资源初始化与关闭失败。", errors)

    def derive(self, errors):
        return type(self)(self.resource, errors)


class Resource:
    def _abort_initialization(self, error):
        try:
            self.close()
        except BaseException as closing:
            raise InitializationError(self, [error, closing]) from None

    def _abort(self, error):
        try:
            self.close()
        except BaseException as closing:
            raise BaseExceptionGroup("本机通信与关闭失败。", [error, closing]) from None

    def __enter__(self):
        return self

    def __exit__(self, _kind, error, _traceback):
        if error is None:
            self.close()
        else:
            self._abort(error)
