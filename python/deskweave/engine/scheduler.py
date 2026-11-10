from __future__ import annotations

import heapq
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future
from dataclasses import replace

from deskweave.adapters.execution import Change, ExecutionStore
from deskweave.adapters.runs import RunStore
from deskweave.domain.execution import TERMINAL, Frame, Run, bind_effects, compile_workflow
from deskweave.domain.frozen import Value
from deskweave.engine.actions import LOGIC_PORTS
from deskweave.engine.execution import Kernel, fault
from deskweave.errors import DeskWeaveError


class YieldStep(Exception):
    pass


class BudgetExpired(Exception):
    pass


class CallBudgetExpired(Exception):
    pass


class Runtime:
    def __init__(
        self, workspace, *, clock=time.monotonic, threaded=True, max_runs=17, ports=LOGIC_PORTS
    ):
        self.workspace, self.clock, self.max_runs = workspace, clock, max_runs
        self.ports = ports
        self.store, self.runs = ExecutionStore(workspace), RunStore(workspace)
        self._condition = threading.Condition()
        self._step_lock = threading.RLock()
        self._active = {}
        self._ready, self._queued = deque(), set()
        self._timers, self._heap, self._timer_seq = {}, [], 0
        self._waiters, self._waiting_on = defaultdict(set), {}
        self._commands, self._faults = [], {}
        self._closing = False
        self._thread = None
        self.kernel = Kernel(self.store, self._checkpoint, ports)
        self._executing = None
        self._executing_frame = None
        self._attempted = None
        if threaded:
            self._thread = threading.Thread(
                target=self._serve, name="deskweave-runtime", daemon=True
            )
            self._thread.start()

    @property
    def supported_actions(self):
        return self.ports.actions

    def _checkpoint(self):
        run = self._executing
        if run is None:
            return
        run.access.revalidate()
        if run.active(self.clock()) >= run.plan["limits"]["activeMs"] / 1000:
            raise BudgetExpired()
        if (
            self._executing_frame is not None
            and run.active(self.clock()) >= self._executing_frame.deadline
        ):
            raise CallBudgetExpired()
        with self._condition:
            if self._closing or any(
                item[0] == run.id and item[1] in {"pause", "stop"} for item in self._commands
            ):
                raise YieldStep()

    def attach(self, run_id, access):
        with self._step_lock:
            if self._closing:
                raise DeskWeaveError("WORKSPACE_CLOSED", "执行器已关闭。")
            if run_id in self._active:
                access.check_snapshot(self._active[run_id].plan)
                return self.store.snapshot(run_id)
            if len(self._active) >= self.max_runs:
                raise DeskWeaveError("LIMIT_EXCEEDED", "执行器活动运行数量超过限制。")
            stored = self.runs.read(run_id)
            plan = stored["snapshot"]
            access.check_snapshot(plan)
            if stored["state"] != "queued":
                raise DeskWeaveError("INVALID_ARGUMENT", "运行已启动或已结束。")
            if (
                sum(
                    int(item["definitionLength"]) + int(item["dependenciesLength"])
                    for item in plan["workflows"]
                )
                > 67_108_864
            ):
                raise DeskWeaveError("LIMIT_EXCEEDED", "运行定义总大小超过 64MiB。")
            programs, bindings = {}, {}
            with self.workspace.budget(None, authorize=access.revalidate):
                for reference in plan["workflows"]:
                    key = reference["workflowId"], reference["revision"]
                    record = self.workspace.read(*key)
                    if record["definitionHash"] != reference["definitionHash"]:
                        raise DeskWeaveError("DEPENDENCY_MISMATCH", "流程版本内容已变化。")
                    program = compile_workflow(record["definition"], self.workspace.check_deadline)
                    for node in program.nodes.values():
                        if node["action"] not in self.supported_actions:
                            raise DeskWeaveError(
                                "METHOD_UNAVAILABLE", "执行器不支持此流程中的动作。"
                            )
                    programs[key], bindings[key] = program, record["dependencies"]
                bind_effects(programs, bindings, self.workspace.check_deadline)
            plan["_bindings"] = bindings
            state = plan["initialState"]
            inputs = Value.build(
                {
                    name: Value.freeze(value)
                    .marked(name in state["sensitiveInputs"])
                    .declared(
                        programs[(plan["root"]["workflowId"], plan["root"]["revision"])].definition[
                            "parameters"
                        ][name]["type"]
                    )
                    for name, value in state["inputs"].items()
                }
            )
            root = Frame(
                stored["rootFrameId"],
                None,
                (plan["root"]["workflowId"], plan["root"]["revision"]),
                plan["root"]["entry"],
                inputs=inputs,
                variables=Value.freeze(state["variables"]),
                depth_limit=plan["limits"]["callDepth"],
                deadline=plan["limits"]["activeMs"] / 1000,
                targets={
                    name: (token, plan["targetKinds"][name])
                    for name, token in plan["targets"].items()
                },
            )
            now = self.clock()
            before = Run(
                run_id,
                root.id,
                plan,
                programs,
                access,
                {},
                Value.freeze(state["runVariables"]),
                0,
                "queued",
                stored["controlVersion"],
                stored["dataVersion"],
                int(stored["lastSeq"]),
                0,
                now,
            )
            change = Change(
                replace(before, state="preparing", control_version=before.control_version + 1)
            )
            change.frames[root.id] = root
            for scope, values in (
                ("inputs", root.inputs),
                ("local", root.variables),
                ("shared", before.shared),
            ):
                for name, value in values.data.items():
                    change.value("" if scope == "shared" else root.id, scope, name, value)
            change.ready.add(root.id)
            self.store.commit(before, change, now, initial=True)
            self._active[run_id] = change.run
            self._publish(change)
            return self.store.snapshot(run_id)

    def control(self, run_id, command, expected_control_version, *, access):
        if command not in {"pause", "resume", "stop"}:
            raise DeskWeaveError("INVALID_ARGUMENT", "运行控制操作无效。")
        future = Future()
        with self._condition:
            if self._closing:
                future.set_exception(DeskWeaveError("WORKSPACE_CLOSED", "执行器已关闭。"))
            elif len(self._commands) >= 64:
                future.set_exception(DeskWeaveError("LIMIT_EXCEEDED", "控制请求数量超过限制。"))
            else:
                self._commands.append((run_id, command, expected_control_version, future, access))
                self._condition.notify()
        return future

    def _command(self, item):
        run_id, command, expected, future, access = item
        if not future.set_running_or_notify_cancel():
            return
        try:
            self.store.check_access(run_id, access, "run.execute")
            run = self._active.get(run_id)
            if run is None:
                raise DeskWeaveError("INVALID_ARGUMENT", "运行已结束或尚未接入执行器。")
            run.access.revalidate()
            if type(expected) is not int or expected != run.control_version:
                raise DeskWeaveError("REVISION_CONFLICT", "运行控制版本已变化。")
            now = self.clock()
            if command == "stop":
                change = self.kernel.terminate(run, "cancelled")
            elif command == "pause":
                if run.state == "paused":
                    future.set_result(self.store.snapshot(run_id))
                    return
                change = Change(
                    replace(
                        run, state="paused", paused_at=now, control_version=run.control_version + 1
                    )
                )
            else:
                if run.state != "paused":
                    raise DeskWeaveError("INVALID_ARGUMENT", "运行未暂停。")
                change = Change(
                    replace(
                        run,
                        state="running",
                        paused_at=None,
                        paused_total=run.paused_total + now - run.paused_at,
                        control_version=run.control_version + 1,
                    )
                )
            self.store.commit(run, change, self.clock())
            self._active[run_id] = change.run
            self._publish(change, all_frames=True)
            future.set_result(self.store.snapshot(run_id))
        except Exception as error:
            future.set_exception(error)

    def _enqueue(self, run_id, frame_id):
        key = run_id, frame_id
        if key not in self._queued:
            self._ready.append(key)
            self._queued.add(key)

    def _timer(self, key, due):
        self._timer_seq += 1
        value = due, self._timer_seq
        self._timers[key] = value
        heapq.heappush(self._heap, (*value, key))

    def _unlink_wait(self, key):
        child = self._waiting_on.pop(key, None)
        if child is not None:
            self._waiters[child].discard(key)
            if not self._waiters[child]:
                del self._waiters[child]

    def _schedule_frame(self, run, frame):
        key = run.id, frame.id
        self._timers.pop(key, None)
        self._unlink_wait(key)
        if run.state == "paused":
            return
        if frame.state == "ready":
            self._enqueue(*key)
            return
        if frame.state == "collecting" and not frame.children:
            self._enqueue(*key)
            return
        child = frame.phase.get("child")
        if child:
            if child not in run.frames:
                self._enqueue(*key)
            else:
                child_key = run.id, child
                self._waiters[child_key].add(key)
                self._waiting_on[key] = child_key
        due = min(
            frame.deadline,
            frame.phase.get("until", frame.deadline),
            frame.phase.get("deadline", frame.deadline),
        )
        self._timer(key, run.started + run.paused_total + due)

    def _publish(self, change, *, all_frames=False):
        run = change.run
        for identity in change.removed:
            key = run.id, identity
            self._timers.pop(key, None)
            self._queued.discard(key)
            self._unlink_wait(key)
            for waiter in self._waiters.pop(key, set()):
                self._waiting_on.pop(waiter, None)
                self._enqueue(*waiter)
        if run.state in TERMINAL:
            self._active.pop(run.id, None)
            self._faults.pop(run.id, None)
            self._timers.pop((run.id, ""), None)
        else:
            if run.state == "paused":
                self._timer((run.id, ""), run.paused_at + 1800)
            else:
                self._timers.pop((run.id, ""), None)
            frames = (
                run.frames.values()
                if all_frames
                else (run.frames[identity] for identity in change.frames if identity in run.frames)
            )
            for frame in frames:
                self._schedule_frame(run, frame)
            if run.state != "paused":
                for identity in change.ready:
                    if identity in run.frames:
                        self._enqueue(run.id, identity)
        if len(self._heap) > 2 * len(self._timers) + 32:
            self._heap = [(due, seq, key) for key, (due, seq) in self._timers.items()]
            heapq.heapify(self._heap)
        with self._condition:
            self._condition.notify()

    def _system_stop(self, run, now, state, error):
        change = self.kernel.terminate(run, state, error)
        change.system = True
        self.store.commit(run, change, self.clock())
        self._active[run.id] = change.run
        self._publish(change)

    def tick(self):
        with self._step_lock:
            command = None
            with self._condition:
                if self._commands:
                    index = min(
                        range(len(self._commands)),
                        key=lambda i: {"stop": 0, "pause": 1, "resume": 2}[self._commands[i][1]],
                    )
                    command = self._commands.pop(index)
            if command is not None:
                self._command(command)
                return True
            now = self.clock()
            for run in tuple(self._active.values()):
                self._attempted = run.id
                if run.id in self._faults:
                    continue
                try:
                    run.access.revalidate()
                except DeskWeaveError:
                    self._system_stop(
                        run, now, "cancelled", fault("ACCESS_DENIED", "运行授权已失效。")
                    )
                    return True
                if run.state == "paused" and now - run.paused_at >= 1800:
                    self._system_stop(
                        run, now, "cancelled", fault("TIMEOUT", "运行暂停时间超过限制。")
                    )
                    return True
                if run.state != "paused" and (
                    run.active(now) >= run.plan["limits"]["activeMs"] / 1000
                ):
                    self._system_stop(
                        run, now, "failed", fault("LIMIT_EXCEEDED", "运行执行预算已耗尽。")
                    )
                    return True
            while self._heap and self._heap[0][0] <= now:
                due, seq, key = heapq.heappop(self._heap)
                if self._timers.get(key) != (due, seq):
                    continue
                del self._timers[key]
                if key[1]:
                    self._enqueue(*key)
            while self._ready:
                key = self._ready.popleft()
                self._queued.discard(key)
                run = self._active.get(key[0])
                if (
                    run is None
                    or key[1] not in run.frames
                    or run.state == "paused"
                    or key[0] in self._faults
                ):
                    continue
                self._attempted = run.id
                try:
                    if run.state == "preparing":
                        change = Change(
                            replace(run, state="running", control_version=run.control_version + 1)
                        )
                        change.ready.add(key[1])
                    else:
                        self._executing = run
                        self._executing_frame = run.frames[key[1]]
                        change = self.kernel.step(run, key[1], now)
                    self.store.commit(run, change, self.clock(), checkpoint=self._checkpoint)
                except YieldStep:
                    self._enqueue(*key)
                    return True
                except CallBudgetExpired:
                    change = Change(replace(run))
                    self.kernel._observed = False
                    self.kernel._collect(
                        change,
                        change.edit(key[1]),
                        "failed",
                        Value.freeze(None),
                        fault("LIMIT_EXCEEDED", "调用执行时间超过限制。"),
                    )
                    self.store.commit(run, change, self.clock())
                    self._active[run.id] = change.run
                    self._publish(change)
                    return True
                except BudgetExpired:
                    self._system_stop(
                        run,
                        self.clock(),
                        "failed",
                        fault("LIMIT_EXCEEDED", "运行执行时间超过限制。"),
                    )
                    return True
                except DeskWeaveError as error:
                    if error.code == "ACCESS_DENIED":
                        self._system_stop(
                            run,
                            self.clock(),
                            "cancelled",
                            fault("ACCESS_DENIED", "运行授权已失效。"),
                        )
                        return True
                    self._enqueue(*key)
                    raise
                except Exception:
                    self._enqueue(*key)
                    raise
                finally:
                    self._executing = None
                    self._executing_frame = None
                self._active[run.id] = change.run
                self._publish(change)
                return True
            return False

    def pump(self, steps=10000):
        for count in range(steps):
            if not self.tick():
                return count
        return steps

    def retry(self, run_id, *, access):
        self.store.check_access(run_id, access, "run.execute")
        with self._step_lock:
            self._faults.pop(run_id, None)
            run = self._active[run_id]
            self._publish(Change(run), all_frames=True)

    def _serve(self):
        while True:
            with self._condition:
                if self._closing:
                    return
            try:
                if self.tick():
                    continue
            except Exception as error:
                with self._step_lock:
                    if self._attempted in self._active:
                        self._faults[self._attempted] = error
            with self._condition:
                if not self._closing:
                    delay = (
                        max(0.001, min(0.1, self._heap[0][0] - self.clock())) if self._heap else 0.1
                    )
                    self._condition.wait(delay)

    def read_state(self, run_id, *, access):
        self.store.check_access(run_id, access, "run.read")
        state = self.store.snapshot(run_id)
        if run_id in self._faults:
            state["storageFailure"] = {"code": "STORAGE_ERROR", "message": "运行状态保存失败。"}
        access.revalidate()
        if state["error"] and state["error"].get("sensitive"):
            state["error"] = {"code": state["error"]["code"]}
        return state

    def read_value(self, run_id, scope, name, *, access, frame_id=None, version=None):
        root = self.store.check_access(run_id, access, "run.read")
        if scope not in {"inputs", "local", "shared", "previous", "outputs", "calls"}:
            raise DeskWeaveError("INVALID_ARGUMENT", "变量范围无效。")
        if scope == "shared":
            if frame_id is not None:
                raise DeskWeaveError("INVALID_ARGUMENT", "共享变量不属于单个调用。")
            frame_id = ""
        elif frame_id is None:
            frame_id = root
        if frame_id:
            self.store.check_frame(run_id, frame_id)
        value = self.store.value(run_id, frame_id, scope, name, version=version)
        access.revalidate()
        return {"visibility": "unavailable"} if value is None else value.projection()

    def result(self, run_id, *, access):
        state = self.read_state(run_id, access=access)
        state["output"] = self.read_value(
            run_id, "outputs", "", access=access, version=state["dataVersion"]
        )
        return state

    def close(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        with self._step_lock:
            failures = []
            for run in tuple(self._active.values()):
                try:
                    self._system_stop(
                        run, self.clock(), "cancelled", fault("CANCELLED", "执行器已关闭。")
                    )
                except Exception as error:
                    failures.append(error)
            with self._condition:
                self._closing = True
                for _run_id, _command, _expected, future, _access in self._commands:
                    if future.cancelled():
                        continue
                    future.set_exception(DeskWeaveError("WORKSPACE_CLOSED", "执行器已关闭。"))
                self._commands.clear()
                self._condition.notify_all()
        if self._thread is not None:
            self._thread.join()
        if failures:
            raise ExceptionGroup("执行器收尾失败。", failures)
