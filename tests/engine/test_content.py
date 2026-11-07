import base64
import hashlib
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock, patch

from deskweave.adapters.content import MAX_CHUNK, MAX_CONTENT
from deskweave.adapters.workspace import Workspace
from deskweave.engine.service import Engine
from deskweave.json import digest, dumps, loads
from tests.engine.test_service import EngineCase, workflow


class ContentCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.engine.close_session(self.editor.id)
        self.editor = self.engine.open_session(
            "studio",
            {"workflow.read", "workflow.write", "asset.write"},
            local=True,
            capabilities={"contentRefsV1"},
        )

    def tearDown(self):
        self.engine.close()
        super().tearDown()

    def begin(self, data, purpose="workflowModel", **kwargs):
        return self.success(
            "content.begin",
            {
                "purpose": purpose,
                "mediaType": "application/json",
                "length": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            **kwargs,
        )

    def write(self, identity, offset, block, **kwargs):
        return self.request(
            "content.write",
            {
                "uploadId": identity,
                "offset": offset,
                "chunk": base64.b64encode(block).decode(),
                "chunkSha256": hashlib.sha256(block).hexdigest(),
            },
            **kwargs,
        )

    def upload(self, definition=None, *, data=None, purpose="workflowModel"):
        if data is None:
            data = dumps(
                {"definition": definition or workflow(), "dependencies": {}}, max_bytes=MAX_CONTENT
            )
        started = self.begin(data, purpose)
        identity = started["uploadId"]
        for offset in range(0, len(data), MAX_CHUNK):
            response = self.write(identity, offset, data[offset : offset + MAX_CHUNK])
            self.assertTrue(response["ok"], response)
        return identity, self.success("content.seal", {"uploadId": identity})["reference"]

    @staticmethod
    def save_params(reference, identity="hello", base=None):
        return {
            "workflowId": identity,
            "draftVersion": 1,
            "baseRevision": base,
            "modelRef": reference,
        }

    def read_all(self, descriptor):
        data = bytearray()
        while len(data) < descriptor["length"]:
            page = self.success(
                "resource.read",
                {"resourceId": descriptor["resourceId"], "offset": len(data), "length": MAX_CHUNK},
            )
            self.assertEqual(page["offset"], len(data))
            self.assertEqual(page["totalLength"], descriptor["length"])
            self.assertEqual(page["sha256"], descriptor["sha256"])
            block = base64.b64decode(page["chunk"], validate=True)
            self.assertEqual(len(block), page["length"])
            self.assertTrue(block)
            data.extend(block)
        self.assertEqual(hashlib.sha256(data).hexdigest(), descriptor["sha256"])
        return bytes(data)


class ContentTests(ContentCase):
    def test_large_model_upload_save_resource_read(self):
        definition = workflow()
        definition["variables"]["payload"] = ["x" * 230000 for _ in range(6)]
        raw = dumps({"definition": definition, "dependencies": {}}, max_bytes=MAX_CONTENT)
        self.assertGreater(len(raw), 1048576)
        identity, reference = self.upload(data=raw)
        saved = self.success("workflow.save", self.save_params(reference))
        self.assertEqual(saved["definitionHash"], digest(definition))
        read = self.success("workflow.read", {"workflowId": "hello", "revision": saved["revision"]})
        self.assertNotIn("definition", read)
        self.assertEqual(
            loads(self.read_all(read["modelResource"]), max_bytes=MAX_CONTENT),
            {"definition": definition, "dependencies": {}},
        )
        self.success("content.discard", {"uploadId": identity})
        self.assertEqual(
            loads(self.read_all(read["modelResource"]), max_bytes=MAX_CONTENT)["definition"],
            definition,
        )
        inline = self.request(
            "workflow.read",
            {"workflowId": "hello", "revision": saved["revision"], "contentMode": "inline"},
        )
        self.assertEqual(inline["error"]["code"], "LIMIT_EXCEEDED")
        legacy = self.request(
            "workflow.read",
            {"workflowId": "hello", "revision": saved["revision"]},
            session=self.remote,
        )
        self.assertEqual(legacy["error"]["code"], "LIMIT_EXCEEDED")

    def test_capability_and_purpose_permissions_are_independent(self):
        params = {
            "purpose": "workflowModel",
            "mediaType": "application/json",
            "length": 1,
            "sha256": "0" * 64,
        }
        self.assertEqual(
            self.request("content.begin", params, session=self.remote)["error"]["code"],
            "CAPABILITY_REQUIRED",
        )
        session = self.engine.open_session(
            "viewer", {"workflow.read"}, capabilities={"contentRefsV1"}
        )
        for purpose in ("workflowModel", "trajectoryEdit"):
            result = self.request("content.begin", {**params, "purpose": purpose}, session=session)
            self.assertEqual(result["error"]["code"], "ACCESS_DENIED")
        invalid = self.request("content.begin", {**params, "purpose": "arbitrary"})
        self.assertEqual(invalid["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.engine.contents.uploads, {})

    def test_chunk_replay_requires_original_boundaries_and_bytes(self):
        data = b'{"sample":"012345678901234567890123456789"}'
        identity = self.begin(data)["uploadId"]
        offset = 0
        parts = []
        for length in (1, 8, 9, 1, 12, len(data) - 31):
            block = data[offset : offset + length]
            parts.append((offset, block))
            response = self.write(identity, float(offset), block)
            self.assertTrue(response["ok"], response)
            self.assertFalse(response["result"]["replayed"])
            offset += length
        for offset, block in parts:
            response = self.write(identity, offset, block)
            self.assertTrue(response["result"]["replayed"])
            self.assertEqual(response["result"]["nextOffset"], len(data))
        for offset, block in (
            (0, data[:9]),
            (1, data[1:5]),
            (2, data[2:9]),
            (1, b"x" * 8),
            (len(data) + 1, b"x"),
        ):
            response = self.write(identity, offset, block)
            self.assertEqual(response["error"]["code"], "CONTENT_OFFSET_CONFLICT")

    def test_strict_base64_hash_and_declared_length(self):
        identity = self.begin(b"12")["uploadId"]
        params = {
            "uploadId": identity,
            "offset": 0,
            "chunkSha256": hashlib.sha256(b"f").hexdigest(),
        }
        for chunk in ("Zg==\n", "Zh==", "Zg===", "汉汉汉汉"):
            response = self.request("content.write", {**params, "chunk": chunk})
            self.assertEqual(response["error"]["code"], "INVALID_ARGUMENT")
        response = self.request("content.write", {**params, "chunk": "eA=="})
        self.assertEqual(response["error"]["code"], "CONTENT_HASH_MISMATCH")
        self.assertEqual(
            self.write(identity, 0, b"123")["error"]["code"], "CONTENT_OFFSET_CONFLICT"
        )
        self.assertEqual(self.engine.contents.uploads[identity].received, 0)

    def test_seal_checks_missing_hash_json_and_structure(self):
        identity = self.begin(b"{}")["uploadId"]
        self.assertEqual(
            self.request("content.seal", {"uploadId": identity})["error"]["code"],
            "CONTENT_INCOMPLETE",
        )
        self.assertTrue(self.write(identity, 0, b"[]")["ok"])
        self.assertEqual(
            self.request("content.seal", {"uploadId": identity})["error"]["code"],
            "CONTENT_HASH_MISMATCH",
        )
        for data, code in (
            (b'{"a":1,"a":2}', "INVALID_ARGUMENT"),
            (b'"\xff"', "INVALID_ARGUMENT"),
            (b"{}", "INVALID_CONTENT"),
        ):
            identity = self.begin(data)["uploadId"]
            self.assertTrue(self.write(identity, 0, data)["ok"])
            response = self.request("content.seal", {"uploadId": identity})
            self.assertEqual(response["error"]["code"], code)
            if code == "INVALID_CONTENT":
                self.assertIn("contentPath", response["error"]["details"])
        self.assertEqual(self.engine.contents.resources, {})

    def test_seal_and_reads_do_not_extend_lifetime(self):
        identity, reference = self.upload()
        sealed = self.success("content.seal", {"uploadId": identity})
        self.now[0] += 30
        self.assertEqual(self.success("content.seal", {"uploadId": identity}), sealed)
        self.assertTrue(self.read_all(reference))
        self.now[0] += 30
        self.assertEqual(
            self.request(
                "resource.read", {"resourceId": reference["resourceId"], "offset": 0, "length": 1}
            )["error"]["code"],
            "RESOURCE_NOT_FOUND",
        )
        self.assertEqual(self.engine.contents.uploads, {})

    def test_monotonic_expiry_survives_backward_wall_clock(self):
        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        identity = self.begin(b"{}")["uploadId"]
        self.now[0] -= 600
        clock[0] = 60.0
        self.assertEqual(self.write(identity, 0, b"{}")["error"]["code"], "CONTENT_NOT_FOUND")

    def test_upload_capacity_is_reserved_and_released(self):
        params = {
            "purpose": "workflowModel",
            "mediaType": "application/json",
            "length": MAX_CONTENT,
            "sha256": "0" * 64,
        }
        first = self.success("content.begin", params)
        self.success("content.begin", {**params, "length": float(MAX_CONTENT)})
        self.assertEqual(
            self.request("content.begin", {**params, "length": 1})["error"]["code"],
            "LIMIT_EXCEEDED",
        )
        self.assertEqual(
            self.request("content.begin", {**params, "length": MAX_CONTENT + 1})["error"]["code"],
            "INVALID_ARGUMENT",
        )
        self.success("content.discard", {"uploadId": first["uploadId"]})
        self.success("content.begin", params)
        self.now[0] += 60
        for _ in range(16):
            self.success("content.begin", {**params, "length": 1})
        self.assertEqual(
            self.request("content.begin", {**params, "length": 1})["error"]["code"],
            "LIMIT_EXCEEDED",
        )
        self.assertEqual(len(self.engine.contents.uploads), 16)

    def test_owner_scope_and_current_permission_are_checked(self):
        identity, reference = self.upload()
        other = self.engine.open_session(
            "other", {"workflow.write", "workflow.read"}, capabilities={"contentRefsV1"}
        )
        self.assertEqual(
            self.write(identity, 0, b"{}", session=other)["error"]["code"], "CONTENT_NOT_FOUND"
        )
        self.success("content.discard", {"uploadId": identity}, session=other)
        self.assertIn(identity, self.engine.contents.uploads)
        self.assertEqual(
            self.request(
                "resource.read",
                {"resourceId": reference["resourceId"], "offset": 0, "length": 1},
                session=other,
            )["error"]["code"],
            "RESOURCE_NOT_FOUND",
        )
        self.engine._sessions[self.editor.id] = replace(self.editor, permissions=frozenset())
        self.assertEqual(
            self.request("content.seal", {"uploadId": identity})["error"]["code"], "ACCESS_DENIED"
        )
        self.engine._sessions[self.editor.id] = replace(
            self.editor, workflow_ids=frozenset({"other"})
        )
        self.assertEqual(
            self.request(
                "resource.read", {"resourceId": reference["resourceId"], "offset": 0, "length": 1}
            )["error"]["code"],
            "ACCESS_DENIED",
        )
        self.engine._sessions[self.editor.id] = self.editor

    def test_scope_is_checked_before_sealed_reference_is_published(self):
        definition = workflow(identity="not-granted")
        data = dumps({"definition": definition, "dependencies": {}})
        identity = self.begin(data)["uploadId"]
        self.assertTrue(self.write(identity, 0, data)["ok"])
        self.engine._sessions[self.editor.id] = replace(
            self.editor, workflow_ids=frozenset({"hello"})
        )
        self.assertEqual(
            self.request("content.seal", {"uploadId": identity})["error"]["code"], "ACCESS_DENIED"
        )
        self.assertEqual(self.engine.contents.resources, {})

    def test_reference_identity_purpose_and_workflow_are_checked(self):
        identity, reference = self.upload()
        for field, value in (("length", reference["length"] + 1), ("sha256", "0" * 64)):
            changed = {**reference, field: value}
            self.assertEqual(
                self.request("workflow.save", self.save_params(changed))["error"]["code"],
                "CONTENT_REFERENCE_INVALID",
            )
        self.assertEqual(
            self.request("workflow.save", self.save_params(reference, "other"))["error"]["code"],
            "INVALID_ARGUMENT",
        )
        trajectory = {
            "formatVersion": "1.0",
            "coordinateMode": "physical",
            "events": [{"kind": "pointer.move", "atUs": 0, "x": 1, "y": 1}],
        }
        _, trajectory_ref = self.upload(data=dumps(trajectory), purpose="trajectoryEdit")
        self.assertEqual(
            self.request("workflow.save", self.save_params(trajectory_ref))["error"]["code"],
            "CONTENT_REFERENCE_INVALID",
        )
        item = self.engine.contents.uploads[identity]
        item.stream.seek(0)
        item.stream.write(b"!")
        item.stream.flush()
        self.assertEqual(
            self.request("workflow.save", self.save_params(reference))["error"]["code"],
            "CONTENT_HASH_MISMATCH",
        )

    def test_reference_save_retains_semantic_validation(self):
        definition = workflow()
        definition["nodes"][1]["on"]["next"] = "assign"
        _, reference = self.upload(definition)
        self.assertEqual(
            self.request("workflow.save", self.save_params(reference))["error"]["code"],
            "INVALID_WORKFLOW",
        )
        definition = workflow()
        definition["resources"] = {"image": {"kind": "image"}}
        _, reference = self.upload(definition)
        self.assertEqual(
            self.request("workflow.save", self.save_params(reference))["error"]["code"],
            "METHOD_UNAVAILABLE",
        )

    def test_reference_replay_recovers_conflict_after_missing_receipt(self):
        first = self.save()
        current = self.save(workflow(name="外部更新"), first["revision"], session=self.remote)
        _, reference = self.upload(workflow(name="本地草稿"))
        params = self.save_params(reference, base=first["revision"])
        encoded = dumps(params)
        request_id = "recover-reference-conflict"

        original = self.request("workflow.save", params, request_id=request_id)
        self.assertEqual(original["error"]["code"], "REVISION_CONFLICT")
        status = self.success("request.status", {"requestId": request_id})
        self.assertEqual((status["state"], status["result"]), ("notReceived", None))
        replay = self.request("workflow.save", params, request_id=request_id)
        self.assertEqual(replay["error"], original["error"])
        self.assertEqual(dumps(params), encoded)

        history = self.success("workflow.revisions", {"workflowId": "hello"})
        self.assertEqual(history["headRevision"], current["revision"])
        self.assertEqual(len(history["items"]), 2)
        saved = self.success(
            "workflow.read", {"workflowId": "hello", "revision": current["revision"]}
        )
        self.assertEqual(saved["definition"]["name"], "外部更新")

    def test_reference_replay_recovers_storage_failure_without_duplicate_versions(self):
        original = self.save()
        for identity, base in (("hello", original["revision"]), ("saved-copy", None)):
            with self.subTest(workflow=identity):
                definition = workflow(identity, "恢复保存")
                _, reference = self.upload(definition)
                params = self.save_params(reference, identity, base)
                encoded = dumps(params)
                request_id = "recover-storage-" + identity
                with patch.object(
                    self.workspace, "_atomic_write", side_effect=OSError("disk full")
                ):
                    failed = self.request("workflow.save", params, request_id=request_id)
                    self.assertEqual(failed["error"]["code"], "STORAGE_UNAVAILABLE")
                    status = self.success("request.status", {"requestId": request_id})
                    self.assertEqual((status["state"], status["result"]), ("notReceived", None))
                    repeated = self.request("workflow.save", params, request_id=request_id)
                    self.assertEqual(repeated["error"], failed["error"])
                self.assertEqual(dumps(params), encoded)

                saved = self.success("workflow.save", params, request_id=request_id)
                self.assertEqual(
                    self.success("workflow.save", params, request_id=request_id), saved
                )
                status = self.success("request.status", {"requestId": request_id})
                self.assertEqual((status["state"], status["result"]), ("finished", saved))
                history = self.success("workflow.revisions", {"workflowId": identity})
                self.assertEqual(history["headRevision"], saved["revision"])
                self.assertEqual(len(history["items"]), 2 if base else 1)
                actual = self.success(
                    "workflow.read", {"workflowId": identity, "revision": saved["revision"]}
                )
                self.assertEqual(actual["definition"], definition)

    def test_saved_receipt_survives_discard_restart_and_reference_changes(self):
        identity, reference = self.upload()
        params = self.save_params(reference)
        saved = self.success("workflow.save", params, request_id="save-reference")
        self.success("content.discard", {"uploadId": identity})
        self.assertEqual(self.success("workflow.save", params, request_id="save-reference"), saved)
        _, another = self.upload()
        self.assertEqual(
            self.request("workflow.save", self.save_params(another), request_id="save-reference")[
                "error"
            ]["code"],
            "REQUEST_CONFLICT",
        )
        self.engine.close()
        self.workspace = Workspace(self.directory)
        self.engine = Engine(self.workspace, clock=lambda: self.now[0])
        self.editor = self.engine.open_session(
            "studio",
            {"workflow.write", "workflow.read"},
            local=True,
            capabilities={"contentRefsV1"},
        )
        self.assertEqual(self.success("workflow.save", params, request_id="save-reference"), saved)
        self.assertEqual(
            self.success("request.status", {"requestId": "save-reference"})["result"], saved
        )
        self.assertEqual(
            self.request(
                "resource.read", {"resourceId": another["resourceId"], "offset": 0, "length": 1}
            )["error"]["code"],
            "RESOURCE_NOT_FOUND",
        )

    def test_save_pins_admitted_content_until_commit(self):
        _, reference = self.upload()
        original = self.workspace.save

        def save_after_expiry(*args, **kwargs):
            self.now[0] += 61
            return original(*args, **kwargs)

        with patch.object(self.workspace, "save", side_effect=save_after_expiry):
            saved = self.success("workflow.save", self.save_params(reference))
        self.assertEqual(
            self.success("workflow.read", {"workflowId": "hello", "revision": saved["revision"]})[
                "definition"
            ],
            workflow(),
        )

    def test_export_modes_cache_chunk_bounds_and_expiry(self):
        saved = self.save()
        params = {"workflowId": "hello", "revision": saved["revision"]}
        self.assertIn("definition", self.success("workflow.read", params))
        exported = self.success("workflow.read", {**params, "contentMode": "resource"})[
            "modelResource"
        ]
        self.now[0] += 10
        self.assertEqual(
            self.success("workflow.read", {**params, "contentMode": "resource"})["modelResource"],
            exported,
        )
        eof = self.success(
            "resource.read",
            {
                "resourceId": exported["resourceId"],
                "offset": float(exported["length"]),
                "length": 1.0,
            },
        )
        self.assertEqual((eof["length"], eof["chunk"]), (0, ""))
        self.assertEqual(
            self.request(
                "resource.read",
                {
                    "resourceId": exported["resourceId"],
                    "offset": exported["length"] + 1,
                    "length": 1,
                },
            )["error"]["code"],
            "INVALID_ARGUMENT",
        )
        self.now[0] += 50
        self.assertEqual(
            self.request(
                "resource.read", {"resourceId": exported["resourceId"], "offset": 0, "length": 1}
            )["error"]["code"],
            "RESOURCE_NOT_FOUND",
        )
        replacement = self.success("workflow.read", {**params, "contentMode": "resource"})[
            "modelResource"
        ]
        self.assertNotEqual(replacement["resourceId"], exported["resourceId"])

    def test_session_close_and_revoke_release_owned_content(self):
        identity, reference = self.upload()
        saved = self.save()
        self.success(
            "workflow.read",
            {"workflowId": "hello", "revision": saved["revision"], "contentMode": "resource"},
        )
        streams = [item.stream for item in set(self.engine.contents.resources.values())]
        self.engine.revoke_client("studio")
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual(self.engine.contents.uploads, {})
        self.assertEqual(self.engine.contents.resources, {})
        response = self.write(identity, 0, b"{}")
        self.assertEqual(response["error"]["code"], "ACCESS_DENIED")

    def test_seal_deadline_does_not_publish_reference(self):
        data = dumps({"definition": workflow(), "dependencies": {}})
        identity = self.begin(data)["uploadId"]
        self.assertTrue(self.write(identity, 0, data)["ok"])
        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        from deskweave.engine import service

        original = service.validate

        def delayed(reference, value, **kwargs):
            result = original(reference, value, **kwargs)
            if reference.endswith("#/$defs/workflowModel"):
                clock[0] = 30.0
            return result

        with patch.object(service, "validate", side_effect=delayed):
            response = self.request("content.seal", {"uploadId": identity})
        self.assertEqual(response["error"]["code"], "DEADLINE_EXCEEDED")
        self.assertFalse(self.engine.contents.uploads[identity].sealed)
        self.assertEqual(self.engine.contents.resources, {})

    def test_expiry_matches_reported_utc_second(self):
        self.now[0] += 0.125
        started = self.begin(b"{}")
        expires = datetime.fromisoformat(started["expiresAt"]).timestamp()
        self.assertEqual(expires, int(self.now[0]) + 60)
        self.now[0] = expires - 0.001
        self.assertTrue(self.write(started["uploadId"], 0, b"{}")["ok"])
        self.now[0] = expires
        self.assertEqual(
            self.write(started["uploadId"], 0, b"{}")["error"]["code"], "CONTENT_NOT_FOUND"
        )
        self.now[0] += 0.25
        identity, _ = self.upload()
        sealed = self.success("content.seal", {"uploadId": identity})
        self.assertEqual(
            datetime.fromisoformat(sealed["expiresAt"]).timestamp(), int(self.now[0]) + 60
        )
        saved = self.save()
        exported = self.success(
            "workflow.read",
            {"workflowId": "hello", "revision": saved["revision"], "contentMode": "resource"},
        )["modelResource"]
        self.assertEqual(
            datetime.fromisoformat(exported["expiresAt"]).timestamp(), int(self.now[0]) + 60
        )

    def test_discard_resource_is_idempotent_and_owner_scoped(self):
        identity, reference = self.upload()
        saved = self.success("workflow.save", self.save_params(reference))
        params = {"workflowId": "hello", "revision": saved["revision"], "contentMode": "resource"}
        exported = self.success("workflow.read", params)["modelResource"]
        other = self.engine.open_session(
            "other", {"workflow.read", "workflow.write"}, capabilities={"contentRefsV1"}
        )
        self.success("content.discard", {"resourceId": exported["resourceId"]}, session=other)
        self.assertTrue(self.read_all(exported))
        for invalid in ({}, {"uploadId": identity, "resourceId": reference["resourceId"]}):
            self.assertEqual(
                self.request("content.discard", invalid)["error"]["code"], "INVALID_ARGUMENT"
            )
        for resource in (reference, exported):
            item = self.engine.contents.resources[resource["resourceId"]]
            for _ in range(2):
                self.success("content.discard", {"resourceId": resource["resourceId"]})
            self.assertTrue(item.stream.closed)
            self.assertEqual(
                self.request(
                    "resource.read",
                    {"resourceId": resource["resourceId"], "offset": 0, "length": 1},
                )["error"]["code"],
                "RESOURCE_NOT_FOUND",
            )
        self.assertNotIn(identity, self.engine.contents.uploads)
        self.assertNotEqual(
            self.success("workflow.read", params)["modelResource"]["resourceId"],
            exported["resourceId"],
        )

    def test_output_count_capacity_reuses_and_releases_resources(self):
        outputs = []
        for index in range(17):
            definition = workflow(identity=f"export-{index}")
            saved = self.save(definition)
            params = {
                "workflowId": definition["id"],
                "revision": saved["revision"],
                "contentMode": "resource",
            }
            if index < 16:
                outputs.append((params, self.success("workflow.read", params)["modelResource"]))
        self.assertEqual(self.request("workflow.read", params)["error"]["code"], "LIMIT_EXCEEDED")
        first_params, first = outputs[0]
        self.assertEqual(self.success("workflow.read", first_params)["modelResource"], first)
        self.begin(b"{}")
        self.success("content.discard", {"resourceId": first["resourceId"]})
        self.success("workflow.read", params)
        self.now[0] += 60
        self.success("workflow.read", first_params)
        self.assertEqual(len(self.engine.contents.resources), 1)
        self.assertEqual(len(self.engine.contents.uploads), 0)

    def test_output_byte_capacity_is_independent_of_upload_capacity(self):
        saved = self.save()
        params = {"workflowId": "hello", "revision": saved["revision"], "contentMode": "resource"}
        data = dumps({"definition": workflow(), "dependencies": {}})
        sessions = [
            self.engine.open_session(str(index), {"workflow.read"}, capabilities={"contentRefsV1"})
            for index in range(2)
        ]
        with patch("deskweave.adapters.content.MAX_RESERVED", len(data) * 2):
            self.begin(data)
            first = self.success("workflow.read", params)["modelResource"]
            self.success("workflow.read", params, session=sessions[0])
            self.assertEqual(
                self.request("workflow.read", params, session=sessions[1])["error"]["code"],
                "LIMIT_EXCEEDED",
            )
            self.success("content.discard", {"resourceId": first["resourceId"]})
            self.success("workflow.read", params, session=sessions[1])

    def test_auto_mode_uses_exact_model_byte_threshold(self):
        for size in (262144, 262145):
            definition = workflow(identity=str(size))
            definition["variables"]["payload"] = ""
            model = {"definition": definition, "dependencies": {}}
            definition["variables"]["payload"] = "x" * (size - len(dumps(model)))
            self.assertEqual(len(dumps(model)), size)
            saved = self.save(definition)
            result = self.success(
                "workflow.read", {"workflowId": str(size), "revision": saved["revision"]}
            )
            if size == 262144:
                self.assertEqual(result["definition"], definition)
            else:
                self.assertEqual(loads(self.read_all(result["modelResource"])), model)

    def test_partial_write_and_timeout_restore_previous_offset(self):
        for failure in ("storage", "deadline"):
            with self.subTest(failure=failure):
                clock = [0.0]
                self.engine.monotonic = lambda clock=clock: clock[0]
                raw = dumps({"definition": workflow(), "dependencies": {}})
                identity = self.begin(raw)["uploadId"]
                self.assertTrue(self.write(identity, 0, raw[:5])["ok"])
                item = self.engine.contents.uploads[identity]
                stream = item.stream
                wrapper = Mock(wraps=stream)

                def fail_write(block, stream=stream):
                    stream.write(block[:2])
                    raise OSError("write failure")

                def delayed_flush(stream=stream, clock=clock):
                    stream.flush()
                    clock[0] = 30.0

                if failure == "storage":
                    wrapper.write.side_effect = fail_write
                else:
                    wrapper.flush.side_effect = delayed_flush
                item.stream = wrapper
                try:
                    response = self.write(identity, 5, raw[5:])
                finally:
                    item.stream = stream
                self.assertEqual(
                    response["error"]["code"],
                    "STORAGE_UNAVAILABLE" if failure == "storage" else "DEADLINE_EXCEEDED",
                )
                self.assertEqual(item.received, 5)
                stream.seek(0)
                self.assertEqual(stream.read(), raw[:5])
                clock[0] = 0.0
                self.assertTrue(self.write(identity, 5, raw[5:])["ok"])
                self.success("content.seal", {"uploadId": identity})
                self.success("content.discard", {"uploadId": identity})

    def test_close_failure_still_revokes_all_sessions_and_content(self):
        first = self.begin(b"{}")
        self.begin(b"{}")
        other = self.engine.open_session(
            "studio", {"workflow.read", "workflow.write"}, capabilities={"contentRefsV1"}
        )
        self.begin(b"{}", session=other)
        for session in (self.editor, other):
            self.success("workflow.validate", {"definition": workflow()}, session=session)
        streams = [item.stream for item in self.engine.contents.uploads.values()]
        item = self.engine.contents.uploads[first["uploadId"]]
        stream = item.stream
        wrapper = Mock(wraps=stream)

        def close_failure():
            stream.close()
            raise OSError("close failure")

        wrapper.close.side_effect = close_failure
        item.stream = wrapper
        with patch.object(self.engine.pages, "release", wraps=self.engine.pages.release) as release:
            with self.assertRaisesRegex(OSError, "close failure"):
                self.engine.revoke_client("studio")
            self.assertEqual(
                {call.args[0] for call in release.call_args_list}, {self.editor.id, other.id}
            )
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual(self.engine.contents.uploads, {})
        self.assertEqual(self.engine._collections, {})
        self.assertNotIn(self.editor.id, self.engine._sessions)
        self.assertNotIn(other.id, self.engine._sessions)

    def test_exact_maximum_model_saves_and_reads_without_metadata_overhead(self):
        self.engine.monotonic = lambda: 0.0
        definition = workflow()
        definition["variables"]["payload"] = ["x" * 262144] * 127 + [""]
        model = {"definition": definition, "dependencies": {}}
        definition["variables"]["payload"][-1] = "x" * (
            MAX_CONTENT - len(dumps(model, max_bytes=MAX_CONTENT))
        )
        raw = dumps(model, max_bytes=MAX_CONTENT)
        self.assertEqual(len(raw), MAX_CONTENT)
        identity, reference = self.upload(data=raw)
        saved = self.success("workflow.save", self.save_params(reference))
        result = self.success(
            "workflow.read", {"workflowId": "hello", "revision": saved["revision"]}
        )
        self.assertEqual(result["modelResource"]["length"], MAX_CONTENT)
        self.assertEqual(self.read_all(result["modelResource"]), raw)
        self.success("content.discard", {"uploadId": identity})
