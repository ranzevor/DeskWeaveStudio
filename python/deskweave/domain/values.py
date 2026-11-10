from __future__ import annotations

import math
import operator
from collections.abc import Callable

from deskweave.contracts import validate
from deskweave.domain.frozen import PathMissing, Value
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


def evaluate_value(
    value, resolve, *, checkpoint=lambda: None, observe_control=lambda sensitive: None
):
    observed = False

    def checked(item):
        nonlocal observed
        observed |= item.sensitive
        return item

    def boolean(item, path):
        observe_control(item.sensitive and not item.typed)
        if item.kind != "boolean":
            raise ExpressionError("TYPE_MISMATCH", "布尔运算需要布尔值。", path)
        return item.data

    def number(item, path):
        observe_control(item.sensitive and not item.typed)
        if item.kind != "number":
            raise ExpressionError("TYPE_MISMATCH", "数值运算需要数字。", path)
        return item.data

    def visit(item, path=()):
        nonlocal observed
        checkpoint()
        if type(item) is list:
            return Value.build(tuple(visit(child, (*path, i)) for i, child in enumerate(item)))
        if type(item) is not dict:
            return Value.freeze(item)
        if "$literal" in item:
            return Value.freeze(item["$literal"])
        if "$expr" not in item:
            return Value.build({key: visit(child, (*path, key)) for key, child in item.items()})
        expr = item["$expr"]
        op = expr["op"]
        if op == "path":
            try:
                return checked(resolve(expr["path"]))
            except PathMissing as error:
                observed |= error.sensitive
                missing = MissingPath((*path, "$expr", "path", error.index))
                missing.sensitive = error.sensitive
                raise missing from error
        args = expr["args"]
        if op == "coalesce":
            control = False
            for index, argument in enumerate(args):
                prior, observed = observed, False
                try:
                    result = visit(argument, (*path, "$expr", "args", index))
                except MissingPath as error:
                    control |= observed or getattr(error, "sensitive", False)
                    continue
                finally:
                    observed |= prior
                control |= result.sensitive
                if result.data is not None:
                    return result.marked(control, conditional=True)
            return Value.build(None, control)
        first_path, second_path = (*path, "$expr", "args", 0), (*path, "$expr", "args", 1)
        first = visit(args[0], first_path)
        if op in {"and", "or"}:
            value = boolean(first, first_path)
            if (op == "and" and not value) or (op == "or" and value):
                return Value.build(value, first.sensitive)
            second = visit(args[1], second_path)
            return Value.build(boolean(second, second_path), first.sensitive or second.sensitive)
        if op == "not":
            return Value.build(not boolean(first, first_path), first.sensitive)
        if op == "length":
            observe_control(first.sensitive)
            if first.kind not in {"string", "array"}:
                raise ExpressionError("TYPE_MISMATCH", "长度运算需要文本或数组。", first_path)
            size = (
                len(first.data.encode("utf-16-le")) // 2
                if first.kind == "string"
                else len(first.data)
            )
            return Value.build(size, first.sensitive)
        second = visit(args[1], second_path)
        sensitive = first.sensitive or second.sensitive
        if op in {"equal", "notEqual"}:
            equal = dumps(first.raw(), checkpoint=checkpoint) == dumps(
                second.raw(), checkpoint=checkpoint
            )
            return Value.build(equal if op == "equal" else not equal, sensitive)
        left, right = number(first, first_path), number(second, second_path)
        observe_control(sensitive)
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
        return Value.build(result, sensitive)

    try:
        return visit(value)
    except DeskWeaveError as error:
        error.sensitive = observed or getattr(error, "sensitive", False)
        raise


def validate_result(result, expected_type="any", *, checkpoint=lambda: None):
    try:
        raw = result.raw()
        dumps(raw, max_bytes=MAX_FRAME, max_text_bytes=262_144, checkpoint=checkpoint)
        validate("urn:deskweave:values:1#/$defs/raw", raw, checkpoint=checkpoint)
        if not matches_type(raw, expected_type):
            raise ExpressionError("TYPE_MISMATCH", "表达式结果类型与字段要求不一致。")
    except DeskWeaveError as error:
        error.sensitive = result.sensitive
        raise
    return result


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
    validate("urn:deskweave:values:1#/$defs/value", value, checkpoint=checkpoint)
    validate(
        "urn:deskweave:service:1#/$defs/expression.preview/properties/params",
        {"value": value, "context": context, "expectedType": expected_type},
        checkpoint=checkpoint,
    )
    result = evaluate_value(value, Value.freeze(context).lookup, checkpoint=checkpoint)
    return validate_result(result, expected_type, checkpoint=checkpoint).raw()
