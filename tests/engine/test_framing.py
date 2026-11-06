import struct
import unittest

from deskweave.adapters.framing import FrameDecoder, encode_frame
from deskweave.contracts import validate
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, dumps, loads


class JsonTests(unittest.TestCase):
    def test_invalid_inputs(self):
        for raw in [
            b'{"a":1,"a":2}',
            b'{"a":{"b":1,"b":2}}',
            b"NaN",
            b"Infinity",
            b"1e999",
            b'"\\ud800"',
            b'"\xff"',
            b"\xef\xbb\xbf{}",
            b"01",
            b"{",
        ]:
            with self.subTest(raw=raw), self.assertRaises(DeskWeaveError):
                loads(raw)
        for value in [float("nan"), float("inf"), 10**1000, object(), {1: 2}]:
            with self.subTest(value=type(value)), self.assertRaises(DeskWeaveError):
                dumps(value)

    def test_depth_and_escaping(self):
        self.assertEqual(loads(dumps({"text": '"\\[{]\n😀'})), {"text": '"\\[{]\n😀'})
        self.assertEqual(loads(b"[" * 64 + b"0" + b"]" * 64), self.nested(64))
        with self.assertRaises(DeskWeaveError):
            loads(b"[" * 65 + b"0" + b"]" * 65)

    @staticmethod
    def nested(count):
        value = 0
        for _ in range(count):
            value = [value]
        return value

    def test_exact_frame_boundary(self):
        value = {"parts": ["x" * 220000] * 4 + [""]}
        value["parts"][-1] = "x" * (MAX_FRAME - len(dumps(value)))
        raw = dumps(value)
        self.assertEqual(len(raw), MAX_FRAME)
        self.assertEqual(loads(raw), value)
        value["parts"][-1] += "x"
        with self.assertRaises(DeskWeaveError):
            dumps(value)

    def test_canonical_equivalence(self):
        self.assertEqual(dumps({"z": -0.0, "a": 1.0}), b'{"a":1,"z":0}')

    def test_container_sizes_follow_schema(self):
        for reference, value in (
            (
                "urn:deskweave:package:1#/properties/resources/items/properties/bindings",
                {f"r{i}": "resource" for i in range(10000)},
            ),
            (
                "urn:deskweave:service:1#/$defs/template.names/properties/params/properties/overrides",
                {f"candidate{i}": f"名称{i}" for i in range(2000)},
            ),
            (
                "urn:deskweave:trajectory:1#/properties/events",
                [{"kind": "pointer.move", "atUs": i, "x": i, "y": 0} for i in range(10001)],
            ),
            ("urn:deskweave:draft:1#/$defs/modelContent/properties/source", "源码" * 50000),
        ):
            with self.subTest(reference=reference):
                validate(reference, value)
                encoded = dumps(value)
                self.assertLess(len(encoded), MAX_FRAME)
                self.assertEqual(loads(encoded), value)

    def test_wide_containers_keep_frame_budget(self):
        for value in ({f"k{i}": None for i in range(10001)}, [None] * 100000):
            encoded = dumps(value)
            self.assertEqual(loads(encoded), value)
            with self.assertRaises(DeskWeaveError) as caught:
                dumps(value, max_bytes=len(encoded) - 1)
            self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")

    def test_large_text_keeps_byte_and_unicode_checks(self):
        value = "x" * (MAX_FRAME - 2)
        self.assertEqual(len(dumps(value)), MAX_FRAME)
        self.assertEqual(loads(dumps(value)), value)
        with self.assertRaises(DeskWeaveError) as caught:
            dumps(value + "x")
        self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")
        with self.assertRaises(DeskWeaveError) as caught:
            dumps(value[:-1] + "\ud800")
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_envelope_payloads_use_general_json(self):
        payload = {
            "records": {f"r{i}": {"kind": "image"} for i in range(1001)},
            "events": [None] * 10001,
            "source": "源码" * 50000,
        }
        for reference, message in (
            (
                "urn:deskweave:message:1#/$defs/requestEnvelope",
                {
                    "protocolVersion": "1.0",
                    "kind": "request",
                    "id": "request",
                    "session": "session",
                    "workspace": "workspace",
                    "method": "example.payload",
                    "deadlineMs": 30000,
                    "params": payload,
                },
            ),
            (
                "urn:deskweave:message:1",
                {
                    "protocolVersion": "1.0",
                    "kind": "response",
                    "replyTo": "request",
                    "ok": True,
                    "result": payload,
                },
            ),
            (
                "urn:deskweave:message:1",
                {
                    "protocolVersion": "1.0",
                    "kind": "response",
                    "replyTo": "request",
                    "ok": False,
                    "error": {
                        "code": "INVALID_ARGUMENT",
                        "message": "字段无效。",
                        "retry": "never",
                        "details": payload,
                    },
                },
            ),
            (
                "urn:deskweave:message:1",
                {
                    "protocolVersion": "1.0",
                    "kind": "event",
                    "streamId": "stream",
                    "instanceEpoch": "epoch",
                    "seq": "1",
                    "event": "example.updated",
                    "occurredAt": "2026-01-01T00:00:00Z",
                    "payload": payload,
                },
            ),
        ):
            with self.subTest(kind=message["kind"], ok=message.get("ok")):
                validate(reference, message)
                encoded = dumps(message)
                self.assertLess(len(encoded), MAX_FRAME)
                self.assertEqual(loads(encoded), message)
        for reference in (
            "urn:deskweave:service:1#/$defs/request.status/properties/result/properties/result",
            "urn:deskweave:service:1#/$defs/stream.subscribe/properties/result/properties/snapshot",
            "urn:deskweave:operation-results:1#/$defs/operationSnapshot/properties/result",
            "urn:deskweave:operation-results:1#/$defs/error/properties/details",
        ):
            with self.subTest(reference=reference):
                validate(reference, payload)


class FramingTests(unittest.TestCase):
    def test_fragmentation_and_concatenation(self):
        expected = [{"text": "桌面😀"}, {"number": 12}]
        raw = b"".join(encode_frame(value) for value in expected)
        for chunk_size in (1, 2, 3, 4, 7, len(raw)):
            with self.subTest(chunk_size=chunk_size):
                decoder, actual = FrameDecoder(), []
                for start in range(0, len(raw), chunk_size):
                    actual.extend(decoder.feed(raw[start : start + chunk_size]))
                decoder.finish()
                self.assertEqual(actual, expected)

    def test_invalid_frame_is_terminal(self):
        for payload in [
            struct.pack("<I", 0),
            struct.pack("<I", MAX_FRAME + 1),
            struct.pack("<I", 4) + b"null",
            struct.pack("<I", 1) + b"x",
        ]:
            decoder = FrameDecoder()
            with self.subTest(payload=payload), self.assertRaises(DeskWeaveError):
                decoder.feed(payload)
            with self.assertRaises(DeskWeaveError):
                decoder.feed(encode_frame({}))

    def test_partial_eof(self):
        raw = encode_frame({"message": "hello"})
        for position in range(1, len(raw)):
            decoder = FrameDecoder()
            decoder.feed(raw[:position])
            with self.subTest(position=position), self.assertRaises(DeskWeaveError):
                decoder.finish()
