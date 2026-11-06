import sqlite3
import uuid
from contextlib import closing
from unittest.mock import patch

from deskweave.contracts import validate
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps
from tests.engine.test_service import EngineCase, workflow
from tests.engine.test_values import expr, path


class BoundaryTests(EngineCase):
    def test_preview_parameter_byte_budget(self):
        for size in (262143, 262144, 262145):
            with self.subTest(size=size):
                params = {"value": ["汉" * 40000, ""]}
                params["value"][1] = "x" * (size - len(dumps(params)))
                self.assertEqual(len(dumps(params)), size)
                response = self.request("expression.preview", params)
                if size <= 262144:
                    self.assertTrue(response["ok"], response)
                    self.assertEqual(len(dumps(response["result"])), size)
                else:
                    self.assertFalse(response["ok"], "oversized parameters accepted")
                    self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")

    def test_preview_result_byte_budget(self):
        for size in (262143, 262144, 262145):
            with self.subTest(size=size):
                base = len(dumps({"value": ["", "", ""]}))
                length, remainder = divmod(size - base, 2)
                params = {
                    "value": [path("vars", "text"), path("vars", "text"), "x" * remainder],
                    "context": {"vars": {"text": "x" * length}},
                }
                self.assertLess(len(dumps(params)), 262144)
                response = self.request("expression.preview", params)
                if size <= 262144:
                    self.assertTrue(response["ok"], response)
                    self.assertEqual(len(dumps(response["result"])), size)
                else:
                    self.assertFalse(response["ok"], "oversized result accepted")
                    self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")

    def test_nested_action_diagnostics_locate_fields(self):
        definition = workflow()
        definition["nodes"][0].update(action="time.wait", params={})
        result = self.success("workflow.validate", {"definition": definition})
        page = self.success(
            "query.read",
            {
                "kind": "diagnostics",
                "collectionId": result["diagnosticsQuery"]["collectionId"],
            },
        )
        self.assertEqual(
            [item["value"]["fieldPath"] for item in page["items"]], ["/nodes/0/params/durationMs"]
        )

    def test_catalog_snapshots_do_not_copy_all_members(self):
        with self.workspace.transaction() as connection:
            connection.executemany(
                "INSERT INTO "
                "revision(sequence,id,workflow_id,parent_id,name,definition_hash,dependencies_hash,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    (
                        i,
                        f"rev-{i}",
                        f"flow-{i}",
                        None,
                        f"流程 {i}",
                        "0" * 64,
                        "1" * 64,
                        "2026-01-01T00:00:00Z",
                    )
                    for i in range(1, 2001)
                ),
            )
            connection.executemany(
                "INSERT INTO workflow VALUES(?,?,?,?,?)",
                (
                    (f"flow-{i}", f"流程 {i}", f"rev-{i}", "2026-01-01T00:00:00Z", i)
                    for i in range(1, 2001)
                ),
            )
            before = connection.total_changes
        for _ in range(4):
            page = self.success("workspace.catalog", {"kind": "workflow", "pageSize": 1})
            self.assertEqual(page["items"][0]["id"], "flow-2000")
        with self.workspace.transaction() as connection:
            self.assertLessEqual(connection.total_changes - before, 16)

    def test_expression_error_contract(self):
        for value, reason, location in (
            ({"answer": expr("divide", 1, 0)}, "DIVISION_BY_ZERO", ["answer", "$expr", "args", 1]),
            ([path("vars", "missing")], "PATH_MISSING", [0, "$expr", "path", 0]),
            (expr("add", True, 1), "TYPE_MISMATCH", ["$expr", "args", 0]),
        ):
            response = self.request(
                "expression.preview", {"value": value}, request_id=uuid.uuid4().hex
            )
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")
            self.assertEqual(
                response["error"]["details"], {"expressionPath": location, "reason": reason}
            )
            validate("urn:deskweave:message:1", response)
            validate(
                "urn:deskweave:values:1#/$defs/expressionErrorDetails", response["error"]["details"]
            )

    def test_action_diagnostics_discriminate_multiple_fields_and_unknown_action(self):
        cases = (
            (
                {"action": "data.assign", "params": {}},
                {"/nodes/0/params/name", "/nodes/0/params/value"},
            ),
            (
                {"action": "time.wait", "params": {"durationMs": -1, "unexpected": 1}},
                {"/nodes/0/params/durationMs", "/nodes/0/params/unexpected"},
            ),
            ({"action": "unknown", "params": {}}, {"/nodes/0/action"}),
            (
                {
                    "action": "time.wait",
                    "params": {"durationMs": {"$expr": {"op": "divide", "args": [1]}}},
                },
                {"/nodes/0/params/durationMs/$expr/args"},
            ),
            (
                {"action": "time.wait", "params": {"durationMs": {"$literal": "bad"}}},
                {"/nodes/0/params/durationMs/$literal"},
            ),
            (
                {
                    "action": "time.wait",
                    "params": {"durationMs": {"$expr": {"op": "unknown", "args": []}}},
                },
                {"/nodes/0/params/durationMs/$expr/op"},
            ),
        )
        for update, expected in cases:
            with self.subTest(update=update):
                definition = workflow()
                definition["nodes"][0].update(update)
                result = self.success("workflow.validate", {"definition": definition})
                identity = result["diagnosticsQuery"]["collectionId"]
                page = self.success("query.read", {"collectionId": identity, "kind": "diagnostics"})
                self.assertEqual({item["value"]["fieldPath"] for item in page["items"]}, expected)
                self.success("query.close", {"collectionId": identity})

    def test_expression_paths_are_lossless_and_expected_type_is_strict(self):
        value = {"a/~汉": [expr("length", 12)]}
        response = self.request("expression.preview", {"value": value})
        self.assertEqual(
            response["error"]["details"],
            {"expressionPath": ["a/~汉", 0, "$expr", "args", 0], "reason": "TYPE_MISMATCH"},
        )
        response = self.request("expression.preview", {"value": False, "expectedType": "number"})
        self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(
            response["error"]["details"], {"expressionPath": [], "reason": "TYPE_MISMATCH"}
        )
        for result in (False, 0, ""):
            self.assertEqual(
                self.success(
                    "expression.preview",
                    {"value": expr("coalesce", path("vars", "missing"), result)},
                )["value"],
                result,
            )
        response = self.request(
            "expression.preview", {"value": expr("coalesce", expr("divide", 1, 0), 1)}
        )
        self.assertEqual(response["error"]["details"]["reason"], "DIVISION_BY_ZERO")
        self.assertFalse(
            self.success("expression.preview", {"value": expr("and", False, expr("divide", 1, 0))})[
                "value"
            ]
        )

    def test_expression_limit_codes_and_locations(self):
        self.engine.monotonic = lambda: 0.0
        for value, reason in (
            (expr("multiply", 9007199254740991, 2), "NUMERIC_RANGE"),
            ([expr("add", 0, 1)] * 257, "AST_NODES"),
        ):
            response = self.request("expression.preview", {"value": value})
            self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")
            self.assertEqual(response["error"]["details"]["reason"], reason)
            validate(
                "urn:deskweave:values:1#/$defs/expressionErrorDetails", response["error"]["details"]
            )

    def seed_versions(self, count, *, history=False):
        with self.workspace.transaction() as connection:
            connection.executemany(
                "INSERT INTO revision(sequence,id,workflow_id,parent_id,name,definition_hash,"
                "dependencies_hash,created_at,name_key,valid_until) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        i,
                        f"rev-{i}",
                        "history" if history else f"flow-{i:06}",
                        None if i == 1 or not history else f"rev-{i - 1}",
                        f"Item {i:06}",
                        "0" * 64,
                        "1" * 64,
                        "2026-01-01T00:00:00Z",
                        f"item {i:06}",
                        i + 1 if history and i < count else None,
                    )
                    for i in range(1, count + 1)
                ),
            )
            connection.executemany(
                "INSERT INTO workflow VALUES(?,?,?,?,?)",
                (
                    (
                        "history" if history else f"flow-{i:06}",
                        f"Item {i:06}",
                        f"rev-{i}",
                        "2026-01-01T00:00:00Z",
                        i,
                    )
                    for i in ([count] if history else range(1, count + 1))
                ),
            )

    def test_large_catalog_all_snapshot_slots_are_constant_size(self):
        self.seed_versions(40000)
        sessions = [self.engine.open_session(f"catalog-{i}", {"workflow.read"}) for i in range(5)]
        with self.workspace.transaction() as connection:
            before = connection.total_changes
        pages = []
        for session in sessions[:4]:
            for _ in range(8):
                page = self.success(
                    "workspace.catalog", {"kind": "workflow", "pageSize": 1}, session=session
                )
                self.assertEqual(page["items"][0]["id"], "flow-040000")
                pages.append(page)
        with self.workspace.transaction() as connection:
            self.assertLessEqual(connection.total_changes - before, 64)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM query_snapshot").fetchone()[0], 32
            )
            self.assertLess(
                connection.execute(
                    "SELECT SUM(length(source)+length(extra)) FROM query_snapshot"
                ).fetchone()[0],
                10000,
            )
        response = self.request("workspace.catalog", {"kind": "workflow"}, session=sessions[4])
        self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")
        self.success("query.close", {"snapshotId": pages[0]["snapshotId"]}, session=sessions[0])
        self.success("workspace.catalog", {"kind": "workflow"}, session=sessions[4])

    def test_large_history_pages_share_revision_storage(self):
        self.seed_versions(12000, history=True)
        with self.workspace.transaction() as connection:
            before = connection.total_changes
        for _ in range(8):
            first = self.success("workflow.revisions", {"workflowId": "history", "pageSize": 1})
            next_page = self.success(
                "workflow.revisions",
                {"workflowId": "history", "pageSize": 1, "cursor": first["nextCursor"]},
            )
            self.assertEqual(next_page["items"][0]["revision"], "rev-11999")
        with self.workspace.transaction() as connection:
            self.assertLessEqual(connection.total_changes - before, 24)

    def test_name_order_snapshots_keep_old_names_versions_and_ties(self):
        first = self.save(workflow("a", "Alpha"))
        second = self.save(workflow("b", "Alpha"))
        self.save(workflow("c", "Beta"))
        snapshots = {}
        for order in ("nameAsc", "nameDesc", "updatedDesc"):
            snapshots[order] = self.success(
                "workspace.catalog", {"kind": "workflow", "order": order, "pageSize": 1}
            )
        self.save(workflow("a", "Zulu"), first["revision"])
        self.save(workflow("b", "Aardvark"), second["revision"])
        self.save(workflow("d", "Alpha"))
        for order, expected in (
            ("nameAsc", ["a", "b", "c"]),
            ("nameDesc", ["c", "a", "b"]),
            ("updatedDesc", ["c", "b", "a"]),
        ):
            page = snapshots[order]
            items = list(page["items"])
            while page["nextCursor"]:
                params = {
                    "kind": "workflow",
                    "order": order,
                    "pageSize": 1,
                    "cursor": page["nextCursor"],
                }
                page = self.success("workspace.catalog", params)
                self.assertEqual(self.success("workspace.catalog", params), page)
                items.extend(page["items"])
            self.assertEqual([item["id"] for item in items], expected)
            self.assertEqual(
                {item["id"]: item["name"] for item in items},
                {"a": "Alpha", "b": "Alpha", "c": "Beta"},
            )
            self.assertEqual(
                next(item["version"] for item in items if item["id"] == "a"), first["revision"]
            )

    def test_diagnostic_pages_use_byte_budget_without_copying_sources(self):
        identity = uuid.uuid4().hex
        self.engine._collections[identity] = {
            "owner": self.editor.id,
            "workflow": None,
            "version": "v1",
            "expires": self.now[0] + 300,
            "items": [
                {
                    "code": "码" * 200,
                    "message": "汉" * 500,
                    "fieldPath": "/" + "路" * 250,
                    "nodeId": "节" * 200,
                }
                for _ in range(150)
            ],
        }
        params = {
            "collectionId": identity,
            "kind": "diagnostics",
            "pageSize": 100,
            "includeTotal": True,
        }
        first = self.success("query.read", params)
        self.assertLess(len(first["items"]), 100)
        self.assertLessEqual(len(dumps(first)), 262144)
        self.assertEqual(first["matchedCount"], 150)
        indices = [item["index"] for item in first["items"]]
        page = first
        while page["nextCursor"]:
            following = {**params, "cursor": page["nextCursor"]}
            page = self.success("query.read", following)
            self.assertEqual(self.success("query.read", following), page)
            self.assertLessEqual(len(dumps(page)), 262144)
            indices.extend(item["index"] for item in page["items"])
        self.assertEqual(indices, list(range(150)))
        with self.workspace.transaction() as connection:
            self.assertLess(
                connection.execute(
                    "SELECT SUM(length(source)+length(extra)) FROM query_snapshot"
                ).fetchone()[0],
                1000,
            )

    def test_single_oversized_page_item_rolls_back_initial_snapshot(self):
        def project(page):
            page.pop("extra")
            return page

        with self.assertRaises(DeskWeaveError) as caught:
            self.engine.pages.read(
                self.editor.id,
                1,
                "oversized",
                1,
                factory=lambda _c: ({}, {}),
                reader=lambda *_args: iter([(1, {"a": "x" * 200000, "b": "x" * 200000})]),
                project=project,
            )
        self.assertEqual(caught.exception.code, "QUERY_ITEM_TOO_LARGE")
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM query_snapshot").fetchone()[0], 0
            )

    def test_cursor_capacity_release_and_query_timeout_leave_no_partial_snapshot(self):
        self.seed_versions(20)
        with patch("deskweave.engine.pagination.MAX_CURSOR_POSITIONS", 2):
            first = self.success("workspace.catalog", {"kind": "workflow", "pageSize": 1})
            second = self.success(
                "workspace.catalog",
                {"kind": "workflow", "pageSize": 1, "cursor": first["nextCursor"]},
            )
            response = self.request("workspace.catalog", {"kind": "workflow", "pageSize": 1})
            self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")
            self.assertEqual(
                self.success(
                    "workspace.catalog",
                    {"kind": "workflow", "pageSize": 1, "cursor": first["nextCursor"]},
                ),
                second,
            )
            with self.workspace.transaction() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM query_snapshot").fetchone()[0], 1
                )
            self.success("query.close", {"snapshotId": first["snapshotId"]})
            self.success("workspace.catalog", {"kind": "workflow", "pageSize": 1})
        self.engine.close_session(self.editor.id)
        self.editor = self.engine.open_session("studio", {"workflow.read"}, local=True)
        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        original = self.workspace.catalog_rows

        def delayed(*args):
            clock[0] = 2.0
            yield from original(*args)

        with patch.object(self.workspace, "catalog_rows", side_effect=delayed):
            response = self.request("workspace.catalog", {"kind": "workflow", "pageSize": 1})
        self.assertEqual(response["error"]["code"], "DEADLINE_EXCEEDED")
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM query_snapshot").fetchone()[0], 0
            )

    def prepare_version_one_database(self):
        first = self.save(workflow(name="Cafe\u0301"), request_id="first-save")
        second = self.save(workflow(name="Second"), first["revision"])
        self.engine.close()
        with closing(sqlite3.connect(self.directory / "data/workspace.db")) as connection:
            connection.executescript("""
                DROP INDEX revision_catalog_name_asc;
                DROP INDEX revision_catalog_name_desc;
                ALTER TABLE revision DROP COLUMN name_key;
                ALTER TABLE revision DROP COLUMN valid_until;
                DROP TABLE query_cursor;
                DROP TABLE query_snapshot;
                CREATE TABLE query_snapshot (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    authorization_version INTEGER NOT NULL,
                    binding TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL NOT NULL, extra TEXT NOT NULL);
                CREATE TABLE query_member (
                    snapshot_id TEXT NOT NULL REFERENCES query_snapshot(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, ordinal));
                CREATE TABLE query_cursor (
                    token TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL
                    REFERENCES query_snapshot(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, UNIQUE(snapshot_id, ordinal));
                PRAGMA user_version=1;
            """)
        return first, second

    def test_existing_workspace_preserves_versions_and_receipts(self):
        from deskweave.adapters.workspace import Workspace
        from deskweave.engine.service import Engine

        first, second = self.prepare_version_one_database()
        self.workspace = Workspace(self.directory)
        self.engine = Engine(self.workspace, clock=lambda: self.now[0])
        self.editor = self.engine.open_session(
            "studio", {"workflow.read", "workflow.write"}, local=True
        )
        old = self.success("workflow.read", {"workflowId": "hello", "revision": first["revision"]})
        self.assertEqual(old["definition"]["name"], "Cafe\u0301")
        self.assertEqual(
            self.success("request.status", {"requestId": "first-save"})["result"], first
        )
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            rows = connection.execute(
                "SELECT sequence,name_key,valid_until FROM revision ORDER BY sequence"
            ).fetchall()
            self.assertEqual(tuple(rows[0]), (1, "café", 2))
            self.assertEqual(tuple(rows[1]), (2, "second", None))
        self.assertEqual(
            self.success("workspace.catalog", {"kind": "workflow", "order": "nameAsc"})["items"][0][
                "version"
            ],
            second["revision"],
        )
        self.save(workflow(name="Third"), second["revision"])

    def test_storage_index_setup_failure_rolls_back_and_can_retry(self):
        from deskweave.adapters.workspace import Workspace

        first, _second = self.prepare_version_one_database()
        with patch(
            "deskweave.adapters.workspace.unicodedata.normalize",
            side_effect=ValueError("index setup failed"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                Workspace(self.directory)
        with closing(sqlite3.connect(self.directory / "data/workspace.db")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn(
                "name_key", {row[1] for row in connection.execute("PRAGMA table_info(revision)")}
            )
        self.workspace = Workspace(self.directory)
        self.assertEqual(
            self.workspace.read("hello", first["revision"])["definition"]["name"], "Cafe\u0301"
        )

    def test_preview_budget_counts_json_escape_bytes(self):
        for length, allowed in ((130000, True), (140000, False)):
            response = self.request("expression.preview", {"value": "\n" * length})
            self.assertEqual(response["ok"], allowed)
            if not allowed:
                self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")

    def test_integral_json_number_is_a_valid_array_index(self):
        value = self.success(
            "expression.preview",
            {"value": path("vars", "rows", 1.0), "context": {"vars": {"rows": ["a", "b"]}}},
        )
        self.assertEqual(value["value"], "b")

    def test_integral_json_page_sizes_work_across_queries(self):
        saved = self.save()
        checked = self.success("workflow.validate", {"definition": workflow()})
        for method, params in (
            ("workspace.catalog", {"kind": "workflow"}),
            ("workflow.revisions", {"workflowId": "hello"}),
            (
                "query.read",
                {
                    "collectionId": checked["diagnosticsQuery"]["collectionId"],
                    "kind": "diagnostics",
                },
            ),
        ):
            with self.subTest(method=method):
                page = self.success(method, {**params, "pageSize": 1.0})
                self.assertLessEqual(len(page["items"]), 1)
                for invalid in (True, 1.5, "1"):
                    response = self.request(method, {**params, "pageSize": invalid})
                    self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")
        self.assertTrue(saved["revision"])

    def test_structural_diagnostics_keep_node_filter_and_pagination(self):
        definition = workflow()
        del definition["name"]
        definition["nodes"][0].update(action="time.wait", params={"unexpected": 1})
        definition["nodes"][1]["action"] = "unknown"
        result = self.success("workflow.validate", {"definition": definition})
        identity = result["diagnosticsQuery"]["collectionId"]
        base = {"collectionId": identity, "kind": "diagnostics", "includeTotal": True}
        all_rows = self.success("query.read", base)
        self.assertEqual(result["diagnosticCount"], 4)
        self.assertEqual(all_rows["matchedCount"], 4)
        self.assertNotIn(
            "nodeId",
            next(
                item["value"] for item in all_rows["items"] if item["value"]["fieldPath"] == "/name"
            ),
        )
        expected = [
            item for item in all_rows["items"] if item["value"]["fieldPath"].startswith("/nodes/0/")
        ]
        for order in ("asc", "desc"):
            params = {
                **base,
                "pageSize": 1,
                "order": order,
                "filter": {"nodeId": "assign", "codes": ["WORKFLOW_SCHEMA"], "text": "字段"},
            }
            first = self.success("query.read", params)
            self.assertEqual(first["matchedCount"], 2)
            self.assertEqual(first["countState"], "exact")
            following = {**params, "cursor": first["nextCursor"]}
            second = self.success("query.read", following)
            self.assertEqual(second, self.success("query.read", following))
            self.assertEqual(second["matchedCount"], 2)
            self.assertIsNone(second["nextCursor"])
            items = first["items"] + second["items"]
            self.assertEqual(items, expected if order == "asc" else expected[::-1])
            self.assertEqual({item["value"]["nodeId"] for item in items}, {"assign"})
            self.success("query.close", {"snapshotId": first["querySnapshot"]["snapshotId"]})
        other = self.success("query.read", {**base, "filter": {"nodeId": "wait"}})
        self.assertEqual(other["matchedCount"], 1)
        self.assertEqual(other["items"][0]["value"]["fieldPath"], "/nodes/1/action")
        empty = self.success(
            "query.read", {**base, "filter": {"nodeId": "assign", "codes": ["ENTRY_NOT_FOUND"]}}
        )
        self.assertEqual(empty["matchedCount"], 0)
        self.assertEqual(empty["items"], [])
        self.assertIsNone(empty["nextCursor"])

    def test_structural_diagnostics_skip_invalid_or_missing_node_ids(self):
        for identity in (None, True, 12, [], {}, "", "0bad", "with space", "x" * 65):
            with self.subTest(identity=identity):
                definition = workflow()
                definition["nodes"][0].update(action="time.wait", params={})
                if identity is None:
                    del definition["nodes"][0]["id"]
                else:
                    definition["nodes"][0]["id"] = identity
                result = self.success("workflow.validate", {"definition": definition})
                collection_id = result["diagnosticsQuery"]["collectionId"]
                params = {
                    "collectionId": collection_id,
                    "kind": "diagnostics",
                    "includeTotal": True,
                }
                rows = self.success("query.read", params)
                self.assertIn(
                    "/nodes/0/params/durationMs",
                    {item["value"]["fieldPath"] for item in rows["items"]},
                )
                self.assertTrue(all("nodeId" not in item["value"] for item in rows["items"]))
                filtered = self.success("query.read", {**params, "filter": {"nodeId": "assign"}})
                self.assertEqual(filtered["matchedCount"], 0)
                self.success("query.close", {"collectionId": collection_id})

    def test_duplicate_node_ids_do_not_claim_diagnostics(self):
        for structural in (True, False):
            definition = workflow()
            definition["entry"] = "shared"
            definition["nodes"] = [
                {
                    "id": "shared",
                    "title": "等待",
                    "action": "time.wait",
                    "params": {} if structural else {"durationMs": 0},
                    "on": {"next": "missing"},
                }
                for _ in range(2)
            ]
            result = self.success("workflow.validate", {"definition": definition})
            identity = result["diagnosticsQuery"]["collectionId"]
            params = {"collectionId": identity, "kind": "diagnostics", "includeTotal": True}
            page = self.success("query.read", params)
            self.assertTrue(page["items"])
            self.assertTrue(all("nodeId" not in item["value"] for item in page["items"]))
            self.assertEqual(
                self.success("query.read", {**params, "filter": {"nodeId": "shared"}})[
                    "matchedCount"
                ],
                0,
            )
            self.success("query.close", {"collectionId": identity})

    def test_structural_diagnostics_use_outer_node_identity(self):
        definition = workflow()
        node_id = "A" + "x" * 63
        definition["nodes"][0].update(
            id=node_id,
            action="time.wait",
            params={"durationMs": {"$literal": "bad"}, "nested": {"nodes": [{"id": "impostor"}]}},
        )
        definition["nodes"][1].update(id="n", action="unknown")
        result = self.success("workflow.validate", {"definition": definition})
        identity = result["diagnosticsQuery"]["collectionId"]
        params = {"collectionId": identity, "kind": "diagnostics", "includeTotal": True}
        outer = self.success("query.read", {**params, "filter": {"nodeId": node_id}})
        self.assertEqual(outer["matchedCount"], 2)
        self.assertEqual(
            {item["value"]["fieldPath"] for item in outer["items"]},
            {"/nodes/0/params/durationMs/$literal", "/nodes/0/params/nested"},
        )
        self.assertEqual(
            self.success("query.read", {**params, "filter": {"nodeId": "n"}})["matchedCount"], 1
        )
        self.assertEqual(
            self.success("query.read", {**params, "filter": {"nodeId": "impostor"}})[
                "matchedCount"
            ],
            0,
        )
