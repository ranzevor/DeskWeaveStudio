import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from deskweave.adapters.workspace import Workspace
from deskweave.contracts import validate
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError

ROOT = Path(__file__).resolve().parents[2]


def workflow(identity="hello", name="问候"):
    value = json.loads((ROOT / "examples/workflows/hello.json").read_text(encoding="utf-8"))
    value.update(id=identity, name=name)
    return value


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "workspace"
        self.workspace = Workspace(self.directory, create=True)
        self.now = [1_800_000_000.0]
        self.engine = Engine(self.workspace, clock=lambda: self.now[0])
        self.editor = self.engine.open_session(
            "studio", {"workflow.read", "workflow.write"}, local=True
        )
        self.remote = self.engine.open_session("bridge-client", {"workflow.read", "workflow.write"})

    def tearDown(self):
        self.workspace.close()
        self.temporary.cleanup()

    def request(self, method, params, *, session=None, request_id=None):
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
                "deadlineMs": 30000,
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
        self.engine = Engine(self.workspace, clock=lambda: self.now[0])
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
