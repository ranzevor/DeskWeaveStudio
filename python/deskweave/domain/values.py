from __future__ import annotations

import copy
import math
import operator
from collections.abc import Callable

from deskweave.contracts import validate
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, MAX_SAFE_INTEGER, dumps


class ExpressionError(DeskWeaveError):
    def __init__(self, reason: str, message: str, path=(), *, code="INVALID_ARGUMENT"):
        super().__init__(code, message, details={"expressionPath": list(path), "reason": reason})


class MissingPath(ExpressionError):
    def __init__(self, path):
        super().__init__("PATH_MISSING", "数据路径不存在。", path)


def value_type(value: object) -> str:
    return {
        type(None): "null",
        bool: "boolean",
        int: "number",
        float: "number",
        str: "string",
        list: "array",
        dict: "object",
    }[type(value)]


def matches_type(value: object, expected: str) -> bool:
    if expected == "integer":
        return type(value) in (int, float) and math.isfinite(value) and int(value) == value
    return expected == "any" or value_type(value) == expected


def _shape(value: object, checkpoint: Callable[[], None] = lambda: None) -> None:
    operations = 0
    stack = [(value, 0, ())]
    while stack:
        checkpoint()
        item, depth, path = stack.pop()
        if depth > 32:
            raise ExpressionError(
                "AST_DEPTH", "表达式嵌套超过 32 层。", path, code="LIMIT_EXCEEDED"
            )
        if type(item) is list:
            stack.extend((child, depth + 1, (*path, index)) for index, child in enumerate(item))
        elif type(item) is dict:
            if "$literal" in item:
                continue
            if "$expr" in item:
                operations += 1
                if operations > 256:
                    raise ExpressionError(
                        "AST_NODES", "表达式超过 256 个运算节点。", path, code="LIMIT_EXCEEDED"
                    )
                expr = item["$expr"]
                if type(expr) is dict and type(expr.get("args")) is list:
                    stack.extend(
                        (child, depth + 1, (*path, "$expr", "args", index))
                        for index, child in enumerate(expr["args"])
                    )
            else:
                stack.extend((child, depth + 1, (*path, key)) for key, child in item.items())


def evaluate(
    value: object,
    context: dict | None = None,
    expected_type: str = "any",
    *,
    checkpoint: Callable[[], None] = lambda: None,
) -> object:
    context = {} if context is None else context
    dumps(value, max_text_bytes=262_144, checkpoint=checkpoint)
    dumps(context, max_text_bytes=262_144, checkpoint=checkpoint)
    _shape(value, checkpoint)
    validate("urn:deskweave:values:1#/$defs/value", value)
    validate(
        "urn:deskweave:service:1#/$defs/expression.preview/properties/params",
        {
            "value": value,
            "context": context,
            "expectedType": expected_type,
        },
    )

    def boolean(item, path):
        if type(item) is not bool:
            raise ExpressionError("TYPE_MISMATCH", "布尔运算需要布尔值。", path)
        return item

    def number(item, path):
        if type(item) not in (int, float):
            raise ExpressionError("TYPE_MISMATCH", "数值运算需要数字。", path)
        return item

    def visit(item, path=()):
        checkpoint()
        if type(item) is list:
            return [visit(child, (*path, index)) for index, child in enumerate(item)]
        if type(item) is not dict:
            return item
        if "$literal" in item:
            return copy.deepcopy(item["$literal"])
        if "$expr" not in item:
            return {key: visit(child, (*path, key)) for key, child in item.items()}
        expr = item["$expr"]
        op = expr["op"]
        if op == "path":
            result = context
            for index, key in enumerate(expr["path"]):
                checkpoint()
                if type(result) is dict and type(key) is str and key in result:
                    result = result[key]
                elif (
                    type(result) is list and matches_type(key, "integer") and 0 <= key < len(result)
                ):
                    result = result[int(key)]
                else:
                    raise MissingPath((*path, "$expr", "path", index))
            return copy.deepcopy(result)
        args = expr["args"]
        if op == "coalesce":
            for index, argument in enumerate(args):
                try:
                    result = visit(argument, (*path, "$expr", "args", index))
                except MissingPath:
                    continue
                if result is not None:
                    return result
            return None
        first_path, second_path = (*path, "$expr", "args", 0), (*path, "$expr", "args", 1)
        first = visit(args[0], first_path)
        if op == "and":
            return boolean(first, first_path) and boolean(visit(args[1], second_path), second_path)
        if op == "or":
            return boolean(first, first_path) or boolean(visit(args[1], second_path), second_path)
        if op == "not":
            return not boolean(first, first_path)
        if op == "length":
            if type(first) not in (str, list):
                raise ExpressionError("TYPE_MISMATCH", "长度运算需要文本或数组。", first_path)
            return len(first.encode("utf-16-le")) // 2 if type(first) is str else len(first)
        second = visit(args[1], second_path)
        if op in {"equal", "notEqual"}:
            equal = dumps(first, checkpoint=checkpoint) == dumps(second, checkpoint=checkpoint)
            return equal if op == "equal" else not equal
        left, right = number(first, first_path), number(second, second_path)
        if op == "divide" and right == 0:
            raise ExpressionError("DIVISION_BY_ZERO", "除数不能为零。", second_path)
        result = {
            "add": operator.add,
            "subtract": operator.sub,
            "multiply": operator.mul,
            "divide": operator.truediv,
            "less": operator.lt,
            "lessEqual": operator.le,
            "greater": operator.gt,
            "greaterEqual": operator.ge,
        }[op](left, right)
        if type(result) in (int, float) and (
            not math.isfinite(result) or abs(result) > MAX_SAFE_INTEGER
        ):
            raise ExpressionError(
                "NUMERIC_RANGE",
                "运算结果超出数值范围。",
                (*path, "$expr", "op"),
                code="LIMIT_EXCEEDED",
            )
        return result

    result = visit(value)
    dumps(result, max_bytes=MAX_FRAME, max_text_bytes=262_144, checkpoint=checkpoint)
    validate("urn:deskweave:values:1#/$defs/raw", result)
    checkpoint()
    if not matches_type(result, expected_type):
        raise ExpressionError("TYPE_MISMATCH", "表达式结果类型与字段要求不一致。")
    return result
