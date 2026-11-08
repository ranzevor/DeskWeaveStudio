from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from deskweave.errors import DeskWeaveError
from deskweave.json import dumps, json_pointer

_checkpoint = ContextVar("schema_checkpoint", default=lambda: None)


def _budgeted(keyword):
    def check(validator, rule, instance, schema):
        checkpoint = _checkpoint.get()
        checkpoint()
        yield from keyword(validator, rule, instance, schema)
        checkpoint()

    return check


BudgetValidator = validators.extend(
    Draft202012Validator,
    {name: _budgeted(keyword) for name, keyword in Draft202012Validator.VALIDATORS.items()},
)


@lru_cache(maxsize=1)
def contract_registry() -> tuple[dict, Registry]:
    directory = Path(__file__).with_name("contracts")
    if not directory.is_dir():
        directory = Path(__file__).resolve().parents[2] / "contracts"
    schemas = {}
    for path in directory.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[schema["$id"]] = schema
    if "urn:deskweave:service:1" not in schemas:
        raise RuntimeError("缺少 Engine 公共契约。")
    resources = []
    for identifier, schema in schemas.items():
        if schema["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise RuntimeError("公共契约使用了不支持的 JSON Schema 版本。")
        # 固定公共契约的方言，避免跨资源 evolve 切回不带预算的验证器。
        contents = {key: value for key, value in schema.items() if key != "$schema"}
        resources.append(
            (identifier, Resource.from_contents(contents, default_specification=DRAFT202012))
        )
    registry = Registry().with_resources(resources)
    return schemas, registry


@lru_cache(maxsize=1)
def contract_digest() -> str:
    schemas, _ = contract_registry()
    documents = {"schema:" + identity: contents for identity, contents in schemas.items()}
    directory = Path(__file__).with_name("contracts")
    if not directory.is_dir():
        directory = Path(__file__).resolve().parents[2] / "contracts"
    for path in directory.glob("*.json"):
        if not path.name.endswith(".schema.json"):
            documents["catalog:" + path.name] = json.loads(path.read_text(encoding="utf-8"))
    return hashlib.sha256(dumps(documents, max_bytes=32 * 1024 * 1024)).hexdigest()


@lru_cache(maxsize=128)
def validator(reference: str) -> Draft202012Validator:
    _, registry = contract_registry()
    return BudgetValidator({"$ref": reference}, registry=registry, format_checker=FormatChecker())


def _errors(reference, value, checkpoint):
    errors = validator(reference).iter_errors(value)
    while True:
        token = _checkpoint.set(checkpoint)
        try:
            checkpoint()
            error = next(errors, None)
        finally:
            _checkpoint.reset(token)
        if error is None:
            return
        yield error


def schema_contents(reference: str) -> dict:
    schemas, _ = contract_registry()
    annotations = {}
    while True:
        identity, _, pointer = reference.partition("#")
        value = schemas[identity]
        for part in pointer.lstrip("/").split("/") if pointer else ():
            value = value[part.replace("~1", "/").replace("~0", "~")]
        if "$ref" not in value:
            return {**value, **annotations}
        annotations = {**{key: item for key, item in value.items() if key != "$ref"}, **annotations}
        reference = value["$ref"]
        if reference.startswith("#"):
            reference = identity + reference


def value_roots(reference: str, value: object, checkpoint: Callable[[], None] = lambda: None):
    def visit(schema, item, path, base):
        pending = [(schema, base)]
        shapes = []
        while pending:
            checkpoint()
            current, identity = pending.pop()
            if type(current) is not dict:
                continue
            target = current.get("$ref")
            if target:
                target = identity + target if target.startswith("#") else target
                if target in {
                    "urn:deskweave:values:1#/$defs/value",
                    "urn:deskweave:values:1#/$defs/expressionValue",
                }:
                    yield path, item
                    return
                if target == "urn:deskweave:values:1#/$defs/raw":
                    continue
                pending.append((schema_contents(target), target.partition("#")[0]))
            shapes.append((current, identity))
            for keyword in ("allOf", "anyOf", "oneOf"):
                pending.extend((branch, identity) for branch in current.get(keyword, []))
        for current, identity in shapes:
            if type(item) is dict:
                properties = current.get("properties", {})
                for key, child in item.items():
                    child_schema = properties.get(key, current.get("additionalProperties"))
                    if type(child_schema) is dict:
                        yield from visit(child_schema, child, (*path, key), identity)
            elif type(item) is list:
                prefix = current.get("prefixItems", [])
                for index, child in enumerate(item):
                    child_schema = prefix[index] if index < len(prefix) else current.get("items")
                    if type(child_schema) is dict:
                        yield from visit(child_schema, child, (*path, index), identity)

    seen = set()
    for path, item in visit({"$ref": reference}, value, (), reference.partition("#")[0]):
        checkpoint()
        if path not in seen:
            seen.add(path)
            yield path, item


def schema_issues(reference: str, value: object, checkpoint: Callable[[], None] = lambda: None):
    def expand(error, prefix=()):
        checkpoint()
        path = prefix + tuple(error.absolute_path)
        if type(error.schema) is not dict:
            yield path, "schema"
            return
        if error.schema.get("$id") == "urn:deskweave:actions:1":
            if type(error.instance) is not dict:
                yield path, "type"
                return
            action = error.instance.get("action")
            if type(action) is not str or action not in error.schema["$defs"]:
                yield (*path, "action"), "action"
                return
            selected = "urn:deskweave:actions:1#/$defs/" + action
            found = False
            for child in _errors(selected, error.instance, checkpoint):
                found = True
                yield from expand(child, path)
            if not found:
                yield path, error.validator
            return
        if error.context and error.validator in {"oneOf", "anyOf"}:
            groups = {}
            for child in error.context:
                groups.setdefault(child.schema_path[0], []).append(child)
            base = tuple(error.absolute_path)
            candidates = [
                group
                for group in groups.values()
                if not any(
                    child.validator == "type"
                    and tuple(child.absolute_path) == base
                    or child.validator in {"const", "enum"}
                    and tuple(child.absolute_path)
                    in [(*base, key) for key in ("op", "action", "kind", "mode")]
                    for child in group
                )
            ]
            if not candidates and type(error.instance) is dict:
                for key in ("op", "kind", "mode"):
                    if key in error.instance and all(
                        any(
                            child.validator in {"const", "enum"}
                            and tuple(child.absolute_path) == (*base, key)
                            for child in group
                        )
                        for group in groups.values()
                    ):
                        yield (*path, key), "enum"
                        return
            if type(error.instance) is dict:
                for marker in ("$expr", "$literal"):
                    if marker in error.instance:
                        marked = [
                            group
                            for group in candidates
                            if any(
                                tuple(child.absolute_path)[: len(base) + 1] == (*base, marker)
                                for child in group
                            )
                        ]
                        if marked:
                            candidates = marked
            if len(candidates) == 1:
                for child in candidates[0]:
                    yield from expand(child, prefix)
                return
        if error.validator == "required":
            for name in error.validator_value:
                if name not in error.instance:
                    yield (*path, name), "required"
        elif error.validator == "additionalProperties" and error.validator_value is False:
            for name in error.instance:
                if name not in error.schema.get("properties", {}) and not any(
                    re.search(pattern, name)
                    for pattern in error.schema.get("patternProperties", {})
                ):
                    yield (*path, name), "additionalProperties"
        else:
            yield path, error.validator

    seen = set()
    for error in _errors(reference, value, checkpoint):
        for path, reason in expand(error):
            if path not in seen:
                seen.add(path)
                yield path, reason
    checkpoint()


def validate(
    reference: str, value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> None:
    issue = next(schema_issues(reference, value, checkpoint), None)
    if issue is not None:
        path = json_pointer(issue[0])
        raise DeskWeaveError(
            "INVALID_ARGUMENT", "参数不符合接口要求。", details={"fieldPath": path}
        )


def method_schema(method: str, field: str) -> str:
    return "urn:deskweave:service:1#/$defs/" + method + "/properties/" + field
