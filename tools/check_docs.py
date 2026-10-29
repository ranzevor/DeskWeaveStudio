"""检查文档链接、需求追踪、公共契约与设计样例。"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from design_contract_checks import settings_errors, management_errors
from canonical_json import dumps as canonical_bytes, digest as canonical_digest
from authoring_contract_checks import authoring_errors
from recording_contract_checks import recording_errors, trajectory_source_errors
from platform_contract_checks import platform_errors
from query_contract_checks import query_errors, catalog_errors as query_catalog_errors
from service_alignment_checks import alignment_errors, alignment_regressions
from mcp_protocol_checks import protocol_checks

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", ".venv-docs", "__pycache__", "node_modules", "target", "dist", ".cache"}
ERRORS: list[str] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        ERRORS.append(message)


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"重复 JSON 键：{key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"非法 JSON 数值：{value}")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)


def headings(text: str) -> set[str]:
    result = set(re.findall(r'<a id="([^"]+)">', text))
    occurrences: dict[str, int] = {}
    for title in re.findall(r"^#{1,6} (.+)$", text, re.M):
        slug = re.sub(r"[^\w\s-]", "", title.lower().strip(), flags=re.U)
        slug = re.sub(r"\s", "-", slug)
        count = occurrences.get(slug, 0)
        occurrences[slug] = count + 1
        result.add(slug if not count else f"{slug}-{count}")
    return result


def inside(path: Path, root: Path = ROOT) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def workflow_errors(data: dict) -> list[str]:
    errors = []
    nodes = data["nodes"]
    by_id = {node["id"]: node for node in nodes}
    if len(by_id) != len(nodes):
        errors.append("节点身份重复")
    if data["entry"] not in by_id:
        errors.append("入口不存在")
    edges: dict[str, list[str]] = {}
    expected_resource = {
        "flow.call": ("workflow", "workflow"),
        "flow.repeat": ("workflow", "workflow"),
        "flow.loop": ("workflow", "workflow"),
        "vision.match": ("image", "image"),
        "input.trajectory": ("trajectory", "trajectory"),
    }
    for node in nodes:
        edges[node["id"]] = [target for target in node["on"].values() if target is not None]
        if any(target not in by_id for target in edges[node["id"]]):
            errors.append("出口不存在")
        target = node["params"].get("target")
        if target is not None and target not in data["targets"]:
            errors.append("目标槽不存在")
        if node["action"] in expected_resource:
            field, kind = expected_resource[node["action"]]
            resource = data["resources"].get(node["params"][field])
            if resource is None or resource["kind"] != kind:
                errors.append("资源缺失或类型错误")
    if data.get("kind", "workflow") != "loopBody" and any(node["action"] in {"flow.break", "flow.continue"} for node in nodes):
        errors.append("循环控制需要 loopBody 定义")
    visited, active = set(), set()
    stack = [(data["entry"], False)]
    while stack:
        key, exiting = stack.pop()
        if exiting:
            active.remove(key)
            continue
        if key in active:
            errors.append("普通节点图存在回边")
            continue
        if key in visited or key not in by_id:
            continue
        visited.add(key)
        active.add(key)
        stack.append((key, True))
        stack.extend((target, False) for target in reversed(edges.get(key, [])))
    if visited != set(by_id):
        errors.append("存在不可达节点")
    for parameter in data["parameters"].values():
        if "default" not in parameter:
            continue
        default, kind = parameter["default"], parameter["type"]
        valid = {
            "string": isinstance(default, str),
            "boolean": isinstance(default, bool),
            "integer": isinstance(default, int) and not isinstance(default, bool),
            "number": isinstance(default, (int, float)) and not isinstance(default, bool),
            "array": isinstance(default, list),
            "object": isinstance(default, dict),
        }[kind]
        if not valid:
            errors.append("参数默认值类型错误")
    return errors


def trajectory_errors(data: dict) -> list[str]:
    errors, held = trajectory_source_errors(data), set()
    last = -1
    for event in data["events"]:
        if event["atUs"] < last:
            errors.append("轨迹时间逆序")
        last = event["atUs"]
        kind = event["kind"]
        if data["coordinateMode"] == "physical" and kind == "pointer.delta":
            errors.append("坐标模式冲突")
        if data["coordinateMode"] == "relative" and kind == "pointer.move":
            errors.append("坐标模式冲突")
        if kind in ("key.down", "key.up", "pointer.button"):
            key = ("key", event["key"]) if kind.startswith("key.") else ("button", event["button"])
            down = kind == "key.down" or event.get("state") == "down"
            if down:
                if key in held:
                    errors.append("重复按下")
                held.add(key)
            else:
                if key not in held:
                    errors.append("释放了未按下输入")
                held.discard(key)
    if held:
        errors.append("轨迹结束仍有输入按住")
    return errors


def package_errors(data: dict, base: Path, validators=None) -> list[str]:
    errors, keys, paths = [], set(), set()
    resources = {item["key"]: item for item in data["resources"]}
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved |= {prefix + number for prefix in ("COM", "LPT") for number in "123456789¹²³"}
    for item in data["resources"]:
        raw_path = item["path"]
        parts = raw_path.split("/")
        if (
            any(c in raw_path for c in '\\\\:<>"|?*')
            or any(ord(c) < 32 or ord(c) == 127 for c in raw_path)
            or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)
            or any(part.split(".")[0].upper() in reserved for part in parts)
        ):
            errors.append("任务包路径不合法")
            continue
        target = base.joinpath(*parts)
        if item["key"] in keys or raw_path.casefold() in paths:
            errors.append("任务包对象或路径重复")
        keys.add(item["key"])
        paths.add(raw_path.casefold())
        linked = False
        candidate = base
        for part in parts:
            candidate = candidate / part
            linked |= candidate.is_symlink() or candidate.is_junction()
        if not inside(target, base) or linked:
            errors.append("任务包路径越界或含链接")
            continue
        if not target.is_file():
            errors.append("任务包对象缺失")
            continue
        content = target.read_bytes()
        if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            errors.append("任务包内容不符")
        if item["kind"] in {"workflow", "trajectory"}:
            if item["mediaType"] != "application/json":
                errors.append("任务包媒体类型错误")
                continue
            try:
                definition = load_json(target)
            except (ValueError, UnicodeError):
                errors.append("任务包对象不是有效 JSON")
                continue
            if validators and list(validators[item["kind"] + ".schema.json"].iter_errors(definition)):
                errors.append("任务包对象结构错误")
                continue
            errors.extend(workflow_errors(definition) if item["kind"] == "workflow" else trajectory_errors(definition))
            if item["kind"] == "workflow":
                errors.extend(package_binding_errors(definition, item, resources))
    if sum(item["bytes"] for item in data["resources"]) > 1073741824:
        errors.append("任务包总大小超限")
    if data["entry"] not in resources or resources[data["entry"]]["kind"] != "workflow":
        errors.append("任务包入口不完整")
    return errors



def mcp_config_errors(data: dict) -> list[str]:
    errors = []
    if data["transport"] == "stdio":
        return errors
    host = data["host"].lower()
    if any(char in host for char in "*/\\@?#") or any(char.isspace() for char in host):
        errors.append("监听地址格式错误")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and not data["tls"]["enabled"]:
        errors.append("非回环监听需要 TLS")
    tls = data["tls"]
    if tls["enabled"] and not (tls["certificateGrant"] and tls["privateKeyGrant"]):
        errors.append("TLS 需要证书与私钥能力")
    if not tls["enabled"] and (tls["certificateGrant"] or tls["privateKeyGrant"]):
        errors.append("关闭 TLS 时不得保留证书能力")
    for value in data["allowedHosts"]:
        if any(char in value for char in "*/\\@?#") or any(char.isspace() for char in value):
            errors.append("Host 必须为明确的主机名称")
    for value in data["allowedOrigins"]:
        try:
            origin = urlsplit(value)
            origin.port
        except ValueError:
            errors.append("Origin 地址格式错误")
            continue
        if (origin.scheme not in {"http", "https"} or not origin.hostname or "*" in value
                or origin.username or origin.password or origin.path or origin.query or origin.fragment):
            errors.append("Origin 必须为明确的 HTTP 来源")
    return errors


def vision_errors(data: dict, kind: str) -> list[str]:
    errors = []
    if kind == "observationRecord":
        checked = data["checkedAt"]
        captured = data["capturedAt"]
        completed = data["completedAt"]
        if checked > completed:
            errors.append("检查时间晚于完成时间")
        if captured is not None and not checked <= captured <= completed:
            errors.append("采集时间不在检查与完成之间")
    if kind == "observationSession":
        if data.get("stoppedAt") is not None and data["startedAt"] > data["stoppedAt"]:
            errors.append("会话结束时间早于开始时间")
    if kind == "extractionOptions":
        if data["minArea"] > data["maxArea"]:
            errors.append("最小面积大于最大面积")
        if data["minAspectRatio"] > data["maxAspectRatio"]:
            errors.append("最小宽高比大于最大宽高比")
    if kind == "connectionOptions" and data.get("imagePayloadTemplate") is not None:
        pending = [(data["imagePayloadTemplate"], 0)]
        allowed = {"imageDataUrl", "imageBase64", "detail"}
        while pending:
            value, depth = pending.pop()
            if depth > 16:
                errors.append("图像模板嵌套过深")
                break
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
            elif isinstance(value, str) and ("{" in value or "}" in value):
                match = re.fullmatch(r"\{\{([A-Za-z][A-Za-z0-9]*)\}\}", value)
                if not match or match[1] not in allowed:
                    errors.append("图像模板包含未知或非完整字符串占位符")
    if kind == "observationPlan" and data.get("outputSchema") is not None:
        schema = data["outputSchema"]
        from jsonschema import Draft202012Validator, SchemaError
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            return ["输出结构不是有效 JSON Schema"]
        allowed_keys = {"type", "properties", "required", "additionalProperties", "items", "enum", "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties", "description", "title", "$defs", "$ref", "oneOf", "anyOf", "allOf", "not"}
        pending = [(schema, (), 0)]
        references = {}
        while pending:
            value, location, depth = pending.pop()
            if depth > 16:
                errors.append("输出结构嵌套过深")
                break
            if isinstance(value, bool):
                continue
            if not isinstance(value, dict):
                errors.append("输出结构节点必须为 Schema 对象")
                continue
            if set(value) - allowed_keys:
                errors.append("输出结构包含不支持的关键字")
            if "$ref" in value:
                reference = value["$ref"]
                if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                    errors.append("输出结构只接受本地定义引用")
                else:
                    references[location] = reference[2:].split("/")
            for key in ("properties", "$defs"):
                pending.extend((child, location + (key, name), depth + 1) for name, child in value.get(key, {}).items())
            for key in ("items", "additionalProperties", "not"):
                if key in value:
                    pending.append((value[key], location + (key,), depth + 1))
            for key in ("oneOf", "anyOf", "allOf"):
                pending.extend((child, location + (key, str(i)), depth + 1) for i, child in enumerate(value.get(key, [])))
        def resolve(parts):
            current = schema
            try:
                for part in parts:
                    current = current[part.replace("~1", "/").replace("~0", "~")]
                return current
            except (KeyError, TypeError):
                return None
        def visit(value, active, depth=0):
            if depth > 32:
                return True
            if isinstance(value, dict):
                if "$ref" in value:
                    reference = value["$ref"]
                    if reference in active:
                        return True
                    target = resolve(reference[2:].split("/")) if isinstance(reference, str) and reference.startswith("#/$defs/") else None
                    if target is None:
                        errors.append("输出结构引用不存在")
                    elif visit(target, active | {reference}, depth + 1):
                        return True
                children = []
                for key in ("properties", "$defs"):
                    children.extend(value.get(key, {}).values())
                for key in ("items", "additionalProperties", "not"):
                    if key in value:
                        children.append(value[key])
                for key in ("oneOf", "anyOf", "allOf"):
                    children.extend(value.get(key, []))
                return any(visit(child, active, depth + 1) for child in children)
            if isinstance(value, list):
                return any(visit(child, active, depth + 1) for child in value)
            return False
        if visit(schema, set()):
            errors.append("输出结构存在递归引用或超出深度")
    return errors




def draft_errors(data: dict, workflow_validator) -> list[str]:
    errors = []
    expected, edit = data["expectedEditVersion"], data["editVersion"]
    if expected is not None and edit <= expected:
        errors.append("草稿编辑代次必须超过预期已保存代次")
    model, model_edit = data["lastValidModel"], data["modelEditVersion"]
    if model_edit is not None and model_edit > edit:
        errors.append("模型代次不能超过当前编辑代次")
    def parsed_source(text):
        try:
            return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
        except (ValueError, TypeError, RecursionError):
            return None
    inline_model = model.get("value") if isinstance(model, dict) and model["kind"] == "inline" else None
    if inline_model:
        historical = parsed_source(inline_model["source"])
        if historical != inline_model["definition"]:
            errors.append("最后有效源码与模型定义不一致")
        if inline_model["definition"]["id"] != data["workflowId"]:
            errors.append("最后有效模型属于另一流程")
        if list(workflow_validator.iter_errors(inline_model["definition"])):
            errors.append("最后有效模型的结构无效")
    if data["source"]["kind"] == "inline":
        current = parsed_source(data["source"]["text"])
        valid = isinstance(current, dict) and not list(workflow_validator.iter_errors(current))
        if valid:
            if current["id"] != data["workflowId"]:
                errors.append("当前草稿属于另一流程")
            if model is None or model_edit != edit:
                errors.append("有效源码必须保存同代次模型")
            if inline_model and current != inline_model["definition"]:
                errors.append("当前有效源码与保存模型不一致")
        elif model is not None and model_edit >= edit:
            errors.append("无效源码的 modelEditVersion 必须小于 editVersion")
    return errors


def operation_errors(snapshot: dict, context=None) -> list[str]:
    errors = []
    if snapshot["state"] != "succeeded":
        return errors
    result = snapshot["result"]
    errors.extend(query_errors(result, "producer"))
    if snapshot["method"] == "asset.import" and result["entryWorkflow"] is not None:
        entry = result["entryWorkflow"]
        if not any(row["kind"] == "workflow" and row["objectId"] == entry["workflowId"] and row["version"] == entry["revision"] for row in (context or {}).get("objects", [])):
            errors.append("导入入口未指向本次实际导入的流程版本")
    if snapshot["method"] == "vision.observe":
        errors.extend(vision_errors(result, "observationRecord"))
    return errors


def mcp_catalog_checks(schemas: dict, registry, validator_class, formats) -> None:
    catalog = load_json(ROOT / "contracts/mcp-catalog.json")
    service = schemas["service.schema.json"]["$defs"]
    permissions = set(schemas["mcp.schema.json"]["$defs"]["tokenGrant"]["properties"]["permissions"]["items"]["enum"])
    local = set(catalog["authorization"]["localOnlyMethods"])
    dispatch = [row["$ref"].removeprefix("#/$defs/") for row in schemas["service.schema.json"]["oneOf"]]
    require(len(dispatch) == len(set(dispatch)) and set(dispatch) == set(service), "服务分发与方法定义不一致")
    for key, definition in service.items():
        require(definition["properties"]["method"]["const"] == key, "服务方法身份不一致")
    transport = {"event.subscribe", "event.read", "event.unsubscribe", "stream.subscribe", "stream.read", "stream.unsubscribe"}
    methods = {row["method"] for row in catalog["tools"]}
    require(not methods & local, "MCP 公开了本地管理方法")
    require(all(name in service for name in local | transport), "MCP 本地或订阅方法不存在")
    names = [row["name"] for row in catalog["tools"]]
    require(len(names) == len(set(names)) == len(methods), "MCP 工具身份或方法重复")
    require(set(service) == methods | local | transport | {row["method"] for row in catalog["resources"]}, "MCP 方法边界未覆盖全部服务契约")
    for row in catalog["tools"] + catalog["resources"]:
        method = row["method"]
        require(method in service, "MCP 引用了未知服务方法")
        require(set(row["permissions"]) <= permissions, "MCP 包含未知权限")
        for field, part in (("inputSchema", "params"), ("outputSchema", "result")) if "name" in row and "uriTemplate" not in row else (("resultSchema", "result"),):
            expected = "urn:deskweave:service:1#/$defs/" + method + "/properties/" + part
            require(row.get(field) == {"$ref": expected}, "MCP 方法 Schema 映射错误")
            registry.resolver().lookup(expected)
    for row in catalog["tools"]:
        if row["method"] == "run.resume":
            hidden = {"resumePlanId", "confirmationGrant"}
            require(set(row.get("forbiddenParams", [])) == hidden, "MCP 未拒绝本地继续材料")
            projection = row.get("publishedInputSchema")
            original = service[row["method"]]["properties"]["params"]
            require(isinstance(projection, dict), "MCP 缺少公开继续参数投影")
            if isinstance(projection, dict):
                require(set(projection["properties"]) == set(original["properties"]) - hidden, "MCP 继续参数投影错误")
                require(projection.get("additionalProperties") is False and projection["required"] == original["required"], "MCP 参数投影约束不完整")
                require(all(projection["properties"][key] == original["properties"][key] for key in projection["properties"]), "MCP 参数投影改变字段类型")
                check = validator_class(projection, registry=registry, format_checker=formats)
                valid = {"runId": "run-example", "expectedControlVersion": 7}
                require(not list(check.iter_errors(valid)), "公开继续参数被错误拒绝")
                for extra in ({"resumePlanId": "plan-example"}, {"confirmationGrant": "grant-example"}, {"resumePlanId": "plan-example", "confirmationGrant": "grant-example"}):
                    require(bool(list(check.iter_errors({**valid, **extra}))), "公开继续入口接受本地材料注入")
    for row in catalog["resources"]:
        require(row["uriTemplate"].startswith("deskweave://"), "MCP 资源 URI 错误")
        props = service[row["method"]]["properties"]["params"].get("properties", {})
        supplied = set(row["defaults"]) | set(row["pathBindings"].values()) | set(row["queryParameters"])
        needed = set(service[row["method"]]["properties"]["params"].get("required", []))
        require(needed <= supplied <= set(props), "MCP 资源参数映射不完整")
    shared_prompt_fields = schemas["mcp.schema.json"]["$defs"]["promptArguments"]["properties"]
    require(set(shared_prompt_fields) == set().union(*(set(prompt["argumentSchema"]["properties"]) for prompt in catalog["prompts"])), "MCP Prompt 公共参数字段不一致")
    for prompt in catalog["prompts"]:
        schema = prompt["argumentSchema"]
        validator_class.check_schema(schema)
        checker = validator_class(schema, registry=registry, format_checker=formats)
        required = set(schema["required"])
        require(required == {arg["name"] for arg in prompt["arguments"] if arg["required"]}, "MCP Prompt 必填参数不一致")
        valid = {name: "示例" for name in schema["properties"]}
        require(not list(checker.iter_errors(valid)), "合法 Prompt 参数被拒绝")
        require(bool(list(checker.iter_errors({**valid, "unexpected": "x"}))), "Prompt 未拒绝未知参数")
        if required:
            missing = dict(valid)
            missing.pop(next(iter(required)))
            require(bool(list(checker.iter_errors(missing))), "Prompt 未拒绝缺失参数")
        for message in prompt["messages"]:
            placeholders = set(re.findall(r"\{(\w+)\}", message["content"]["textTemplate"]))
            require(placeholders <= set(schema["properties"]), "Prompt 包含未知占位符")
        require(prompt["sideEffects"] == "none", "读取 Prompt 不得产生副作用")
    for error in query_catalog_errors(schemas, catalog):
        require(False, error)
    print(f"MCP：{len(names)} 个工具，{len(catalog['resources'])} 个资源，{len(catalog['prompts'])} 个预设任务；13 组参数边界")



def schema_target(node: dict, registry, base: str) -> dict:
    resolver = registry.resolver(base_uri=base)
    while "$ref" in node:
        resolved = resolver.lookup(node["$ref"])
        node, resolver = resolved.contents, resolved.resolver
    return node


def cli_catalog_checks(schemas: dict, registry, validator_class, formats) -> None:
    catalog = load_json(ROOT / "contracts/cli-catalog.json")
    require(catalog["formatVersion"] == "1.0" and catalog["executable"] == "deskweave", "CLI 版本或入口不一致")
    require(catalog["workspaceOption"] == "locatorOnly", "CLI 将定位参数当作授权")
    local_permissions = set(catalog.get("localPermissions", []))
    require(local_permissions == {"workspace.manage"}, "CLI 本地管理能力声明不一致")
    require(catalog.get("waitPermissionPolicy") == "beforeAdmissionWhenWaitOrForegroundHold", "CLI 未在创建对象前核对等待权限")
    commands = catalog["commands"]
    names = [row["name"] for row in commands]
    require(len(names) == len(set(names)), "CLI 子命令重复")
    service = schemas["service.schema.json"]["$defs"]
    allowed = set(schemas["mcp.schema.json"]["$defs"]["tokenGrant"]["properties"]["permissions"]["items"]["enum"])
    for row in commands:
        require(row["method"] in service, "CLI 引用未知服务方法")
        params = schema_target(service[row["method"]]["properties"]["params"], registry, "urn:deskweave:service:1")
        for key, part in (("inputSchema", "params"), ("outputSchema", "result")):
            expected = "urn:deskweave:service:1#/$defs/" + row["method"] + "/properties/" + part
            require(row[key] == {"$ref": expected}, "CLI 未复用服务契约：" + row["name"])
            registry.resolver().lookup(expected)
        require(set(row["permissions"]) <= allowed | local_permissions, "CLI 使用未知能力：" + row["name"])
        if set(row["permissions"]) & local_permissions:
            local_methods = load_json(ROOT / "contracts/mcp-catalog.json")["authorization"]["localOnlyMethods"]
            require(row["method"] in local_methods, "CLI 本地管理边界不一致")
        require(row["completion"] in {"result", "operation", "run", "listener", "stdio"}, "CLI 完成口径无效")

        result = schema_target(service[row["method"]]["properties"]["result"], registry, "urn:deskweave:service:1")
        projection = row.get("publishedOutputSchema")
        bindings = row.get("localResultBindings", {})
        if projection is not None:
            validator_class.check_schema(projection)
            published = set(projection.get("properties", {}))
            original = set(result.get("properties", {}))
            require(published.isdisjoint(bindings) and published | set(bindings) == original, "CLI 输出投影遗漏字段或泄漏本地能力")
            require(set(projection.get("required", [])) == set(result.get("required", [])) - set(bindings), "CLI 输出投影必填不一致")
            require(projection.get("additionalProperties") is False, "CLI 输出投影允许未知字段")
            require(all(projection["properties"][key] == result["properties"][key] for key in published), "CLI 输出投影改变字段含义")
        else:
            require(not bindings, "CLI 本地结果绑定缺少输出投影")
        if row["completion"] in {"operation", "run"}:
            expected_wait = "operation.get" if row["completion"] == "operation" else "run.snapshot"
            require(row.get("waitMethod") == expected_wait, "CLI 等待方法不一致")
            expected_policy = "holdUnlessDetached" if row["method"] == "run.start" else "holdUntilCleanup" if row["method"] == "input.submit" else "receiptOnly"
            require(row.get("foregroundPolicy") == expected_policy, "CLI 前台生命周期不一致")
            if expected_policy == "holdUnlessDetached":
                require(row.get("defaults", {}).get("detached") is False, "CLI 默认运行生命周期不一致")

        require(isinstance(row["authorization"], str) and bool(row["authorization"]), "CLI 缺少对象授权规则")

        for key, value in row.get("defaults", {}).items():
            require(key in params.get("properties", {}), "CLI 默认字段不存在")
            if key in params.get("properties", {}):
                validator_class(params["properties"][key], registry=registry, format_checker=formats).validate(value)
        if "waitMethod" in row:
            required_wait = {"run.snapshot": ["run.read"], "observation.session": ["observation.read"], "bridge.status": ["workspace.manage"], "operation.get": []}
            require(row["waitMethod"] in required_wait, "CLI 等待引用未知方法")
            require(row.get("waitPermissions") == required_wait.get(row["waitMethod"]), "CLI 等待权限不一致")
        if row["method"] == "observation.start":
            require(row.get("foregroundPolicy") == "holdUnlessDetached" and row.get("defaults", {}).get("detached") is False, "CLI 观察所有者退出策略不一致")
            require(row.get("waitMethod") == "observation.session" and row.get("completionSchema") == {"$ref": "urn:deskweave:service:1#/$defs/observation.session/properties/result"}, "CLI 缺少观察最终状态查询")

        require(set(row.get("fixedParams", {})) <= set(params.get("properties", {})), "CLI 固定参数不存在")
        require(set(row.get("localBindings", {})) <= set(params.get("properties", {})), "CLI 本地绑定字段不存在")
        require(set(row.get("forbiddenParams", [])) <= set(params.get("properties", {})), "CLI 禁入字段不存在")
        if row["method"] == "run.resume":
            require(set(row.get("forbiddenParams", [])) == {"resumePlanId", "confirmationGrant"}, "CLI 继续入口未拒绝本地确认材料")
        for key, value in row.get("fixedParams", {}).items():
            validator_class(params["properties"][key], registry=registry, format_checker=formats).validate(value)
        if row["completion"] in {"operation", "run", "listener"}:
            method = {"operation": "operation.get", "run": "run.snapshot", "listener": "bridge.status"}[row["completion"]]
            require(row.get("completionSchema") == {"$ref": "urn:deskweave:service:1#/$defs/" + method + "/properties/result"}, "CLI 缺少实际完成结果契约")
    print(f"CLI：{len(commands)} 条子命令，{len(set(row['method'] for row in commands))} 个服务方法；对象绑定与完成口径一致")




def settings_catalog_checks(schemas: dict, registry, validator_class, formats) -> None:
    catalog = load_json(ROOT / "contracts/settings-catalog.json")
    rows = catalog["keys"]
    names = [row["key"] for row in rows]
    require(len(names) == len(set(names)), "设置目录键重复")
    definitions = schemas["settings.schema.json"]["$defs"]
    values = definitions["values"]
    require(set(values["properties"]) == set(values["required"]) == set(names) and values["additionalProperties"] is False, "设置完整值与目录不一致")
    require(set(definitions["overrides"]["properties"]) == set(names) and definitions["overrides"]["additionalProperties"] is False, "设置覆盖与目录不一致")
    changes = definitions["change"]["oneOf"]
    sets = {row["properties"]["key"]["const"]: row["properties"]["value"] for row in changes if row["properties"]["op"] == {"const": "set"}}
    resets = next(row["properties"]["key"]["enum"] for row in changes if row["properties"]["op"] == {"const": "reset"})
    require(set(sets) == set(resets) == set(names), "设置修改与重置键集合不一致")
    for row in rows:
        name = row["key"]
        validator_class(row["valueSchema"], registry=registry, format_checker=formats).validate(row["default"])
        for platform, value in row.get("platformDefaults", {}).items():
            require(platform in {"windows", "macos", "linux"}, "设置默认值平台无效")
            validator_class(row["valueSchema"], registry=registry, format_checker=formats).validate(value)
        require(row["valueSchema"] == values["properties"][name] == sets[name], "设置类型定义不一致：" + name)
        require(bool(row["scopes"]) and set(row["scopes"]) <= {"user", "workspace"}, "设置作用域无效")
        require(row["activation"] in {"immediate", "nextRun", "restartAgent"}, "设置生效方式无效")
        for override in row["runOverrides"]:
            method, field = override.rsplit(".", 1)
            require(method in schemas["service.schema.json"]["$defs"], "设置覆盖引用不存在的方法")
            if method in schemas["service.schema.json"]["$defs"]:
                params = schema_target(schemas["service.schema.json"]["$defs"][method]["properties"]["params"], registry, "urn:deskweave:service:1")
                require(field in params.get("properties", {}), "设置覆盖引用不存在的参数")
    print(f"设置：{len(names)} 个键，类型、默认、作用域、覆盖入口与生效目录一致")


def operation_catalog_checks(schemas: dict, registry, validator_class, formats) -> None:
    catalog = load_json(ROOT / "contracts/operation-catalog.json")
    require(catalog["formatVersion"] == "1.0", "作业目录版本不一致")
    service = schemas["service.schema.json"]["$defs"]
    accepted = {
        method for method, definition in service.items()
        if definition["properties"]["result"].get("properties", {}).get("accepted") == {"const": True}
        and "operationId" in definition["properties"]["result"].get("required", [])
    }
    rows = catalog["operations"]
    methods = [row["method"] for row in rows]
    require(len(methods) == len(set(methods)) and set(methods) == accepted, "作业目录未精确覆盖异步方法")
    definitions = schemas["operation-results.schema.json"]["$defs"]
    snapshot = definitions["operationSnapshot"]
    require(set(snapshot["properties"]["method"]["enum"]) == accepted, "作业快照方法集合不一致")
    succeeded = next(row["then"] for row in snapshot["allOf"] if row["if"]["properties"]["state"] == {"const": "succeeded"})
    mapping = {row["properties"]["method"]["const"]: row["properties"]["result"] for row in succeeded["oneOf"]}
    require(set(mapping) == accepted and len(succeeded["oneOf"]) == len(accepted), "作业成功结果分发不完整")
    for method in accepted:
        expected = {"$ref": "urn:deskweave:operation-results:1#/$defs/" + method}
        require(mapping[method] == expected, "作业快照结果映射错误：" + method)
        registry.resolver().lookup(expected["$ref"])
    for row in rows:
        validator_class.check_schema(row["resultSchema"])
        if "$ref" in row["resultSchema"]:
            registry.resolver().lookup(row["resultSchema"]["$ref"])
    for method in ("operation.get", "operation.cancel"):
        require(service[method]["properties"]["result"] == {"$ref": "urn:deskweave:operation-results:1#/$defs/operationSnapshot"}, "作业查询或取消未采用完整快照")
    cases = load_json(ROOT / "examples/cases.json")
    covered = set()
    for case in cases:
        if case["expected"] == "pass" and case["schema"].split("#")[0] == "operation-results.schema.json":
            value = load_json(ROOT / "examples" / case["file"])
            if value["state"] == "succeeded":
                covered.add(value["method"])
                row = next(row for row in rows if row["method"] == value["method"])
                validator_class(row["resultSchema"], registry=registry, format_checker=formats).validate(value["result"])
    require(covered == accepted, "异步方法缺少完整成功结果样例")
    print(f"异步作业：{len(accepted)} 个方法的回执、最终结果及状态分发一致")


def control_digest(value):
    return canonical_digest(value)


def control_time(value):
    from datetime import datetime
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def pause_errors(value):
    errors = []
    cursor = value["cursor"]
    if cursor is not None and cursor["nextEventIndex"] > cursor["eventCount"]:
        errors.append("暂停游标超过源事件总数")
    if control_time(value["createdAt"]) >= control_time(value["expiresAt"]):
        errors.append("暂停期限没有覆盖创建时间")
    held = value["heldInputs"]
    ordered = sorted(held, key=lambda v: (v["kind"], v.get("key", v.get("button", ""))))
    if held != ordered:
        errors.append("暂停持有集合顺序不稳定")
    tokens = [v["targetToken"] for v in value["targets"]]
    if len(tokens) != len(set(tokens)) or tokens != sorted(tokens):
        errors.append("暂停目标重复或顺序不稳定")
    for name, field in (("heldInputs", "heldInputsHash"), ("targets", "targetStateHash")):
        if control_digest(value[name]) != value[field]:
            errors.append("暂停集合摘要不符")
    if value["inputTargetToken"] is not None and value["inputTargetToken"] not in tokens:
        errors.append("暂停输入目标不在授权目标集合")
    if (cursor is not None or held) and value["inputTargetToken"] is None:
        errors.append("暂停输入缺少唯一目标")
    selected = ["pauseId", "leaseId", "agentEpoch", "generation", "cursor", "heldInputsHash", "pointer", "targetStateHash", "inputTargetToken"]
    if control_digest({key: value[key] for key in selected}) != value["checkpointHash"]:
        errors.append("暂停检查点摘要不符")
    return errors


def execution_errors(data, kind, context=None):
    errors = []
    if kind == "pauseSnapshot":
        return pause_errors(data)
    if kind == "leaseSnapshot":
        snapshot = data["pauseSnapshot"]
        if snapshot is not None:
            errors.extend(pause_errors(snapshot))
            if any(snapshot[key] != data[key] for key in ("leaseId", "agentEpoch", "generation")):
                errors.append("租约与暂停检查点身份不符")
        return errors
    if kind == "resumePlan":
        if not context:
            return ["继续计划缺少实时核对上下文"]
        pause = context["pauseSnapshot"]
        errors.extend(pause_errors(pause))
        for key in ("pauseId", "agentEpoch", "generation"):
            if data[key] != pause[key]:
                errors.append("继续计划与实时检查点不符")
        if data["runId"] != context["runId"] or data["controlVersion"] != context["controlVersion"]:
            errors.append("继续计划与运行控制版本不符")
        if data["heldInputs"] != pause["heldInputs"]:
            errors.append("继续计划持有集合改变")
        if data["pointerToRestore"] is not None and data["pointerToRestore"] != pause["pointer"]:
            errors.append("继续指针目的地不符")
        if data["restoreTargetToken"] is not None:
            if data["restoreTargetToken"] != pause["inputTargetToken"] or data["restoreTargetToken"] not in {target["targetToken"] for target in pause["targets"]}:
                errors.append("继续恢复目标没有绑定暂停输入目标")
        elif data["heldInputs"] or data["pointerToRestore"]:
            errors.append("继续恢复输入缺少目标")
        if data["requiresConfirmation"] != bool(data["heldInputs"] or data["pointerToRestore"] or data["restoreTargetToken"]):
            errors.append("继续确认要求不符")
        expires = control_time(data["expiresAt"])
        prepared = control_time(context["preparedAt"])
        if not prepared < expires <= control_time(pause["expiresAt"]) or (expires-prepared).total_seconds() > 60:
            errors.append("继续计划有效期越界")
        if control_time(context["now"]) >= expires:
            errors.append("继续计划已过期")
        if context["currentAgentEpoch"] != pause["agentEpoch"] or context["currentGeneration"] != pause["generation"]:
            errors.append("原生代次已经改变")
        if context["currentTargets"] != pause["targets"]:
            errors.append("目标或拓扑已经改变")
        if context["planState"] != "prepared":
            errors.append("继续计划已经消耗或取消")
        selected = ["runId", "controlVersion", "pauseId", "agentEpoch", "generation", "heldInputs", "pointerToRestore", "restoreTargetToken", "expiresAt"]
        value = {key: data[key] for key in selected}
        value["checkpointHash"] = pause["checkpointHash"]
        if control_digest(value) != data["planHash"]:
            errors.append("继续计划摘要不符")
        return errors
    if kind == "resumeRequest":
        if not context:
            return ["继续请求缺少实时核对上下文"]
        plan = context["resumePlan"]
        errors.extend(execution_errors(plan, "resumePlan", context))
        params = data["params"]
        if params["runId"] != plan["runId"] or params["expectedControlVersion"] != plan["controlVersion"]:
            errors.append("继续请求运行控制版本不符")
        if plan["requiresConfirmation"]:
            if context.get("clientKind", "trustedHost") != "trustedHost":
                errors.append("当前认证来源不能使用本地确认能力")
            if params.get("resumePlanId") != plan["planId"] or params.get("confirmationGrant") != context["registeredGrant"]:
                errors.append("继续请求缺少绑定本地确认")
        elif params.get("resumePlanId", plan["planId"]) != plan["planId"]:
            errors.append("继续请求引用其他计划")
        return errors
    if kind in {"breakpointProfile", "breakpointUpdate"}:
        if not context:
            return ["断点缺少固定版本闭包"]
        value = data if kind == "breakpointProfile" else data["params"]
        if (value["workflowId"], value["revision"]) != (context["rootWorkflowId"], context["rootRevision"]):
            errors.append("断点配置根版本不符")
        allowed = {(row["workflowId"], row["revision"], node) for row in context["allowedNodes"] for node in row["nodeIds"]}
        identities = [(row["workflowId"], row["revision"], row["nodeId"]) for row in value["breakpoints"]]
        if len(identities) != len(set(identities)):
            errors.append("断点节点身份重复")
        if not set(identities) <= allowed:
            errors.append("断点不在固定版本闭包")
        if kind == "breakpointUpdate" and value["expectedVersion"] != context["currentVersion"]:
            errors.append("断点配置版本冲突")
        if kind == "breakpointProfile" and value["version"] == 0 and value["breakpoints"]:
            errors.append("初始断点配置必须为空")
        return errors
    return ["未知执行控制样例类型"]


def package_binding_errors(definition, item, resources):
    errors = []
    if "sourceId" in item and item["sourceId"] != definition["id"]:
        errors.append("任务包来源流程身份不符")
    bindings = item.get("bindings", {key: key for key in definition["resources"]})
    if set(bindings) != set(definition["resources"]):
        errors.append("任务包本地绑定没有准确覆盖依赖")
    for key, required in definition["resources"].items():
        mapped = bindings.get(key)
        if mapped not in resources or resources[mapped]["kind"] != required["kind"]:
            errors.append("任务包依赖闭包不完整或类型不符")
    return errors


def import_errors(data, kind, context, base):
    errors = []
    if not context:
        return ["导入映射缺少固定源资料"]
    fixture_paths = [context[key] for key in ("sourceManifest", "sourceFile", "mappingFile") if context.get(key)]
    fixture_paths.extend(context.get("targetFiles", {}).values())
    if any(not isinstance(value, str) or not inside(base / value, base) for value in fixture_paths):
        return ["导入来源或目标样例路径越界"]
    single_kind = context.get("inputKind")
    if single_kind in {"image", "trajectory", "workflow"}:
        source_path = base / context["sourceFile"]
        source_bytes = source_path.read_bytes()
        source_definition = json.loads(source_bytes) if single_kind == "workflow" else None
        if source_definition is not None and source_definition["resources"]:
            errors.append("独立流程文件必须附带依赖包")
        resources = {"input": {"key": "input", "path": source_path.name, "kind": single_kind, "bytes": len(source_bytes), "sha256": hashlib.sha256(source_bytes).hexdigest(), "sourceId": source_definition["id"] if source_definition is not None else None, "sourceVersion": None}}
        source_entry = "input" if single_kind == "workflow" else None
    else:
        source_path = base / context["sourceManifest"]
        manifest = load_json(source_path)
        resources = {row["key"]: row for row in manifest["resources"]}
        source_entry = manifest["entry"]
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != data["sourceManifestHash"]:
        errors.append("导入来源摘要不符")
    if kind == "plan":
        mapping_path = base / context["mappingFile"]
        mappings = load_json(mapping_path)
        if hashlib.sha256(mapping_path.read_bytes()).hexdigest() != data["mappingSha256"]:
            errors.append("导入预览映射资源摘要不符")
        if data["mappingCount"] != len(mappings) or data["previewMappings"] != mappings[:100]:
            errors.append("导入预览没有对应完整映射资源")
        if data["catalogVersion"] != context["currentCatalogVersion"]:
            errors.append("导入目录代次已经改变")
        if control_time(context["now"]) >= control_time(data["expiresAt"]):
            errors.append("导入计划已经过期")
    elif kind == "manifest":
        mappings = data["mappings"]
    else:
        return ["未知导入样例类型"]
    identities = [row["sourceKey"] for row in mappings]
    if len(identities) != len(set(identities)) or set(identities) != set(resources):
        errors.append("导入映射缺项或来源逻辑键重复")
    by_key = {row["sourceKey"]: row for row in mappings}
    for row in mappings:
        source = resources.get(row["sourceKey"])
        if source is None:
            continue
        source_file = source_path.parent / source["path"]
        if not inside(source_file, base):
            errors.append("导入源资源样例路径越界")
            continue
        source_bytes = source_file.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        if source_hash != source["sha256"] or source_hash != row["sourceSha256"] or len(source_bytes) != source["bytes"]:
            errors.append("导入映射没有保持原始字节摘要")
        if row["sourceKind"] != source["kind"] or row["targetKind"] != source["kind"]:
            errors.append("导入映射类型不符")
        if row["sourceId"] != source.get("sourceId") or row["sourceVersion"] != source.get("sourceVersion"):
            errors.append("导入映射来源身份或版本不符")
        target_file = base / context["targetFiles"][row["sourceKey"]]
        if hashlib.sha256(target_file.read_bytes()).hexdigest() != row["targetSha256"]:
            errors.append("导入目标内容摘要不符")
        if row["sourceKind"] == "workflow":
            target = load_json(target_file)
            expected = json.loads(source_bytes)
            expected["id"] = row["targetId"]
            if target != expected or row["targetId"] == row["sourceId"] or row["disposition"] != "created":
                errors.append("导入流程没有创建独立身份或改写了任意内容")
            local = source.get("bindings", {key: key for key in expected["resources"]})
            fixed = context.get("targetBindings", {}).get(row["sourceKey"], {})
            for key, global_key in local.items():
                mapped = by_key.get(global_key)
                if mapped is None:
                    errors.append("导入目标依赖映射不完整")
                    continue
                if mapped["targetKind"] == "workflow":
                    expected_binding = {"workflowId": mapped["targetId"], "revision": mapped["targetVersion"]}
                else:
                    expected_binding = {"assetVersion": mapped["targetVersion"]}
                if fixed.get(key) != expected_binding:
                    errors.append("导入依赖没有固定本次目标身份")
        elif row["sourceSha256"] != row["targetSha256"]:
            errors.append("导入资产字节被改写")
    entry = by_key.get(source_entry) if source_entry is not None else None
    expected_entry = {"workflowId": entry["targetId"], "revision": entry["targetVersion"]} if entry is not None else None
    if (source_entry is not None and entry is None) or data["entryWorkflow"] != expected_entry:
        errors.append("导入入口不是本次映射的目标版本")
    return errors


def boundary_checks(validators: dict) -> None:
    graph = load_json(ROOT / "examples/workflows/hello.json")
    graph["entry"] = "n0"
    graph["nodes"] = [
        {
            "id": f"n{i}", "title": "边界节点", "action": "time.wait",
            "params": {"durationMs": 0},
            "on": {"next": f"n{i + 1}"} if i < 999 else {},
        }
        for i in range(1000)
    ]
    require(not list(validators["workflow.schema.json"].iter_errors(graph)), "1,000 节点结构被错误拒绝")
    require(not workflow_errors(graph), "1,000 节点链应通过语义检查")
    cycle = deepcopy(graph)
    cycle["nodes"][-1]["on"] = {"next": "n0"}
    require(any("回边" in error for error in workflow_errors(cycle)), "深图回边未拒绝")
    oversized = deepcopy(graph)
    oversized["nodes"].append({"id": "overflow", "title": "超出节点", "action": "time.wait", "params": {"durationMs": 0}, "on": {}})
    require(bool(list(validators["workflow.schema.json"].iter_errors(oversized))), "1,001 节点未拒绝")
    manifest = load_json(ROOT / "examples/manifest.json")
    require(not package_errors(manifest, ROOT / "examples", validators), "合法任务包被错误拒绝")
    paths = [
        "../outside", "a/../x", "a//b", "./x", "a:b", "C:/x",
        "//server/share", "x\\\\a", "NUL.json", "CON", "com1.log",
        "LPT².txt", "a./b", "a /b", "a/\u0001b", "a/b.", "a/b ",
    ]
    for raw_path in paths:
        invalid = deepcopy(manifest)
        invalid["resources"][0]["path"] = raw_path
        structural = list(validators["package.schema.json"].iter_errors(invalid))
        rejected = bool(structural) or any("路径" in error for error in package_errors(invalid, ROOT / "examples", validators))
        require(rejected, f"危险包路径未拒绝：{raw_path!r}")
    segment = load_json(ROOT / "examples/messages/agent-segment.json")["params"]
    payload = {"targetToken": segment["targetToken"], "operation": segment["operation"]}
    canonical = canonical_bytes(payload)
    require(hashlib.sha256(canonical).hexdigest() == segment["payloadHash"], "输入段样例摘要不符")
    draft_write = load_json(ROOT / "examples/drafts/write-resource.json")
    draft_snapshot = load_json(ROOT / "examples/drafts/snapshot-invalid-source.json")
    for source, target in (("source-invalid.txt", "source"), ("model-content.json", "lastValidModel")):
        raw = (ROOT / "examples/drafts" / source).read_bytes()
        for descriptor in (draft_write[target], draft_snapshot["sourceResource" if target == "source" else "lastValidModelResource"]):
            require(descriptor["length"] == len(raw) and descriptor["sha256"] == hashlib.sha256(raw).hexdigest(), "草稿资源示例字节身份不一致")
    print(f"边界：3 组节点数量/深图检查，{len(paths)} 组危险路径，1 组输入段样例摘要")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts", action="store_true")
    args = parser.parse_args()
    files = [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP)]
    markdown = [p for p in files if p.suffix == ".md"]
    json_files = [p for p in files if p.suffix == ".json"]
    for path in files:
        if path.suffix in {".md", ".json", ".py", ".yml", ".yaml", ".txt"}:
            try:
                text = path.read_bytes().decode("utf-8")
                require(not text.startswith("\ufeff"), f"{path.relative_to(ROOT)}：不得使用 BOM")
                require("\r" not in text, f"{path.relative_to(ROOT)}：需使用 LF")
            except UnicodeError:
                ERRORS.append(f"{path.relative_to(ROOT)}：不是 UTF-8")
    for path in markdown:
        text = path.read_text(encoding="utf-8")
        fences = re.findall(r"^```.*$", text, re.M)
        require(len(fences) % 2 == 0, f"{path.relative_to(ROOT)}：代码围栏未闭合")
        plain = re.sub(r"(?ms)^```.*?^```[ \t]*$", "", text)
        for label in re.findall(r"\]\(([^)]+)\)", plain):
            label = label.strip("<>")
            parsed = urlsplit(label)
            if parsed.scheme:
                continue
            target = path.parent.joinpath(unquote(parsed.path)).resolve() if parsed.path else path
            require(inside(target), f"{path.relative_to(ROOT)}：本地链接越界")
            require(target.exists(), f"{path.relative_to(ROOT)}：链接缺失 {label}")
            if target.is_dir():
                target = target / "README.md"
                require(target.is_file(), f"{path.relative_to(ROOT)}：目录缺少 README.md {label}")
            if parsed.fragment and target.is_file() and target.suffix == ".md":
                require(unquote(parsed.fragment) in headings(target.read_text(encoding="utf-8")), f"{path.relative_to(ROOT)}：锚点缺失 {label}")
        for block in re.findall(r"```json\n([\s\S]*?)\n```", text):
            try:
                json.loads(block, object_pairs_hook=pairs, parse_constant=reject_constant)
            except ValueError as exc:
                ERRORS.append(f"{path.relative_to(ROOT)}：JSON 示例错误 {exc}")
    parsed_json = {}
    for path in json_files:
        try:
            parsed_json[path] = load_json(path)
        except ValueError as exc:
            ERRORS.append(f"{path.relative_to(ROOT)}：{exc}")
    index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
    for path in (ROOT / "docs").rglob("*.md"):
        if path.name != "README.md":
            require(path.relative_to(ROOT / "docs").as_posix() in index, f"文档未加入导航：{path.name}")
    requirements = (ROOT / "docs/product/requirements.md").read_text(encoding="utf-8")
    trace = (ROOT / "docs/quality/traceability.md").read_text(encoding="utf-8")
    requirement_ids = re.findall(r"^\| (DW-(?:N)?\d+) \|", requirements, re.M)
    trace_ids = re.findall(r"^\| (DW-(?:N)?\d+) \|", trace, re.M)
    require(len(requirement_ids) == len(set(requirement_ids)), "需求身份重复")
    require(set(requirement_ids) == set(trace_ids) and len(trace_ids) == len(set(trace_ids)), "需求与追踪矩阵不一致")
    quality = (ROOT / "docs/quality/verification.md").read_text(encoding="utf-8")
    case_ids = set(re.findall(r"^\| (Q\d+) \|", quality, re.M))
    require(set(re.findall(r"\bQ\d+\b", trace)) <= case_ids, "追踪矩阵包含未定义用例")
    case_count = 0
    if args.contracts:
        try:
            import yaml
            from jsonschema import Draft202012Validator, FormatChecker
            from referencing import Registry, Resource
        except ImportError:
            print("缺少检查依赖；请安装 requirements-docs.txt", file=sys.stderr)
            return 2
        schemas = {p.name: load_json(p) for p in (ROOT / "contracts").glob("*.schema.json")}
        registry = Registry().with_resources((data["$id"], Resource.from_contents(data)) for data in schemas.values())
        for data in schemas.values():
            Draft202012Validator.check_schema(data)
        for source in schemas.values():
            resolver = registry.resolver(base_uri=source["$id"])
            pending_refs = [source]
            while pending_refs:
                fragment = pending_refs.pop()
                if isinstance(fragment, dict):
                    if "$ref" in fragment:
                        resolver.lookup(fragment["$ref"])
                    pending_refs.extend(fragment.values())
                elif isinstance(fragment, list):
                    pending_refs.extend(fragment)
        formats = FormatChecker()

        @formats.checks("date-time")
        def date_time(value):
            if not isinstance(value, str):
                return True
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
                return False
            try:
                datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
                return True
            except ValueError:
                return False

        validators = {name: Draft202012Validator(data, registry=registry, format_checker=formats) for name, data in schemas.items()}
        for case in load_json(ROOT / "examples/cases.json"):
            path = ROOT / "examples" / case["file"]
            require(inside(path, ROOT / "examples"), "用例路径越界")
            data = load_json(path)
            require(case["semantic"] in {"none", "workflow", "trajectory", "package", "vision", "draft", "operation", "mcp", "settings", "management", "execution", "import", "authoring", "recording", "query", "platform"}, "未登记的用例语义检查类型")
            schema_name, separator, fragment = case["schema"].partition("#")
            selected = validators[schema_name]
            if separator:
                selected = Draft202012Validator({"$ref": schemas[schema_name]["$id"] + "#" + fragment}, registry=registry, format_checker=formats)
            schema_errors = list(selected.iter_errors(data))
            semantic_errors = []
            if not schema_errors:
                if case["semantic"] == "workflow":
                    semantic_errors = workflow_errors(data)
                elif case["semantic"] == "trajectory":
                    semantic_errors = trajectory_errors(data)
                elif case["semantic"] == "package":
                    semantic_errors = package_errors(data, path.parent, validators)
                elif case["semantic"] == "vision":
                    semantic_errors = vision_errors(data, case["visionType"])
                elif case["semantic"] == "draft":
                    semantic_errors = draft_errors(data, validators["workflow.schema.json"])
                elif case["semantic"] == "operation":
                    context_path = ROOT / "examples" / case["context"] if case.get("context") else None
                    require(context_path is None or inside(context_path, ROOT / "examples"), "作业上下文路径越界")
                    context = load_json(context_path) if context_path and inside(context_path, ROOT / "examples") else None
                    semantic_errors = operation_errors(data, context)
                elif case["semantic"] == "settings":
                    semantic_errors = settings_errors(data, load_json(ROOT / "contracts/settings-catalog.json"), platform=case.get("platform"))
                elif case["semantic"] == "management":
                    management_context = None
                    if case.get("context"):
                        management_path = ROOT / "examples" / case["context"]
                        require(inside(management_path, ROOT / "examples"), "管理上下文路径越界")
                        if inside(management_path, ROOT / "examples"):
                            management_context = load_json(management_path)
                    semantic_errors = management_errors(data, case["managementType"], management_context)
                elif case["semantic"] == "authoring":
                    context = None
                    if case.get("context"):
                        context_path = ROOT / "examples" / case["context"]
                        require(inside(context_path, ROOT / "examples"), "编辑上下文路径越界")
                        if inside(context_path, ROOT / "examples"):
                            context = load_json(context_path)
                    semantic_errors = authoring_errors(data, case["authoringType"], context, ROOT / "examples")
                elif case["semantic"] in {"execution", "import"}:
                    context_path = ROOT / "examples" / case["context"] if case.get("context") else None
                    require(context_path is None or inside(context_path, ROOT / "examples"), "语义上下文路径越界")
                    context = load_json(context_path) if context_path and inside(context_path, ROOT / "examples") else None
                    semantic_errors = execution_errors(data, case["executionType"], context) if case["semantic"] == "execution" else import_errors(data, case["importType"], context, ROOT / "examples")
                elif case["semantic"] == "recording":
                    context_path = ROOT / "examples" / case["context"] if case.get("context") else None
                    require(context_path is None or inside(context_path, ROOT / "examples"), "录制上下文路径越界")
                    context = load_json(context_path) if context_path and inside(context_path, ROOT / "examples") else None
                    semantic_errors = recording_errors(data, case["recordingType"], context)
                elif case["semantic"] == "query":
                    context_path = ROOT / "examples" / case["context"] if case.get("context") else None
                    require(context_path is None or inside(context_path, ROOT / "examples"), "查询上下文路径越界")
                    context = load_json(context_path) if context_path and inside(context_path, ROOT / "examples") else None
                    semantic_errors = query_errors(data, case["queryType"], context)
                elif case["semantic"] == "platform":
                    semantic_errors = platform_errors(data, case["platformType"])
                elif case["semantic"] == "mcp":
                    semantic_errors = mcp_config_errors(data)
                if case.get("request"):
                    original = load_json(ROOT / "examples" / case["request"])
                    if data.get("replyTo") != original["id"]:
                        semantic_errors.append("响应没有对应原请求")
                    result_schema = {"$ref": "urn:deskweave:service:1#/$defs/" + original["method"] + "/properties/result"}
                    result_validator = Draft202012Validator(result_schema, registry=registry, format_checker=formats)
                    schema_errors.extend(result_validator.iter_errors(data.get("result")))
            actual = "schema-fail" if schema_errors else "semantic-fail" if semantic_errors else "pass"
            require(actual == case["expected"], f"{case['file']}：预期 {case['expected']}，实际 {actual}")
            case_count += 1
        boundary_checks(validators)
        mcp_catalog_checks(schemas, registry, Draft202012Validator, formats)
        cli_catalog_checks(schemas, registry, Draft202012Validator, formats)
        mcp = load_json(ROOT / "contracts/mcp-catalog.json")
        cli = load_json(ROOT / "contracts/cli-catalog.json")
        protocol_errors, protocol_count = protocol_checks(ROOT, mcp, schemas, registry, Draft202012Validator, formats)
        for error in protocol_errors:
            require(False, error)
        print(f"MCP 双代协议：{protocol_count} 组目录、头体和订阅边界检查")
        for error in alignment_errors(schemas, mcp, cli, registry, Draft202012Validator, formats):
            require(False, error)
        regression_errors, regression_count = alignment_regressions(schemas, mcp, cli, registry, Draft202012Validator, formats)
        for error in regression_errors:
            require(False, error)
        print(f"多入口：{len(schemas['service.schema.json']['$defs'])} 个服务方法；{regression_count} 组目录一致性回归")
        operation_catalog_checks(schemas, registry, Draft202012Validator, formats)
        settings_catalog_checks(schemas, registry, Draft202012Validator, formats)
        catalog = load_json(ROOT / "contracts/action-catalog.json")["actions"]
        action_ids = [row["id"] for row in catalog]
        require(len(action_ids) == len(set(action_ids)), "动作目录重复")
        require(set(action_ids) == set(schemas["actions.schema.json"]["$defs"]), "动作目录与 Schema 不一致")
        for row in catalog:
            key = row["id"]
            require(row.get("nodeSchema") == "urn:deskweave:actions:1#/$defs/" + key, "动作节点 Schema 引用错误")
            require(row.get("parameterSchema") == "urn:deskweave:actions:1#/$defs/" + key + "/properties/params", "动作参数 Schema 引用错误")
            expected_effect = (
                "delegated" if key in {"flow.call", "flow.join", "flow.repeat", "flow.loop"} else
                "network" if key == "vision.observe" else
                "capture" if key in {"screen.capture", "vision.match"} else
                "input" if key.startswith(("pointer.", "keyboard.", "input.")) else "none"
            )
            require(row.get("effect") == expected_effect, "动作效果类别不一致")
            require(all(isinstance(row.get(field), str) and row[field] for field in ("label", "group", "outputs")), "动作元数据缺失")
        workflow_text = (ROOT / "docs/design/workflow.md").read_text(encoding="utf-8")
        require(all(key in workflow_text for key in action_ids), "动作文档不完整")
        for path in (ROOT / ".github").rglob("*.yml"):
            data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
            require(isinstance(data, dict), f"{path.name}：YAML 顶层应为对象")
            if path.parent.name == "workflows":
                require(data.get("permissions", {}).get("contents") == "read", "文档 CI 权限不是只读")
                require("pull_request_target" not in data.get("on", {}), "文档 CI 使用了特权 PR 事件")
                for job in data["jobs"].values():
                    for step in job["steps"]:
                        if "uses" in step:
                            require(bool(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])), "Action 没有固定提交")
        print(f"契约：{len(schemas)} 份 Schema，{len(action_ids)} 个动作，{case_count} 个样例")
    for error in ERRORS:
        print(error, file=sys.stderr)
    print(f"文档：{len(markdown)} 份；JSON：{len(json_files)} 份；需求：{len(requirement_ids)} 项；错误：{len(ERRORS)}")
    return 1 if ERRORS else 0


if __name__ == "__main__":
    raise SystemExit(main())
