from __future__ import annotations

from dataclasses import dataclass, replace
from json import dumps as text_json
from types import MappingProxyType

from deskweave.json import dumps


class PathMissing(Exception):
    def __init__(self, index, sensitive=False):
        self.index = index
        self.sensitive = sensitive


@dataclass(frozen=True, slots=True)
class Value:
    data: object
    label: bool = False
    sensitive: bool = False
    typed: bool = False
    absent: frozenset = frozenset()
    present: bool = True
    json_size: int = 0
    text_size: int = 0
    depth: int = 0
    shape_ok: bool = True

    @classmethod
    def build(cls, data, label=False, *, typed=False, absent=frozenset(), present=True):
        children = (
            data.values() if isinstance(data, dict) else data if isinstance(data, tuple) else ()
        )
        sensitive = label or bool(absent) or any(child.sensitive for child in children)
        depth = max((child.depth + 1 for child in children), default=0)
        text_size = max((child.text_size for child in children), default=0)
        shape_ok = all(child.shape_ok for child in children)
        if isinstance(data, dict):
            size = 2 + max(0, len(data) - 1)
            for key, child in data.items():
                size += (
                    len(text_json(key, ensure_ascii=False).encode("utf-8")) + 1 + child.json_size
                )
                text_size = max(text_size, len(key.encode("utf-8")))
            shape_ok &= len(data) <= 1000
            stored = MappingProxyType(data)
        elif isinstance(data, tuple):
            size = 2 + max(0, len(data) - 1) + sum(child.json_size for child in data)
            shape_ok &= len(data) <= 10000
            stored = data
        elif type(data) is str:
            size = len(text_json(data, ensure_ascii=False).encode("utf-8"))
            text_size = len(data.encode("utf-8"))
            stored = data
        else:
            size, stored = len(dumps(data)), data
        return cls(
            stored, label, sensitive, typed, absent, present, size, text_size, depth, shape_ok
        )

    @property
    def result_safe(self):
        return (
            self.json_size <= 1_048_576
            and self.text_size <= 262_144
            and self.depth <= 64
            and self.shape_ok
        )

    @classmethod
    def freeze(cls, raw, labels=None):
        labels = labels or {}
        if labels.get("format") == 1:
            expanded = []
            for entry in labels["nodes"]:
                parent, key, flags, *extra = entry
                current = {
                    "s": bool(flags & 1),
                    "t": bool(flags & 2),
                    "p": not bool(flags & 4),
                    "a": extra[0] if extra else [],
                    "c": {},
                }
                if parent >= 0:
                    expanded[parent]["c"][str(key)] = current
                expanded.append(current)
            labels = expanded[0] if expanded else {}
        marked = labels.get("s", False)
        typed = labels.get("t", False)
        flags = {
            "typed": typed,
            "absent": frozenset(labels.get("a", [])),
            "present": labels.get("p", True),
        }
        children = labels.get("c", {})
        if type(raw) is dict:
            return cls.build(
                {key: cls.freeze(value, children.get(key)) for key, value in raw.items()},
                marked,
                **flags,
            )
        if type(raw) is list:
            return cls.build(
                tuple(cls.freeze(value, children.get(str(i))) for i, value in enumerate(raw)),
                marked,
                **flags,
            )
        return cls.build(raw, marked, **flags)

    @property
    def kind(self):
        if isinstance(self.data, MappingProxyType):
            return "object"
        if isinstance(self.data, tuple):
            return "array"
        return {
            type(None): "null",
            bool: "boolean",
            int: "number",
            float: "number",
            str: "string",
        }[type(self.data)]

    def declared(self, expected):
        return replace(self, typed=expected != "any")

    def marked(self, sensitive, *, conditional=False):
        if not sensitive:
            return self
        return replace(self, label=True, sensitive=True, typed=self.typed and not conditional)

    def size_sensitive(self):
        if not self.sensitive:
            return False
        if self.label and self.kind in {"string", "array", "object"}:
            return True
        children = (
            self.data.values()
            if self.kind == "object"
            else self.data
            if self.kind == "array"
            else ()
        )
        return any(child.size_sensitive() for child in children)

    def raw(self):
        if self.kind == "object":
            return {key: value.raw() for key, value in self.data.items()}
        if self.kind == "array":
            return [value.raw() for value in self.data]
        return self.data

    def labels(self):
        if not self.sensitive:
            return None
        nodes, pending = [], [(-1, None, self)]
        while pending:
            parent, key, value = pending.pop()
            identity = len(nodes)
            flags = int(value.label) | (int(value.typed) << 1) | (int(not value.present) << 2)
            entry = [parent, key, flags]
            if value.absent:
                entry.append(sorted(value.absent, key=str))
            nodes.append(entry)
            if value.label:
                continue
            children = (
                value.data.items()
                if value.kind == "object"
                else enumerate(value.data)
                if value.kind == "array"
                else ()
            )
            pending.extend(
                (identity, child_key, child) for child_key, child in children if child.sensitive
            )
        return {"format": 1, "nodes": nodes}

    def lookup(self, path):
        result, inherited = self, False
        for index, key in enumerate(path):
            inherited |= result.label or key in result.absent
            if result.kind == "object" and type(key) is str and key in result.data:
                result = result.data[key]
            elif (
                result.kind == "array"
                and type(key) in (int, float)
                and int(key) == key
                and 0 <= key < len(result.data)
            ):
                result = result.data[int(key)]
            else:
                raise PathMissing(index, inherited)
        return result.marked(inherited)

    def updated(
        self, path, replacement, *, observe_control=lambda sensitive: None, inherited=False
    ):
        if not path:
            return replacement
        inherited |= self.label
        observe_control(inherited)
        key, *rest = path
        if self.kind == "object" and type(key) is str:
            if rest and key not in self.data:
                raise PathMissing(0, inherited or key in self.absent)
            data = dict(self.data)
            data[key] = data.get(key, NULL).updated(
                rest, replacement, observe_control=observe_control, inherited=inherited
            )
            return Value.build(
                data, self.label, absent=self.absent - {key} if not rest else self.absent
            )
        if (
            self.kind == "array"
            and type(key) in (int, float)
            and int(key) == key
            and 0 <= key < len(self.data)
        ):
            data = list(self.data)
            data[int(key)] = data[int(key)].updated(
                rest, replacement, observe_control=observe_control, inherited=inherited
            )
            return Value.build(
                tuple(data), self.label, absent=self.absent - {key} if not rest else self.absent
            )
        raise PathMissing(0, inherited)

    def tainted(self, path):
        if not path or self.label:
            return self.marked(True, conditional=True)
        key, *rest = path
        if self.kind == "object" and type(key) is str:
            if key not in self.data:
                return Value.build(dict(self.data), self.label, absent=self.absent | {key})
            data = dict(self.data)
            data[key] = data[key].tainted(rest)
            return Value.build(data, self.label, absent=self.absent)
        if self.kind == "array" and type(key) in (int, float) and int(key) == key:
            if not 0 <= key < len(self.data):
                return Value.build(self.data, self.label, absent=self.absent | {int(key)})
            data = list(self.data)
            data[int(key)] = data[int(key)].tainted(rest)
            return Value.build(tuple(data), self.label, absent=self.absent)
        return self.marked(True, conditional=True)

    def projection(self):
        result = {"valueType": self.kind, "visibility": "redacted" if self.sensitive else "visible"}
        if not self.sensitive:
            result["value"] = self.raw()
        return result


NULL = Value.build(None)
EMPTY = Value.build({})
