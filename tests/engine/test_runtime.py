import sqlite3
import time
from dataclasses import replace
from unittest.mock import patch

from deskweave.domain.frozen import Value
from deskweave.domain.values import evaluate_value
from deskweave.engine.run_planning import RunAccess, RunService
from deskweave.engine.scheduler import Runtime
from deskweave.errors import DeskWeaveError
from tests.engine.test_runs import definition
from tests.engine.test_service import EngineCase


def path(*keys):
    return {"$expr": {"op": "path", "path": list(keys)}}


def operation(op, *args):
    return {"$expr": {"op": op, "args": list(args)}}


def node(identity, action, params, **outlets):
    return {"id": identity, "title": identity, "action": action, "params": params, "on": outlets}


def flow(identity, nodes, **fields):
    value = definition(identity)
    value.update(nodes=nodes, entry=nodes[0]["id"], **fields)
    return value


class RuntimeTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.access = RunAccess(
            "runner",
            "grant",
            1,
            frozenset({"run.execute", "run.read", "workflow.read", "asset.read"}),
        )
        self.runtime = Runtime(self.workspace, clock=lambda: self.elapsed[0], threaded=False)
        self.planner = RunService(self.workspace)

    def tearDown(self):
        self.runtime.close()
        super().tearDown()

    def save_flow(self, value, children=None):
        return self.success(
            "workflow.save",
            {
                "workflowId": value["id"],
                "draftVersion": 1,
                "baseRevision": None,
                "definition": value,
                "dependencies": children or {},
            },
        )["revision"]

    def start(self, value, children=None, **params):
        revision = self.save_flow(value, children)
        accepted = self.planner.accept(
            self.access,
            value["id"],
            {
                "workflowId": value["id"],
                "revision": revision,
                "inputs": {},
                "targets": {},
                "detached": False,
                **params,
            },
        )
        self.runtime.attach(accepted["runId"], self.access)
        return accepted["runId"]

    def child(self, value):
        return {"kind": "workflow", "workflowId": value["id"], "revision": self.save_flow(value)}

    def result(self, run):
        self.runtime.pump()
        return self.runtime.result(run, access=self.access)

    def test_assignment_branch_assert_and_return(self):
        run = self.start(
            flow(
                "logic",
                [
                    node(
                        "assign",
                        "data.assign",
                        {"name": "answer", "value": operation("add", 40, 2)},
                        next="check",
                    ),
                    node(
                        "check",
                        "logic.assert",
                        {"condition": operation("equal", path("vars", "answer"), 42)},
                        next="branch",
                    ),
                    node("branch", "logic.branch", {"condition": True}, yes="yes", no="no"),
                    node("yes", "flow.return", {"values": {"answer": path("vars", "answer")}}),
                    node("no", "flow.return", {"values": {"answer": 0}}),
                ],
            )
        )
        result = self.result(run)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["output"]["value"], {"answer": 42})
        self.assertFalse(self.runtime._active)

    def test_pause_freezes_wait_and_resume_preserves_remaining_time(self):
        run = self.start(
            flow(
                "wait",
                [
                    node("wait", "time.wait", {"durationMs": 1000}, next="done"),
                    node("done", "flow.return", {"values": {"ok": True}}),
                ],
            )
        )
        self.runtime.pump()
        self.elapsed[0] = 0.4
        future = self.runtime.control(
            run,
            "pause",
            self.runtime.read_state(run, access=self.access)["controlVersion"],
            access=self.access,
        )
        self.runtime.tick()
        self.assertEqual(future.result()["state"], "paused")
        self.elapsed[0] = 100.4
        self.runtime.pump()
        self.assertEqual(self.runtime.read_state(run, access=self.access)["state"], "paused")
        future = self.runtime.control(
            run,
            "resume",
            self.runtime.read_state(run, access=self.access)["controlVersion"],
            access=self.access,
        )
        self.runtime.tick()
        self.assertEqual(future.result()["state"], "running")
        self.elapsed[0] = 100.9
        self.runtime.pump()
        self.assertEqual(self.runtime.read_state(run, access=self.access)["state"], "running")
        self.elapsed[0] = 101.01
        self.assertEqual(self.result(run)["state"], "succeeded")

    def test_synchronous_call_defaults_and_isolated_locals(self):
        child = self.child(
            flow(
                "child",
                [
                    node(
                        "return",
                        "flow.return",
                        {"values": {"x": path("inputs", "x"), "local": path("vars", "x")}},
                    )
                ],
                parameters={
                    "x": {"type": "integer", "required": True, "sensitive": False, "default": 7}
                },
                variables={"x": 9},
            )
        )
        call = node(
            "call", "flow.call", {"workflow": "child", "inputs": {}, "mode": "wait"}, next="done"
        )
        call["store"] = "answer"
        run = self.start(
            flow(
                "parent",
                [
                    call,
                    node(
                        "done",
                        "flow.return",
                        {"values": {"child": path("vars", "answer"), "local": path("vars", "x")}},
                    ),
                ],
                resources={"child": {"kind": "workflow"}},
                variables={"x": 3},
            ),
            {"child": child},
        )
        self.assertEqual(
            self.result(run)["output"]["value"], {"child": {"x": 7, "local": 9}, "local": 3}
        )

    def test_background_join_twice_and_parent_waits(self):
        child = self.child(flow("child", [node("done", "flow.return", {"values": {"x": 8}})]))
        call = node(
            "call", "flow.call", {"workflow": "child", "inputs": {}, "mode": "start"}, next="join"
        )
        call["store"] = "handle"
        first = node(
            "join", "flow.join", {"handle": path("vars", "handle"), "timeoutMs": 1000}, next="again"
        )
        first["store"] = "one"
        second = node(
            "again", "flow.join", {"handle": path("vars", "handle"), "timeoutMs": 0}, next="done"
        )
        second["store"] = "two"
        run = self.start(
            flow(
                "parent",
                [
                    call,
                    first,
                    second,
                    node(
                        "done",
                        "flow.return",
                        {"values": {"one": path("vars", "one"), "two": path("vars", "two")}},
                    ),
                ],
                resources={"child": {"kind": "workflow"}},
            ),
            {"child": child},
        )
        self.assertEqual(self.result(run)["output"]["value"], {"one": {"x": 8}, "two": {"x": 8}})
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM frame WHERE run_id=?", (run,)).fetchone()[
                    0
                ],
                2,
            )

    def test_explicit_child_failure_recovery(self):
        child = self.child(flow("child", [node("fail", "logic.assert", {"condition": False})]))
        run = self.start(
            flow(
                "parent",
                [
                    node(
                        "call",
                        "flow.call",
                        {"workflow": "child", "inputs": {}, "mode": "wait"},
                        error="recover",
                    ),
                    node("recover", "flow.return", {"values": {"recovered": True}}),
                ],
                resources={"child": {"kind": "workflow"}},
            ),
            {"child": child},
        )
        self.assertEqual(self.result(run)["state"], "succeeded")

    def test_unhandled_background_failure_fails_parent(self):
        child = self.child(flow("child", [node("fail", "logic.assert", {"condition": False})]))
        run = self.start(
            flow(
                "parent",
                [
                    node(
                        "call",
                        "flow.call",
                        {"workflow": "child", "inputs": {}, "mode": "start"},
                        next="done",
                    ),
                    node("done", "flow.return", {"values": {"ok": True}}),
                ],
                resources={"child": {"kind": "workflow"}},
            ),
            {"child": child},
        )
        self.assertEqual(self.result(run)["state"], "failed")

    def test_repeat_and_zero_repeat(self):
        child = self.child(
            flow(
                "child",
                [
                    node(
                        "add",
                        "data.assign",
                        {
                            "name": "count",
                            "scope": "run",
                            "value": operation("add", path("run", "count"), 1),
                        },
                        next="done",
                    ),
                    node(
                        "done",
                        "flow.return",
                        {"values": {"index": path("runtime", "iterationIndex")}},
                    ),
                ],
            )
        )
        repeat = node(
            "repeat", "flow.repeat", {"workflow": "child", "inputs": {}, "count": 3}, next="zero"
        )
        repeat["store"] = "result"
        run = self.start(
            flow(
                "parent",
                [
                    repeat,
                    node(
                        "zero",
                        "flow.repeat",
                        {"workflow": "child", "inputs": {}, "count": 0},
                        next="done",
                    ),
                    node(
                        "done",
                        "flow.return",
                        {
                            "values": {
                                "count": path("run", "count"),
                                "result": path("vars", "result"),
                            }
                        },
                    ),
                ],
                resources={"child": {"kind": "workflow"}},
                runVariables={"count": 0},
            ),
            {"child": child},
        )
        self.assertEqual(
            self.result(run)["output"]["value"],
            {"count": 3, "result": {"iterations": 3, "lastOutput": {"index": 2}}},
        )

    def test_loop_break_and_until(self):
        child = self.child(
            flow("body", [node("exit", "flow.break", {"values": {"found": True}})], kind="loopBody")
        )
        loop = node(
            "loop",
            "flow.loop",
            {
                "workflow": "body",
                "inputs": {},
                "mode": "until",
                "condition": False,
                "maxIterations": 3,
                "timeoutMs": 1000,
            },
            next="done",
        )
        loop["store"] = "result"
        run = self.start(
            flow(
                "parent",
                [loop, node("done", "flow.return", {"values": {"result": path("vars", "result")}})],
                resources={"body": {"kind": "workflow"}},
            ),
            {"body": child},
        )
        self.assertEqual(
            self.result(run)["output"]["value"]["result"],
            {"iterations": 1, "lastOutput": {"found": True}},
        )

    def test_shared_version_conflict_can_be_handled(self):
        run = self.start(
            flow(
                "shared",
                [
                    node(
                        "one", "data.assign", {"scope": "run", "name": "x", "value": 1}, next="two"
                    ),
                    node(
                        "two",
                        "data.assign",
                        {"scope": "run", "name": "x", "value": 2, "expectedVersion": 0},
                        error="done",
                    ),
                    node(
                        "done",
                        "flow.return",
                        {"values": {"x": path("run", "x"), "error": path("prev", "error", "code")}},
                    ),
                ],
                runVariables={"x": 0},
            )
        )
        self.assertEqual(
            self.result(run)["output"]["value"], {"x": 1, "error": "VARIABLE_CONFLICT"}
        )

    def test_sensitive_branch_join_and_full_overwrite(self):
        run = self.start(
            flow(
                "sensitive",
                [
                    node(
                        "choose",
                        "logic.branch",
                        {"condition": path("inputs", "secret")},
                        yes="yes",
                        no="no",
                    ),
                    node("yes", "data.assign", {"name": "selected", "value": 1}, next="public"),
                    node("no", "data.assign", {"name": "selected", "value": 2}, next="public"),
                    node("public", "data.assign", {"name": "clean", "value": 3}, next="replace"),
                    node("replace", "data.assign", {"name": "secretCopy", "value": 4}, next="done"),
                    node("done", "flow.return", {"values": {"ok": True}}),
                ],
                parameters={"secret": {"type": "boolean", "required": True, "sensitive": True}},
                variables={"secretCopy": 8},
            ),
            inputs={"secret": True},
        )
        self.result(run)
        self.assertEqual(
            self.runtime.read_value(run, "local", "selected", access=self.access)["visibility"],
            "redacted",
        )
        self.assertEqual(
            self.runtime.read_value(run, "local", "clean", access=self.access)["value"], 3
        )
        self.assertEqual(
            self.runtime.read_value(run, "local", "secretCopy", access=self.access)["value"], 4
        )

    def test_stop_beats_queued_resume(self):
        run = self.start(flow("wait", [node("wait", "time.wait", {"durationMs": 10000})]))
        self.runtime.pump()
        pause = self.runtime.control(
            run,
            "pause",
            self.runtime.read_state(run, access=self.access)["controlVersion"],
            access=self.access,
        )
        self.runtime.tick()
        version = pause.result()["controlVersion"]
        resume = self.runtime.control(run, "resume", version, access=self.access)
        stop = self.runtime.control(run, "stop", version, access=self.access)
        self.runtime.pump()
        self.assertEqual(stop.result()["state"], "cancelled")
        with self.assertRaises(DeskWeaveError):
            resume.result()

    def test_revocation_cancels_paused_tree(self):
        live = [True]

        def check():
            if not live[0]:
                raise DeskWeaveError("ACCESS_DENIED", "授权已撤销。")

        self.access = replace(self.access, revalidate=check)
        run = self.start(flow("wait", [node("wait", "time.wait", {"durationMs": 10000})]))
        self.runtime.pump()
        live[0] = False
        self.runtime.pump()
        self.assertEqual(self.runtime.store.snapshot(run)["state"], "cancelled")
        with self.assertRaises(DeskWeaveError) as denied:
            self.runtime.read_state(run, access=self.access)
        self.assertEqual(denied.exception.code, "ACCESS_DENIED")

    def test_commit_failure_retries_original_step(self):
        run = self.start(
            flow(
                "atomic",
                [
                    node(
                        "assign",
                        "data.assign",
                        {
                            "scope": "run",
                            "name": "x",
                            "value": operation("add", path("run", "x"), 1),
                        },
                        next="done",
                    ),
                    node("done", "flow.return", {"values": {"x": path("run", "x")}}),
                ],
                runVariables={"x": 0},
            )
        )
        self.runtime.tick()
        before = self.runtime.read_state(run, access=self.access)
        with self.workspace.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER reject_execution BEFORE INSERT ON run_event BEGIN "
                "SELECT RAISE(ABORT,'storage failed'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.tick()
        self.assertEqual(self.runtime.read_state(run, access=self.access), before)
        self.assertEqual(
            self.runtime.read_value(run, "shared", "x", access=self.access)["value"], 0
        )
        with self.workspace.transaction() as connection:
            connection.execute("DROP TRIGGER reject_execution")
        self.assertEqual(self.result(run)["output"]["value"], {"x": 1})

    def test_unsupported_action_rejected_before_run_registration(self):
        value = flow(
            "native",
            [node("capture", "screen.capture", {"target": "screen"})],
            targets={"screen": {"kind": "display", "required": True}},
        )
        revision = self.save_flow(value)
        with self.assertRaises(DeskWeaveError) as caught:
            self.planner.accept(
                self.access,
                "native",
                {
                    "workflowId": "native",
                    "revision": revision,
                    "inputs": {},
                    "targets": {"screen": "target"},
                    "detached": False,
                },
            )
        self.assertEqual(caught.exception.code, "METHOD_UNAVAILABLE")
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM run").fetchone()[0], 0)

    def test_unread_values_are_not_serialized_each_step(self):
        run = self.start(
            flow(
                "values",
                [
                    node("assign", "data.assign", {"name": "x", "value": 1}, next="done"),
                    node("done", "flow.return", {"values": {"x": path("vars", "x")}}),
                ],
                variables={"unused": ["x" * 1000] * 100},
            )
        )
        unused = (
            self.runtime._active[run]
            .frames[self.runtime._active[run].root]
            .variables.data["unused"]
        )
        original = Value.raw

        def raw(value):
            self.assertIsNot(value, unused)
            return original(value)

        with patch.object(Value, "raw", raw):
            self.assertEqual(self.result(run)["state"], "succeeded")

    def test_sensitive_short_circuit_and_missing_path(self):
        context = Value.freeze({"secret": {"ok": True}}).updated(
            ["secret"], Value.freeze({"ok": True}).marked(True)
        )
        self.assertFalse(
            evaluate_value(operation("and", False, path("secret", "ok")), context.lookup).sensitive
        )
        result = evaluate_value(operation("coalesce", path("secret", "missing"), 7), context.lookup)
        self.assertTrue(result.sensitive)
        self.assertEqual(result.raw(), 7)

    def test_wait_completion_uses_no_extra_node_budget(self):
        value = flow("one", [node("wait", "time.wait", {"durationMs": 10})])
        value["limits"]["steps"] = 1
        run = self.start(value)
        self.runtime.pump()
        self.elapsed[0] = 0.02
        self.assertEqual(self.result(run)["state"], "succeeded")

    def test_global_step_budget_stops_tree(self):
        child = self.child(flow("child", [node("done", "flow.return", {"values": {}})]))
        value = flow(
            "one",
            [node("call", "flow.call", {"workflow": "child", "inputs": {}, "mode": "wait"})],
            resources={"child": {"kind": "workflow"}},
        )
        value["limits"]["steps"] = 1
        run = self.start(value, {"child": child})
        self.assertEqual(self.result(run)["state"], "failed")
        self.assertFalse(self.runtime._active)

    def test_join_timeout_is_recoverable_after_child_cancel(self):
        child = self.child(flow("child", [node("wait", "time.wait", {"durationMs": 10000})]))
        call = node(
            "call", "flow.call", {"workflow": "child", "inputs": {}, "mode": "start"}, next="join"
        )
        call["store"] = "handle"
        run = self.start(
            flow(
                "parent",
                [
                    call,
                    node(
                        "join",
                        "flow.join",
                        {"handle": path("vars", "handle"), "timeoutMs": 10},
                        error="done",
                    ),
                    node("done", "flow.return", {"values": {"recovered": True}}),
                ],
                resources={"child": {"kind": "workflow"}},
            ),
            {"child": child},
        )
        self.runtime.pump()
        self.elapsed[0] = 0.02
        result = self.result(run)
        self.assertEqual(result["state"], "succeeded")
        with self.workspace.transaction() as connection:
            states = [
                row[0]
                for row in connection.execute(
                    "SELECT state FROM frame WHERE run_id=? ORDER BY sequence", (run,)
                )
            ]
        self.assertEqual(states, ["succeeded", "cancelled"])

    def test_background_failure_recovered_by_join(self):
        child = self.child(flow("child", [node("fail", "logic.assert", {"condition": False})]))
        call = node(
            "call", "flow.call", {"workflow": "child", "inputs": {}, "mode": "start"}, next="join"
        )
        call["store"] = "handle"
        run = self.start(
            flow(
                "parent",
                [
                    call,
                    node(
                        "join",
                        "flow.join",
                        {"handle": path("vars", "handle"), "timeoutMs": 1000},
                        error="recover",
                    ),
                    node("recover", "flow.return", {"values": {"recovered": True}}),
                ],
                resources={"child": {"kind": "workflow"}},
            ),
            {"child": child},
        )
        self.assertEqual(self.result(run)["state"], "succeeded")

    def test_nested_sync_continue_propagates_to_loop(self):
        leaf = self.child(
            flow("leaf", [node("next", "flow.continue", {"values": {"ok": True}})], kind="loopBody")
        )
        body_definition = flow(
            "body",
            [node("call", "flow.call", {"workflow": "leaf", "inputs": {}, "mode": "wait"})],
            kind="loopBody",
            resources={"leaf": {"kind": "workflow"}},
        )
        body_revision = self.save_flow(body_definition, {"leaf": leaf})
        body = {"kind": "workflow", "workflowId": "body", "revision": body_revision}
        repeat = node(
            "repeat", "flow.repeat", {"workflow": "body", "inputs": {}, "count": 3}, next="done"
        )
        repeat["store"] = "out"
        run = self.start(
            flow(
                "root",
                [repeat, node("done", "flow.return", {"values": {"out": path("vars", "out")}})],
                resources={"body": {"kind": "workflow"}},
            ),
            {"body": body},
        )
        self.assertEqual(
            self.result(run)["output"]["value"]["out"],
            {"iterations": 3, "lastOutput": {"ok": True}},
        )

    def test_while_zero_and_iteration_limit(self):
        child = self.child(flow("child", [node("done", "flow.return", {"values": {}})]))
        run = self.start(
            flow(
                "zero",
                [
                    node(
                        "loop",
                        "flow.loop",
                        {
                            "workflow": "child",
                            "inputs": {},
                            "mode": "while",
                            "condition": False,
                            "maxIterations": 1,
                            "timeoutMs": 1000,
                        },
                    )
                ],
                resources={"child": {"kind": "workflow"}},
            ),
            {"child": child},
        )
        self.assertEqual(self.result(run)["state"], "succeeded")
        run = self.start(
            flow(
                "limit",
                [
                    node(
                        "loop",
                        "flow.loop",
                        {
                            "workflow": "child",
                            "inputs": {},
                            "mode": "until",
                            "condition": False,
                            "maxIterations": 1,
                            "timeoutMs": 1000,
                        },
                    )
                ],
                resources={"child": {"kind": "workflow"}},
            ),
            {"child": child},
        )
        result = self.result(run)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "LOOP_LIMIT")

    def test_sensitive_child_result_and_error_handler(self):
        child = self.child(
            flow(
                "child",
                [
                    node(
                        "test", "logic.assert", {"condition": path("inputs", "secret")}, next="done"
                    ),
                    node("done", "flow.return", {"values": {"result": 1}}),
                ],
                parameters={"secret": {"type": "boolean", "required": True, "sensitive": True}},
            )
        )
        for secret in [True, False]:
            call = node(
                "call",
                "flow.call",
                {"workflow": "child", "inputs": {"secret": secret}, "mode": "wait"},
                next="success",
                error="failure",
            )
            call["store"] = "child"
            run = self.start(
                flow(
                    "case" + str(secret),
                    [
                        call,
                        node(
                            "success", "flow.return", {"values": {"value": path("vars", "child")}}
                        ),
                        node("failure", "flow.return", {"values": {"value": 0}}),
                    ],
                    resources={"child": {"kind": "workflow"}},
                ),
                {"child": child},
            )
            self.assertEqual(self.result(run)["output"]["visibility"], "redacted")

    def test_partial_replacement_preserves_other_sensitive_children(self):
        value = Value.build({"a": Value.freeze(1).marked(True), "b": Value.freeze(2)})
        edited = value.updated(["b"], Value.freeze(3))
        self.assertTrue(edited.sensitive)
        self.assertEqual(edited.raw(), {"a": 1, "b": 3})
        self.assertFalse(edited.updated(["a"], Value.freeze(4)).sensitive)
        self.assertIs(edited.data["a"], value.data["a"])

    def test_many_completed_calls_release_active_frames(self):
        child = self.child(flow("child", [node("done", "flow.return", {"values": {"ok": True}})]))
        run = self.start(
            flow(
                "root",
                [node("repeat", "flow.repeat", {"workflow": "child", "inputs": {}, "count": 120})],
                resources={"child": {"kind": "workflow"}},
            ),
            {"child": child},
        )
        peak = 0
        for _ in range(1000):
            if not self.runtime.tick():
                break
            peak = max(
                peak, max((len(item.frames) for item in self.runtime._active.values()), default=0)
            )
            self.assertLessEqual(len(self.runtime._heap), 2 * len(self.runtime._timers) + 32)
        self.assertLessEqual(peak, 2)
        self.assertEqual(self.runtime.read_state(run, access=self.access)["state"], "succeeded")
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM frame WHERE run_id=?", (run,)).fetchone()[
                    0
                ],
                121,
            )

    def test_scheduler_releases_locks_during_wait_and_processes_stop(self):
        self.runtime.close()
        self.runtime = Runtime(self.workspace)
        run = self.start(flow("longWait", [node("wait", "time.wait", {"durationMs": 60000})]))
        deadline = time.monotonic() + 3
        while (
            self.runtime.read_state(run, access=self.access)["state"] != "running"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        version = self.runtime.read_state(run, access=self.access)["controlVersion"]
        result = self.runtime.control(run, "stop", version, access=self.access).result(timeout=3)
        self.assertEqual(result["state"], "cancelled")

    def test_control_during_expression_does_not_commit_partial_node(self):
        run = self.start(
            flow(
                "stop",
                [
                    node("assign", "data.assign", {"name": "x", "value": operation("add", 1, 2)}),
                ],
            )
        )
        self.runtime.tick()
        original = self.runtime.kernel.checkpoint
        stop = []

        def checkpoint():
            if not stop:
                stop.append(
                    self.runtime.control(
                        run,
                        "stop",
                        self.runtime.read_state(run, access=self.access)["controlVersion"],
                        access=self.access,
                    )
                )
            original()

        with patch.object(self.runtime.kernel, "checkpoint", checkpoint):
            self.runtime.pump()
        self.assertEqual(stop[0].result()["state"], "cancelled")
        self.assertEqual(
            self.runtime.read_value(run, "local", "x", access=self.access)["visibility"],
            "unavailable",
        )

    def test_active_attach_and_reads_check_current_owner(self):
        run = self.start(flow("owner", [node("wait", "time.wait", {"durationMs": 1000})]))
        other = replace(self.access, client_id="other")
        for call in (
            lambda: self.runtime.attach(run, other),
            lambda: self.runtime.read_state(run, access=other),
            lambda: self.runtime.read_value(run, "local", "x", access=other),
            lambda: self.runtime.result(run, access=other),
        ):
            with self.assertRaises(DeskWeaveError) as caught:
                call()
            self.assertEqual(caught.exception.code, "ACCESS_DENIED")
        future = self.runtime.control(run, "stop", 1, access=other)
        self.runtime.tick()
        with self.assertRaises(DeskWeaveError) as caught:
            future.result()
        self.assertEqual(caught.exception.code, "ACCESS_DENIED")

    def test_sensitive_expression_normal_and_error_paths_meet(self):
        for secret in (0, 1):
            run = self.start(
                flow(
                    "guard" + str(secret),
                    [
                        node(
                            "divide",
                            "data.assign",
                            {
                                "name": "x",
                                "value": operation("divide", 1, path("inputs", "secret")),
                            },
                            next="yes",
                            error="no",
                        ),
                        node("yes", "data.assign", {"name": "choice", "value": True}, next="done"),
                        node("no", "data.assign", {"name": "choice", "value": False}, next="done"),
                        node("done", "flow.return", {"values": {"public": 7}}),
                    ],
                    parameters={"secret": {"type": "integer", "required": True, "sensitive": True}},
                ),
                inputs={"secret": secret},
            )
            result = self.result(run)
            self.assertEqual(
                self.runtime.read_value(run, "local", "choice", access=self.access)["visibility"],
                "redacted",
            )
            self.assertEqual(result["output"]["value"], {"public": 7})

    def test_combined_return_limit_uses_error_outlet(self):
        run = self.start(
            flow(
                "combined",
                [
                    node(
                        "large",
                        "flow.return",
                        {"values": {f"v{i}": path("inputs", "text") for i in range(6)}},
                        error="recover",
                    ),
                    node("recover", "flow.return", {"values": {"handled": True}}),
                ],
                parameters={"text": {"type": "string", "required": True, "sensitive": False}},
            ),
            inputs={"text": "x" * 200000},
        )
        self.assertEqual(self.result(run)["output"]["value"], {"handled": True})

    def test_pause_retention_and_active_budget(self):
        run = self.start(flow("retention", [node("wait", "time.wait", {"durationMs": 10000})]))
        self.runtime.pump()
        future = self.runtime.control(
            run,
            "pause",
            self.runtime.read_state(run, access=self.access)["controlVersion"],
            access=self.access,
        )
        self.runtime.tick()
        self.assertEqual(future.result()["state"], "paused")
        self.elapsed[0] = 1801
        self.assertEqual(self.result(run)["state"], "cancelled")
        value = flow("budget", [node("wait", "time.wait", {"durationMs": 10000})])
        value["limits"]["activeMs"] = 10
        run = self.start(value)
        self.runtime.pump()
        self.elapsed[0] += 0.011
        self.assertEqual(self.result(run)["error"]["code"], "LIMIT_EXCEEDED")

    def test_child_budget_expiry_during_expression_discards_step(self):
        child_flow = flow("short", [node("assign", "data.assign", {"name": "x", "value": 1})])
        child_flow["limits"]["activeMs"] = 1
        child = self.child(child_flow)
        run = self.start(
            flow(
                "root",
                [
                    node(
                        "call",
                        "flow.call",
                        {"workflow": "short", "inputs": {}, "mode": "wait"},
                        error="recover",
                    ),
                    node("recover", "flow.return", {"values": {"handled": True}}),
                ],
                resources={"short": {"kind": "workflow"}},
            ),
            {"short": child},
        )
        self.runtime.tick()
        self.runtime.tick()
        child_id = next(
            identity
            for identity in self.runtime._active[run].frames
            if identity != self.runtime._active[run].root
        )
        original = self.runtime.kernel.checkpoint

        def checkpoint():
            self.elapsed[0] = 0.010
            original()

        with patch.object(self.runtime.kernel, "checkpoint", checkpoint):
            self.runtime.tick()
        self.assertIsNone(self.runtime.store.value(run, child_id, "local", "x"))
        self.assertEqual(self.result(run)["output"]["value"], {"handled": True})

    def test_call_depth_and_background_budget(self):
        child = self.child(flow("wait", [node("wait", "time.wait", {"durationMs": 10000})]))
        for mode, limit in (("wait", "callDepth"), ("start", "parallelCalls")):
            calls = [
                node(
                    "first",
                    "flow.call",
                    {"workflow": "child", "inputs": {}, "mode": mode},
                    next="second",
                ),
                node("second", "flow.call", {"workflow": "child", "inputs": {}, "mode": mode}),
            ]
            value = flow("budget" + mode, calls, resources={"child": {"kind": "workflow"}})
            value["limits"][limit] = 1
            run = self.start(value, {"child": child})
            result = self.result(run)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error"]["code"], "LIMIT_EXCEEDED")
            with self.workspace.transaction() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM frame WHERE run_id=? AND "
                        "state NOT IN ('succeeded','failed','cancelled')",
                        (run,),
                    ).fetchone()[0],
                    0,
                )

    def test_cross_run_join_is_rejected(self):
        first = self.start(flow("first", [node("wait", "time.wait", {"durationMs": 1000})]))
        handle = self.runtime._active[first].root
        second = self.start(
            flow("second", [node("join", "flow.join", {"handle": handle, "timeoutMs": 10})])
        )
        self.assertEqual(self.result(second)["error"]["code"], "INVALID_ARGUMENT")
        self.assertIn(first, self.runtime._active)

    def test_cancelled_control_future_does_not_break_scheduler(self):
        run = self.start(
            flow("cancelFuture", [node("done", "flow.return", {"values": {"ok": True}})])
        )
        future = self.runtime.control(run, "pause", 1, access=self.access)
        self.assertTrue(future.cancel())
        self.assertEqual(self.result(run)["output"]["value"], {"ok": True})

    def test_value_read_uses_requested_data_version(self):
        run = self.start(
            flow(
                "versions",
                [
                    node("first", "data.assign", {"name": "x", "value": 1}, next="second"),
                    node("second", "data.assign", {"name": "x", "value": 2}),
                ],
            )
        )
        self.runtime.tick()
        self.runtime.tick()
        version = self.runtime.read_state(run, access=self.access)["dataVersion"]
        self.runtime.pump()
        self.assertEqual(
            self.runtime.read_value(run, "local", "x", access=self.access, version=version)[
                "value"
            ],
            1,
        )
        self.assertEqual(self.runtime.read_value(run, "local", "x", access=self.access)["value"], 2)

    def test_commit_exception_preserves_memory_and_persistent_state(self):
        run = self.start(flow("commit", [node("assign", "data.assign", {"name": "x", "value": 1})]))
        self.runtime.tick()
        before = self.runtime.read_state(run, access=self.access)
        original = self.workspace._connection

        class RejectCommit:
            def __getattr__(self, name):
                return getattr(original, name)

            def commit(self):
                raise sqlite3.OperationalError("commit rejected")

        self.workspace._connection = RejectCommit()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.runtime.tick()
        finally:
            self.workspace._connection = original
        self.assertEqual(self.runtime.read_state(run, access=self.access), before)
        self.assertIsNone(
            self.runtime.store.value(run, self.runtime._active[run].root, "local", "x")
        )
        self.assertEqual(self.result(run)["state"], "succeeded")

    def test_shared_definitions_are_compiled_once(self):
        child = self.child(flow("shared", [node("done", "flow.return", {"values": {}})]))
        value = flow(
            "dedup",
            [node("done", "flow.return", {"values": {}})],
            resources={key: {"kind": "workflow"} for key in ("a", "b")},
        )
        from deskweave.domain.execution import compile_workflow

        with patch(
            "deskweave.engine.scheduler.compile_workflow", wraps=compile_workflow
        ) as compiled:
            run = self.start(value, {"a": child, "b": child})
        self.assertEqual(compiled.call_count, 2)
        self.assertEqual(self.result(run)["state"], "succeeded")

    def test_coalesce_partial_argument_read_keeps_sensitive_label(self):
        context = Value.build({"secret": Value.freeze(2).marked(True)})
        result = evaluate_value(
            operation("coalesce", {"a": path("secret"), "b": path("absent")}, 7), context.lookup
        )
        self.assertEqual(result.raw(), 7)
        self.assertTrue(result.sensitive)

    def test_sensitive_nested_type_selects_error_path(self):
        for flag in (True, 1):
            run = self.start(
                flow(
                    "type" + str(flag),
                    [
                        node(
                            "branch",
                            "logic.branch",
                            {"condition": path("inputs", "secret", "flag")},
                            yes="done",
                            no="done",
                            error="failed",
                        ),
                        node("done", "flow.return", {"values": {"ok": True}}),
                        node("failed", "flow.return", {"values": {"ok": False}}),
                    ],
                    parameters={"secret": {"type": "object", "required": True, "sensitive": True}},
                ),
                inputs={"secret": {"flag": flag}},
            )
            self.assertEqual(self.result(run)["output"]["visibility"], "redacted")

    def test_non_sensitive_complete_overwrite_clears_old_label(self):
        run = self.start(
            flow(
                "overwrite",
                [
                    node(
                        "copy",
                        "data.assign",
                        {"name": "x", "value": path("inputs", "secret")},
                        next="replace",
                    ),
                    node("replace", "data.assign", {"name": "x", "value": False}, next="done"),
                    node("done", "flow.return", {"values": {"x": path("vars", "x")}}),
                ],
                parameters={"secret": {"type": "boolean", "required": True, "sensitive": True}},
            ),
            inputs={"secret": True},
        )
        self.assertEqual(self.result(run)["output"]["value"], {"x": False})

    def test_self_join_and_sibling_wait_cycle_are_rejected(self):
        for target in ("a", "b"):
            child = self.child(
                flow(
                    "child" + target,
                    [
                        node("wait", "time.wait", {"durationMs": 10}, next="join"),
                        node(
                            "join", "flow.join", {"handle": path("run", target), "timeoutMs": 1000}
                        ),
                    ],
                )
            )
            call_a = node(
                "a", "flow.call", {"workflow": "child", "inputs": {}, "mode": "start"}, next="saveA"
            )
            call_a["store"] = "a"
            call_b = node(
                "b", "flow.call", {"workflow": "other", "inputs": {}, "mode": "start"}, next="saveB"
            )
            call_b["store"] = "b"
            other = self.child(
                flow(
                    "other" + target,
                    [
                        node("wait", "time.wait", {"durationMs": 10}, next="join"),
                        node("join", "flow.join", {"handle": path("run", "a"), "timeoutMs": 1000}),
                    ],
                )
            )
            run = self.start(
                flow(
                    "cycle" + target,
                    [
                        call_a,
                        node(
                            "saveA",
                            "data.assign",
                            {"scope": "run", "name": "a", "value": path("vars", "a")},
                            next="b",
                        ),
                        call_b,
                        node(
                            "saveB",
                            "data.assign",
                            {"scope": "run", "name": "b", "value": path("vars", "b")},
                        ),
                    ],
                    resources={"child": {"kind": "workflow"}, "other": {"kind": "workflow"}},
                ),
                {"child": child, "other": other},
            )
            self.runtime.pump()
            self.elapsed[0] += 0.02
            result = self.result(run)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_background_loop_error_recovery_pause_resume_and_stop(self):
        body = self.child(
            flow(
                "body",
                [
                    node("assert", "logic.assert", {"condition": False}, error="recover"),
                    node("recover", "time.wait", {"durationMs": 10}, next="done"),
                    node("done", "flow.return", {"values": {"value": 7}}),
                ],
            )
        )
        repeat = node(
            "repeat", "flow.repeat", {"workflow": "body", "inputs": {}, "count": 2}, next="done"
        )
        repeat["store"] = "loop"
        revision = self.save_flow(
            flow(
                "loop",
                [repeat, node("done", "flow.return", {"values": {"loop": path("vars", "loop")}})],
                resources={"body": {"kind": "workflow"}},
            ),
            {"body": body},
        )
        background = {"kind": "workflow", "workflowId": "loop", "revision": revision}
        for stop in (False, True):
            call = node(
                "start",
                "flow.call",
                {"workflow": "loop", "inputs": {}, "mode": "start"},
                next="join",
            )
            call["store"] = "handle"
            join = node(
                "join",
                "flow.join",
                {"handle": path("vars", "handle"), "timeoutMs": 1000},
                next="done",
            )
            join["store"] = "result"
            run = self.start(
                flow(
                    "composed" + str(stop),
                    [
                        call,
                        join,
                        node("done", "flow.return", {"values": {"result": path("vars", "result")}}),
                    ],
                    resources={"loop": {"kind": "workflow"}},
                ),
                {"loop": background},
            )
            self.runtime.pump()
            paused = self.runtime.control(
                run,
                "pause",
                self.runtime.read_state(run, access=self.access)["controlVersion"],
                access=self.access,
            )
            self.runtime.tick()
            self.elapsed[0] += 20
            self.assertEqual(self.result(run)["state"], "paused")
            control = self.runtime.control(
                run,
                "stop" if stop else "resume",
                paused.result()["controlVersion"],
                access=self.access,
            )
            self.runtime.tick()
            control.result()
            if not stop:
                for _ in range(3):
                    self.runtime.pump()
                    self.elapsed[0] += 0.02
                result = self.result(run)
                self.assertEqual(result["state"], "succeeded")
                self.assertEqual(
                    result["output"]["value"],
                    {"result": {"loop": {"iterations": 2, "lastOutput": {"value": 7}}}},
                )
            else:
                self.assertEqual(self.result(run)["state"], "cancelled")
            with self.workspace.transaction() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM frame WHERE run_id=? AND "
                        "state NOT IN ('succeeded','failed','cancelled')",
                        (run,),
                    ).fetchone()[0],
                    0,
                )

    def test_port_registration_matches_executable_capability(self):
        from deskweave.engine.actions import ActionPorts

        executed = []

        class Port:
            actions = frozenset({"time.wait"})

            def execute(self, kernel, change, frame, node, now):
                executed.append(node["id"])
                kernel._complete(change, frame, node, Value.freeze({"elapsedActiveMs": 0}))

        ports = ActionPorts(Port())
        self.runtime.close()
        self.runtime = Runtime(self.workspace, threaded=False, ports=ports)
        self.planner = RunService(self.workspace, ports=ports)
        run = self.start(flow("port", [node("wait", "time.wait", {"durationMs": 0})]))
        self.assertEqual(self.result(run)["state"], "succeeded")
        self.assertEqual(executed, ["wait"])
        with self.assertRaises(ValueError):
            ActionPorts(Port(), Port())

    def test_sensitive_coalesce_selection_does_not_preserve_fallback_type(self):
        for value in (None, 7):
            run = self.start(
                flow(
                    "fallback" + str(value),
                    [
                        node(
                            "branch",
                            "logic.branch",
                            {
                                "condition": operation(
                                    "coalesce",
                                    path("inputs", "secret", "value"),
                                    path("inputs", "fallback"),
                                )
                            },
                            yes="done",
                            no="done",
                            error="failed",
                        ),
                        node("done", "flow.return", {"values": {"ok": True}}),
                        node("failed", "flow.return", {"values": {"ok": False}}),
                    ],
                    parameters={
                        "secret": {"type": "object", "required": True, "sensitive": True},
                        "fallback": {"type": "boolean", "required": True, "sensitive": False},
                    },
                ),
                inputs={"secret": {"value": value}, "fallback": False},
            )
            self.assertEqual(self.result(run)["output"]["visibility"], "redacted")

    def test_storage_fault_isolates_one_run_and_retry_finishes(self):
        from threading import Event, Thread

        bad = self.start(flow("faulted", [node("done", "flow.return", {"values": {"value": 1}})]))
        good = self.start(flow("healthy", [node("done", "flow.return", {"values": {"value": 2}})]))
        released = [False]
        completed = {bad: Event(), good: Event()}
        commit = self.runtime.store.commit

        def controlled(before, change, now, **kwargs):
            if before.id == bad and not released[0]:
                raise sqlite3.OperationalError("injected storage failure")
            commit(before, change, now, **kwargs)
            if change.run.state == "succeeded":
                completed[before.id].set()

        with patch.object(self.runtime.store, "commit", side_effect=controlled):
            self.runtime._thread = Thread(target=self.runtime._serve)
            self.runtime._thread.start()
            self.assertTrue(completed[good].wait(3))
            self.assertIn("storageFailure", self.runtime.read_state(bad, access=self.access))
            self.assertEqual(
                self.runtime.result(good, access=self.access)["output"]["value"], {"value": 2}
            )
            released[0] = True
            self.runtime.retry(bad, access=self.access)
            self.assertTrue(completed[bad].wait(3))
            self.assertEqual(
                self.runtime.result(bad, access=self.access)["output"]["value"], {"value": 1}
            )

    def test_sensitive_update_path_shape_controls_success_and_error(self):
        for index, secret in enumerate(({"inner": {}}, {"inner": 1})):
            run = self.start(
                flow(
                    "shape" + str(index),
                    [
                        node(
                            "copy",
                            "data.assign",
                            {"name": "secret", "value": path("inputs", "secret")},
                            next="patch",
                            error="patch",
                        ),
                        node(
                            "patch",
                            "data.assign",
                            {"name": "secret", "path": ["inner", "x"], "value": 0},
                            next="done",
                            error="fail",
                        ),
                        node("done", "flow.return", {"values": {"ok": True}}),
                        node("fail", "flow.return", {"values": {"ok": False}}),
                    ],
                    parameters={"secret": {"type": "object", "required": True, "sensitive": True}},
                ),
                inputs={"secret": secret},
            )
            self.assertEqual(self.result(run)["output"]["visibility"], "redacted")

    def test_skipped_conditional_writes_label_old_and_missing_values(self):
        for initial in ({}, {"x": 0}):
            for secret in (True, False):
                identity = "skipped" + str(len(initial)) + str(secret)
                run = self.start(
                    flow(
                        identity,
                        [
                            node(
                                "choose",
                                "logic.branch",
                                {"condition": path("inputs", "secret")},
                                yes="write",
                                no="skip",
                            ),
                            node("write", "data.assign", {"name": "x", "value": 1}, next="joined"),
                            node("skip", "time.wait", {"durationMs": 0}, next="joined"),
                            node(
                                "joined",
                                "data.assign",
                                {
                                    "name": "observed",
                                    "value": operation("coalesce", path("vars", "x"), 0),
                                },
                                next="clean",
                            ),
                            node("clean", "data.assign", {"name": "x", "value": 5}, next="done"),
                            node(
                                "done",
                                "flow.return",
                                {
                                    "values": {
                                        "public": path("vars", "public"),
                                        "x": path("vars", "x"),
                                    }
                                },
                            ),
                        ],
                        parameters={
                            "secret": {"type": "boolean", "required": True, "sensitive": True}
                        },
                        variables={"public": 9, **initial},
                    ),
                    inputs={"secret": secret},
                )
                self.assertEqual(self.result(run)["output"]["value"], {"public": 9, "x": 5})
                self.assertEqual(
                    self.runtime.read_value(run, "local", "observed", access=self.access)[
                        "visibility"
                    ],
                    "redacted",
                )

    def test_skipped_nested_write_labels_only_its_path(self):
        run = self.start(
            flow(
                "nestedSkip",
                [
                    node(
                        "choose",
                        "logic.branch",
                        {"condition": path("inputs", "secret")},
                        yes="write",
                        no="skip",
                    ),
                    node(
                        "write",
                        "data.assign",
                        {"name": "config", "path": ["x"], "value": 1},
                        next="joined",
                    ),
                    node("skip", "time.wait", {"durationMs": 0}, next="joined"),
                    node(
                        "joined",
                        "data.assign",
                        {
                            "name": "observed",
                            "value": operation("coalesce", path("vars", "config", "x"), 0),
                        },
                        next="done",
                    ),
                    node("done", "flow.return", {"values": {"z": path("vars", "config", "z")}}),
                ],
                parameters={"secret": {"type": "boolean", "required": True, "sensitive": True}},
                variables={"config": {"z": 9}},
            ),
            inputs={"secret": False},
        )
        self.assertEqual(self.result(run)["output"]["value"], {"z": 9})
        self.assertEqual(
            self.runtime.read_value(run, "local", "observed", access=self.access)["visibility"],
            "redacted",
        )

    def test_skipped_call_labels_store_calls_and_shared_effects(self):
        child = self.child(
            flow(
                "writer",
                [
                    node(
                        "write",
                        "data.assign",
                        {"scope": "run", "name": "x", "value": 1},
                        next="done",
                    ),
                    node("done", "flow.return", {"values": {"x": 1}}),
                ],
            )
        )
        call = node(
            "call", "flow.call", {"workflow": "writer", "inputs": {}, "mode": "wait"}, next="done"
        )
        call["store"] = "output"
        run = self.start(
            flow(
                "skipCall",
                [
                    node(
                        "choose",
                        "logic.branch",
                        {"condition": path("inputs", "secret")},
                        yes="call",
                        no="skip",
                    ),
                    call,
                    node("skip", "time.wait", {"durationMs": 0}, next="done"),
                    node("done", "flow.return", {"values": {"x": path("run", "x")}}),
                ],
                parameters={"secret": {"type": "boolean", "required": True, "sensitive": True}},
                resources={"writer": {"kind": "workflow"}},
                runVariables={"x": 0},
            ),
            {"writer": child},
            inputs={"secret": False},
        )
        self.assertEqual(self.result(run)["output"]["visibility"], "redacted")
        self.assertEqual(
            self.runtime.read_value(run, "local", "output", access=self.access)["visibility"],
            "redacted",
        )
        self.assertEqual(
            self.runtime.read_value(run, "calls", "call", access=self.access)["visibility"],
            "redacted",
        )

    def test_sensitive_zero_repeat_labels_skipped_shared_write(self):
        child = self.child(
            flow(
                "writer", [node("write", "data.assign", {"scope": "run", "name": "x", "value": 1})]
            )
        )
        run = self.start(
            flow(
                "zeroSensitive",
                [
                    node(
                        "repeat",
                        "flow.repeat",
                        {"workflow": "writer", "inputs": {}, "count": path("inputs", "count")},
                        next="done",
                        error="done",
                    ),
                    node("done", "flow.return", {"values": {"x": path("run", "x")}}),
                ],
                parameters={"count": {"type": "integer", "required": True, "sensitive": True}},
                resources={"writer": {"kind": "workflow"}},
                runVariables={"x": 0},
            ),
            {"writer": child},
            inputs={"count": 0},
        )
        self.assertEqual(self.result(run)["output"]["visibility"], "redacted")

    def test_shared_path_write_preserves_same_step_control_labels(self):
        for index, inner in enumerate(({}, 1)):
            run = self.start(
                flow(
                    "sameStep" + str(index),
                    [
                        node(
                            "copy",
                            "data.assign",
                            {"scope": "run", "name": "config", "value": path("inputs", "secret")},
                            next="patch",
                            error="patch",
                        ),
                        node(
                            "patch",
                            "data.assign",
                            {"scope": "run", "name": "config", "path": ["inner", "x"], "value": 0},
                            next="skip",
                            error="write",
                        ),
                        node("skip", "time.wait", {"durationMs": 0}, next="done"),
                        node(
                            "write",
                            "data.assign",
                            {"scope": "run", "name": "y", "value": 1},
                            next="done",
                        ),
                        node("done", "flow.return", {"values": {"y": path("run", "y")}}),
                    ],
                    parameters={"secret": {"type": "object", "required": True, "sensitive": True}},
                    runVariables={"y": 0},
                ),
                inputs={"secret": {"inner": inner}},
            )
            self.assertEqual(self.result(run)["output"]["visibility"], "redacted")

    def test_sensitive_branch_chains_reuse_static_write_analysis(self):
        for writes in ("none", "local", "shared"):
            measured = []
            for count in (32, 64):
                nodes = []
                for index in range(count):
                    nodes.extend(
                        [
                            node(
                                f"b{index}",
                                "logic.branch",
                                {"condition": path("inputs", "secret")},
                                yes=f"w{index}"
                                if writes == "shared"
                                else f"b{index + 1}"
                                if index + 1 < count
                                else "done",
                                no=f"exit{index}",
                            ),
                            node(f"exit{index}", "flow.return", {"values": {}}),
                        ]
                    )
                if writes == "shared":
                    nodes.extend(
                        node(
                            f"w{index}",
                            "data.assign",
                            {"scope": "run", "name": "x", "value": 1},
                            next=f"b{index + 1}" if index + 1 < count else "done",
                        )
                        for index in range(count)
                    )
                nodes.append(
                    node("done", "data.assign", {"name": "x", "value": 1})
                    if writes == "local"
                    else node("done", "flow.return", {"values": {}})
                )
                run = self.start(
                    flow(
                        f"chain{count}{writes}",
                        nodes,
                        parameters={
                            "secret": {"type": "boolean", "required": True, "sensitive": True}
                        },
                    ),
                    inputs={"secret": True},
                )
                accesses = [0]

                class CountingNodes(dict):
                    def __init__(self, source, counter):
                        super().__init__(source)
                        self.counter = counter

                    def __getitem__(self, key):
                        self.counter[0] += 1
                        return super().__getitem__(key)

                active = self.runtime._active[run]
                for key, program in active.programs.items():
                    active.programs[key] = replace(
                        program, nodes=CountingNodes(program.nodes, accesses)
                    )
                self.assertEqual(self.result(run)["state"], "succeeded")
                measured.append(accesses[0])
            self.assertLessEqual(measured[1], measured[0] * 2 + 4)

    def test_background_public_overwrite_invalidates_control_effect_cache(self):
        child = self.child(
            flow(
                "publicWriter",
                [
                    node("wait", "time.wait", {"durationMs": 10}, next="write"),
                    node("write", "data.assign", {"scope": "run", "name": "x", "value": 0}),
                ],
            )
        )
        run = self.start(
            flow(
                "cacheInvalidation",
                [
                    node(
                        "start",
                        "flow.call",
                        {"workflow": "writer", "inputs": {}, "mode": "start"},
                        next="outer",
                    ),
                    node(
                        "outer",
                        "logic.branch",
                        {"condition": path("inputs", "outer")},
                        yes="wait",
                        no="exit",
                    ),
                    node("exit", "flow.return", {"values": {}}),
                    node("wait", "time.wait", {"durationMs": 20}, next="inner"),
                    node(
                        "inner",
                        "logic.branch",
                        {"condition": path("inputs", "inner")},
                        yes="write",
                        no="skip",
                    ),
                    node(
                        "write",
                        "data.assign",
                        {"scope": "run", "name": "x", "value": 1},
                        next="done",
                    ),
                    node("skip", "time.wait", {"durationMs": 0}, next="done"),
                    node("done", "flow.return", {"values": {"x": path("run", "x")}}),
                ],
                parameters={
                    name: {"type": "boolean", "required": True, "sensitive": True}
                    for name in ("outer", "inner")
                },
                resources={"writer": {"kind": "workflow"}},
                runVariables={"x": 5},
            ),
            {"writer": child},
            inputs={"outer": True, "inner": False},
        )
        self.runtime.pump()
        self.elapsed[0] = 0.011
        self.runtime.pump()
        self.assertEqual(
            self.runtime.read_value(run, "shared", "x", access=self.access)["value"], 0
        )
        self.assertEqual(self.runtime._active[run].label_epoch, 1)
        self.elapsed[0] = 0.021
        self.assertEqual(self.result(run)["output"]["visibility"], "redacted")
        self.assertEqual(
            self.runtime.read_value(run, "shared", "x", access=self.access)["visibility"],
            "redacted",
        )
