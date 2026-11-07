import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import rfc8785

from deskweave import contracts
from deskweave import json as codec
from deskweave.adapters import content, workspace
from deskweave.engine import service
from deskweave.errors import DeskWeaveError
from tests.engine.test_content import ContentCase
from tests.engine.test_service import workflow


def expired():
    return DeskWeaveError("DEADLINE_EXCEEDED", "请求处理超时。")


class ProcessingTests(unittest.TestCase):
    def test_json_array_parsing_checks_budget_between_values(self):
        original = json.decoder.JSONArray
        active = False
        count = 0

        def array(*args, **kwargs):
            nonlocal active
            active = True
            return original(*args, **kwargs)

        def checkpoint():
            nonlocal count
            if active:
                count += 1
                if count == 20:
                    raise expired()

        with patch.object(json.decoder, "JSONArray", side_effect=array):
            with self.assertRaises(DeskWeaveError) as caught:
                codec.loads(b"[" + b"null," * 9999 + b"null]", checkpoint=checkpoint)
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.assertEqual(count, 20)

    def test_recursive_schema_checks_budget_on_valid_cross_resource_content(self):
        definition = workflow()
        definition["variables"]["rows"] = [{"value": i, "enabled": True} for i in range(1000)]
        model = {"definition": definition, "dependencies": {}}
        reference = "urn:deskweave:content:1#/$defs/workflowModel"
        contracts.validate(reference, model)
        count = 0

        def checkpoint():
            nonlocal count
            count += 1
            if count == 200:
                raise expired()

        with self.assertRaises(DeskWeaveError) as caught:
            contracts.validate(reference, model, checkpoint=checkpoint)
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.assertEqual(count, 200)
        contracts.validate(reference, {"definition": workflow(), "dependencies": {}})

    def test_schema_budget_does_not_leak_to_concurrent_validation(self):
        entered = threading.Event()
        finished = threading.Event()

        def checkpoint():
            entered.set()
            if not finished.wait(5):
                raise RuntimeError("concurrent validation did not complete")
            raise expired()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(
                contracts.validate, "urn:deskweave:trajectory:1", {}, checkpoint=checkpoint
            )
            try:
                self.assertTrue(entered.wait(5))
                contracts.validate("urn:deskweave:workflow:1", workflow())
            finally:
                finished.set()
            with self.assertRaises(DeskWeaveError) as caught:
                pending.result(timeout=5)
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")

    def test_encoding_checks_budget_between_text_blocks(self):
        original = rfc8785.dumps
        count = 0

        def scalar(value):
            nonlocal count
            count += 1
            return original(value)

        def checkpoint():
            if count == 2:
                raise expired()

        with patch.object(rfc8785, "dumps", side_effect=scalar):
            with self.assertRaises(DeskWeaveError) as caught:
                codec.dumps("汉😀\n" * 20000, checkpoint=checkpoint)
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.assertEqual(count, 2)

    def test_chunked_encoding_matches_canonical_bytes(self):
        value = {
            "😀": ['汉😀\x00\n\\"' * 5000, -0.0, 1e-7, 1e-6, 1.0],
            "\ue000": {"tail": "x" * (codec.TEXT_CHUNK - 1) + "😀"},
        }
        expected = rfc8785.dumps(value)
        self.assertEqual(codec.dumps(value), expected)
        self.assertEqual(codec.loads(expected), value)


class ServiceProcessingTests(ContentCase):
    def test_short_seal_deadline_releases_engine_for_another_session(self):
        definition = workflow()
        definition["variables"]["rows"] = [
            {"value": i, "enabled": True, "label": "条目"} for i in range(10000)
        ]
        raw = codec.dumps({"definition": definition, "dependencies": {}})
        identity = self.begin(raw)["uploadId"]
        for offset in range(0, len(raw), content.MAX_CHUNK):
            self.assertTrue(
                self.write(identity, offset, raw[offset : offset + content.MAX_CHUNK])["ok"]
            )
        entered = threading.Event()
        original = content.loads

        def parsing(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)

        with patch.object(content, "loads", side_effect=parsing):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(
                    self.request, "content.seal", {"uploadId": identity}, deadline_ms=20
                )
                self.assertTrue(entered.wait(5))
                other = pool.submit(
                    self.request,
                    "request.status",
                    {"requestId": "missing"},
                    session=self.remote,
                    deadline_ms=500,
                )
                sealed = pending.result(timeout=5)
                status = other.result(timeout=5)
        self.assertEqual(sealed["error"]["code"], "DEADLINE_EXCEEDED")
        self.assertTrue(status["ok"], status)
        self.assertFalse(self.engine.contents.uploads[identity].sealed)
        self.assertEqual(self.engine.contents.resources, {})
        self.success("content.seal", {"uploadId": identity})

    def test_saved_model_and_reference_parsing_use_request_budget(self):
        _, reference = self.upload()
        saved = self.save()
        for module, method, params in (
            (content, "workflow.save", self.save_params(reference, base=saved["revision"])),
            (workspace, "workflow.read", {"workflowId": "hello", "revision": saved["revision"]}),
        ):
            with self.subTest(method=method):
                clock = [0.0]
                self.engine.monotonic = lambda clock=clock: clock[0]
                count = 0

                def parsing(data, clock=clock, **kwargs):
                    checkpoint = kwargs["checkpoint"]

                    def limited():
                        nonlocal count
                        count += 1
                        if count == 10:
                            clock[0] = 30.0
                        checkpoint()

                    return codec.loads(data, **{**kwargs, "checkpoint": limited})

                with patch.object(module, "loads", side_effect=parsing):
                    result = self.request(method, params)
                self.assertEqual(result["error"]["code"], "DEADLINE_EXCEEDED")
                self.assertEqual(count, 10)
        self.engine.monotonic = time.monotonic
        self.assertEqual(self.workspace.read("hello", saved["revision"])["definition"], workflow())

    def test_result_validation_preserves_deadline_error(self):
        clock = [0.0]
        self.engine.monotonic = lambda: clock[0]
        original = service.validate

        def validate(reference, value, **kwargs):
            if reference.endswith("/result"):
                checkpoint = kwargs["checkpoint"]
                count = 0

                def limited():
                    nonlocal count
                    count += 1
                    if count == 5:
                        clock[0] = 30.0
                    checkpoint()

                kwargs["checkpoint"] = limited
            return original(reference, value, **kwargs)

        with patch.object(service, "validate", side_effect=validate):
            response = self.request("request.status", {"requestId": "missing"})
        self.assertEqual(response["error"]["code"], "DEADLINE_EXCEEDED")
        self.assertNotIn("result", response)
