from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


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

    @classmethod
    def build(cls, data, label=False, *, typed=False, absent=frozenset(), present=True):
        children = (
            data.values() if isinstance(data, dict) else data if isinstance(data, tuple) else ()
        )
        sensitive = label or bool(absent) or any(child.sensitive for child in children)
        return cls(
            MappingProxyType(data) if isinstance(data, dict) else data,
            label,
            sensitive,
            typed,
            absent,
            present,
        )

    @classmethod
    def freeze(cls, raw, labels=None):
        labels = labels or {}
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
        return Value(
            self.data, self.label, self.sensitive, expected != "any", self.absent, self.present
        )

    def marked(self, sensitive, *, conditional=False):
        if not sensitive:
            return self
        return Value(
            self.data, True, True, self.typed and not conditional, self.absent, self.present
        )

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
        if self.label:
            return {
                "s": True,
                "t": self.typed,
                "a": sorted(self.absent, key=str),
                "p": self.present,
            }
        children = self.data.items() if self.kind == "object" else enumerate(self.data)
        return {
            "t": self.typed,
            "a": sorted(self.absent, key=str),
            "p": self.present,
            "c": {str(key): value.labels() for key, value in children if value.sensitive},
        }

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
