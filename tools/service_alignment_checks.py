"""检查 MCP、CLI 与 Engine 服务契约的映射和权限边界。"""
from __future__ import annotations

from copy import deepcopy
import re


def alignment_errors(schemas, mcp, cli, registry, validator_class, formats):
    errors = []
    service = schemas["service.schema.json"]
    methods = service["$defs"]
    local_rows = mcp["authorization"]["localOnlyMethods"]
    local = set(local_rows)
    tools = {row["method"]: row for row in mcp["tools"]}
    kind_permissions = mcp["authorization"]["catalogKindPermissions"]

    def require(ok, message):
        if not ok:
            errors.append(message)

    def params_for(row):
        method = row["method"]
        require(method in methods, "入口引用未知服务方法：" + method)
        return methods.get(method, {}).get("properties", {}).get("params", {})

    def check_values(row, values, label):
        params = params_for(row)
        properties = params.get("properties", {})
        for key, value in values.items():
            require(key in properties, label + "字段不存在：" + key)
            if key not in properties:
                continue
            ref = service["$id"] + "#/$defs/" + row["method"] + "/properties/params/properties/" + key
            validator = validator_class({"$ref": ref}, registry=registry, format_checker=formats)
            require(not list(validator.iter_errors(value)), label + "值不符合字段约束：" + key)
            if "default" in properties[key] and label.endswith("默认"):
                require(value == properties[key]["default"], label + "值与服务不一致：" + key)

    def expected_permissions(row, bound):
        if row["method"] == "workspace.catalog":
            kind = bound.get("kind")
            require(kind in kind_permissions, "目录入口缺少固定对象类型")
            return {kind_permissions[kind]} if kind in kind_permissions else set()
        if row["method"] in tools:
            return set(tools[row["method"]]["permissions"])
        if row["method"] == "event.read":
            return {"run.read"}
        return None

    require(len(local_rows) == len(local), "本地方法分类重复")
    for row in mcp["tools"]:
        require(row["method"] not in local, "本地方法公开为 MCP 工具")
        require("defaults" not in row and "fixedParams" not in row, "MCP 工具私自装配业务参数")
        require("publishedOutputSchema" not in row, "MCP 工具另设业务结果结构")
        if row["method"] != "run.resume":
            require("publishedInputSchema" not in row and not row.get("forbiddenParams"), "MCP 工具存在未定义的参数投影")

    for row in mcp["resources"]:
        method = row["method"]
        params = params_for(row)
        properties = params.get("properties", {})
        path = row["pathBindings"]
        query = row["queryParameters"]
        defaults = row["defaults"]
        require(method not in local, "本地方法公开为 MCP 资源")
        require(row["resultSchema"] == {"$ref": service["$id"] + "#/$defs/" + method + "/properties/result"}, "MCP 资源另设业务结果结构")
        path_tokens = set(re.findall(r"\{([A-Za-z][A-Za-z0-9_]*)\}", row["uriTemplate"]))
        workspace = {row["workspaceParameter"]} if row["workspaceParameter"] else set()
        require(path_tokens == set(path) | workspace and not set(path) & workspace, "资源 URI 路径绑定不一致")
        query_groups = re.findall(r"\{\?([^}]+)\}", row["uriTemplate"])
        uri_query = query_groups[0].split(",") if len(query_groups) == 1 else []
        require(len(query_groups) <= 1 and uri_query == query and len(query) == len(set(query)), "资源 URI 查询字段不一致")
        require(len(path.values()) == len(set(path.values())), "资源路径重复绑定业务字段")
        require(not set(path.values()) & (set(query) | set(defaults)), "资源字段存在多个输入来源")
        supplied = set(path.values()) | set(query) | set(defaults)
        require(set(params.get("required", [])) <= supplied <= set(properties), "资源参数未覆盖服务契约")
        check_values(row, defaults, "资源默认")
        permissions = expected_permissions(row, defaults)
        if permissions is not None:
            require(set(row["permissions"]) == permissions, "资源与工具的权限不一致：" + method)

    for row in cli["commands"]:
        check_values(row, row.get("defaults", {}), "CLI 默认")
        check_values(row, row.get("fixedParams", {}), "CLI 固定参数")
        if row["method"] in tools:
            permissions = expected_permissions(row, row.get("fixedParams", {}))
            require(set(row["permissions"]) == permissions, "CLI 与 MCP 的权限不一致：" + row["name"])

    context = mcp.get("requestContext", {})
    require(context.get("metadataSchema") == {"$ref": "urn:deskweave:mcp:1#/$defs/requestMetadata"}, "MCP 请求元数据未复用契约")
    require(context.get("requestIdKey") == "deskweave/requestId" and context.get("engineEnvelopeField") == "id", "MCP 请求身份映射不一致")
    require(context.get("whenOmitted") == "generateBeforeDispatch" and context.get("resultMetaKey") == context.get("requestIdKey"), "MCP 请求身份生成或回传不一致")
    require(context.get("scope") == ["workspace", "authenticatedClient"] and context.get("statusMethod") == "request.status", "MCP 请求恢复范围不一致")
    metadata = schemas["mcp.schema.json"]["$defs"].get("requestMetadata", {})
    id_ref = {"$ref": "urn:deskweave:message:1#/$defs/requestEnvelope/properties/id"}
    require(metadata.get("properties", {}).get("deskweave/requestId") == id_ref, "MCP 请求身份类型与本机信封不一致")
    for part in ("params", "result"):
        field = methods["request.status"]["properties"][part]["properties"]["requestId"]
        validator = validator_class(field, registry=registry, format_checker=formats)
        require(all(not list(validator.iter_errors(value)) for value in ("r", "r" * 128)), "原请求查询不能接收合法的本机请求身份")
    return errors


def alignment_regressions(schemas, mcp, cli, registry, validator_class, formats):
    """目录的合法变化与权限、参数漂移使用成对检查。"""
    cases = []

    def changed(label, mutate, valid=False):
        sample_mcp, sample_cli = deepcopy(mcp), deepcopy(cli)
        mutate(sample_mcp, sample_cli)
        cases.append((label, sample_mcp, sample_cli, valid))

    changed("目录条目重排", lambda m, c: m["resources"].reverse(), True)
    changed("修改显示名称", lambda m, c: m["resources"][0].update(name="流程"), True)
    changed("合法页大小默认漂移", lambda m, c: m["resources"][0]["defaults"].update(pageSize=25))
    changed("越界页大小", lambda m, c: m["resources"][0]["defaults"].update(pageSize=101))
    changed("未知默认字段", lambda m, c: m["resources"][0]["defaults"].update(offset=0))
    changed("URI 字段遗漏", lambda m, c: m["resources"][0]["queryParameters"].remove("states"))
    changed("资源权限扩大", lambda m, c: m["resources"][0].update(permissions=[]))
    changed("路径覆盖默认字段", lambda m, c: m["resources"][0].update(pathBindings={"workspaceId": "pageSize"}))
    changed("资源结果漂移", lambda m, c: m["resources"][0].update(resultSchema={"type": "object"}))
    changed("本地方法公开为资源", lambda m, c: m["resources"][-1].update(method="bridge.read", resultSchema={"$ref": "urn:deskweave:service:1#/$defs/bridge.read/properties/result"}))
    changed("CLI 权限扩大", lambda m, c: next(r for r in c["commands"] if r["name"] == "workflow list").update(permissions=[]))
    changed("CLI 默认值漂移", lambda m, c: next(r for r in c["commands"] if r["name"] == "workflow list")["defaults"].update(pageSize=25))
    changed("工具私设参数默认值", lambda m, c: m["tools"][0].update(defaults={"pageSize": 25}))
    changed("工具私设发布结构", lambda m, c: m["tools"][0].update(publishedInputSchema={"type": "object"}))
    changed("请求恢复作用域丢失", lambda m, c: m["requestContext"].update(scope=["workspace"]))
    changed("请求编号映射漂移", lambda m, c: m["requestContext"].update(engineEnvelopeField="session"))
    errors = []
    for label, sample_mcp, sample_cli, valid in cases:
        actual = not alignment_errors(schemas, sample_mcp, sample_cli, registry, validator_class, formats)
        if actual != valid:
            errors.append("入口一致性回归失败：" + label)
    return errors, len(cases)
