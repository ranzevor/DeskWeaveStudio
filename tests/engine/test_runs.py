import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

from deskweave.adapters.assets import Assets
from deskweave.adapters.dependencies import Dependencies
from deskweave.adapters.runs import RunStore
from deskweave.adapters.workspace import Workspace
from deskweave.contracts import validate
from deskweave.domain.runs import RunPlan
from deskweave.engine.run_planning import DEFAULT_LIMITS, RunAccess, RunService
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps
from tests.engine.test_service import EngineCase, workflow


def definition(identity="root"):
    value = workflow(identity)
    value.update(
        parameters={},
        variables={},
        runVariables={},
        targets={},
        resources={},
        entry="done",
        nodes=[
            {
                "id": "done",
                "title": "返回结果",
                "action": "flow.return",
                "params": {"values": {"answer": 1}},
                "on": {},
            }
        ],
    )
    return value


class RunTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.runs = RunService(self.workspace)
        self.access = RunAccess(
            "runner", "grant-1", 1, frozenset({"run.execute", "workflow.read", "asset.read"})
        )
        self.editor = self.engine.open_session(
            "editor", {"workflow.read", "workflow.write", "asset.read"}
        )

    def saved(self, value=None, dependencies=None, base=None):
        value = value or definition()
        result = self.success(
            "workflow.save",
            {
                "workflowId": value["id"],
                "draftVersion": 1,
                "baseRevision": base,
                "definition": value,
                "dependencies": dependencies or {},
            },
        )
        return {
            "workflowId": value["id"],
            "revision": result["revision"],
            "inputs": {},
            "targets": {},
            "detached": False,
        }

    def empty_runs(self):
        with self.workspace.transaction() as connection:
            for table in ["run", "frame", "run_event", "run_workflow_ref", "run_asset_ref"]:
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM command_receipt WHERE method='run.start'"
                ).fetchone()[0],
                0,
            )

    def test_prepare_fixes_defaults_initial_state_and_budget(self):
        value = definition()
        value["parameters"] = {
            "count": {"type": "integer", "required": True, "sensitive": False, "default": 3},
            "secret": {"type": "string", "required": False, "sensitive": True, "default": "sample"},
            "absent": {"type": "string", "required": False, "sensitive": False},
        }
        value["variables"] = {"local": [1, 2]}
        value["runVariables"] = {"shared": {"a": 1}}
        params = self.saved(value)
        params["runVariables"] = {"shared": {"b": 2}}
        plan = self.runs.prepare(self.access, params)
        data = plan.value()
        self.assertEqual(
            data["initialState"],
            {
                "inputs": {"count": 3, "secret": "sample"},
                "variables": {"local": [1, 2]},
                "runVariables": {"shared": {"b": 2}},
                "sensitiveInputs": ["secret"],
            },
        )
        self.assertEqual(
            data["limits"],
            {key: min(value["limits"][key], limit) for key, limit in DEFAULT_LIMITS.items()},
        )
        self.assertEqual(
            data["authorization"], {"clientId": "runner", "ref": "grant-1", "version": 1}
        )
        self.assertEqual(data["interruptPolicy"], "pause")
        data["initialState"]["variables"]["local"].append(3)
        params["runVariables"]["shared"]["b"] = 9
        self.assertEqual(plan.value()["initialState"]["variables"]["local"], [1, 2])
        self.assertEqual(plan.value()["initialState"]["runVariables"]["shared"], {"b": 2})
        with self.assertRaises(FrozenInstanceError):
            plan.content = b"changed"
        self.empty_runs()

    def test_input_validation_keeps_raw_data(self):
        value = definition()
        value["parameters"] = {
            "count": {"type": "integer", "required": True, "sensitive": False},
            "payload": {"type": "object", "required": False, "sensitive": False},
        }
        params = self.saved(value)
        for inputs in [{}, {"count": True}, {"count": 1.1}, {"count": 1, "other": 0}]:
            with self.subTest(inputs=inputs), self.assertRaises(DeskWeaveError):
                self.runs.accept(self.access, "bad", params | {"inputs": inputs})
        payload = {"$expr": {"op": "unknown", "args": []}}
        plan = self.runs.prepare(
            self.access, params | {"inputs": {"count": 2.0, "payload": payload}}
        )
        self.assertEqual(plan.value()["initialState"]["inputs"]["payload"], payload)
        self.empty_runs()

    def test_request_limits_override_defaults_but_not_workflow_limits(self):
        value = definition()
        value["limits"] = {
            "activeMs": 1000000,
            "steps": 50000,
            "callDepth": 20,
            "parallelCalls": 20,
            "inputEvents": 200000,
        }
        params = self.saved(value)
        requested = {
            "activeMs": 900000.0,
            "steps": 60000,
            "callDepth": 10,
            "parallelCalls": 32,
            "inputEvents": 100000,
        }
        limits = self.runs.prepare(self.access, params | {"limits": requested}).value()["limits"]
        self.assertEqual(
            limits, {key: min(int(requested[key]), value["limits"][key]) for key in requested}
        )

    def test_plan_and_accepted_run_keep_old_asset_and_child(self):
        raw = dumps(
            {
                "formatVersion": "1.0",
                "coordinateMode": "physical",
                "events": [{"kind": "pointer.move", "atUs": 0, "x": 1, "y": 2}],
            }
        )
        assets = Assets(self.workspace)
        asset = assets.put("line", "线条", "trajectory", io.BytesIO(raw))
        child = definition("child")
        child["resources"] = {"line": {"kind": "trajectory"}}
        child_params = self.saved(
            child,
            {"line": {key: asset[key] for key in ("kind", "assetId", "assetVersion", "sha256")}},
        )
        root = definition()
        root["resources"] = {"child": {"kind": "workflow"}}
        params = self.saved(
            root,
            {
                "child": {
                    "kind": "workflow",
                    "workflowId": "child",
                    "revision": child_params["revision"],
                }
            },
        )
        plan = self.runs.prepare(self.access, params)
        self.saved(definition("child"), base=child_params["revision"])
        assets.put(
            "line", "线条", "trajectory", io.BytesIO(raw), expected_version=asset["assetVersion"]
        )
        accepted = self.runs.store.create(plan, "fixed", authorize=self.access.check_snapshot)
        stored = self.runs.store.read(accepted["runId"])
        self.assertEqual(stored["snapshotHash"], plan.sha256)
        self.assertEqual(stored["snapshot"]["assets"][0]["assetVersion"], asset["assetVersion"])
        self.assertIn(
            child_params["revision"], [v["revision"] for v in stored["snapshot"]["workflows"]]
        )
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM run_workflow_ref").fetchone()[0], 2
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM run_asset_ref").fetchone()[0], 1
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM revision WHERE id=?", (params["revision"],))

    def test_shared_graph_is_read_once_and_indexed_immutably(self):
        leaf = self.saved(definition("leaf"))
        child = definition("child")
        child["resources"] = {"leaf": {"kind": "workflow"}}
        child_params = self.saved(
            child,
            {"leaf": {"kind": "workflow", "workflowId": "leaf", "revision": leaf["revision"]}},
        )
        root = definition()
        root["resources"] = {key: {"kind": "workflow"} for key in ("a", "b")}
        params = self.saved(
            root,
            {
                key: {
                    "kind": "workflow",
                    "workflowId": "child",
                    "revision": child_params["revision"],
                }
                for key in ("a", "b")
            },
        )
        with patch.object(self.workspace, "read", wraps=self.workspace.read) as read:
            graph = Dependencies(self.workspace, self.access.workflow, lambda: None).resolve(
                params["workflowId"], params["revision"]
            )
        self.assertEqual(read.call_count, 3)
        self.assertIn(("leaf", leaf["revision"]), graph.workflows)
        with self.assertRaises(TypeError):
            graph.workflows[("new", "new")] = graph.workflows[("leaf", leaf["revision"])]

    def test_resolver_reuse_does_not_reuse_old_authorization(self):
        params = self.saved()
        calls = []
        resolver = Dependencies(self.workspace, calls.append, lambda: None)
        resolver.resolve(params["workflowId"], params["revision"])
        resolver.resolve(params["workflowId"], params["revision"])
        self.assertEqual(calls, ["root", "root"])

    def test_dependency_permissions_are_checked_transitively(self):
        child = self.saved(definition("private"))
        root = definition()
        root["resources"] = {"child": {"kind": "workflow"}}
        params = self.saved(
            root,
            {"child": {"kind": "workflow", "workflowId": "private", "revision": child["revision"]}},
        )
        for access in [
            replace(self.access, workflow_ids=frozenset({"root"})),
            replace(self.access, permissions=frozenset({"run.execute"})),
        ]:
            with self.assertRaises(DeskWeaveError) as caught:
                self.runs.accept(access, "restricted", params)
            self.assertEqual(caught.exception.code, "ACCESS_DENIED")
        self.empty_runs()

    def test_required_targets_use_current_authorizer(self):
        value = definition()
        value["targets"] = {"main": {"kind": "window", "required": True}}
        params = self.saved(value)
        with self.assertRaises(DeskWeaveError):
            self.runs.prepare(self.access, params)
        with self.assertRaises(DeskWeaveError) as caught:
            self.runs.prepare(self.access, params | {"targets": {"main": "target-1"}})
        self.assertEqual(caught.exception.code, "METHOD_UNAVAILABLE")
        calls = []
        access = replace(self.access, authorize_target=lambda *args: calls.append(args))
        plan = self.runs.prepare(access, params | {"targets": {"main": "target-1"}})
        self.assertEqual(calls, [("main", "target-1", "window")])
        self.assertEqual(plan.value()["targetKinds"], {"main": "window"})

    def test_accept_atomically_creates_frame_event_references_and_receipt(self):
        params = self.saved()
        result = self.runs.accept(self.access, "accepted", params)
        validate("urn:deskweave:service:1#/$defs/run.start/properties/result", result)
        stored = self.runs.store.read(result["runId"])
        self.assertEqual(
            (stored["state"], stored["controlVersion"], stored["dataVersion"], stored["lastSeq"]),
            ("queued", 0, 0, "1"),
        )
        with self.workspace.transaction() as connection:
            frame = connection.execute(
                "SELECT * FROM frame WHERE id=?", (stored["rootFrameId"],)
            ).fetchone()
            self.assertEqual(frame["run_id"], result["runId"])
            self.assertEqual(frame["node_id"], "done")
            event = connection.execute(
                "SELECT * FROM run_event WHERE run_id=?", (result["runId"],)
            ).fetchone()
            self.assertEqual(event["kind"], "run.state.changed")
            self.assertEqual(
                self.workspace._read_object(event["payload_hash"]),
                {"state": "queued", "controlVersion": 0},
            )
            receipt = connection.execute(
                "SELECT * FROM command_receipt WHERE request_id='accepted'"
            ).fetchone()
            self.assertEqual(
                (receipt["subject_kind"], receipt["subject_id"]), ("run", result["runId"])
            )
        stored["snapshot"]["name"] = "altered"
        self.assertNotEqual(self.runs.store.read(result["runId"])["snapshot"]["name"], "altered")

    def test_idempotency_survives_restart_and_new_definition_head(self):
        params = self.saved()
        first = self.runs.accept(self.access, "once", params)
        self.saved(definition(), base=params["revision"])
        self.engine.close()
        self.workspace = Workspace(self.directory)
        self.engine = self.make_engine()
        self.runs = RunService(self.workspace)
        self.assertEqual(
            self.runs.accept(self.access, "once", params | {"interruptPolicy": "pause"}), first
        )
        with self.assertRaises(DeskWeaveError) as caught:
            self.runs.accept(self.access, "once", params | {"detached": True})
        self.assertEqual(caught.exception.code, "REQUEST_CONFLICT")

    def test_replay_checks_original_authorization(self):
        params = self.saved()
        self.runs.accept(self.access, "once", params)
        for access in [
            replace(self.access, authorization_ref="another-grant"),
            replace(self.access, authorization_version=2),
            replace(self.access, workflow_ids=frozenset()),
        ]:
            with self.assertRaises(DeskWeaveError) as caught:
                self.runs.accept(access, "once", params)
            self.assertEqual(caught.exception.code, "ACCESS_DENIED")

    def test_parallel_duplicate_requests_create_one_run(self):
        params = self.saved()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: self.runs.accept(self.access, "parallel", params), range(4))
            )
        self.assertEqual(len({result["runId"] for result in results}), 1)
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM run").fetchone()[0], 1)

    def test_commit_failure_rolls_back_all_run_rows(self):
        params = self.saved()
        with self.workspace.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER fail_run_event BEFORE INSERT ON run_event "
                "BEGIN SELECT RAISE(ABORT, 'failed event'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.runs.accept(self.access, "retry", params)
        self.empty_runs()
        with self.workspace.transaction() as connection:
            connection.execute("DROP TRIGGER fail_run_event")
        self.runs.accept(self.access, "retry", params)

    def test_cancellation_before_commit_rolls_back_all_run_rows(self):
        from deskweave.adapters.runs import write_receipt

        params = self.saved()
        cancelled = threading.Event()

        def authorize():
            if cancelled.is_set():
                raise DeskWeaveError("CANCELLED", "请求已取消。")

        def interrupt(*args, **kwargs):
            write_receipt(*args, **kwargs)
            cancelled.set()

        access = replace(self.access, revalidate=authorize)
        with patch("deskweave.adapters.runs.write_receipt", side_effect=interrupt):
            with self.assertRaises(DeskWeaveError) as caught:
                self.runs.accept(access, "cancelled", params)
        self.assertEqual(caught.exception.code, "CANCELLED")
        self.empty_runs()
        cancelled.clear()
        self.runs.accept(access, "cancelled", params)

    def test_revocation_and_storage_failure_before_registration(self):
        params = self.saved()

        def revoked():
            raise DeskWeaveError("ACCESS_DENIED", "授权已撤销。")

        with self.assertRaises(DeskWeaveError):
            self.runs.accept(replace(self.access, revalidate=revoked), "revoked", params)
        with patch.object(self.runs.store.blobs, "put", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.runs.accept(self.access, "full", params)
        self.empty_runs()

    def test_content_corruption_between_preparation_and_acceptance(self):
        params = self.saved()
        plan = self.runs.prepare(self.access, params)
        identity = plan.value()["workflows"][0]["definitionHash"]
        path = self.directory / "objects/sha256" / identity
        data = path.read_bytes()
        path.write_bytes(b"!" + data[1:])
        with self.assertRaises(DeskWeaveError) as caught:
            self.runs.store.create(plan, "corrupt", authorize=self.access.check_snapshot)
        self.assertEqual(caught.exception.code, "STORAGE_CORRUPT")
        self.empty_runs()

    def test_cross_workspace_plan_and_malformed_snapshot_are_rejected(self):
        plan = self.runs.prepare(self.access, self.saved())
        value = plan.value()
        value["workspaceId"] = "another"
        with self.assertRaises(DeskWeaveError) as caught:
            self.runs.store.create(
                RunPlan(dumps(value), plan.request_hash),
                "other",
                authorize=self.access.check_snapshot,
            )
        self.assertEqual(caught.exception.code, "WORKSPACE_MISMATCH")
        value = plan.value()
        value["workflows"] = []
        with self.assertRaises(DeskWeaveError):
            RunPlan(dumps(value), plan.request_hash).value()
        self.empty_runs()

    def test_debug_entry_parameters_do_not_register_partial_run(self):
        params = self.saved()
        for fields in [
            {"startNode": "done", "initialVariables": {}, "planId": "plan"},
            {"initialVariables": {}},
            {"planId": "plan"},
        ]:
            with self.assertRaises(DeskWeaveError) as caught:
                self.runs.accept(self.access, "debug", params | fields)
            self.assertEqual(caught.exception.code, "METHOD_UNAVAILABLE")
        self.empty_runs()

    def legacy_v3(self):
        self.engine.close()
        with closing(sqlite3.connect(self.directory / "data/workspace.db")) as connection:
            for name in [
                "execution_value",
                "execution_unhandled",
                "execution_frame",
                "execution_run",
                "run_asset_ref",
                "run_workflow_ref",
                "run_event",
                "frame",
                "run",
            ]:
                connection.execute(f"DROP TABLE {name}")
            connection.execute(
                "ALTER TABLE command_receipt RENAME COLUMN subject_id TO workflow_id"
            )
            for name in ["subject_kind", "authorization_ref", "authorization_version"]:
                connection.execute(f"ALTER TABLE command_receipt DROP COLUMN {name}")
            connection.execute("PRAGMA user_version=3")

    def reopen(self):
        self.workspace = Workspace(self.directory)
        self.engine = self.make_engine()
        self.runs = RunService(self.workspace)
        self.editor = self.engine.open_session(
            "editor", {"workflow.read", "workflow.write", "asset.read"}
        )

    def test_v3_receipts_and_versions_upgrade_without_reinterpretation(self):
        params = self.saved()
        with self.workspace.transaction() as connection:
            before = tuple(
                connection.execute(
                    "SELECT client_id,request_id,method,subject_id,payload_hash,result,created_at "
                    "FROM command_receipt"
                ).fetchone()
            )
        self.legacy_v3()
        self.reopen()
        with self.workspace.transaction() as connection:
            after = tuple(
                connection.execute(
                    "SELECT client_id,request_id,method,subject_id,payload_hash,result,created_at "
                    "FROM command_receipt"
                ).fetchone()
            )
            self.assertEqual(after, before)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(
            self.success("request.status", {"requestId": before[1]})["result"],
            json.loads(before[5]),
        )
        self.runs.accept(self.access, "new-run", params)

    def test_v3_upgrade_failure_is_atomic_and_reopenable(self):
        self.saved()
        self.legacy_v3()
        connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("CREATE INDEX IF NOT EXISTS run_asset_target"):
                    raise sqlite3.OperationalError("schema upgrade failure")
                return super().execute(sql, *args)

        with patch(
            "deskweave.adapters.workspace.sqlite3.connect",
            side_effect=lambda *args, **kwargs: connect(*args, factory=FailingConnection, **kwargs),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                Workspace(self.directory)
        with closing(connect(self.directory / "data/workspace.db")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(command_receipt)")}
            self.assertIn("workflow_id", columns)
            self.assertNotIn("subject_id", columns)
            self.assertIsNone(
                connection.execute("SELECT name FROM sqlite_master WHERE name='run'").fetchone()
            )
        self.reopen()

    def test_internal_run_receipts_are_not_workflow_save_receipts(self):
        params = self.saved()
        result = self.runs.accept(self.access, "internal", params)
        session = self.engine.open_session("runner", {"workflow.read", "workflow.write"})
        response = self.request("request.status", {"requestId": "internal"}, session=session)
        self.assertEqual(response["error"]["code"], "ACCESS_DENIED")
        self.assertEqual(RunStore(self.workspace).read(result["runId"])["state"], "queued")
        self.assertNotIn("run.start", self.engine.methods)

    def test_deadline_before_commit_preserves_empty_admission(self):
        from deskweave.adapters.runs import write_receipt

        params = self.saved()
        elapsed = [0.0]

        def expire(*args, **kwargs):
            write_receipt(*args, **kwargs)
            elapsed[0] = 2.0

        with patch("deskweave.adapters.runs.write_receipt", side_effect=expire):
            with self.assertRaises(DeskWeaveError) as caught:
                with self.workspace.budget(1.0, lambda: elapsed[0]):
                    self.runs.accept(self.access, "deadline", params)
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.empty_runs()

    def test_metadata_change_after_prepare_rejects_registration(self):
        params = self.saved()
        plan = self.runs.prepare(self.access, params)
        changed = definition()
        changed["name"] = "已修改"
        identity = self.workspace._put_object(changed)
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE revision SET definition_hash=? WHERE id=?", (identity, params["revision"])
            )
        with self.assertRaises(DeskWeaveError) as caught:
            self.runs.store.create(plan, "changed", authorize=self.access.check_snapshot)
        self.assertEqual(caught.exception.code, "DEPENDENCY_MISMATCH")
        self.empty_runs()

    def legacy_v4(self):
        self.engine.close()
        with closing(sqlite3.connect(self.directory / "data/workspace.db")) as connection:
            for name in (
                "execution_value",
                "execution_unhandled",
                "execution_frame",
                "execution_run",
            ):
                connection.execute(f"DROP TABLE {name}")
            connection.execute("PRAGMA user_version=4")

    def test_v4_upgrade_preserves_run_plan_and_original_receipt(self):
        params = self.saved()
        accepted = self.runs.accept(self.access, "accepted-v4", params)
        before = self.runs.store.read(accepted["runId"])
        self.legacy_v4()
        self.reopen()
        self.assertEqual(self.runs.store.read(accepted["runId"]), before)
        self.assertEqual(self.runs.accept(self.access, "accepted-v4", params), accepted)
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM execution_run").fetchone()[0], 0
            )

    def test_v4_upgrade_failure_restores_entire_schema(self):
        self.saved()
        self.legacy_v4()
        connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("CREATE TABLE IF NOT EXISTS execution_unhandled"):
                    raise sqlite3.OperationalError("execution schema upgrade failed")
                return super().execute(sql, *args)

        with patch(
            "deskweave.adapters.workspace.sqlite3.connect",
            side_effect=lambda *args, **kwargs: connect(*args, factory=FailingConnection, **kwargs),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                Workspace(self.directory)
        with closing(connect(self.directory / "data/workspace.db")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertFalse(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'execution_%'"
                ).fetchall()
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM command_receipt").fetchone()[0], 1
            )
        self.reopen()
