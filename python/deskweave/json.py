from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from itertools import chain

import rfc8785

from deskweave.errors import DeskWeaveError

MAX_FRAME = 1_048_576
MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _invalid(message: str) -> DeskWeaveError:
    return DeskWeaveError("INVALID_ARGUMENT", message)


def check_value(
    value: object,
    *,
    max_bytes: int = MAX_FRAME,
    max_depth: int = 64,
    max_text_bytes: int | None = None,
    checkpoint: Callable[[], None] = lambda: None,
) -> None:
    stack = [(iter((value,)), 0)]
    size = 0
    while stack:
        checkpoint()
        children, depth = stack[-1]
        try:
            item = next(children)
        except StopIteration:
            stack.pop()
            continue
        if depth > max_depth:
            raise _invalid("JSON 嵌套层数超过限制。")
        if item is None or type(item) is bool:
            size += 4 if item is None or item is True else 5
        elif type(item) in (int, float):
            if abs(item) > MAX_SAFE_INTEGER or not math.isfinite(item):
                raise _invalid("数值超出允许范围。")
            size += len(str(item)) if type(item) is int else 1
        elif type(item) is str:
            try:
                length = len(item.encode("utf-8"))
                if max_text_bytes is not None and length > max_text_bytes:
                    raise DeskWeaveError("LIMIT_EXCEEDED", "JSON 文本超过大小限制。")
                size += length + 2
            except UnicodeEncodeError as error:
                raise _invalid("文本包含无效 Unicode 字符。") from error
        elif type(item) is list:
            size += max(0, len(item) - 1) + 2
            stack.append((iter(item), depth + 1))
        elif type(item) is dict:
            if any(type(key) is not str for key in item):
                raise _invalid("JSON 对象的键必须是文本。")
            size += len(item) + max(0, len(item) - 1) + 2
            stack.append((chain.from_iterable(item.items()), depth + 1))
        else:
            raise _invalid("值不是 JSON 数据。")
        if size > max_bytes:
            raise DeskWeaveError("LIMIT_EXCEEDED", "JSON 内容超过大小限制。")


def dumps(
    value: object,
    *,
    max_bytes: int = MAX_FRAME,
    max_text_bytes: int | None = None,
    checkpoint: Callable[[], None] = lambda: None,
) -> bytes:
    check_value(value, max_bytes=max_bytes, max_text_bytes=max_text_bytes, checkpoint=checkpoint)
    try:
        encoded = rfc8785.dumps(value)
    except (ValueError, OverflowError, RecursionError) as error:
        raise _invalid("JSON 内容无法编码。") from error
    checkpoint()
    if len(encoded) > max_bytes:
        raise DeskWeaveError("LIMIT_EXCEEDED", "JSON 内容超过大小限制。")
    return encoded


def loads(data: bytes | str, *, max_bytes: int = MAX_FRAME) -> object:
    if isinstance(data, str):
        try:
            data = data.encode("utf-8")
        except UnicodeEncodeError as error:
            raise _invalid("文本包含无效 Unicode 字符。") from error
    if len(data) > max_bytes:
        raise DeskWeaveError("LIMIT_EXCEEDED", "JSON 内容超过大小限制。")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _invalid("消息不是有效 UTF-8。") from error
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 64:
                raise _invalid("JSON 嵌套层数超过限制。")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise _invalid("JSON 对象包含重复字段。")
            result[key] = value
        return result

    def constant(_value):
        raise _invalid("JSON 不接受非有限数。")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as error:
        raise _invalid("JSON 语法无效。") from error
    dumps(value, max_bytes=max_bytes)
    return value


def digest(value: object) -> str:
    return hashlib.sha256(dumps(value, max_bytes=33_554_432)).hexdigest()
