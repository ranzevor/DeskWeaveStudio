"""编辑提交、查询投影及内容引用的关联检查。"""
from __future__ import annotations
import base64
import hashlib
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from canonical_json import digest

ROOT = Path(__file__).resolve().parents[1]


def fixture(base, name):
    path = (base / name).resolve()
    if not path.is_relative_to(base.resolve()) or path.is_symlink():
        raise ValueError("样例资源路径越界")
    return path


@lru_cache(maxsize=1)
def registry():
    from referencing import Registry, Resource
    values = [json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "contracts").glob("*.schema.json")]
    return Registry().with_resources((value["$id"], Resource.from_contents(value)) for value in values)


def validate(value, reference):
    from jsonschema import Draft202012Validator
    return list(Draft202012Validator({"$ref": reference}, registry=registry()).iter_errors(value))


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sensitive_names(context):
    marked = set(context.get("sensitiveNames", []))
    links = context.get("derivations", [])
    while True:
        found = {row["name"] for row in links if set(row.get("reads", []) + row.get("controls", [])) & marked}
        if found <= marked:
            return marked
        marked |= found


def authoring_errors(data, kind, context=None, base=None):
    context, base = context or {}, base or ROOT / "examples"
    errors = []
    def require(condition, message):
        if not condition:
            errors.append(message)
    if kind in {"revisionPage", "connectionPage", "variablesPage"}:
        seconds = (timestamp(data["expiresAt"]) - timestamp(data["snapshotAt"])).total_seconds()
        require(0 < seconds <= (60 if kind == "variablesPage" else 300), "查询快照期限不正确")
        if context.get("now"):
            require(timestamp(context["now"]) < timestamp(data["expiresAt"]), "查询快照已经过期")
        items = data["items"]
        key = {"revisionPage": "revision", "connectionPage": "connectionId", "variablesPage": "name"}[kind]
        require(len({row[key] for row in items}) == len(items), "分页成员身份重复")
        query = context.get("query", {})
        require(len(items) <= query.get("pageSize", 100), "查询页超过请求大小")
        if kind == "revisionPage":
            require(not query or data["workflowId"] == query["workflowId"], "流程版本查询身份不符")
        if kind == "variablesPage":
            for key in ("runId", "frameId", "scope"):
                require(key not in query or data[key] == query[key], "变量页没有绑定原查询")
            require(query.get("snapshotId") is None or data["snapshotId"] == query["snapshotId"], "变量页切换了固定快照")
            require("dataVersion" not in context or data["dataVersion"] == context["dataVersion"], "变量快照代次不一致")
            require(data["total"] >= len(items), "变量总数小于本页数量")
            if query and query.get("cursor") is None:
                require(data["total"] == len(items) if data["nextCursor"] is None else data["total"] > len(items), "变量首页分页与总数不符")
            marked = sensitive_names(context)
            for row in items:
                if row["name"] in marked:
                    require(row["visibility"] == "redacted", "敏感输入或派生值未遮蔽")
                if row["visibility"] == "visible" and "value" in row:
                    require(len(json.dumps(row["value"], ensure_ascii=False).encode()) <= 4096, "大变量必须使用资源引用")
                    value = row["value"]
                    actual = "null" if value is None else "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "string" if isinstance(value, str) else "array" if isinstance(value, list) else "object"
                    require(row["valueType"] == actual, "变量显示类型与实际值不符")
    elif kind == "draftTestResult":
        query = context.get("query", {})
        for key in ("editorId", "editVersion"):
            require(key not in query or data[key] == query[key], "测试结果不属于当前编辑代次")
        require(not data["modelAvailable"] or data["configurationValid"] and data["reachable"] and data["reason"] is None, "模型可用状态与测试阶段不符")
        require(data["configurationValid"] or not data["reachable"] and not data["modelAvailable"], "无效配置仍表示网络成功")
        if context.get("configuration"):
            require(data["configurationHash"] == digest(context["configuration"]), "测试结果配置摘要不符")
    elif kind in {"contentBegin", "contentRef", "contentWrite"}:
        if kind == "contentWrite":
            try:
                raw = base64.b64decode(data["chunk"], validate=True)
            except ValueError:
                return ["内容块不是严格Base64"]
            require(0 < len(raw) <= 196608, "内容块大小超出范围")
            require(hashlib.sha256(raw).hexdigest() == data["chunkSha256"], "内容块摘要不符")
            offset = context.get("nextOffset", data["offset"])
            replay = context.get("previous", {}).get(str(data["offset"]))
            require(data["offset"] == offset or replay == data["chunkSha256"], "分块偏移冲突或重复内容改变")
        else:
            raw = fixture(base, context["contentFile"]).read_bytes()
            require(len(raw) == data["length"] and hashlib.sha256(raw).hexdigest() == data["sha256"], "内容引用的字节身份不符")
            if context.get("purpose") == "workflowModel":
                value = json.loads(raw)
                require(not validate(value, "urn:deskweave:content:1#/$defs/workflowModel"), "内容资源不是完整流程模型")
            if context.get("purpose") == "trajectoryEdit":
                value = json.loads(raw)
                require(not validate(value, "urn:deskweave:trajectory:1"), "内容资源不是完整轨迹")
                from check_docs import trajectory_errors
                errors.extend(trajectory_errors(value))
    elif kind == "editCommit":
        plan = context["plan"]
        require(data["editId"] == plan["editId"] and data["expectedSourceSha256"] == plan["sourceSha256"], "编辑未固定原来源")
        require(timestamp(context["now"]) < timestamp(plan["expiresAt"]), "编辑计划已经过期")
        destination = plan["destination"]
        if destination["mode"] == "newVersion":
            require(destination["expectedCurrentVersion"] == context["currentVersion"], "编辑目标头已经改变")
        edit = data["content"]
        require((edit["kind"] == "imageCrop") == (plan["assetKind"] == "image"), "编辑内容与来源类型不符")
        if edit["kind"] == "imageCrop":
            roi = edit["roi"]
            require(roi["x"] >= 0 and roi["y"] >= 0 and roi["x"] + roi["width"] <= plan["sourceWidth"] and roi["y"] + roi["height"] <= plan["sourceHeight"], "裁剪矩形越过固定源图")
        else:
            reference = edit["contentRef"]
            errors.extend(authoring_errors(reference, "contentRef", context, base))
            value = json.loads(fixture(base, context["contentFile"]).read_bytes())
            require(not validate(value, "urn:deskweave:trajectory:1"), "编辑结果不是轨迹结构")
            from check_docs import trajectory_errors
            errors.extend(trajectory_errors(value))
            require(all(event["atUs"] < edit["durationUs"] for event in value["events"]), "轨迹事件超出编辑时长")
    elif kind == "editResult":
        require(data["createdNewVersion"] == (data["previousVersion"] is not None), "新版本结果与前版本关系不符")
        if context.get("contentFile"):
            raw = fixture(base, context["contentFile"]).read_bytes()
            require(data["length"] == len(raw) and data["sha256"] == hashlib.sha256(raw).hexdigest(), "正式资产结果摘要不符")
    elif kind == "workflowReference":
        require("modelRef" in data and "definition" not in data and "dependencies" not in data, "资源化保存仍携带内嵌模型")
        errors.extend(authoring_errors(data["modelRef"], "contentRef", context, base))
        value = json.loads(fixture(base, context["contentFile"]).read_bytes())
        require(value["definition"]["id"] == data["workflowId"], "资源化流程身份不符")
    else:
        errors.append("未登记的编辑关联检查类型")
    return errors
