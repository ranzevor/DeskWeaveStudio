from __future__ import annotations

import uuid
from dataclasses import replace
from heapq import merge

from deskweave.adapters.execution import Change
from deskweave.domain.execution import Frame
from deskweave.domain.frozen import EMPTY, NULL, PathMissing, Value
from deskweave.domain.values import evaluate_value, matches_type, validate_result
from deskweave.engine.actions import LOGIC_PORTS
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, dumps


def fault(code, message, sensitive=False, details=None):
    result = {"code": code, "message": message, "sensitive": sensitive}
    if details is not None:
        result["details"] = details
    return result


class Kernel:
    def __init__(self, store, checkpoint=lambda: None, ports=LOGIC_PORTS):
        self.ports = ports
        self._observed = False
        self._tainted = set()
        self.store = store
        self.checkpoint = checkpoint

    def _program(self, change, frame):
        return change.run.programs[frame.workflow]

    def _context(self, change, frame, *, loop=None):
        runtime = {
            "runVariablesVersion": Value.freeze(change.run.shared_version),
            "iterationIndex": Value.freeze(frame.loop_index),
            "loop": Value.build(
                {
                    "index": Value.freeze(frame.loop_index),
                    "lastOutput": frame.loop_last,
                }
            ),
        }
        if loop is not None:
            runtime["loop"] = Value.build(
                {
                    "index": Value.freeze(loop),
                    "lastOutput": frame.iteration_result,
                }
            )
        return Value.build(
            {
                "inputs": frame.inputs,
                "vars": frame.variables,
                "run": change.run.shared,
                "prev": frame.previous,
                "runtime": Value.build(runtime),
            }
        )

    def _call_value(self, change, frame, name):
        key = frame.id, "calls", name
        if key in change.values:
            return change.values[key]
        return self.store.value(
            change.run.id, frame.id, "calls", name, version=change.run.data_version
        )

    def _call_names(self, change, frame):
        after = None
        while True:
            self.checkpoint()
            page = self.store.value_names(
                change.run.id, frame.id, "calls", change.run.data_version, after=after
            )
            if not page:
                return
            yield from page
            after = page[-1]

    def _resolve_calls(self, change, frame, path):
        if len(path) > 1:
            if type(path[1]) is not str:
                raise PathMissing(1)
            value = self._call_value(change, frame, path[1])
            if value is None or not value.present:
                raise PathMissing(1, value.sensitive if value is not None else False)
            try:
                return value.lookup(path[2:])
            except PathMissing as error:
                raise PathMissing(error.index + 2, error.sensitive) from error
        names = sorted(
            name
            for (identity, scope, name) in change.values
            if identity == frame.id and scope == "calls"
        )
        data, absent, previous, size, sensitive = {}, set(), None, 2, False
        for name in merge(self._call_names(change, frame), names):
            self.checkpoint()
            if name == previous:
                continue
            previous = name
            value = self._call_value(change, frame, name)
            if value is None:
                continue
            sensitive |= value.sensitive
            if not value.present:
                absent.add(name)
                continue
            size += (
                len(dumps(name))
                + len(dumps(value.raw(), checkpoint=self.checkpoint))
                + 1
                + bool(data)
            )
            if len(data) >= 1000 or size > MAX_FRAME:
                error = DeskWeaveError("LIMIT_EXCEEDED", "调用结果集合超过表达式结果限制。")
                error.sensitive = sensitive
                raise error
            data[name] = value
        return Value.build(data, absent=frozenset(absent))

    def _taint_region(self, change, frame, join):
        identity = frame.id, frame.node, join
        if identity in self._tainted:
            return
        self._tainted.add(identity)
        program = self._program(change, frame)
        region = program.regions[join]
        covered = [
            target
            for target, _marked in frame.control
            if program.regions[target][0] <= region[0] and region[1] <= program.regions[target][1]
        ]
        if any(frame.taint_epochs.get(target) == change.run.label_epoch for target in covered):
            return
        local_effects = not covered
        frame.taint_epochs[join] = change.run.label_epoch
        effects, visited = {}, set()
        pending = [(frame.workflow, frame.node, True)]
        while pending:
            self.checkpoint()
            workflow, node_id, local = pending.pop()
            key = workflow, node_id, local
            if node_id is None or (local and node_id == join) or key in visited:
                continue
            visited.add(key)
            program = change.run.programs[workflow]
            relevant = program.effects if local and local_effects else program.shared_effects
            if not relevant[node_id]:
                continue
            node = program.nodes[node_id]
            action, params = node["action"], node["params"]
            if action == "data.assign":
                scope = "shared" if params.get("scope") == "run" else "local"
                if (local and local_effects) or scope == "shared":
                    effects.setdefault((scope, params["name"]), set()).add(
                        tuple(params.get("path", []))
                    )
            if local and local_effects and "store" in node:
                effects.setdefault(("local", node["store"]), set()).add(())
            if action in {"flow.call", "flow.repeat", "flow.loop"}:
                if local and local_effects:
                    effects.setdefault(("calls", node_id), set()).add(())
                binding = change.run.plan["_bindings"][workflow][params["workflow"]]
                target = binding["workflowId"], binding["revision"]
                pending.append((target, change.run.programs[target].definition["entry"], False))
            elif local and local_effects and action == "flow.join":
                effects.setdefault(("calls", node_id), set()).add(())
            pending.extend((workflow, target, local) for target in node["on"].values())
        for scope, name in effects:
            if scope != "calls":
                continue
            self.checkpoint()
            value = self._call_value(change, frame, name)
            value = Value.build(None, True, present=False) if value is None else value.tainted(())
            change.value(frame.id, "calls", name, value)
        roots = {"local": frame.variables, "shared": change.run.shared}
        for scope, root in roots.items():
            selected = [(name, paths) for (kind, name), paths in effects.items() if kind == scope]
            if not selected:
                continue
            data, absent = dict(root.data), set(root.absent)
            for name, paths in selected:
                self.checkpoint()
                if name not in data:
                    absent.add(name)
                    value = Value.build(None, True, present=False)
                else:
                    value = data[name]
                    for path in paths:
                        value = value.tainted(path)
                    data[name] = value
                change.value("" if scope == "shared" else frame.id, scope, name, value)
            updated = Value.build(data, root.label, absent=frozenset(absent))
            if scope == "shared":
                change.run.shared = updated
            else:
                frame.variables = updated

    def _guard(self, change, frame, sensitive):
        if sensitive:
            self._taint_region(change, frame, self._program(change, frame).error_joins[frame.node])
            item = (self._program(change, frame).error_joins[frame.node], True)
            if item not in frame.control:
                frame.control += (item,)

    def _value(self, change, frame, expression, expected="any", *, loop=None):
        change.run.access.revalidate()
        context = self._context(change, frame, loop=loop)
        result = evaluate_value(
            expression,
            lambda path: (
                self._resolve_calls(change, frame, path)
                if path and path[0] == "calls"
                else context.lookup(path)
            ),
            checkpoint=self.checkpoint,
            observe_control=lambda sensitive: self._guard(change, frame, sensitive),
        )
        self._observed |= result.sensitive
        self._guard(
            change,
            frame,
            result.size_sensitive()
            or (expected != "any" and result.sensitive and not result.typed),
        )
        return validate_result(result, expected, checkpoint=self.checkpoint)

    def _integer(self, change, frame, expression, minimum, maximum, *, loop=None):
        value = self._value(change, frame, expression, loop=loop)
        self._guard(change, frame, value.sensitive)
        if not matches_type(value.raw(), "integer") or not minimum <= value.data <= maximum:
            error = DeskWeaveError("INVALID_ARGUMENT", "动作整数参数超出范围。")
            error.sensitive = value.sensitive
            raise error
        return int(value.data), value.sensitive

    def _object(self, change, frame, expressions, *, loop=None):
        values = {}
        try:
            for key, expression in expressions.items():
                values[key] = self._value(change, frame, expression, loop=loop)
        except DeskWeaveError as error:
            error.sensitive = getattr(error, "sensitive", False) or any(
                value.sensitive for value in values.values()
            )
            raise
        result = Value.build(values)
        self._guard(change, frame, result.size_sensitive())
        return validate_result(result, checkpoint=self.checkpoint)

    def _variable(self, change, frame, name, value):
        if name not in frame.variables.data and len(frame.variables.data) >= 1000:
            raise DeskWeaveError("LIMIT_EXCEEDED", "局部变量数量超过限制。")
        frame.variables = frame.variables.updated([name], value)
        change.value(frame.id, "local", name, value)

    def _previous(self, change, frame, value):
        frame.previous = value
        change.value(frame.id, "previous", "", value)

    def _complete(self, change, frame, node, value, target=None, *, explicit_target=False):
        value = validate_result(
            value.marked(frame.sensitive, conditional=True), checkpoint=self.checkpoint
        )
        self._previous(change, frame, value)
        if "store" in node:
            self._variable(change, frame, node["store"], value)
        if node["action"] in {"flow.call", "flow.join", "flow.repeat", "flow.loop"}:
            change.value(frame.id, "calls", node["id"], value)
        frame.phase = {}
        frame.state = "ready"
        target = target if explicit_target else node["on"].get("next")
        change.events.append(("node.completed", {"frameId": frame.id, "nodeId": node["id"]}))
        if target is None:
            self._collect(change, frame, "succeeded", EMPTY)
        else:
            frame.node = target
            change.ready.add(frame.id)

    def _error(self, change, frame, error, *, child=None):
        error = {
            **error,
            "sensitive": error.get("sensitive", False)
            or frame.sensitive
            or self._observed
            or frame.phase.get("sensitive", False),
        }
        node = self._program(change, frame).nodes[frame.node]
        self._guard(change, frame, error["sensitive"])
        if child is not None:
            outcome = self.store.outcome(change.run.id, child)
            if node["on"].get("error") and outcome["parent"]:
                change.handled.add((outcome["parent"], child))
        self._previous(
            change,
            frame,
            Value.freeze(
                {
                    "error": {
                        "code": error["code"],
                        "message": error["message"],
                        **({"details": error["details"]} if "details" in error else {}),
                    }
                }
            ).marked(error.get("sensitive", False) or frame.sensitive),
        )
        change.events.append(
            (
                "node.failed",
                {
                    "frameId": frame.id,
                    "nodeId": node["id"],
                    "code": error["code"],
                },
            )
        )
        target = node["on"].get("error")
        if target is None:
            self._collect(change, frame, "failed", NULL, error)
            return
        join = self._program(change, frame).error_joins[frame.node]
        if error.get("sensitive", False) and (join, True) not in frame.control:
            frame.control += ((join, True),)
        frame.node, frame.state, frame.phase = target, "ready", {}
        change.ready.add(frame.id)

    def _finish(self, change, frame, state):
        frame.state, frame.phase = state, {}
        change.value(frame.id, "outputs", "", frame.outputs)
        change.removed.add(frame.id)
        change.events.append(("frame.completed", {"frameId": frame.id, "state": state}))
        if frame.background:
            change.run.background -= 1
            ancestor = frame.parent
            while ancestor is not None:
                current = change.edit(ancestor)
                current.parallel -= 1
                ancestor = current.parent
        if frame.parent is not None:
            parent = change.edit(frame.parent)
            parent.children = parent.children - {frame.id}
            if state != "succeeded":
                change.unhandled.add((parent.id, frame.id))
            if parent.state in {"joining", "collecting"}:
                change.ready.add(parent.id)
        else:
            change.run.state, change.run.error = state, frame.error
            change.run.control_version += 1

    def _collect(self, change, frame, state, output, error=None):
        frame.outputs = output.marked(frame.sensitive, conditional=True)
        frame.error = (
            None
            if error is None
            else {**error, "sensitive": error.get("sensitive", False) or frame.sensitive}
        )
        frame.state, frame.phase = "collecting", {"kind": "collect", "goal": state}
        if state != "succeeded" and frame.children:
            for child in tuple(frame.children):
                self._cancel_tree(change, child, fault("CANCELLED", "父调用已结束。"))
        if frame.children:
            return
        if state == "succeeded":
            unhandled = self.store.unhandled(change.run.id, frame.id)
            if unhandled is not None:
                state, frame.error = "failed", unhandled
        self._finish(change, frame, state)

    def _cancel_tree(self, change, root, error):
        order, pending = [], [root]
        while pending:
            identity = pending.pop()
            frame = change.get(identity)
            order.append(identity)
            pending.extend(frame.children)
        for identity in reversed(order):
            frame = change.edit(identity)
            frame.error, frame.outputs, frame.children = error, NULL, frozenset()
            self._finish(change, frame, "cancelled")

    def _spawn(self, change, parent, node, inputs, now, *, background=False, loop=False):
        binding = change.run.plan["_bindings"][parent.workflow][node["params"]["workflow"]]
        key = binding["workflowId"], binding["revision"]
        definition = change.run.programs[key].definition
        if parent.depth >= parent.depth_limit:
            raise DeskWeaveError("LIMIT_EXCEEDED", "调用深度超过限制。")
        if background:
            if change.run.background >= change.run.plan["limits"]["parallelCalls"]:
                raise DeskWeaveError("LIMIT_EXCEEDED", "后台调用数量超过限制。")
            ancestor = parent.id
            while ancestor is not None:
                current = change.get(ancestor)
                if (
                    current.parallel
                    >= self._program(change, current).definition["limits"]["parallelCalls"]
                ):
                    raise DeskWeaveError("LIMIT_EXCEEDED", "子流程后台调用数量超过限制。")
                ancestor = current.parent
        if not inputs.data.keys() <= definition["parameters"].keys():
            raise DeskWeaveError("INVALID_ARGUMENT", "子流程包含未声明的输入参数。")
        values = {}
        for name, spec in definition["parameters"].items():
            value = inputs.data.get(name)
            if value is None and "default" in spec:
                value = Value.freeze(spec["default"])
            if value is None:
                if spec["required"]:
                    raise DeskWeaveError("INVALID_ARGUMENT", "子流程缺少必填输入参数。")
                continue
            self._guard(change, parent, value.sensitive)
            if not matches_type(value.raw(), spec["type"]):
                error = DeskWeaveError("INVALID_ARGUMENT", "子流程输入参数类型不匹配。")
                error.sensitive = value.sensitive
                raise error
            values[name] = value.marked(spec["sensitive"]).declared(spec["type"])
        effective_inputs = validate_result(Value.build(values), checkpoint=self.checkpoint)
        parent_targets = parent.targets
        targets = {}
        for name, spec in definition["targets"].items():
            if name in parent_targets and parent_targets[name][1] == spec["kind"]:
                targets[name] = parent_targets[name]
                change.run.access.target(name, *targets[name])
            elif spec["required"]:
                raise DeskWeaveError("INVALID_ARGUMENT", "子流程缺少同类型的必需目标。")
        if (
            definition.get("kind") == "loopBody"
            and not loop
            and (background or not parent.loop_owner)
        ):
            raise DeskWeaveError("INVALID_WORKFLOW", "循环体需要同步循环上下文。")
        identity = uuid.uuid4().hex
        child = Frame(
            identity,
            parent.id,
            key,
            definition["entry"],
            inputs=effective_inputs,
            variables=Value.freeze(definition["variables"]),
            inherited=parent.sensitive,
            depth=parent.depth + 1,
            depth_limit=min(parent.depth_limit, parent.depth + definition["limits"]["callDepth"]),
            deadline=min(parent.deadline, now + definition["limits"]["activeMs"] / 1000),
            background=background,
            targets=targets,
            loop_owner=parent.id if loop else None if background else parent.loop_owner,
            loop_index=parent.phase["index"] if loop else parent.loop_index,
            loop_last=parent.iteration_result if loop else parent.loop_last,
        )
        change.value(identity, "runtime", "loopLast", child.loop_last)
        change.new.add(identity)
        change.frames[identity] = child
        parent.children |= {identity}
        if background:
            change.run.background += 1
            ancestor = parent.id
            while ancestor is not None:
                current = change.edit(ancestor)
                current.parallel += 1
                ancestor = current.parent
        for scope, values in (("inputs", child.inputs), ("local", child.variables)):
            for name, value in values.data.items():
                change.value(identity, scope, name, value)
        change.ready.add(identity)
        return identity

    def _loop(self, change, frame, node, now):
        phase, params = frame.phase, node["params"]
        self._observed |= phase["sensitive"]
        child = phase.get("child")
        if now >= phase["deadline"]:
            error = fault("LOOP_LIMIT", "循环执行时间超过限制。", frame.sensitive)
            if child in change.run.frames:
                self._cancel_tree(change, child, error)
                frame.phase = {"kind": "cancel", "child": child, "error": error}
                change.ready.add(frame.id)
            else:
                self._error(change, frame, error, child=child)
            return
        if child:
            if child in change.run.frames and child not in change.removed:
                return
            result = self.store.outcome(change.run.id, child)
            if result["state"] != "succeeded":
                self._error(change, frame, result["error"], child=child)
                return
            phase["sensitive"] |= result["metadata"].get("outcomeSensitive", False)
            frame.iteration_result = result["output"]
            change.value(frame.id, "runtime", "iterationResult", frame.iteration_result)
            phase["iterations"] += 1
            phase["child"] = None
            signal = result["metadata"]["signal"]
            if signal:
                if result["metadata"]["signalOwner"] != frame.id:
                    frame.signal, frame.signal_owner = signal, result["metadata"]["signalOwner"]
                    self._collect(change, frame, "succeeded", frame.iteration_result)
                    return
                if signal == "break":
                    self._loop_done(change, frame, node)
                    return
            if phase["mode"] == "until":
                condition = self._value(
                    change, frame, params["condition"], "boolean", loop=phase["index"]
                )
                phase["sensitive"] |= condition.sensitive
                self._guard(change, frame, phase["sensitive"])
                if condition.data:
                    self._loop_done(change, frame, node)
                    return
            phase["index"] += 1
        if phase["mode"] == "repeat":
            if phase["index"] >= phase["limit"]:
                self._loop_done(change, frame, node)
                return
        elif phase["mode"] == "while":
            condition = self._value(
                change, frame, params["condition"], "boolean", loop=phase["index"]
            )
            phase["sensitive"] |= condition.sensitive
            self._guard(change, frame, phase["sensitive"])
            if not condition.data:
                self._loop_done(change, frame, node)
                return
        if phase["index"] >= phase["limit"]:
            self._error(
                change, frame, fault("LOOP_LIMIT", "循环次数超过限制。", phase["sensitive"])
            )
            return
        inputs = self._object(change, frame, params["inputs"], loop=phase["index"])
        child = self._spawn(change, frame, node, inputs, now, loop=True)
        change.frames[child].inherited |= phase["sensitive"]
        phase["child"] = child
        frame.state = "joining"

    def _loop_done(self, change, frame, node):
        self._guard(change, frame, frame.phase["sensitive"])
        value = Value.build(
            {
                "iterations": Value.freeze(frame.phase["iterations"]),
                "lastOutput": frame.iteration_result,
            }
        ).marked(frame.phase["sensitive"])
        self._complete(change, frame, node, value)

    def _cycle(self, change, source, target):
        pending, visited = [target], set()
        while pending:
            identity = pending.pop()
            if identity == source:
                return True
            if identity in visited or identity not in change.run.frames:
                continue
            visited.add(identity)
            frame = change.get(identity)
            if frame.phase.get("child"):
                pending.append(frame.phase["child"])
            if frame.state == "collecting":
                pending.extend(frame.children)
        ancestor = change.get(source).parent
        while ancestor is not None:
            if ancestor == target:
                return True
            ancestor = change.get(ancestor).parent
        return False

    def step(self, run, identity, wall_now):
        self._observed = False
        self._tainted = set()
        change = Change(replace(run))
        frame = change.edit(identity)
        now = run.active(wall_now)
        node = self._program(change, frame).nodes[frame.node]
        try:
            run.access.revalidate()
            if frame.state == "collecting":
                self._collect(change, frame, frame.phase["goal"], frame.outputs, frame.error)
                return change
            if now >= frame.deadline:
                self._collect(
                    change, frame, "failed", NULL, fault("LIMIT_EXCEEDED", "调用执行时间超过限制。")
                )
                return change
            kind = frame.phase.get("kind")
            if kind == "wait":
                if now >= frame.phase["until"]:
                    self._complete(
                        change,
                        frame,
                        node,
                        Value.freeze(
                            {
                                "elapsedActiveMs": int((now - frame.phase["since"]) * 1000),
                            }
                        ).marked(frame.phase["sensitive"]),
                    )
                return change
            if kind == "cancel":
                self._error(change, frame, frame.phase["error"], child=frame.phase["child"])
                return change
            if kind in {"call", "join"}:
                child = frame.phase["child"]
                if child in run.frames:
                    if kind == "join" and now >= frame.phase["until"]:
                        error = fault("TIMEOUT", "等待子流程超时。", frame.phase["sensitive"])
                        self._cancel_tree(change, child, error)
                        frame.phase = {"kind": "cancel", "child": child, "error": error}
                        change.ready.add(frame.id)
                    return change
                result = self.store.outcome(run.id, child)
                self._guard(change, frame, result["metadata"].get("outcomeSensitive", False))
                if result["state"] != "succeeded":
                    self._error(change, frame, result["error"], child=child)
                elif result["metadata"]["signal"]:
                    frame.signal = result["metadata"]["signal"]
                    frame.signal_owner = result["metadata"]["signalOwner"]
                    self._collect(change, frame, "succeeded", result["output"])
                else:
                    self._complete(
                        change,
                        frame,
                        node,
                        result["output"].marked(frame.phase.get("sensitive", False)),
                    )
                return change
            if kind == "loop":
                self._loop(change, frame, node, now)
                return change
            frame.control = tuple(item for item in frame.control if item[0] != frame.node)
            frame.taint_epochs.pop(frame.node, None)
            if run.steps >= run.plan["limits"]["steps"]:
                return self.terminate(run, "failed", fault("LIMIT_EXCEEDED", "运行步骤超过限制。"))
            ancestor = frame.id
            while ancestor is not None:
                current = change.edit(ancestor)
                if current.steps >= self._program(change, current).definition["limits"]["steps"]:
                    self._collect(
                        change, frame, "failed", NULL, fault("LIMIT_EXCEEDED", "调用步骤超过限制。")
                    )
                    return change
                current.steps += 1
                ancestor = current.parent
            change.run.steps += 1
            frame.visit += 1
            self.ports.execute(self, change, frame, node, now)
        except DeskWeaveError as error:
            sensitive = (
                getattr(error, "sensitive", False)
                or frame.sensitive
                or self._observed
                or frame.phase.get("sensitive", False)
            )
            self._tainted = set()
            failed = Change(replace(run, steps=change.run.steps))
            for identity, updated in change.frames.items():
                if identity in run.frames:
                    clean = failed.edit(identity)
                    clean.steps = updated.steps
                    clean.visit = updated.visit
            frame = failed.edit(frame.id)
            frame.control = tuple(item for item in frame.control if item[0] != frame.node)
            self._error(failed, frame, fault(error.code, error.message, sensitive, error.details))
            change = failed
        return change

    def terminate(self, run, state, error=None):
        change = Change(replace(run))
        root = change.edit(run.root)
        for child in tuple(root.children):
            self._cancel_tree(change, child, error or fault("CANCELLED", "运行已停止。"))
        root = change.edit(run.root)
        root.children, root.outputs, root.error = frozenset(), NULL, error
        self._finish(change, root, state)
        return change
