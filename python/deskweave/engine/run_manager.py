from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

from deskweave.adapters.run_lifecycle import RunLifecycleStore
from deskweave.domain.execution import TERMINAL
from deskweave.engine.actions import LOGIC_PORTS
from deskweave.engine.run_planning import RunService
from deskweave.engine.scheduler import Runtime
from deskweave.errors import DeskWeaveError


@dataclass
class PendingRun:
    access: object
    detached: bool
    paused_at: float | None = None
    retry_at: float = 0.0


class RunManager:
    def __init__(self, workspace, *, clock, threaded=True, ports=LOGIC_PORTS):
        self.workspace, self.clock, self.threaded = workspace, clock, threaded
        self.ports = ports
        self.lifecycle = RunLifecycleStore(workspace)
        self.planner = RunService(workspace, ports=ports, max_unfinished=17)
        self._guard = threading.RLock()
        self._closed, self.busy = threading.Event(), threading.Event()
        self._known, self._pending = {}, {}
        self.runtime = Runtime(
            workspace,
            clock=clock,
            threaded=False,
            max_runs=1,
            ports=ports,
            before_tick=self._advance,
            control_handler=self._control_pending,
            signal_handler=self._signal_pending,
            on_publish=self._published,
        )

    def _open(self):
        if self._closed.is_set():
            raise DeskWeaveError("WORKSPACE_CLOSED", "运行服务已关闭。")

    def start(self, access, request_id, params, *, plan_provider=None):
        with self.workspace.budget(None, authorize=self._open):
            self._open()
            if self.threaded:
                self.runtime.start()
            return self.planner.accept(
                access,
                request_id,
                params,
                plan_provider=plan_provider,
                on_accept=lambda result: self._register(
                    result["runId"], access, params["detached"]
                ),
            )

    def _register(self, run_id, access, detached):
        with self._guard:
            if run_id not in self._known:
                entry = PendingRun(access, detached)
                self._known[run_id] = entry
                self._pending[run_id] = entry
                self.busy.set()
        self.runtime.wake()

    def _remove(self, run_id):
        with self._guard:
            self._known.pop(run_id, None)
            self._pending.pop(run_id, None)
            if not self._known:
                self.busy.clear()

    def _published(self, change):
        run = change.run
        if run.state in TERMINAL:
            self._remove(run.id)
        else:
            with self._guard:
                self._pending.pop(run.id, None)

    def _finish_pending(self, run_id, state, *, error=None):
        row = self.lifecycle.read(run_id)
        if row["state"] in TERMINAL:
            self._remove(run_id)
            return self.lifecycle.response(row)
        return self.lifecycle.transition(
            run_id,
            state,
            row["control_version"],
            system=True,
            error=error,
            on_commit=lambda _result: self._remove(run_id),
        )

    def _advance(self):
        if self._closed.is_set():
            return False
        with self._guard:
            pending = tuple(self._pending.items())
        now = self.clock()
        for run_id, entry in pending:
            if now < entry.retry_at:
                continue
            try:
                try:
                    entry.access.revalidate()
                except DeskWeaveError:
                    self._finish_pending(run_id, "cancelled", error={"code": "ACCESS_DENIED"})
                    return True
                if entry.paused_at is not None:
                    if now - entry.paused_at >= 1800:
                        self._finish_pending(run_id, "cancelled", error={"code": "TIMEOUT"})
                        return True
                    continue
                if self.runtime._active:
                    continue
                try:
                    self.runtime.attach(run_id, entry.access)
                except DeskWeaveError as error:
                    state = "cancelled" if error.code == "ACCESS_DENIED" else "failed"
                    self._finish_pending(run_id, state, error={"code": error.code})
                return True
            except (OSError, sqlite3.Error):
                entry.retry_at = self.clock() + 0.1
        return False

    def _control_pending(self, run_id, command, expected, access, receipt):
        row = self.lifecycle.read(run_id)
        if type(expected) is not int or row["control_version"] != expected:
            raise DeskWeaveError("REVISION_CONFLICT", "运行控制版本已变化。")
        if row["state"] in TERMINAL:
            if command != "stop":
                raise DeskWeaveError("INVALID_ARGUMENT", "运行已经结束。")
            return self.lifecycle.transition(
                run_id, row["state"], expected, access=access, receipt=receipt
            )
        with self._guard:
            entry = self._pending.get(run_id)
        if entry is None:
            raise DeskWeaveError("INVALID_ARGUMENT", "运行尚未进入当前调度队列。")

        def retention():
            if (
                command != "stop"
                and entry.paused_at is not None
                and self.clock() - entry.paused_at >= 1800
            ):
                raise DeskWeaveError("TIMEOUT", "运行暂停期限已届满。")

        if command == "resume" and row["state"] != "paused":
            raise DeskWeaveError("INVALID_ARGUMENT", "运行未暂停。")
        if command == "resume":
            entry.access.check_snapshot(self.runtime.runs.read(run_id)["snapshot"])
        state = {"pause": "paused", "resume": "queued", "stop": "cancelled"}[command]

        def publish(_result):
            if state in TERMINAL:
                self._remove(run_id)
            elif state == "paused":
                if entry.paused_at is None:
                    entry.paused_at = self.clock()
            else:
                entry.paused_at = None
            self.runtime.wake()

        try:
            retention()
            return self.lifecycle.transition(
                run_id,
                state,
                expected,
                access=access,
                receipt=receipt,
                on_commit=publish,
                checkpoint=retention,
            )
        except DeskWeaveError as error:
            if error.code == "TIMEOUT":
                self._finish_pending(run_id, "cancelled", error={"code": "TIMEOUT"})
            raise

    def _signal_pending(self, run_id, command):
        with self._guard:
            entry = self._pending.get(run_id)
        if entry is None:
            return
        if command == "stop":
            self._finish_pending(run_id, "cancelled", error={"code": "CANCELLED"})
        elif entry.paused_at is None:
            row = self.lifecycle.read(run_id)
            self.lifecycle.transition(
                run_id,
                "paused",
                row["control_version"],
                system=True,
                on_commit=lambda _result: setattr(entry, "paused_at", self.clock()),
            )

    def has_owned_work(self, client_id):
        with self._guard:
            return any(entry.access.client_id == client_id for entry in self._known.values())

    def client_exited(self, client_id):
        with self._guard:
            selected = [
                key
                for key, entry in self._known.items()
                if entry.access.client_id == client_id and not entry.detached
            ]
        for run_id in selected:
            self.runtime.signal(run_id, "pause")

    def revoke_client(self, client_id, authorization_version=None):
        with self._guard:
            selected = [
                key
                for key, entry in self._known.items()
                if entry.access.client_id == client_id
                and (
                    authorization_version is None
                    or entry.access.authorization_version == authorization_version
                )
            ]
        for run_id in selected:
            self.runtime.signal(run_id, "stop")

    def close(self):
        self._closed.set()
        errors = []
        try:
            self.runtime.close()
        except BaseException as error:
            errors.append(error)
        with self._guard:
            pending = tuple(self._pending)
        for run_id in pending:
            try:
                self._finish_pending(run_id, "cancelled", error={"code": "CANCELLED"})
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("运行服务收尾失败。", errors)
