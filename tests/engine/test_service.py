import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from deskweave.adapters.framing import FrameDecoder, encode_frame
from deskweave.adapters.workspace import Workspace
from deskweave.contracts import validate
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, dumps

ROOT = Path(__file__).resolve().parents[2]


def workflow(identity="hello", name="问候"):
    value = json.loads((ROOT / "examples/workflows/hello.json").read_text(encoding="utf-8"))
    value.update(id=identity, name=name)
    return value


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "workspace"
        self.workspace = Workspace(self.directory, create=True)
        self.now = [1_800_000_000.0]
        self.elapsed = [0.0]
        self.engine = self.make_engine()
        self.editor = self.engine.open_session(
            "studio", {"workflow.read", "workflow.write"}, local=True
        )
        self.remote = self.engine.open_session("bridge-client", {"workflow.read", "workflow.write"})

    def make_engine(self):
        return Engine(self.workspace, clock=lambda: self.now[0], monotonic=lambda: self.elapsed[0])

    def tearDown(self):
        self.workspace.close()
        self.temporary.cleanup()

    def request(self, method, params, *, session=None, request_id=None, deadline_ms=30000):
        session = session or self.editor
        return self.engine.dispatch(
            session.id,
            {
                "protocolVersion": "1.0",
                "kind": "request",
                "id": request_id or uuid.uuid4().hex,
                "session": session.id,
                "workspace": self.workspace.id,
                "method": method,
                "deadlineMs": deadline_ms,
                "params": params,
            },
        )

    def success(self, method, params, **kwargs):
        result = self.request(method, params, **kwargs)
        self.assertTrue(result["ok"], result)
        validate("urn:deskweave:message:1", result)
        return result["result"]

    def save(self, definition=None, base=None, **kwargs):
        definition = definition or workflow()
        params = {
            "workflowId": definition["id"],
            "draftVersion": 1,
            "baseRevision": base,
            "definition": definition,
            "dependencies": {},
        }
        return self.success("workflow.save", params, **kwargs)


class ServiceTests(EngineCase):
    def test_cross_client_save_and_immutable_versions(self):
        first = self.save()
        returned = self.success(
            "workflow.read",
            {"workflowId": "hello", "revision": first["revision"]},
            session=self.remote,
        )
        self.assertEqual(returned["definition"]["name"], "问候")
        second = self.save(workflow(name="已编辑"), first["revision"], session=self.remote)
        old = self.success("workflow.read", {"workflowId": "hello", "revision": first["revision"]})
        new = self.success("workflow.read", {"workflowId": "hello", "revision": second["revision"]})
        self.assertEqual(old["definition"]["name"], "问候")
        self.assertEqual(new["definition"]["name"], "已编辑")
        self.assertNotEqual(first["definitionHash"], second["definitionHash"])

    def test_idempotency_conflict_and_restart(self):
        request_id = "save-one"
        first = self.save(request_id=request_id)
        self.assertEqual(self.save(request_id=request_id), first)
        params = {
            "workflowId": "hello",
            "draftVersion": 2,
            "baseRevision": first["revision"],
            "definition": workflow(),
            "dependencies": {},
        }
        response = self.request("workflow.save", params, request_id=request_id)
        self.assertEqual(response["error"]["code"], "REQUEST_CONFLICT")
        self.workspace.close()
        self.workspace = Workspace(self.directory)
        self.engine = self.make_engine()
        self.editor = self.engine.open_session(
            "studio", {"workflow.read", "workflow.write"}, local=True
        )
        self.assertEqual(self.save(request_id=request_id), first)
        status = self.success("request.status", {"requestId": request_id})
        self.assertEqual((status["state"], status["result"]), ("finished", first))
        self.assertEqual(
            self.success("workflow.revisions", {"workflowId": "hello"})["items"][0]["revision"],
            first["revision"],
        )

    def test_resource_bound_copy_is_rejected_without_writing(self):
        baseline = self.save()
        model = json.loads(
            (ROOT / "examples/authoring/workflow-model-dependencies.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(model["dependencies"]["checkDownload"]["revision"], "7")
        self.assertEqual(model["dependencies"]["downloadTemplate"]["assetVersion"], "3")
        response = self.request(
            "workflow.save",
            {
                "workflowId": model["definition"]["id"],
                "draftVersion": 1,
                "baseRevision": None,
                **model,
            },
            request_id="resource-bound-copy",
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "METHOD_UNAVAILABLE")
        self.assertEqual(
            self.success("request.status", {"requestId": "resource-bound-copy"})["state"],
            "notReceived",
        )
        catalog = self.success("workspace.catalog", {"kind": "workflow"})
        self.assertEqual([item["id"] for item in catalog["items"]], ["hello"])
        saved = self.success(
            "workflow.read", {"workflowId": "hello", "revision": baseline["revision"]}
        )
        self.assertEqual(saved["definition"], workflow())
        self.assertEqual(saved["dependencies"], {})

    def test_concurrent_compare_and_save(self):
        first = self.save()

        def update(index):
            return self.request(
                "workflow.save",
                {
                    "workflowId": "hello",
                    "draftVersion": index,
                    "baseRevision": first["revision"],
                    "definition": workflow(name=f"修改 {index}"),
                    "dependencies": {},
                },
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, (2, 3)))
        self.assertEqual(sum(row["ok"] for row in results), 1)
        self.assertEqual(
            [row["error"]["code"] for row in results if not row["ok"]], ["REVISION_CONFLICT"]
        )
        self.assertEqual(
            len(self.success("workflow.revisions", {"workflowId": "hello"})["items"]), 2
        )

    def test_authorization_and_session_boundaries(self):
        first = self.save()
        read_only = self.engine.open_session("reader", {"workflow.read"}, workflow_ids={"other"})
        denied = self.request(
            "workflow.read",
            {"workflowId": "hello", "revision": first["revision"]},
            session=read_only,
        )
        self.assertEqual(denied["error"]["code"], "ACCESS_DENIED")
        self.assertEqual(
            self.success("workspace.catalog", {"kind": "workflow"}, session=read_only)["items"], []
        )
        denied = self.request("expression.preview", {"value": 1}, session=self.remote)
        self.assertEqual(denied["error"]["code"], "ACCESS_DENIED")
        self.assertEqual(self.success("expression.preview", {"value": False})["value"], False)
        self.engine.close_session(self.editor.id)
        denied = self.request("expression.preview", {"value": 1})
        self.assertEqual(denied["error"]["code"], "ACCESS_DENIED")

    def test_recovered_request_checks_current_permission(self):
        self.save(request_id="saved")
        restricted = self.engine.open_session("studio", {"workflow.read"}, local=True)
        response = self.request("request.status", {"requestId": "saved"}, session=restricted)
        self.assertEqual(response["error"]["code"], "ACCESS_DENIED")
        unseen = self.success("request.status", {"requestId": "saved"}, session=self.remote)
        self.assertEqual(unseen["state"], "notReceived")

    def test_catalog_snapshot_filter_order_and_cursor(self):
        first = self.save(workflow("a", "Alpha"))
        self.save(workflow("b", "Alpine"))
        self.save(workflow("c", "Beta"))
        params = {"kind": "workflow", "nameContains": "AL", "order": "nameAsc", "pageSize": 1}
        page = self.success("workspace.catalog", params)
        self.assertEqual([row["name"] for row in page["items"]], ["Alpha"])
        self.save(workflow("a", "Renamed"), first["revision"])
        self.save(workflow("d", "Albatross"))
        next_params = {**params, "cursor": page["nextCursor"]}
        following = self.success("workspace.catalog", next_params)
        self.assertEqual([row["name"] for row in following["items"]], ["Alpine"])
        self.assertEqual(self.success("workspace.catalog", next_params), following)
        self.assertEqual(following["snapshotId"], page["snapshotId"])
        self.assertIsNone(following["nextCursor"])
        for changed, session in [
            ({"pageSize": 2}, self.editor),
            ({}, self.remote),
            ({"nameContains": "Beta"}, self.editor),
        ]:
            result = self.request("workspace.catalog", {**next_params, **changed}, session=session)
            self.assertEqual(result["error"]["code"], "CURSOR_EXPIRED")
        self.success("query.close", {"snapshotId": page["snapshotId"]}, session=self.remote)
        self.assertEqual(self.success("workspace.catalog", next_params), following)
        self.success("query.close", {"snapshotId": page["snapshotId"]})
        self.success("query.close", {"snapshotId": page["snapshotId"]})
        self.assertEqual(
            self.request("workspace.catalog", next_params)["error"]["code"], "CURSOR_EXPIRED"
        )

    def test_revision_snapshot_and_expiry(self):
        first = self.save()
        second = self.save(workflow(name="第二版"), first["revision"])
        page = self.success("workflow.revisions", {"workflowId": "hello", "pageSize": 1})
        self.save(workflow(name="第三版"), second["revision"])
        params = {"workflowId": "hello", "pageSize": 1, "cursor": page["nextCursor"]}
        next_page = self.success("workflow.revisions", params)
        self.assertEqual(next_page["headRevision"], second["revision"])
        self.assertEqual(next_page["items"][0]["revision"], first["revision"])
        self.now[0] += 300
        self.assertEqual(
            self.request("workflow.revisions", params)["error"]["code"], "CURSOR_EXPIRED"
        )

    def test_diagnostics_query_keeps_source_indices(self):
        definition = workflow()
        definition["nodes"][0]["on"]["next"] = "missing"
        result = self.success("workflow.validate", {"definition": definition})
        self.assertFalse(result["valid"])
        reference = result["diagnosticsQuery"]
        params = {
            "collectionId": reference["collectionId"],
            "kind": "diagnostics",
            "pageSize": 1,
            "filter": {"codes": ["UNREACHABLE_NODE"]},
            "includeTotal": True,
            "order": "desc",
        }
        first = self.success("query.read", params)
        self.assertEqual(first["matchedCount"], 3)
        self.assertEqual(first["countState"], "exact")
        second = self.success("query.read", {**params, "cursor": first["nextCursor"]})
        self.assertGreater(first["items"][0]["index"], second["items"][0]["index"])
        self.assertEqual(first["querySnapshot"]["expiresAt"], reference["expiresAt"])
        denied = self.request("query.read", params, session=self.remote)
        self.assertEqual(denied["error"]["code"], "COLLECTION_NOT_FOUND")
        invalid = self.request("query.read", {**params, "filter": {"unexpected": True}})
        self.assertEqual(invalid["error"]["code"], "INVALID_ARGUMENT")

    def test_snapshot_capacity_and_release(self):
        for _ in range(8):
            self.success("workspace.catalog", {"kind": "workflow"})
        limited = self.request("workspace.catalog", {"kind": "workflow"})
        self.assertEqual(limited["error"]["code"], "LIMIT_EXCEEDED")
        self.now[0] += 301
        self.assertEqual(self.success("workspace.catalog", {"kind": "workflow"})["items"], [])

    def test_failed_write_preserves_previous_version(self):
        first = self.save()
        params = {
            "workflowId": "hello",
            "draftVersion": 2,
            "baseRevision": first["revision"],
            "definition": workflow(name="新内容"),
            "dependencies": {},
        }
        with patch.object(self.workspace, "_atomic_write", side_effect=OSError("disk full")):
            result = self.request("workflow.save", params, request_id="retry-save")
        self.assertEqual(result["error"]["code"], "STORAGE_UNAVAILABLE")
        self.assertEqual(
            self.success("request.status", {"requestId": "retry-save"})["state"], "notReceived"
        )
        history = self.success("workflow.revisions", {"workflowId": "hello"})
        self.assertEqual(history["headRevision"], first["revision"])
        self.success("workflow.save", params, request_id="retry-save")

    def test_missing_or_corrupted_objects_fail(self):
        first = self.save()
        content = self.directory / "objects/sha256" / first["definitionHash"]
        original = content.read_bytes()
        content.write_bytes(b"{}")
        params = {"workflowId": "hello", "revision": first["revision"]}
        self.assertEqual(self.request("workflow.read", params)["error"]["code"], "STORAGE_CORRUPT")
        content.write_bytes(original)
        self.success("workflow.read", params)
        content.unlink()
        self.assertEqual(self.request("workflow.read", params)["error"]["code"], "STORAGE_CORRUPT")

    def test_single_writer_across_processes(self):
        command = """from pathlib import Path
from deskweave.adapters.framing import FrameDecoder, encode_frame
from deskweave.adapters.workspace import Workspace
from deskweave.errors import DeskWeaveError
import sys
try:
    Workspace(Path(sys.argv[1]))
except DeskWeaveError as error:
    assert error.code == 'WORKSPACE_BUSY'
else:
    raise AssertionError('second writer opened')
"""
        completed = subprocess.run(
            [sys.executable, "-c", command, str(self.directory)], capture_output=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))

    def test_schema_rejection_and_duplicate_registration(self):
        for params in [
            {"value": 1, "context": {"other": {}}},
            {"value": 1, "expectedType": "integer"},
        ]:
            self.assertEqual(
                self.request("expression.preview", params)["error"]["code"], "INVALID_ARGUMENT"
            )
        with self.assertRaises(ValueError):
            self.engine.register("workflow.read", lambda *_: {}, set())
        with self.assertRaises(ValueError):
            self.engine.register("invented.method", lambda *_: {}, set())

    def test_invalid_flow_is_not_saved(self):
        definition = workflow()
        definition["nodes"][0]["on"]["next"] = definition["nodes"][0]["id"]
        params = {
            "workflowId": "hello",
            "draftVersion": 1,
            "baseRevision": None,
            "definition": definition,
            "dependencies": {},
        }
        self.assertEqual(self.request("workflow.save", params)["error"]["code"], "INVALID_WORKFLOW")
        self.assertEqual(self.success("workspace.catalog", {"kind": "workflow"})["items"], [])

    def test_deadline_before_commit_keeps_existing_version(self):
        first = self.save()
        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        atomic_write = self.workspace._atomic_write

        def delayed_write(path, data):
            atomic_write(path, data)
            clock[0] = 31.0

        params = {
            "workflowId": "hello",
            "draftVersion": 2,
            "baseRevision": first["revision"],
            "definition": workflow(name="超时保存"),
            "dependencies": {},
        }
        with patch.object(self.workspace, "_atomic_write", side_effect=delayed_write):
            result = self.request("workflow.save", params, request_id="expired-save")
        self.assertEqual(result["error"]["code"], "DEADLINE_EXCEEDED")
        self.assertEqual(
            self.success("request.status", {"requestId": "expired-save"})["state"], "notReceived"
        )
        self.assertEqual(
            self.success("workflow.revisions", {"workflowId": "hello"})["headRevision"],
            first["revision"],
        )

    def test_client_revocation_closes_all_sessions(self):
        second = self.engine.open_session("studio", {"workflow.read"}, local=True)
        self.success("workspace.catalog", {"kind": "workflow"}, session=second)
        self.engine.revoke_client("studio")
        for session in (self.editor, second):
            result = self.request("workspace.catalog", {"kind": "workflow"}, session=session)
            self.assertEqual(result["error"]["code"], "ACCESS_DENIED")
        self.success("workspace.catalog", {"kind": "workflow"}, session=self.remote)

    def test_engine_close_releases_workspace(self):
        self.engine.close()
        self.engine.close()
        with self.assertRaises(DeskWeaveError):
            self.engine.open_session("new", {"workflow.read"})
        result = self.request("expression.preview", {"value": 1})
        self.assertFalse(result["ok"])
        with Workspace(self.directory) as reopened:
            self.assertEqual(reopened.id, self.workspace.id)

    def test_unsupported_methods_and_server_results(self):
        response = self.request("record.start", {})
        self.assertEqual(response["error"]["code"], "METHOD_UNAVAILABLE")
        from deskweave.engine.service import Method

        self.engine._methods["expression.preview"] = Method(
            lambda *_: {"wrong": 1}, frozenset(), {}
        )
        response = self.request("expression.preview", {"value": 1})
        self.assertEqual(response["error"]["code"], "INTERNAL_ERROR")

    def test_catalog_unicode_and_literal_search(self):
        self.save(workflow("cafe", "Cafe\u0301"))
        result = self.success("workspace.catalog", {"kind": "workflow", "nameContains": "CAFÉ"})
        self.assertEqual([row["id"] for row in result["items"]], ["cafe"])
        self.assertEqual(
            self.success("workspace.catalog", {"kind": "workflow", "nameContains": "%_\"'"})[
                "items"
            ],
            [],
        )

    def test_initialization_failure_can_retry(self):
        directory = Path(self.temporary.name) / "invalid-name"
        with self.assertRaises(DeskWeaveError):
            Workspace(directory, create=True, name="")
        with Workspace(directory, create=True, name="新工作空间") as workspace:
            self.assertTrue(workspace.id)

    def test_workspace_metadata_symlink_rejected(self):
        directory = Path(self.temporary.name) / "linked"
        directory.mkdir()
        try:
            (directory / "workspace.json").symlink_to(self.directory / "workspace.json")
        except (OSError, NotImplementedError) as error:
            self.skipTest(str(error))
        with self.assertRaises(DeskWeaveError) as raised:
            Workspace(directory)
        self.assertEqual(raised.exception.code, "RESOURCE_DENIED")

    def test_oversized_object_rejected_before_read(self):
        first = self.save()
        path = self.directory / "objects/sha256" / first["definitionHash"]
        with path.open("r+b") as stream:
            stream.truncate(33554433)
        response = self.request(
            "workflow.read", {"workflowId": "hello", "revision": first["revision"]}
        )
        self.assertEqual(response["error"]["code"], "STORAGE_CORRUPT")

    def test_computation_deadline_returns_protocol_error(self):
        from deskweave.engine import service

        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        for method, name, params in (
            ("expression.preview", "evaluate", {"value": 1}),
            ("workflow.validate", "validate_workflow", {"definition": workflow()}),
        ):
            with self.subTest(method=method):
                clock[0] = 0.0
                original = getattr(service, name)

                def delayed(*args, original=original, **kwargs):
                    result = original(*args, **kwargs)
                    clock[0] = 31.0
                    return result

                with patch.object(service, name, side_effect=delayed):
                    response = self.request(method, params)
                self.assertFalse(response["ok"], response)
                self.assertEqual(response["error"]["code"], "DEADLINE_EXCEEDED")
                self.assertNotIn("result", response)
                validate("urn:deskweave:message:1", response)

    def test_repeated_validation_keeps_latest_results(self):
        for invalid in (False, True):
            for index in range(40):
                definition = workflow(name=f"编辑 {index}")
                if invalid:
                    definition["entry"] = "missing"
                result = self.success("workflow.validate", {"definition": definition})
                self.assertEqual(result["valid"], not invalid)
            page = self.success(
                "query.read",
                {"collectionId": result["diagnosticsQuery"]["collectionId"], "kind": "diagnostics"},
            )
            self.assertEqual(bool(page["items"]), invalid)
            self.success("query.close", {"snapshotId": page["querySnapshot"]["snapshotId"]})

    def test_invalid_definition_returns_multiple_field_diagnostics(self):
        definition = workflow()
        del definition["name"]
        del definition["entry"]
        definition["variables"] = []
        result = self.success("workflow.validate", {"definition": definition})
        self.assertFalse(result["valid"])
        page = self.success(
            "query.read",
            {"collectionId": result["diagnosticsQuery"]["collectionId"], "kind": "diagnostics"},
        )
        diagnostics = [item["value"] for item in page["items"]]
        self.assertEqual({item["code"] for item in diagnostics}, {"WORKFLOW_SCHEMA"})
        self.assertEqual(
            {item["fieldPath"] for item in diagnostics}, {"/name", "/entry", "/variables"}
        )
        self.assertEqual(result["diagnosticCount"], 3)

    def test_full_response_frame_boundary(self):
        request_id = "响应" * 64
        base = None
        for size in (MAX_FRAME - 1, MAX_FRAME, MAX_FRAME + 1):
            with self.subTest(size=size):
                definition = workflow()
                definition["variables"] = {f"part{i}": "x" * 250000 for i in range(4)}
                definition["variables"]["tail"] = ""
                result = {
                    "workflowId": "hello",
                    "revision": "r" * 32,
                    "definition": definition,
                    "dependencies": {},
                    "definitionHash": "0" * 64,
                }
                envelope = {
                    "protocolVersion": "1.0",
                    "kind": "response",
                    "replyTo": request_id,
                    "ok": True,
                    "result": result,
                }
                definition["variables"]["tail"] = "x" * (size - len(dumps(envelope)))
                self.assertLess(len(dumps(result)), MAX_FRAME)
                saved = self.workspace.save(
                    "studio",
                    uuid.uuid4().hex,
                    {
                        "workflowId": "hello",
                        "draftVersion": 1,
                        "baseRevision": base,
                        "definition": definition,
                        "dependencies": {},
                    },
                )
                base = saved["revision"]
                response = self.request(
                    "workflow.read",
                    {"workflowId": "hello", "revision": base},
                    request_id=request_id,
                )
                validate("urn:deskweave:message:1", response)
                self.assertEqual(FrameDecoder().feed(encode_frame(response)), [response])
                if size <= MAX_FRAME:
                    self.assertTrue(response["ok"], response.get("error"))
                    self.assertEqual(len(dumps(response)), size)
                else:
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")
                    self.assertNotIn("result", response)

    def test_expression_checkpoints_stop_remaining_operations(self):
        from deskweave.domain import values
        from tests.engine.test_values import expr

        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        add = values.operator.add
        for deadline_ms, elapsed in ((5, 0.005), (30000, 1.0)):
            clock[0] = 0.0

            def delayed_add(left, right, elapsed=elapsed):
                clock[0] = elapsed
                return add(left, right)

            with patch.object(values.operator, "add", side_effect=delayed_add) as operation:
                response = self.request(
                    "expression.preview",
                    {"value": [expr("add", 1, 2), expr("add", 3, 4)]},
                    deadline_ms=deadline_ms,
                )
            self.assertEqual(response["error"]["code"], "DEADLINE_EXCEEDED")
            self.assertEqual(operation.call_count, 1)
        self.assertEqual(self.success("expression.preview", {"value": 3})["value"], 3)

    def test_workflow_checkpoints_stop_remaining_nodes(self):
        from deskweave.domain import workflow as domain

        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        shape = domain._shape

        def delayed_shape(*args, **kwargs):
            clock[0] = 30.0
            return shape(*args, **kwargs)

        with patch.object(domain, "_shape", side_effect=delayed_shape) as operation:
            response = self.request("workflow.validate", {"definition": workflow()})
        self.assertEqual(response["error"]["code"], "DEADLINE_EXCEEDED")
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(self.engine._collections, {})
        self.assertTrue(self.success("workflow.validate", {"definition": workflow()})["valid"])

    def test_deadline_covers_result_validation_and_serialization(self):
        from deskweave.engine import service

        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        for stage in ("validate", "dumps"):
            with self.subTest(stage=stage):
                clock[0] = 0.0
                original = getattr(service, stage)

                def delayed(*args, original=original, stage=stage, **kwargs):
                    result = original(*args, **kwargs)
                    if stage == "validate" and str(args[0]).endswith("/result"):
                        clock[0] = 1.0
                    if stage == "dumps" and args[0].get("kind") == "response":
                        clock[0] = 1.0
                    return result

                with patch.object(service, stage, side_effect=delayed):
                    response = self.request("expression.preview", {"value": 1})
                self.assertEqual(response["error"]["code"], "DEADLINE_EXCEEDED")
                self.assertNotIn("result", response)
                validate("urn:deskweave:message:1", response)

    def test_timeout_after_commit_can_query_saved_request(self):
        from deskweave.engine import service

        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        encode = service.dumps

        def delayed_response(value, **kwargs):
            result = encode(value, **kwargs)
            if value.get("kind") == "response" and value.get("ok"):
                clock[0] = 30.0
            return result

        with patch.object(service, "dumps", side_effect=delayed_response):
            response = self.request(
                "workflow.save",
                {
                    "workflowId": "hello",
                    "draftVersion": 1,
                    "baseRevision": None,
                    "definition": workflow(),
                    "dependencies": {},
                },
                request_id="committed-before-timeout",
            )
        self.assertEqual(response["error"]["code"], "DEADLINE_EXCEEDED")
        status = self.success("request.status", {"requestId": "committed-before-timeout"})
        self.assertEqual(status["state"], "finished")
        self.assertEqual(
            self.save(request_id="committed-before-timeout"),
            status["result"],
        )

    def test_collection_close_releases_all_pages_and_is_owner_bound(self):
        definition = workflow()
        definition["entry"] = "missing"
        result = self.success("workflow.validate", {"definition": definition})
        params = {
            "collectionId": result["diagnosticsQuery"]["collectionId"],
            "kind": "diagnostics",
            "pageSize": 1,
        }
        page = self.success("query.read", params)
        other = self.success("query.read", {**params, "order": "desc"})
        close = {"collectionId": params["collectionId"]}
        self.success("query.close", close, session=self.remote)
        self.success("query.read", {**params, "cursor": page["nextCursor"]})
        self.success("query.close", {"snapshotId": other["querySnapshot"]["snapshotId"]})
        self.success("query.read", {**params, "cursor": page["nextCursor"]})
        self.success("query.close", close)
        self.success("query.close", close)
        for cursor in (None, page["nextCursor"]):
            response = self.request("query.read", {**params, "cursor": cursor})
            self.assertEqual(response["error"]["code"], "COLLECTION_NOT_FOUND")
        with self.workspace.transaction() as connection:
            for table in ("query_snapshot", "query_cursor"):
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )

    def test_continuous_validation_and_release(self):
        for index in range(40):
            definition = workflow(name=f"校验 {index}")
            definition["entry"] = "missing"
            result = self.success("workflow.validate", {"definition": definition})
            identity = result["diagnosticsQuery"]["collectionId"]
            self.success("query.read", {"collectionId": identity, "kind": "diagnostics"})
            self.success("query.close", {"collectionId": identity})
        self.assertEqual(self.engine._collections, {})
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM query_snapshot").fetchone()[0], 0
            )

    def test_validation_quota_protects_active_queries_and_other_sessions(self):
        definition = workflow()
        definition["entry"] = "missing"
        reference = self.success(
            "workflow.validate", {"definition": definition}, session=self.remote
        )["diagnosticsQuery"]
        params = {"collectionId": reference["collectionId"], "kind": "diagnostics", "pageSize": 1}
        page = self.success("query.read", params, session=self.remote)
        for index in range(40):
            self.success("workflow.validate", {"definition": workflow(name=f"更新 {index}")})
        self.success("query.read", {**params, "cursor": page["nextCursor"]}, session=self.remote)
        references = []
        for _ in range(8):
            reference = self.success("workflow.validate", {"definition": workflow()})[
                "diagnosticsQuery"
            ]
            references.append(reference)
            self.success(
                "query.read", {"collectionId": reference["collectionId"], "kind": "diagnostics"}
            )
        response = self.request("workflow.validate", {"definition": workflow()})
        self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")
        self.success("workflow.validate", {"definition": workflow()}, session=self.remote)
        self.success("query.close", {"collectionId": references[0]["collectionId"]})
        self.success("workflow.validate", {"definition": workflow()})

    def test_global_collection_capacity_and_expiry(self):
        sessions = [
            self.engine.open_session(f"reader-{index}", {"workflow.read"}) for index in range(5)
        ]
        oldest = None
        newest = None
        for session in sessions[:4]:
            for _ in range(8):
                reference = self.success(
                    "workflow.validate", {"definition": workflow()}, session=session
                )["diagnosticsQuery"]
                oldest = oldest or reference
                newest = reference
        self.success("workflow.validate", {"definition": workflow()}, session=sessions[4])
        self.assertEqual(len(self.engine._collections), 32)
        response = self.request(
            "query.read",
            {"collectionId": oldest["collectionId"], "kind": "diagnostics"},
            session=sessions[0],
        )
        self.assertEqual(response["error"]["code"], "COLLECTION_NOT_FOUND")
        params = {"collectionId": newest["collectionId"], "kind": "diagnostics"}
        self.success("query.read", params, session=sessions[3])
        self.now[0] += 300
        self.assertEqual(
            self.request("query.read", params, session=sessions[3])["error"]["code"],
            "CURSOR_EXPIRED",
        )
        self.success("workflow.validate", {"definition": workflow()}, session=sessions[4])
        self.assertEqual(len(self.engine._collections), 1)
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM query_snapshot").fetchone()[0], 0
            )

    def test_validation_handles_malformed_identity_without_losing_authorization(self):
        restricted = self.engine.open_session("limited", {"workflow.read"}, workflow_ids={"hello"})
        for identity in (None, [], {}, 12, "", "x" * 129):
            definition = workflow()
            if identity is None:
                del definition["id"]
            else:
                definition["id"] = identity
            result = self.success(
                "workflow.validate", {"definition": definition}, session=restricted
            )
            self.assertFalse(result["valid"])
            params = {
                "collectionId": result["diagnosticsQuery"]["collectionId"],
                "kind": "diagnostics",
            }
            page = self.success("query.read", params, session=restricted)
            self.assertIn("/id", {item["value"]["fieldPath"] for item in page["items"]})
            self.success(
                "query.close", {"collectionId": params["collectionId"]}, session=restricted
            )
        definition = workflow("other")
        del definition["name"]
        response = self.request("workflow.validate", {"definition": definition}, session=restricted)
        self.assertEqual(response["error"]["code"], "ACCESS_DENIED")

    def test_validation_request_boundaries_remain_strict(self):
        for params in (
            {},
            {"definition": []},
            {"definition": None},
            {"definition": {}, "extra": 1},
            {"definition": {}, "workflowId": "hello", "revision": "rev"},
        ):
            response = self.request("workflow.validate", params)
            self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")
        definition = workflow()
        definition["variables"] = {f"part{i}": "x" * 250000 for i in range(5)}
        response = self.request("workflow.validate", {"definition": definition})
        self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")
        for params in ({}, {"snapshotId": "s", "collectionId": "c"}, {"collectionId": None}):
            response = self.request("query.close", params)
            self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")

    def test_invalid_unicode_request_id_returns_protocol_error(self):
        response = self.request("expression.preview", {"value": 1}, request_id="\ud800")
        self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(response["replyTo"], "invalid")
        self.assertEqual(FrameDecoder().feed(encode_frame(response)), [response])

    def test_all_active_global_collections_require_explicit_release(self):
        sessions = [
            self.engine.open_session(f"active-{index}", {"workflow.read"}) for index in range(5)
        ]
        references = []
        for session in sessions[:4]:
            for _ in range(8):
                reference = self.success(
                    "workflow.validate", {"definition": workflow()}, session=session
                )["diagnosticsQuery"]
                references.append(reference)
                self.success(
                    "query.read",
                    {"collectionId": reference["collectionId"], "kind": "diagnostics"},
                    session=session,
                )
        response = self.request(
            "workflow.validate", {"definition": workflow()}, session=sessions[4]
        )
        self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")
        self.success(
            "query.close", {"collectionId": references[0]["collectionId"]}, session=sessions[0]
        )
        self.success("workflow.validate", {"definition": workflow()}, session=sessions[4])
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM query_snapshot").fetchone()[0], 31
            )
        self.assertEqual(len(self.engine._collections), 32)

    def test_session_end_releases_diagnostic_sources_and_pages(self):
        definition = workflow()
        definition["entry"] = "missing"
        remote = self.success("workflow.validate", {"definition": definition}, session=self.remote)[
            "diagnosticsQuery"
        ]
        params = {"collectionId": remote["collectionId"], "kind": "diagnostics", "pageSize": 1}
        self.success("query.read", params, session=self.remote)
        for close in (
            self.engine.close_session,
            lambda _identity: self.engine.revoke_client("studio"),
        ):
            session = self.engine.open_session("studio", {"workflow.read"})
            reference = self.success(
                "workflow.validate", {"definition": definition}, session=session
            )["diagnosticsQuery"]
            self.success(
                "query.read", {**params, "collectionId": reference["collectionId"]}, session=session
            )
            close(session.id)
            self.assertNotIn(reference["collectionId"], self.engine._collections)
            with self.workspace.transaction() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM query_snapshot WHERE owner=?", (session.id,)
                    ).fetchone()[0],
                    0,
                )
        self.success("query.read", params, session=self.remote)

    def test_oversized_error_details_return_bounded_protocol_error(self):
        from deskweave.engine.service import Method

        def fail(*_args):
            raise DeskWeaveError(
                "INVALID_ARGUMENT",
                "参数错误。",
                details={f"part{index}": "x" * 250000 for index in range(5)},
            )

        self.engine._methods["expression.preview"] = Method(fail, frozenset(), {})
        response = self.request("expression.preview", {"value": 1})
        self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")
        self.assertNotIn("result", response)
        self.assertLess(len(dumps(response)), 1024)
        validate("urn:deskweave:message:1", response)
        self.assertEqual(FrameDecoder().feed(encode_frame(response)), [response])
