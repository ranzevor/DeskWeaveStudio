from __future__ import annotations

import json
import re
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from deskweave.errors import DeskWeaveError


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
    registry = Registry().with_resources(
        (identifier, Resource.from_contents(schema)) for identifier, schema in schemas.items()
    )
    return schemas, registry


@lru_cache(maxsize=128)
def validator(reference: str) -> Draft202012Validator:
    _, registry = contract_registry()
    return Draft202012Validator(
        {"$ref": reference}, registry=registry, format_checker=FormatChecker()
    )


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
            for child in validator(selected).iter_errors(error.instance):
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
    for error in validator(reference).iter_errors(value):
        for path, reason in expand(error):
            if path not in seen:
                seen.add(path)
                yield path, reason
    checkpoint()


def validate(reference: str, value: object) -> None:
    issue = next(schema_issues(reference, value), None)
    if issue is not None:
        path = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in issue[0])
        raise DeskWeaveError(
            "INVALID_ARGUMENT", "参数不符合接口要求。", details={"fieldPath": path}
        )


def method_schema(method: str, field: str) -> str:
    return "urn:deskweave:service:1#/$defs/" + method + "/properties/" + field
