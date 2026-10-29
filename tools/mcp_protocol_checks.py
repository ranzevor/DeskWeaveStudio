"""核对 MCP 双代协议目录、HTTP 头体与订阅示例。"""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import json

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
VERSION = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
SUBSCRIPTION = "io.modelcontextprotocol/subscriptionId"


def http_header_errors(request, headers):
    errors = []
    lowered = {}
    for name, value in headers.items():
        key = name.lower()
        if key in lowered:
            errors.append("重复请求头")
        lowered[key] = value
    params = request.get("params", {})
    expected = {"mcp-protocol-version": params.get("_meta", {}).get(VERSION), "mcp-method": request.get("method")}
    if request.get("method") in {"tools/call", "prompts/get", "resources/read"}:
        expected["mcp-name"] = params.get("uri" if request["method"] == "resources/read" else "name")
    for key, value in expected.items():
        actual = lowered.get(key)
        if not isinstance(actual, str) or not actual or actual.strip() != actual or any(ord(c) < 32 or ord(c) > 126 for c in actual):
            errors.append("请求头缺失或值无效：" + key)
            continue
        if key == "mcp-name" and actual.startswith("=?base64?") and actual.endswith("?="):
            try:
                actual = base64.b64decode(actual[9:-2], validate=True).decode("utf-8")
            except (binascii.Error, UnicodeError):
                errors.append("请求头编码无效")
                continue
        if actual != value:
            errors.append("请求头与正文不一致：" + key)
    return errors


def subscription_errors(request, messages):
    errors = []
    request_id = request["id"]
    wanted = request["params"]["notifications"]
    if not messages or messages[0].get("method") != "notifications/subscriptions/acknowledged":
        return ["订阅首条消息不是确认"]
    accepted = messages[0].get("params", {}).get("notifications", {})
    for key, value in accepted.items():
        if key == "resourceSubscriptions":
            if not set(value) <= set(wanted.get(key, [])):
                errors.append("确认扩大资源范围")
        elif value and not wanted.get(key, False):
            errors.append("确认新增通知类型")
    names = {"notifications/tools/list_changed": "toolsListChanged", "notifications/prompts/list_changed": "promptsListChanged", "notifications/resources/list_changed": "resourcesListChanged"}
    closed = False
    for index, message in enumerate(messages):
        if closed:
            errors.append("订阅结束后仍有消息")
        payload = message.get("result", message.get("params", {}))
        if payload.get("_meta", {}).get(SUBSCRIPTION) != request_id:
            errors.append("订阅身份不匹配")
        if "result" in message:
            if message.get("id") != request_id or payload.get("resultType") != "complete":
                errors.append("订阅终结响应不匹配")
            closed = True
        elif index:
            method = message.get("method")
            if method == "notifications/resources/updated":
                if payload.get("uri") not in accepted.get("resourceSubscriptions", []):
                    errors.append("通知资源未经确认")
            elif method not in names or not accepted.get(names[method], False):
                errors.append("订阅通知类型未经确认")
    return errors


def profile_errors(catalog):
    errors = []
    def require(ok, message):
        if not ok: errors.append(message)
    require(catalog.get("protocolVersion") == MODERN and catalog.get("supportedVersions") == [MODERN, LEGACY], "MCP 主协议与兼容版本不一致")
    require(catalog.get("defaultTransports") == ["stdio", "streamableHttp"], "MCP 默认传输不一致")
    profiles = catalog.get("protocolProfiles", [])
    by_version = {p["version"]: p for p in profiles}
    require(len(profiles) == 2 and set(by_version) == {MODERN, LEGACY}, "MCP 适配目录缺失或重复")
    for version, era, init, session, subscription, disconnect, transports, metadata in (
        (MODERN, "modern", False, None, "subscriptions/listen", "cancelRequest", ["stdio", "streamableHttp"], "modernRequestMetadata"),
        (LEGACY, "legacy", True, "MCP-Session-Id", "resources/subscribe", "endWait", ["stdio", "streamableHttp", "sse"], "requestMetadata"),
    ):
        p = by_version.get(version, {})
        require((p.get("era"), p.get("requiresInitialization"), p.get("sessionHeader"), p.get("subscriptionMethod"), p.get("httpDisconnect")) == (era, init, session, subscription, disconnect), "MCP 协议代际规则混用：" + version)
        require(p.get("transports") == transports, "MCP 适配传输范围不一致")
        require(p.get("requestMetadataSchema") == {"$ref": "urn:deskweave:mcp:1#/$defs/" + metadata}, "MCP 元数据结构未绑定")
        require(not set(p) & {"tools", "resources", "prompts", "serviceMethods"}, "协议适配另设业务目录")
    require(catalog.get("cachePolicy") == {"ttlMs": 0, "cacheScope": "private"}, "工作空间协议缓存范围不一致")
    require(catalog.get("subscriptions", {}).get("maxStreamsPerClient") == 4 and catalog.get("subscriptions", {}).get("maxResourcesPerStream") == 32, "订阅容量不一致")
    return errors


def protocol_checks(root, catalog, schemas, registry, validator_class, formats):
    errors = profile_errors(catalog)
    cases = []
    def check(name, actual, expected):
        cases.append(name)
        if actual != expected: errors.append("MCP 协议边界：" + name)
    def load(name):
        return json.loads((root / "examples/mcp" / (name + ".json")).read_text(encoding="utf-8"))
    request = load("modern-tool-call")
    headers = load("modern-tool-headers")
    check("合法 HTTP 请求", not http_header_errors(request, headers), True)
    check("头名称不区分大小写", not http_header_errors(request, {k.lower():v for k,v in headers.items()}), True)
    check("旧会话头不决定新版身份", not http_header_errors(request, {**headers, "MCP-Session-Id":"unused-session"}), True)
    for name, key, value in (("方法冲突","Mcp-Method","resources/read"),("工具名冲突","Mcp-Name","deskweave_input"),("版本冲突","MCP-Protocol-Version",LEGACY),("缺少方法头","Mcp-Method",None),("头值换行","Mcp-Name","deskweave_run_stop\n"),("非法编码","Mcp-Name","=?base64?%%%?=")):
        bad = deepcopy(headers)
        if value is None: bad.pop(key)
        else: bad[key] = value
        check(name, not http_header_errors(request, bad), False)
    for value in ("中文工具", " padded ", "=?base64?literal?="):
        sample = deepcopy(request); sample["params"]["name"] = value
        encoded = "=?base64?" + base64.b64encode(value.encode()).decode() + "?="
        check("编码后的名称 " + value, not http_header_errors(sample, {**headers,"Mcp-Name":encoded}), True)
    sequence = [load("modern-listen-acknowledged"), load("modern-resource-updated"), load("modern-listen-complete")]
    listen = load("modern-listen-request")
    check("订阅确认更新和正常结束", not subscription_errors(listen, sequence), True)
    check("确认前推送", not subscription_errors(listen, sequence[1:]), False)
    sample = deepcopy(sequence); sample[1]["params"]["_meta"][SUBSCRIPTION] = "another-listen"
    check("跨订阅推送", not subscription_errors(listen, sample), False)
    sample = deepcopy(sequence); sample[1]["params"]["uri"] = "deskweave://workspace/other/runs/run-example/snapshot"
    check("未订阅资源", not subscription_errors(listen, sample), False)
    check("结束后再推送", not subscription_errors(listen, sequence + sequence[1:2]), False)
    sample = deepcopy(sequence); sample[0]["params"]["notifications"]["promptsListChanged"] = True
    check("确认扩大条件", not subscription_errors(listen, sample), False)
    sample = deepcopy(sequence); sample[0]["params"]["notifications"] = {}; sample = [sample[0],sample[2]]
    check("合法缩小确认范围", not subscription_errors(listen, sample), True)
    for name, mutate in (
        ("新版误用会话", lambda x:x["protocolProfiles"][0].update(sessionHeader="MCP-Session-Id")),
        ("旧版断开误作取消", lambda x:x["protocolProfiles"][1].update(httpDisconnect="cancelRequest")),
        ("旧 SSE 成为默认", lambda x:x["defaultTransports"].append("sse")),
        ("重复业务目录", lambda x:x["protocolProfiles"][0].update(tools=[])),
        ("公开缓存工作空间", lambda x:x["cachePolicy"].update(cacheScope="public")),
    ):
        sample = deepcopy(catalog); mutate(sample)
        check(name, not profile_errors(sample), False)
    row = next(x for x in catalog["tools"] if x["name"] == request["params"]["name"])
    params_schema = row.get("publishedInputSchema", row["inputSchema"])
    validator = validator_class(params_schema, registry=registry, format_checker=formats)
    check("两代工具共用业务参数", not list(validator.iter_errors(request["params"]["arguments"])), True)
    check("两代业务参数均拒绝未知字段", not list(validator.iter_errors({**request["params"]["arguments"],"role":"admin"})), False)
    discover = load("modern-discover-result")
    check("发现支持版本与目录一致", discover["supportedVersions"], catalog["supportedVersions"])
    check("发现能力与新版目录一致", discover["capabilities"], catalog["protocolProfiles"][0]["capabilities"])
    check("发现缓存与目录一致", {k:discover[k] for k in ("ttlMs","cacheScope")}, catalog["cachePolicy"])
    return errors, len(cases)
