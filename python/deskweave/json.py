from __future__ import annotations

import codecs
import hashlib
import io
import json
import math
from collections.abc import Callable, Iterable
from itertools import chain

import rfc8785

from deskweave.errors import DeskWeaveError

MAX_FRAME = 1_048_576
TEXT_CHUNK = 16_384
MAX_SAFE_INTEGER = 9_007_199_254_740_991


def json_pointer(path: Iterable[str | int]) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


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
                length = 0
                for offset in range(0, len(item), TEXT_CHUNK):
                    checkpoint()
                    length += len(item[offset : offset + TEXT_CHUNK].encode("utf-8"))
                if max_text_bytes is not None and length > max_text_bytes:
                    raise DeskWeaveError("LIMIT_EXCEEDED", "JSON 文本超过大小限制。")
                size += length + 2
            except UnicodeEncodeError as error:
                raise _invalid("文本包含无效 Unicode 字符。") from error
        elif type(item) is list:
            size += max(0, len(item) - 1) + 2
            stack.append((iter(item), depth + 1))
        elif type(item) is dict:
            for key in item:
                checkpoint()
                if type(key) is not str:
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
    output = io.BytesIO()

    def write(block):
        checkpoint()
        if output.tell() + len(block) > max_bytes:
            raise DeskWeaveError("LIMIT_EXCEEDED", "JSON 内容超过大小限制。")
        output.write(block)

    def encode(item):
        checkpoint()
        if type(item) is str:
            write(b'"')
            for offset in range(0, len(item), TEXT_CHUNK):
                checkpoint()
                write(rfc8785.dumps(item[offset : offset + TEXT_CHUNK])[1:-1])
            write(b'"')
        elif type(item) is list:
            write(b"[")
            for index, child in enumerate(item):
                if index:
                    write(b",")
                encode(child)
            write(b"]")
        elif type(item) is dict:

            def key_order(key):
                checkpoint()
                return key.encode("utf-16be")

            write(b"{")
            for index, key in enumerate(sorted(item, key=key_order)):
                if index:
                    write(b",")
                encode(key)
                write(b":")
                encode(item[key])
            write(b"}")
        else:
            write(rfc8785.dumps(item))

    try:
        encode(value)
    except (ValueError, OverflowError, RecursionError) as error:
        raise _invalid("JSON 内容无法编码。") from error
    checkpoint()
    return output.getvalue()


def loads(
    data: bytes | str,
    *,
    max_bytes: int = MAX_FRAME,
    checkpoint: Callable[[], None] = lambda: None,
) -> object:
    checkpoint()
    if isinstance(data, str):
        try:
            parts = []
            for offset in range(0, len(data), TEXT_CHUNK):
                checkpoint()
                parts.append(data[offset : offset + TEXT_CHUNK].encode("utf-8"))
            data = b"".join(parts)
        except UnicodeEncodeError as error:
            raise _invalid("文本包含无效 Unicode 字符。") from error
    if len(data) > max_bytes:
        raise DeskWeaveError("LIMIT_EXCEEDED", "JSON 内容超过大小限制。")
    try:
        decoder = codecs.getincrementaldecoder("utf-8")()
        parts = []
        for offset in range(0, len(data), TEXT_CHUNK):
            checkpoint()
            parts.append(decoder.decode(data[offset : offset + TEXT_CHUNK]))
        parts.append(decoder.decode(b"", final=True))
        text = "".join(parts)
    except UnicodeDecodeError as error:
        raise _invalid("消息不是有效 UTF-8。") from error
    depth, quoted, escaped = 0, False, False
    for index, char in enumerate(text):
        if index % TEXT_CHUNK == 0:
            checkpoint()
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
            checkpoint()
            if key in result:
                raise _invalid("JSON 对象包含重复字段。")
            result[key] = value
        return result

    def constant(_value):
        raise _invalid("JSON 不接受非有限数。")

    decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=constant)

    def scan_checked(scan):
        def checked(source, offset):
            checkpoint()
            return scan(source, offset)

        return checked

    def array(source, scan):
        checkpoint()
        return json.decoder.JSONArray(source, scan_checked(scan))

    def object_(source, strict, scan, hook, pairs_hook, memo):
        checkpoint()
        return json.decoder.JSONObject(source, strict, scan_checked(scan), hook, pairs_hook, memo)

    decoder.parse_array = array
    decoder.parse_object = object_
    decoder.scan_once = scan_checked(json.scanner.py_make_scanner(decoder))
    try:
        value = decoder.decode(text)
    except (ValueError, RecursionError) as error:
        raise _invalid("JSON 语法无效。") from error
    dumps(value, max_bytes=max_bytes, checkpoint=checkpoint)
    return value


def digest(value: object, *, checkpoint: Callable[[], None] = lambda: None) -> str:
    return hashlib.sha256(dumps(value, max_bytes=33_554_432, checkpoint=checkpoint)).hexdigest()
