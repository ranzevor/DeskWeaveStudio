import struct
import unittest

from deskweave.adapters.framing import FrameDecoder, encode_frame
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
