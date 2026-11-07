import unittest

from deskweave.domain.values import evaluate
from deskweave.errors import DeskWeaveError


def expr(op, *args):
    return {"$expr": {"op": op, "args": list(args)}}


def path(*keys):
    return {"$expr": {"op": "path", "path": list(keys)}}


class ExpressionTests(unittest.TestCase):
    def test_arithmetic_and_numeric_comparisons(self):
        for op, left, right, expected in [
            ("add", 2, 3, 5),
            ("subtract", 7, 2, 5),
            ("multiply", 3, 4, 12),
            ("divide", 7, 2, 3.5),
            ("less", 1, 2, True),
            ("lessEqual", 2, 2, True),
            ("greater", 1, 2, False),
            ("greaterEqual", 2, 2, True),
        ]:
            with self.subTest(op=op):
                self.assertEqual(evaluate(expr(op, left, right)), expected)

    def test_short_circuit_and_coalesce(self):
        missing = path("vars", "absent")
        self.assertFalse(evaluate(expr("and", False, missing)))
        self.assertTrue(evaluate(expr("or", True, expr("divide", 1, 0))))
        self.assertTrue(evaluate(expr("not", False)))
        for value in (False, 0, "", [], {}):
            self.assertEqual(evaluate(expr("coalesce", missing, None, value, 9)), value)
        self.assertIsNone(evaluate(expr("coalesce", missing, None)))
        with self.assertRaises(DeskWeaveError) as caught:
            evaluate(expr("coalesce", expr("divide", 1, 0), 2))
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(caught.exception.details["reason"], "DIVISION_BY_ZERO")

    def test_type_separation_and_json_equality(self):
        self.assertFalse(evaluate(expr("equal", False, 0)))
        self.assertTrue(evaluate(expr("notEqual", True, 1)))
        self.assertTrue(evaluate(expr("equal", {"a": 1, "b": 2}, {"b": 2, "a": 1.0})))
        self.assertFalse(evaluate(expr("equal", [False], [0])))
        for value in (expr("add", True, 1), expr("and", 0, True), expr("length", {})):
            with self.subTest(value=value), self.assertRaises(DeskWeaveError):
                evaluate(value)
        with self.assertRaises(DeskWeaveError):
            evaluate(1, expected_type="boolean")

    def test_path_values_are_data_and_copied(self):
        raw = expr("divide", 1, 0)
        context = {"vars": {"record": raw, "__proto__": "own", "rows": [7]}}
        self.assertEqual(evaluate(path("vars", "record"), context), raw)
        self.assertEqual(evaluate({"$literal": raw}), raw)
        self.assertEqual(evaluate(path("vars", "__proto__"), context), "own")
        self.assertEqual(evaluate(path("vars", "rows", 0), context), 7)
        returned = evaluate(path("vars", "record"), context)
        returned["$expr"]["op"] = "add"
        self.assertEqual(context["vars"]["record"]["$expr"]["op"], "divide")
        for keys in [("vars", "keys"), ("vars", "rows", "0"), ("vars", "rows", 1)]:
            with self.subTest(keys=keys), self.assertRaises(DeskWeaveError):
                evaluate(path(*keys), context)

    def test_length_uses_utf16_units_and_array_items(self):
        self.assertEqual(evaluate(expr("length", "桌面😀")), 4)
        self.assertEqual(evaluate(expr("length", [1, 2, 3])), 3)

    def test_budget_and_unvisited_structure(self):
        self.assertEqual(evaluate([expr("add", 0, 1)] * 256), [1] * 256)
        for value in (
            [expr("add", 0, 1)] * 257,
            expr("and", False, {"$expr": {"op": "unknown", "args": []}}),
            expr("multiply", 9007199254740991, 2),
            expr("divide", 1, 0),
        ):
            with self.subTest(value=str(value)[:80]), self.assertRaises(DeskWeaveError):
                evaluate(value)
        value = 1
        for _ in range(33):
            value = [value]
        with self.assertRaises(DeskWeaveError):
            evaluate(value)

    def test_text_budget_is_measured_in_utf8_bytes(self):
        for size in (262143, 262144, 262145):
            text = "汉" * (size // 3) + "x" * (size % 3)
            self.assertEqual(len(text.encode("utf-8")), size)
            for value, context in (
                (text, None),
                ({"$literal": {"text": text}}, None),
                (path("vars", "text"), {"vars": {"text": text}}),
            ):
                with self.subTest(
                    size=size, literal=isinstance(value, str), context=context is not None
                ):
                    if size <= 262144:
                        expected = (
                            {"text": text}
                            if isinstance(value, dict) and "$literal" in value
                            else text
                        )
                        self.assertEqual(evaluate(value, context), expected)
                    else:
                        with self.assertRaises(DeskWeaveError) as caught:
                            evaluate(value, context)
                        self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")
