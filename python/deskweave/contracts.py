from __future__ import annotations

import json
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


def validate(reference: str, value: object) -> None:
    error = next(validator(reference).iter_errors(value), None)
    if error is not None:
        path = "/" + "/".join(str(part) for part in error.absolute_path)
        raise DeskWeaveError(
            "INVALID_ARGUMENT", "参数不符合接口要求。", details={"fieldPath": path}
        )


def method_schema(method: str, field: str) -> str:
    return "urn:deskweave:service:1#/$defs/" + method + "/properties/" + field
