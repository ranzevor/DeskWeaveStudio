"""公共协议使用的 RFC 8785 字节与摘要。"""
from __future__ import annotations
import hashlib
import json


def dumps(value):
    import rfc8785
    return rfc8785.dumps(value)


def digest(value):
    return hashlib.sha256(dumps(value)).hexdigest()


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("重复 JSON 键")
            result[key] = value
        return result
    def reject(value):
        raise ValueError("JSON 不接受非有限数")
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject)
    dumps(value)
    return value
