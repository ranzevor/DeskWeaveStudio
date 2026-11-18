import sqlite3
from contextlib import closing
from unittest.mock import patch

from deskweave.adapters.run_commands import RunCommand
from deskweave.adapters.runs import RunStore
from deskweave.adapters.workspace import Workspace
from deskweave.engine.run_manager import RunManager
from deskweave.engine.run_planning import RunAccess, RunService
from deskweave.engine.scheduler import Runtime
from tests.engine.test_runs import definition
from tests.engine.test_service import EngineCase


class RunStorageTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.access = RunAccess(
            "runner",
            "grant",
            1,
            frozenset({"workflow.read", "run.execute", "run.control", "run.read"}),
        )
        self.runtime = Runtime(self.workspace, clock=lambda: self.elapsed[0], threaded=False)

    def tearDown(self):
        self.runtime.close()
        super().tearDown()

    def accepted(self):
        value = definition()
        saved = self.save(value, request_id="saved")
        return RunService(self.workspace).accept(
            self.access,
            "started",
            {
                "workflowId": value["id"],
                "revision": saved["revision"],
                "inputs": {},
                "targets": {},
                "detached": False,
            },
        )["runId"]

    def rows(self, table):
        with self.workspace.transaction() as connection:
            return [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def test_run_and_frame_versions_preserve_prior_read_points(self):
        run = self.accepted()
        root = RunStore(self.workspace).read(run)["rootFrameId"]
        with self.workspace.transaction() as connection:
            watermark = connection.execute(
                "SELECT MAX(sequence) FROM run_catalog_version"
            ).fetchone()[0]
        self.runtime.attach(run, self.access)
        self.runtime.pump()
        self.assertEqual(self.runtime.result(run, access=self.access)["state"], "succeeded")
        with self.workspace.transaction() as connection:
            old = connection.execute(
                "SELECT state FROM run_catalog_version WHERE run_id=? AND sequence<=? "
                "AND (valid_until IS NULL OR valid_until>?)",
                (run, watermark, watermark),
            ).fetchone()[0]
            self.assertEqual(old, "queued")
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM run_frame_version WHERE run_id=? AND frame_id=? "
                    "AND version<=0 AND (valid_until IS NULL OR valid_until>0)",
                    (run, root),
                ).fetchone()[0],
                "queued",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM run_frame_version WHERE run_id=? AND frame_id=? "
                    "AND valid_until IS NULL",
                    (run, root),
                ).fetchone()[0],
                "succeeded",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT frame_count FROM run_metrics WHERE run_id=?", (run,)
                ).fetchone()[0],
                1,
            )

    def test_failed_state_publication_rolls_back_views_and_events(self):
        from deskweave.adapters.run_views import record_run

        run = self.accepted()
        before = {
            name: self.rows(name)
            for name in (
                "run",
                "frame",
                "run_event",
                "run_catalog_version",
                "run_frame_version",
                "run_metrics",
            )
        }

        def fail(*args, **kwargs):
            record_run(*args, **kwargs)
            raise sqlite3.OperationalError("state publication failed")

        with patch("deskweave.adapters.execution.record_run", fail):
            with self.assertRaises(sqlite3.OperationalError):
                self.runtime.attach(run, self.access)
        self.assertNotIn(run, self.runtime._active)
        for name, rows in before.items():
            self.assertEqual(self.rows(name), rows, name)
        self.runtime.attach(run, self.access)
        self.runtime.pump()
        self.assertEqual(self.runtime.read_state(run, access=self.access)["state"], "succeeded")

    def format_five(self):
        self.runtime.close()
        self.workspace.close()
        with (
            closing(sqlite3.connect(self.directory / "data/workspace.db")) as connection,
            connection,
        ):
            for table in ("run_catalog_version", "run_frame_version", "run_metrics"):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("DROP INDEX execution_inspection_names")
            connection.execute("DROP INDEX run_state_sequence")
            connection.execute("DROP INDEX frame_unfinished")
            connection.execute("PRAGMA user_version=5")

    def test_format_five_upgrade_preserves_plans_and_receipts(self):
        run = self.accepted()
        original = RunStore(self.workspace).read(run)
        receipts = self.rows("command_receipt")
        self.format_five()
        self.workspace = Workspace(self.directory)
        self.assertEqual(RunStore(self.workspace).read(run), original)
        self.assertEqual(self.rows("command_receipt"), receipts)
        self.assertEqual(len(self.rows("run_frame_version")), 1)
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(
                connection.execute(
                    "SELECT frame_count FROM run_metrics WHERE run_id=?", (run,)
                ).fetchone()[0],
                1,
            )

    def test_format_upgrade_failure_rolls_back_all_new_tables(self):
        from deskweave.adapters.run_views import initialize_run_views

        run = self.accepted()
        self.format_five()

        def fail(connection):
            initialize_run_views(connection)
            raise sqlite3.OperationalError("format upgrade failed")

        with patch("deskweave.adapters.run_views.initialize_run_views", fail):
            with self.assertRaises(sqlite3.OperationalError):
                Workspace(self.directory)
        with (
            closing(sqlite3.connect(self.directory / "data/workspace.db")) as connection,
            connection,
        ):
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            for table in ("run_catalog_version", "run_frame_version", "run_metrics"):
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                )
        self.workspace = Workspace(self.directory)
        self.assertEqual(RunStore(self.workspace).read(run)["state"], "queued")

    def command(self, run, method, version, request_id="control", deadline=100):
        return RunCommand(
            self.access,
            request_id,
            method,
            {"runId": run, "expectedControlVersion": version},
            deadline=deadline,
            clock=lambda: self.elapsed[0],
            authorize=lambda: None,
        )

    def test_control_replay_returns_original_result_after_later_transition(self):
        run = self.accepted()
        self.runtime.attach(run, self.access)
        original = self.runtime.read_state(run, access=self.access)
        receipt = self.command(run, "run.pause", original["controlVersion"])
        pending = self.runtime.control(
            run, "pause", original["controlVersion"], access=self.access, receipt=receipt
        )
        self.runtime.tick()
        paused = pending.result()
        resume = self.runtime.control(run, "resume", paused["controlVersion"], access=self.access)
        self.runtime.tick()
        resumed = resume.result()
        before = {
            name: self.rows(name)
            for name in ("run", "run_event", "command_receipt", "run_catalog_version")
        }
        replay = self.runtime.control(
            run, "pause", original["controlVersion"], access=self.access, receipt=receipt
        )
        self.runtime.tick()
        self.assertEqual(replay.result(), receipt.result(paused))
        self.assertEqual(
            self.runtime.read_state(run, access=self.access)["state"], resumed["state"]
        )
        for name, rows in before.items():
            self.assertEqual(self.rows(name), rows, name)
        conflict = self.runtime.control(
            run,
            "pause",
            resumed["controlVersion"],
            access=self.access,
            receipt=self.command(run, "run.pause", resumed["controlVersion"]),
        )
        self.runtime.tick()
        self.assertEqual(conflict.exception().code, "REQUEST_CONFLICT")
        self.runtime.close()

    def test_control_receipt_failure_rolls_back_control_and_event(self):
        from deskweave.adapters.run_commands import write_receipt

        run = self.accepted()
        self.runtime.attach(run, self.access)
        state = self.runtime.read_state(run, access=self.access)
        before = {
            name: self.rows(name)
            for name in ("run", "run_event", "command_receipt", "run_catalog_version")
        }
        receipt = self.command(run, "run.pause", state["controlVersion"])

        def fail(*args, **kwargs):
            write_receipt(*args, **kwargs)
            raise sqlite3.OperationalError("receipt commit failed")

        with patch("deskweave.adapters.run_commands.write_receipt", fail):
            pending = self.runtime.control(
                run, "pause", state["controlVersion"], access=self.access, receipt=receipt
            )
            self.runtime.tick()
            self.assertIsInstance(pending.exception(), sqlite3.OperationalError)
        for name, rows in before.items():
            self.assertEqual(self.rows(name), rows, name)
        retry = self.runtime.control(
            run, "pause", state["controlVersion"], access=self.access, receipt=receipt
        )
        self.runtime.tick()
        self.assertEqual(retry.result()["state"], "paused")
        self.runtime.close()

    def test_control_deadline_before_commit_preserves_prior_state(self):
        from deskweave.adapters.run_views import record_run

        run = self.accepted()
        self.runtime.attach(run, self.access)
        state = self.runtime.read_state(run, access=self.access)
        before = {
            name: self.rows(name)
            for name in ("run", "run_event", "command_receipt", "run_catalog_version")
        }

        def expire(*args, **kwargs):
            record_run(*args, **kwargs)
            self.elapsed[0] = 2

        with patch("deskweave.adapters.execution.record_run", expire):
            pending = self.runtime.control(
                run,
                "pause",
                state["controlVersion"],
                access=self.access,
                receipt=self.command(run, "run.pause", state["controlVersion"], deadline=1),
            )
            self.runtime.tick()
            self.assertEqual(pending.exception().code, "DEADLINE_EXCEEDED")
        for name, rows in before.items():
            self.assertEqual(self.rows(name), rows, name)
        self.runtime.close()


class RunManagerTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.access = RunAccess(
            "runner",
            "grant",
            1,
            frozenset({"workflow.read", "run.execute", "run.control", "run.read"}),
        )
        self.manager = RunManager(self.workspace, clock=lambda: self.elapsed[0], threaded=False)
        value = definition("wait")
        value["nodes"] = [
            {
                "id": "wait",
                "title": "等待",
                "action": "time.wait",
                "params": {"durationMs": 1000},
                "on": {},
            }
        ]
        value["entry"] = "wait"
        saved = self.save(value)
        self.params = {
            "workflowId": "wait",
            "revision": saved["revision"],
            "inputs": {},
            "targets": {},
            "detached": False,
        }

    def tearDown(self):
        self.manager.close()
        super().tearDown()

    def start(self, request, **params):
        return self.manager.start(self.access, request, self.params | params)["runId"]

    def control(self, run, command):
        row = self.manager.lifecycle.read(run)
        result = self.manager.runtime.control(
            run, command, row["control_version"], access=self.access
        )
        self.manager.runtime.tick()
        return result

    def test_one_active_root_sixteen_waiting_and_replay_capacity(self):
        first = self.start("first")
        self.manager.runtime.pump()
        queued = [self.start(str(index)) for index in range(16)]
        self.assertEqual(self.start("first"), first)
        with self.assertRaises(Exception) as caught:
            self.start("overflow")
        self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")
        self.assertEqual(len(self.manager.runtime._active), 1)
        self.assertEqual(len(self.manager._pending), 16)
        self.assertEqual(self.control(first, "stop").result()["state"], "cancelled")
        self.manager.runtime.pump()
        self.assertEqual(tuple(self.manager.runtime._active), (queued[0],))
        self.start("next")
        self.assertTrue(self.manager.busy.is_set())

    def test_pending_pause_resume_and_retention(self):
        first = self.start("first")
        self.manager.runtime.pump()
        queued = self.start("queued")
        paused = self.control(queued, "pause").result()
        self.assertEqual(paused["state"], "paused")
        self.elapsed[0] = 10
        resumed = self.control(queued, "resume").result()
        self.assertEqual(resumed["state"], "queued")
        self.control(queued, "pause").result()
        self.elapsed[0] = 1810
        expired = self.control(queued, "resume")
        self.assertEqual(expired.exception().code, "TIMEOUT")
        self.assertEqual(self.manager.lifecycle.read(queued)["state"], "cancelled")
        self.assertEqual(self.manager.lifecycle.read(first)["state"], "running")

    def test_departure_pauses_attached_work_and_revocation_stops_all(self):
        attached = self.start("attached")
        self.manager.runtime.pump()
        detached = self.start("detached", detached=True)
        self.manager.client_exited(self.access.client_id)
        self.manager.runtime.pump()
        self.assertEqual(self.manager.lifecycle.read(attached)["state"], "paused")
        self.assertEqual(self.manager.lifecycle.read(detached)["state"], "queued")
        self.assertTrue(self.manager.has_owned_work(self.access.client_id))
        self.assertTrue(self.manager.busy.is_set())
        self.manager.revoke_client(self.access.client_id)
        self.manager.runtime.pump()
        self.assertEqual(self.manager.lifecycle.read(attached)["state"], "cancelled")
        self.assertEqual(self.manager.lifecycle.read(detached)["state"], "cancelled")
        self.assertFalse(self.manager.busy.is_set())
        self.assertFalse(self.manager.has_owned_work(self.access.client_id))


class RunPublicTests(EngineCase):
    def make_engine(self):
        from deskweave.engine.service import Engine

        return Engine(
            self.workspace,
            clock=lambda: self.now[0],
            monotonic=lambda: self.elapsed[0],
            threaded=False,
        )

    def setUp(self):
        super().setUp()
        self.editor = self.engine.open_session(
            "runner",
            {"workflow.read", "workflow.write", "run.execute", "run.control", "run.read"},
            capabilities={"contentRefsV1"},
        )
        self.remote = self.engine.open_session(
            "viewer", {"workflow.read", "run.read", "run.control"}, capabilities={"contentRefsV1"}
        )

    def tearDown(self):
        self.engine.close()
        super().tearDown()

    def start(self, value=None, *, request_id="start", **params):
        value = value or definition()
        revision = self.save(value)["revision"]
        self.params = {
            "workflowId": value["id"],
            "revision": revision,
            "inputs": {},
            "targets": {},
            "detached": False,
            **params,
        }
        return self.success("run.start", self.params, request_id=request_id)["runId"]

    def snapshot(self, run):
        return self.success("run.snapshot", {"runId": run}, session=self.remote)

    def raw_control(self, run, command, expected, **options):
        import time
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self.request,
                "run." + command,
                {"runId": run, "expectedControlVersion": expected},
                **options,
            )
            until = time.monotonic() + 5
            while not future.done() and time.monotonic() < until:
                self.engine.runs.runtime.pump(1)
                time.sleep(0.001)
            return future.result(timeout=1)

    def control(self, run, command, expected, **options):
        result = self.raw_control(run, command, expected, **options)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def test_prepare_start_control_replay_and_cross_client_snapshot(self):
        value = definition("prepared")
        value["nodes"] = [
            {
                "id": "wait",
                "title": "等待",
                "action": "time.wait",
                "params": {"durationMs": 100000},
                "on": {},
            }
        ]
        value["entry"] = "wait"
        revision = self.save(value)["revision"]
        params = {
            "workflowId": value["id"],
            "revision": revision,
            "inputs": {},
            "targets": {},
            "detached": False,
        }
        plan = self.success("run.prepare", params)
        self.assertTrue(plan["valid"])
        result = self.success("run.start", params | {"planId": plan["planId"]}, request_id="start")
        run = result["runId"]
        self.engine.runs.runtime.pump()
        snapshot = self.snapshot(run)
        paused = self.control(
            run, "pause", snapshot["controlVersion"], session=self.remote, request_id="pause"
        )
        self.assertEqual(paused["state"], "paused")
        resumed = self.control(run, "resume", paused["controlVersion"])
        self.assertEqual(resumed["state"], "running")
        replay = self.control(
            run, "pause", snapshot["controlVersion"], session=self.remote, request_id="pause"
        )
        self.assertEqual(replay, paused)
        receipt = self.success("request.status", {"requestId": "pause"}, session=self.remote)
        self.assertEqual(receipt["result"], paused)
        self.assertEqual(
            self.success("request.status", {"requestId": "pause"})["state"], "notReceived"
        )
        self.elapsed[0] += 61
        self.assertEqual(
            self.success("run.start", params | {"planId": plan["planId"]}, request_id="start"),
            result,
        )
        self.control(run, "stop", resumed["controlVersion"])

    def test_frame_pages_and_catalog_keep_source_version(self):
        run = self.start()
        old = self.snapshot(run)
        catalog = self.success("workspace.catalog", {"kind": "run"})
        self.assertEqual(catalog["items"][0]["state"], "queued")
        self.engine.runs.runtime.pump()
        page = self.success(
            "query.read",
            {
                "collectionId": old["framesQuery"]["collectionId"],
                "kind": "runFrames",
                "includeTotal": True,
            },
            session=self.remote,
        )
        self.assertEqual(page["items"][0]["value"]["state"], "waiting")
        self.assertEqual(page["matchedCount"], old["frameCount"])
        current = self.snapshot(run)
        page = self.success(
            "query.read",
            {"collectionId": current["framesQuery"]["collectionId"], "kind": "runFrames"},
            session=self.remote,
        )
        self.assertEqual(page["items"][0]["value"]["state"], "succeeded")
        self.assertEqual(current["state"], "succeeded")

    def test_variables_snapshot_redaction_and_private_resources(self):
        value = definition()
        value["variables"] = {"a": "x" * 5000, "b": 1, "c": 2}
        value["parameters"] = {
            "secret": {"type": "string", "required": False, "sensitive": True, "default": "hidden"}
        }
        run = self.start(value)
        root = self.engine.runs.runtime.runs.read(run)["rootFrameId"]
        params = {
            "runId": run,
            "snapshotId": None,
            "frameId": root,
            "scope": "local",
            "pageSize": 1,
        }
        first = self.success("debug.variables.read", params, session=self.remote)
        self.assertEqual(first["items"][0]["name"], "a")
        resource = first["items"][0]["valueResource"]["resourceId"]
        second = self.success(
            "debug.variables.read",
            params | {"snapshotId": first["snapshotId"], "cursor": first["nextCursor"]},
            session=self.remote,
        )
        self.assertEqual(second["items"][0]["name"], "b")
        secret = self.success(
            "debug.variables.read",
            params | {"snapshotId": first["snapshotId"], "scope": "inputs"},
            session=self.remote,
        )
        self.assertEqual(
            secret["items"], [{"name": "secret", "visibility": "redacted", "valueType": "string"}]
        )
        self.assertFalse(
            self.request("resource.read", {"resourceId": resource, "offset": 0, "length": 10})["ok"]
        )
        self.success(
            "resource.read",
            {"resourceId": resource, "offset": 0, "length": 10},
            session=self.remote,
        )
        self.elapsed[0] += 61
        expired = self.request(
            "debug.variables.read",
            params | {"snapshotId": first["snapshotId"]},
            session=self.remote,
        )
        self.assertEqual(expired["error"]["code"], "INSPECTION_SNAPSHOT_EXPIRED")

    def test_events_are_ordered_and_epoch_mismatch_requires_snapshot(self):
        run = self.start()
        subscription = self.success("event.subscribe", {"runId": run})
        self.engine.runs.runtime.pump()
        params = {
            "runId": run,
            "instanceEpoch": subscription["instanceEpoch"],
            "afterSeq": subscription["snapshot"]["lastSeq"],
            "limit": 2,
        }
        seqs = []
        while True:
            page = self.success("event.read", params)
            seqs.extend(int(event["seq"]) for event in page["events"])
            if not page["hasMore"]:
                break
            params["afterSeq"] = page["events"][-1]["seq"]
        self.assertEqual(seqs, list(range(seqs[0], seqs[-1] + 1)))
        mismatch = self.success("event.read", params | {"instanceEpoch": "old"})
        self.assertTrue(mismatch["resyncRequired"])
        self.success("event.unsubscribe", {"subscriptionId": subscription["subscriptionId"]})
        self.assertFalse(self.engine.events.subscriptions)

    def test_connection_close_and_explicit_client_exit_differ(self):
        run = self.start(detached=True)
        self.engine.close_session(self.editor.id)
        self.engine.runs.runtime.pump()
        self.assertEqual(self.snapshot(run)["state"], "succeeded")
        self.assertFalse(self.engine.has_work)

    def test_restart_interrupts_active_tree_and_preserves_committed_values(self):
        run = self.start()
        self.engine.runs.runtime.pump(2)
        old = self.snapshot(run)
        self.workspace.close()
        self.workspace = Workspace(self.directory)
        self.engine = self.make_engine()
        self.remote = self.engine.open_session("new-reader", {"workflow.read", "run.read"})
        after = self.snapshot(run)
        self.assertEqual(after["state"], "interrupted")
        self.assertGreater(after["controlVersion"], old["controlVersion"])
        self.assertFalse(self.engine.has_work)

    def test_least_privilege_run_read_and_run_control_sessions(self):
        value = definition("leastPrivilege")
        value["variables"] = {"huge": "x" * 5000}
        value["nodes"] = [
            {
                "id": "wait",
                "title": "等待",
                "action": "time.wait",
                "params": {"durationMs": 100000},
                "on": {},
            }
        ]
        value["entry"] = "wait"
        run = self.start(value)
        self.engine.runs.runtime.pump()
        reader = self.engine.open_session("only-read", {"run.read"}, capabilities={"contentRefsV1"})
        controller = self.engine.open_session("only-control", {"run.control"})
        scoped_read = self.engine.open_session(
            "scoped-read", {"run.read"}, workflow_ids={"unrelated"}
        )
        scoped_control = self.engine.open_session(
            "scoped-control", {"run.control"}, workflow_ids={"unrelated"}
        )
        catalog = self.success("workspace.catalog", {"kind": "run"}, session=reader)
        self.assertEqual([item["id"] for item in catalog["items"]], [run])
        self.assertEqual(
            self.success("workspace.catalog", {"kind": "run"}, session=scoped_read)["items"], []
        )
        self.assertEqual(
            self.request("run.snapshot", {"runId": run}, session=scoped_read)["error"]["code"],
            "ACCESS_DENIED",
        )
        snapshot = self.success("run.snapshot", {"runId": run}, session=reader)
        self.assertEqual(snapshot["state"], "running")
        frames = self.success(
            "query.read",
            {"collectionId": snapshot["framesQuery"]["collectionId"], "kind": "runFrames"},
            session=reader,
        )
        self.assertEqual(len(frames["items"]), 1)
        variables = self.success(
            "debug.variables.read",
            {
                "runId": run,
                "snapshotId": None,
                "frameId": frames["items"][0]["value"]["frameId"],
                "scope": "local",
            },
            session=reader,
        )
        resource_id = variables["items"][0]["valueResource"]["resourceId"]
        self.success(
            "resource.read",
            {"resourceId": resource_id, "offset": 0, "length": 16},
            session=reader,
        )
        subscription = self.success("event.subscribe", {"runId": run}, session=reader)
        self.success(
            "event.read",
            {
                "runId": run,
                "instanceEpoch": subscription["instanceEpoch"],
                "afterSeq": "0",
                "limit": 10,
            },
            session=reader,
        )
        self.success(
            "event.unsubscribe", {"subscriptionId": subscription["subscriptionId"]}, session=reader
        )
        invalid = definition("invalid")
        del invalid["id"]
        validated = self.success("workflow.validate", {"definition": invalid}, session=self.editor)
        self.assertEqual(
            self.request(
                "query.read",
                {
                    "collectionId": validated["diagnosticsQuery"]["collectionId"],
                    "kind": "diagnostics",
                },
                session=reader,
            )["error"]["code"],
            "COLLECTION_NOT_FOUND",
        )
        self.assertEqual(
            self.raw_control(run, "pause", snapshot["controlVersion"], session=scoped_control)[
                "error"
            ]["code"],
            "ACCESS_DENIED",
        )
        paused = self.control(run, "pause", snapshot["controlVersion"], session=controller)
        self.assertEqual(paused["state"], "paused")
        resumed = self.control(run, "resume", paused["controlVersion"], session=controller)
        self.assertEqual(resumed["state"], "running")
        stopped = self.control(run, "stop", resumed["controlVersion"], session=controller)
        self.assertEqual(stopped["state"], "cancelled")

    def test_inspection_empty_name_and_error_resource_cleanup(self):
        from deskweave.domain.frozen import Value

        value = definition("inspectEmptyAndCleanup")
        value["variables"] = {"a": 1, "big1": "y" * 5000, "big2": "z" * 5000}
        run = self.start(value, request_id="start-inspect-1")
        root = self.engine.runs.runtime.runs.read(run)["rootFrameId"]
        empty_value = self.workspace._put_object("empty")
        empty_labels = self.workspace._put_object(Value.freeze("empty").labels())
        with self.workspace.transaction() as connection:
            connection.execute(
                "INSERT INTO execution_value VALUES(?,?,?,?,?,?,?,?)",
                (run, root, "local", "", 0, empty_value, empty_labels, None),
            )
        params = {
            "runId": run,
            "snapshotId": None,
            "frameId": root,
            "scope": "local",
            "pageSize": 2,
        }
        first = self.success("debug.variables.read", params, session=self.remote)
        self.assertEqual(first["total"], 4)
        self.assertEqual([item["name"] for item in first["items"]], ["", "a"])
        second = self.success(
            "debug.variables.read",
            params | {"snapshotId": first["snapshotId"], "cursor": first["nextCursor"]},
            session=self.remote,
        )
        self.assertEqual([item["name"] for item in second["items"]], ["big1", "big2"])
        self.assertEqual(len(self.engine.contents.resources), 2)

        before_snapshots = len(self.engine.inspection.snapshots)
        before_resources = len(self.engine.contents.resources)
        bad_frame = self.request(
            "debug.variables.read",
            params | {"frameId": "missing-frame"},
            session=self.remote,
        )
        self.assertEqual(bad_frame["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(len(self.engine.inspection.snapshots), before_snapshots)
        self.assertEqual(len(self.engine.contents.resources), before_resources)

        with patch.object(self.engine, "result_limit", return_value=400):
            bad_cursor = self.request(
                "debug.variables.read",
                {
                    "runId": run,
                    "snapshotId": first["snapshotId"],
                    "frameId": root,
                    "scope": "local",
                    "pageSize": 1,
                    "cursor": "expired-cursor",
                },
                session=self.remote,
            )
            self.assertEqual(bad_cursor["error"]["code"], "CURSOR_EXPIRED")
            self.assertEqual(len(self.engine.inspection.snapshots), before_snapshots)
            self.assertEqual(len(self.engine.contents.resources), before_resources)

        value_huge = definition("inspectResourceCleanup")
        value_huge["variables"] = {"a_big": "y" * 5000, "b_big": "z" * 5000}
        run_huge = self.start(value_huge, request_id="start-inspect-2")
        root_huge = self.engine.runs.runtime.runs.read(run_huge)["rootFrameId"]
        with patch.object(self.engine, "result_limit", return_value=400):
            oversize = self.request(
                "debug.variables.read",
                {
                    "runId": run_huge,
                    "snapshotId": None,
                    "frameId": root_huge,
                    "scope": "local",
                    "pageSize": 2,
                },
                session=self.remote,
            )
            self.assertEqual(oversize["error"]["code"], "QUERY_ITEM_TOO_LARGE")
            self.assertEqual(len(self.engine.inspection.snapshots), before_snapshots)
            self.assertEqual(len(self.engine.contents.resources), before_resources)

        with patch.object(self.engine, "result_limit", return_value=760):
            split = self.success(
                "debug.variables.read",
                {
                    "runId": run_huge,
                    "snapshotId": None,
                    "frameId": root_huge,
                    "scope": "local",
                    "pageSize": 2,
                },
                session=self.remote,
            )
        self.assertEqual([item["name"] for item in split["items"]], ["a_big"])
        self.assertEqual(len(self.engine.contents.resources), before_resources + 1)

    def test_diagnostics_text_code_and_null_node_id_filter(self):
        value = definition("diagFilter")
        value["parameters"] = {
            "count": {"type": "integer", "required": True, "sensitive": False, "default": "bad"}
        }
        value["nodes"] = [
            {
                "id": "wait",
                "title": "等待",
                "action": "time.wait",
                "params": {"durationMs": 1},
                "on": {"next": "missing"},
            }
        ]
        value["entry"] = "wait"
        validated = self.success("workflow.validate", {"definition": value})
        collection_id = validated["diagnosticsQuery"]["collectionId"]
        all_items = self.success(
            "query.read",
            {"collectionId": collection_id, "kind": "diagnostics", "filter": {}},
        )["items"]
        self.assertGreaterEqual(len(all_items), 2)
        workflow_level = self.success(
            "query.read",
            {"collectionId": collection_id, "kind": "diagnostics", "filter": {"nodeId": None}},
        )["items"]
        self.assertEqual([item["value"]["code"] for item in workflow_level], ["PARAMETER_TYPE"])
        node_level = self.success(
            "query.read",
            {"collectionId": collection_id, "kind": "diagnostics", "filter": {"nodeId": "wait"}},
        )["items"]
        self.assertEqual([item["value"]["code"] for item in node_level], ["OUTLET_NOT_FOUND"])

        prepared = definition("prepareDiag")
        prepared["parameters"] = {
            "requiredInput": {"type": "string", "required": True, "sensitive": False}
        }
        revision = self.save(prepared)["revision"]
        plan = self.success(
            "run.prepare",
            {
                "workflowId": prepared["id"],
                "revision": revision,
                "inputs": {},
                "targets": {},
                "detached": False,
            },
        )
        self.assertFalse(plan["valid"])
        diag_id = plan["diagnosticsQuery"]["collectionId"]
        by_code = self.success(
            "query.read",
            {
                "collectionId": diag_id,
                "kind": "diagnostics",
                "filter": {"text": "invalid_argument"},
            },
        )["items"]
        self.assertEqual(len(by_code), 1)
        self.assertEqual(by_code[0]["value"]["code"], "INVALID_ARGUMENT")
        by_null = self.success(
            "query.read",
            {"collectionId": diag_id, "kind": "diagnostics", "filter": {"nodeId": None}},
        )["items"]
        self.assertEqual(len(by_null), 1)
        by_node = self.success(
            "query.read",
            {"collectionId": diag_id, "kind": "diagnostics", "filter": {"nodeId": "wait"}},
        )["items"]
        self.assertEqual(by_node, [])

    def test_event_pending_round_robin_and_subscription_error_isolation(self):
        r1 = self.start(definition("subOne"), request_id="sub-start-1")
        r2 = self.start(definition("subTwo"), request_id="sub-start-2")
        s1 = self.success("event.subscribe", {"runId": r1})["subscriptionId"]
        self.engine.events.subscriptions["broken"] = {
            "owner": self.editor.id,
            "run": "missing-run",
            "after": 0,
            "active": True,
        }
        s2 = self.success("event.subscribe", {"runId": r2})["subscriptionId"]
        self.engine.events.activate(self.editor.id, s1)
        self.engine.events.activate(self.editor.id, s2)
        self.engine.runs.runtime.pump()
        first = self.engine.events.pending(self.editor)
        second = self.engine.events.pending(self.editor)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual({first[0], second[0]}, {s1, s2})
        self.assertNotIn("broken", self.engine.events.subscriptions)

    def test_worker_runtime_close_does_not_wait_on_paused_work(self):
        from pathlib import Path
        from types import SimpleNamespace

        from deskweave.process._worker import Runtime as WorkerRuntime
        from deskweave.transport.authority import ConnectionAuthority
        from deskweave.transport.protocol import PeerIdentity

        value = definition("pausedOnClose")
        value["nodes"] = [
            {
                "id": "wait",
                "title": "等待",
                "action": "time.wait",
                "params": {"durationMs": 100000},
                "on": {},
            }
        ]
        value["entry"] = "wait"
        run = self.start(value)
        self.engine.runs.runtime.pump()
        snap = self.snapshot(run)
        paused = self.control(run, "pause", snap["controlVersion"])
        self.assertEqual(paused["state"], "paused")
        self.assertTrue(self.engine.has_work)

        component = Path(self.temporary.name) / "component.py"
        component.write_bytes(b"")
        item = SimpleNamespace(
            path=component,
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        installation = SimpleNamespace(
            executable=item, components=(item,), macos_requirement=None, digest="a" * 64
        )
        identity = PeerIdentity(
            "windows",
            "fixture-user",
            "fixture-session",
            100,
            "start",
            installation.digest,
            "instance",
        )
        binding = SimpleNamespace(
            identity=identity,
            installation=installation,
            check=lambda checkpoint: checkpoint(),
            _check_process=lambda: None,
            close=lambda: None,
        )
        parent = SimpleNamespace(
            identity=PeerIdentity(
                "windows",
                "fixture-user",
                "fixture-session",
                101,
                "start",
                installation.digest,
                "instance",
            ),
            installation=installation,
            check=lambda checkpoint: checkpoint(),
            _check_process=lambda: None,
            close=lambda: None,
        )
        listener = SimpleNamespace(local=binding, close=lambda: None)
        authority = ConnectionAuthority(self.engine, identity)
        runtime = WorkerRuntime(listener, authority, parent, max_states=16, max_bytes=16384)
        with patch.object(runtime.registry, "depart_clients", side_effect=AssertionError):
            runtime.close()
        self.assertTrue(authority._closed)

    def test_serve_connection_subscribe_concurrent_revoke_does_not_fail(self):
        from types import SimpleNamespace

        from deskweave.errors import DeskWeaveError
        from deskweave.transport.authority import ConnectionAuthority
        from deskweave.transport.connection import ClientConnection
        from deskweave.transport.native.service import serve_connection
        from deskweave.transport.protocol import ConnectionPolicy, PeerIdentity

        run = self.start()
        identity = PeerIdentity(
            "windows",
            "fixture-user",
            "fixture-session",
            100,
            "start",
            "a" * 64,
            "instance",
        )
        authority = ConnectionAuthority(self.engine, identity)
        credential = authority.authorize(ConnectionPolicy(frozenset({"run.read"})), lifetime=60.0)
        ticket = authority.issue(credential, identity)
        client = ClientConnection(ticket, identity, identity)
        queued = [client.start()]

        def receive(*, budget):
            budget.check()
            if not queued:
                raise DeskWeaveError("TRANSPORT_CLOSED", "传输已关闭。")
            return queued.pop(0)

        def send(payload, *, budget=None, **_kwargs):
            if budget is not None:
                budget.check()
            if payload.get("kind") == "connection.challenge":
                queued.append(client.receive(payload))
            elif payload.get("kind") == "connection.ready":
                client.receive(payload)
                queued.append(
                    {
                        "protocolVersion": "1.0",
                        "kind": "request",
                        "id": "sub-race",
                        "method": "event.subscribe",
                        "params": {"runId": run},
                    }
                )
            elif payload.get("replyTo") == "sub-race":
                authority.revoke(credential)

        channel = SimpleNamespace(
            local=identity,
            peer=identity,
            max_frame_bytes=16384,
            check=lambda _check_time=None: None,
            _check_identity=lambda _check_time=None: None,
            receive=receive,
            send=send,
            close=lambda: None,
        )
        with self.assertRaises(DeskWeaveError) as ctx:
            serve_connection(channel, authority)
        self.assertEqual(ctx.exception.code, "TRANSPORT_CLOSED")
        self.assertEqual(self.engine.events.subscriptions, {})
