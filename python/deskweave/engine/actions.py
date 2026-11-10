from __future__ import annotations

from types import MappingProxyType
from typing import Protocol

from deskweave.domain.execution import CORE_ACTIONS
from deskweave.domain.frozen import NULL, PathMissing, Value
from deskweave.domain.values import validate_result
from deskweave.errors import DeskWeaveError


class ActionPort(Protocol):
    actions: frozenset[str]

    def execute(self, kernel, change, frame, node, now): ...


class ActionPorts:
    def __init__(self, *ports: ActionPort):
        handlers = {}
        for port in ports:
            if not callable(port.execute) or not port.actions:
                raise ValueError("动作端口必须声明可执行动作。")
            for action in port.actions:
                if action in handlers:
                    raise ValueError("动作不能由多个端口重复注册。")
                handlers[action] = port.execute
        self._handlers = MappingProxyType(handlers)
        self.actions = frozenset(handlers)

    def execute(self, kernel, change, frame, node, now):
        handler = self._handlers.get(node["action"])
        if handler is None:
            raise DeskWeaveError("METHOD_UNAVAILABLE", "执行器不支持此动作。")
        handler(kernel, change, frame, node, now)


class LogicActions:
    actions = CORE_ACTIONS

    def execute(self, kernel, change, frame, node, now):
        from deskweave.engine.execution import fault

        run = change.run
        params, action = node["params"], node["action"]
        if action == "time.wait":
            duration, sensitive = kernel._integer(change, frame, params["durationMs"], 0, 86400000)
            frame.state, frame.phase = (
                "waiting",
                {
                    "kind": "wait",
                    "since": now,
                    "until": now + duration / 1000,
                    "sensitive": sensitive,
                },
            )
        elif action == "data.assign":
            value = kernel._value(change, frame, params["value"]).marked(
                frame.sensitive, conditional=True
            )
            name, path = params["name"], params.get("path", [])
            shared = params.get("scope", "local") == "run"
            root = change.run.shared if shared else frame.variables
            if shared:
                version, _ = kernel._integer(
                    change,
                    frame,
                    params.get("expectedVersion", change.run.shared_version),
                    0,
                    9007199254740991,
                )
                if version != change.run.shared_version:
                    error = DeskWeaveError(
                        "VARIABLE_CONFLICT",
                        "运行共享变量版本已变化。",
                        details={"currentVersion": change.run.shared_version},
                    )
                    error.sensitive = value.sensitive
                    raise error
            value = value.marked(frame.sensitive, conditional=True)
            root = change.run.shared if shared else frame.variables
            if name not in root.data and len(root.data) >= 1000:
                raise DeskWeaveError("LIMIT_EXCEEDED", "变量数量超过限制。")
            try:
                root.updated(
                    [name, *path],
                    value,
                    observe_control=lambda sensitive: kernel._guard(change, frame, sensitive),
                )
                root = change.run.shared if shared else frame.variables
                value = value.marked(frame.sensitive, conditional=True)
                replacement = root.updated([name, *path], value).lookup([name])
            except PathMissing as error:
                failure = DeskWeaveError("INVALID_ARGUMENT", "变量修改路径不存在或类型不匹配。")
                failure.sensitive = error.sensitive
                raise failure from error
            validate_result(replacement, checkpoint=kernel.checkpoint)
            if shared:
                if not replacement.label and not value.label:
                    try:
                        cleared = root.lookup([name, *path]).sensitive
                    except PathMissing as missing:
                        cleared = missing.sensitive
                    if cleared:
                        change.run.label_epoch += 1
                change.run.shared = root.updated([name], replacement)
                change.run.shared_version += 1
                change.value("", "shared", name, replacement)
            else:
                kernel._variable(change, frame, name, replacement)
            kernel._complete(
                change,
                frame,
                node,
                Value.build(
                    {
                        "name": Value.freeze(name),
                        "scope": Value.freeze("run" if shared else "local"),
                        "value": value,
                        "version": Value.freeze(
                            change.run.shared_version if shared else run.data_version + 1
                        ),
                    }
                ),
            )
        elif action == "logic.branch":
            condition = kernel._value(change, frame, params["condition"], "boolean")
            if condition.sensitive:
                join = kernel._program(change, frame).joins[frame.node]
                kernel._taint_region(change, frame, join)
                if (join, True) not in frame.control:
                    frame.control += ((join, True),)
            kernel._complete(
                change,
                frame,
                node,
                Value.build({"condition": condition}),
                node["on"]["yes" if condition.data else "no"],
                explicit_target=True,
            )
        elif action == "logic.assert":
            condition = kernel._value(change, frame, params["condition"], "boolean")
            if not condition.data:
                kernel._error(
                    change,
                    frame,
                    fault(
                        "ASSERTION_FAILED",
                        params.get("message", "断言条件不成立。"),
                        condition.sensitive,
                    ),
                )
            else:
                if condition.sensitive:
                    frame.control += (
                        (kernel._program(change, frame).error_joins[frame.node], True),
                    )
                kernel._complete(
                    change, frame, node, Value.freeze({"passed": True}).marked(condition.sensitive)
                )
        elif action in {"flow.return", "flow.break", "flow.continue"}:
            output = kernel._object(change, frame, params.get("values", {}))
            if action != "flow.return":
                if not frame.loop_owner:
                    raise DeskWeaveError("INVALID_WORKFLOW", "循环控制动作缺少所属循环。")
                frame.signal, frame.signal_owner = action.split(".")[1], frame.loop_owner
            kernel._collect(change, frame, "succeeded", output)
        elif action == "flow.call":
            inputs = kernel._object(change, frame, params["inputs"])
            child = kernel._spawn(
                change, frame, node, inputs, now, background=params["mode"] == "start"
            )
            if params["mode"] == "start":
                kernel._complete(change, frame, node, Value.freeze(child))
            else:
                frame.state, frame.phase = "joining", {"kind": "call", "child": child}
        elif action == "flow.join":
            handle = kernel._value(change, frame, params["handle"], "string")
            timeout, sensitive = kernel._integer(change, frame, params["timeoutMs"], 0, 86400000)
            kernel._guard(change, frame, handle.sensitive)
            child = handle.data
            if child not in run.frames:
                kernel.store.outcome(run.id, child)
            elif kernel._cycle(change, frame.id, child):
                raise DeskWeaveError("INVALID_ARGUMENT", "子流程等待关系形成循环。")
            frame.state, frame.phase = (
                "joining",
                {
                    "kind": "join",
                    "child": child,
                    "until": now + timeout / 1000,
                    "sensitive": sensitive or handle.sensitive,
                },
            )
            change.ready.add(frame.id)
        elif action in {"flow.repeat", "flow.loop"}:
            count, sensitive = kernel._integer(
                change,
                frame,
                params["count"] if action == "flow.repeat" else params["maxIterations"],
                0 if action == "flow.repeat" else 1,
                10000,
            )
            timeout, marked = (int((frame.deadline - now) * 1000), False)
            if action == "flow.loop":
                timeout, marked = kernel._integer(change, frame, params["timeoutMs"], 1, 86400000)
            frame.phase = {
                "kind": "loop",
                "mode": "repeat" if action == "flow.repeat" else params["mode"],
                "limit": count,
                "index": 0,
                "iterations": 0,
                "child": None,
                "deadline": min(frame.deadline, now + timeout / 1000),
                "sensitive": sensitive or marked,
            }
            frame.iteration_result = NULL
            change.value(frame.id, "runtime", "iterationResult", NULL)
            kernel._loop(change, frame, node, now)
        else:
            raise DeskWeaveError("METHOD_UNAVAILABLE", "动作执行端口尚未提供此动作。")


LOGIC_PORTS = ActionPorts(LogicActions())
