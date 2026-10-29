"""校验设置、管理快照与执行控制样例中的关联约束。"""
from __future__ import annotations
import json
from datetime import datetime


def hotkey(value: str, grammar: dict, platform: str = "windows") -> str:
    tokens = [item.strip(grammar["trimCharacters"]) for item in value.split(grammar["separator"])]
    if len(tokens) < 2 or any(not token or not token.isascii() for token in tokens):
        raise ValueError("快捷键语法无效")
    modifiers = {key.casefold(): key for key in grammar["modifiers"]}
    primary = {key.casefold(): key for key in grammar["primaryKeys"]}
    if tokens[-1].casefold() not in primary or any(key.casefold() not in modifiers for key in tokens[:-1]):
        raise ValueError("快捷键包含未登记的主键或修饰键")
    selected = [modifiers[key.casefold()] for key in tokens[:-1]]
    if len(selected) != len(set(selected)) or not set(selected) & set(grammar["requiredAnyModifiers"]):
        raise ValueError("快捷键的修饰键重复或缺少必要修饰键")
    result = "+".join([key for key in grammar["modifierOrder"] if key in selected] + [primary[tokens[-1].casefold()]])
    if "Win" in selected and (platform != "windows" or "Meta" in selected):
        raise ValueError("Win 修饰键仅用于 Windows 且不能与 Meta 重复")
    comparable = result.replace("Meta", "Win") if platform == "windows" else result
    reserved = grammar.get("reservedByPlatform", {}).get(platform, grammar["reservedCombinations"] if platform == "windows" else [])
    if comparable in reserved:
        raise ValueError("快捷键属于系统保留组合")
    return comparable


def settings_errors(data: dict, catalog: dict, platform: str | None = None) -> list[str]:
    errors = []
    platform = platform or data.get("snapshot", data).get("platform", "windows")
    keys = {row["key"]: row for row in catalog["keys"]}
    def check_hotkeys(values):
        try:
            parsed = [hotkey(value, catalog["hotkeyGrammar"], platform) for value in values.values()]
            if len(parsed) != len(set(parsed)):
                errors.append("有效快捷键存在冲突")
        except ValueError as exc:
            errors.append(str(exc))
    if "updates" in data:
        names = [row["key"] for row in data["updates"]]
        if len(names) != len(set(names)):
            errors.append("设置批次包含重复键")
        for row in data["updates"]:
            if data["scope"] not in keys[row["key"]]["scopes"]:
                errors.append("设置键不允许写入当前作用域")
            if row["op"] == "set" and row["key"] == "input.hotkeys":
                check_hotkeys(row["value"])
        return errors
    snapshot = data.get("snapshot", data)
    for key, value in snapshot["overrides"].items():
        if snapshot["values"][key] != value or snapshot["sources"][key] != snapshot["scope"]:
            errors.append("显式设置覆盖与有效值或来源不一致")
    for key, source in snapshot["sources"].items():
        if source == snapshot["scope"] and key not in snapshot["overrides"]:
            errors.append("本层设置来源缺少对应覆盖")
        if source == "default" and snapshot["values"][key] != keys[key].get("platformDefaults", {}).get(platform, keys[key]["default"]):
            errors.append("默认来源的有效值不等于目录默认值")
    pending = snapshot["pending"]
    if len({row["key"] for row in pending}) != len(pending):
        errors.append("等待生效设置键重复")
    for row in pending:
        definition = keys[row["key"]]
        version = snapshot["baseVersion"] if snapshot["scope"] == "workspace" and definition["scopes"] == ["user"] else snapshot["version"]
        if definition["activation"] != "restartAgent" or row["savedVersion"] > version:
            errors.append("等待生效设置的类别或已提交版本错误")
    check_hotkeys(snapshot["values"]["input.hotkeys"])
    if "changes" in data:
        changes = data["changes"]
        if len({row["key"] for row in changes}) != len(changes):
            errors.append("生效结果包含重复设置键")
        for row in changes:
            if row["activation"] != keys[row["key"]]["activation"]:
                errors.append("逐键生效类别与目录不一致")
        actual = {row["activation"] for row in changes if row["changed"]}
        expected = "immediate" if not actual else "mixed" if len(actual) > 1 else next(iter(actual))
        if data["activation"] != expected:
            errors.append("混合生效汇总与实际修改不一致")
    return errors


def management_errors(data: dict, kind: str, context=None) -> list[str]:
    errors = []
    if kind == "bridge":
        present = data["activeVersion"] is not None
        if present != (data["activeConfig"] is not None):
            errors.append("活动监听配置和版本未成对返回")
        if present and data["activeVersion"] > data["savedVersion"]:
            errors.append("活动监听版本超过已保存版本")
        if data["state"] == "listening" and not present:
            errors.append("监听状态缺少活动配置")
    elif kind == "tokens":
        ids = [row["tokenId"] for row in data["items"]]
        if len(ids) != len(set(ids)):
            errors.append("令牌分页成员重复")
        for row in data["items"]:
            if row["createdAt"] >= row["expiresAt"]:
                errors.append("令牌到期时间不晚于创建时间")
            if row["lastUsedAt"] is not None and not row["createdAt"] <= row["lastUsedAt"] <= data["snapshotAt"]:
                errors.append("令牌使用时间超出实际区间")
            if row["state"] == "revoked" and row["revokedAt"] is None or row["state"] != "revoked" and row["revokedAt"] is not None:
                errors.append("令牌撤销状态与时间不一致")
            if row["state"] == "active" and row["expiresAt"] <= data["snapshotAt"] or row["state"] == "expired" and row["expiresAt"] > data["snapshotAt"]:
                errors.append("令牌过期状态与快照时间不一致")
    elif kind in {"references", "assetReferencePage"}:
        identities = [(row["ownerKind"], row["ownerId"], row["assetVersion"]) for row in data["references"]]
        if len(identities) != len(set(identities)):
            errors.append("资产引用分页成员重复")
        if len(identities) + data["hiddenReferences"] > data["totalReferences"]:
            errors.append("资产引用数量与可见范围不一致")
        if "snapshotAt" in data:
            elapsed = (datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00")) - datetime.fromisoformat(data["snapshotAt"].replace("Z", "+00:00"))).total_seconds()
            if not 0 < elapsed <= 300:
                errors.append("资产引用快照有效期超出范围")
            versions = {data["currentVersion"]} | {row["assetVersion"] for row in data["references"]}
            if data["assetVersion"] is not None:
                versions.add(data["assetVersion"])
            if len(versions) > data["versionCount"]:
                errors.append("引用版本数量超过资产版本总数")
            if data["scope"] == "version" and any(row["assetVersion"] != data["assetVersion"] for row in data["references"]):
                errors.append("单版本查询包含其他版本引用")
            if context is not None:
                rows = data["references"]
                visible = data["totalReferences"] - data["hiddenReferences"]
                if len(rows) > context.get("pageSize", 50):
                    errors.append("资产引用页超过请求大小")
                if "assetId" in context and (data["scope"] != "asset" or data["assetId"] != context["assetId"]):
                    errors.append("资产引用页未绑定所查资产")
                if "assetVersion" in context and (data["scope"] != "version" or data["assetVersion"] != context["assetVersion"]):
                    errors.append("资产引用页未绑定所查版本")
                if context.get("cursor") is None and (visible != len(rows) if data["nextCursor"] is None else visible <= len(rows)):
                    errors.append("资产引用首页续页标记与总数不一致")
    elif kind == "tokenImpactPage":
        elapsed = (datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00")) - datetime.fromisoformat(data["snapshotAt"].replace("Z", "+00:00"))).total_seconds()
        if not 0 < elapsed <= 60:
            errors.append("令牌影响快照有效期超出范围")
        ids = [row["runId"] for row in data["relatedRuns"]]
        if len(ids) != len(set(ids)):
            errors.append("令牌影响分页运行重复")
        if len(ids) + data["hiddenRunCount"] > data["counts"]["activeRuns"]:
            errors.append("令牌影响运行数量与可见范围不一致")
        if any(row["state"] not in {"queued", "preparing", "running", "pausing", "paused", "stopping"} for row in data["relatedRuns"]):
            errors.append("令牌影响查询包含已终结运行")
        if context is not None:
            visible = data["counts"]["activeRuns"] - data["hiddenRunCount"]
            if data["tokenId"] != context["tokenId"] or len(ids) > context["pageSize"]:
                errors.append("令牌影响页与查询身份或大小不符")
            if context.get("cursor") is None and (visible != len(ids) if data["nextCursor"] is None else visible <= len(ids)):
                errors.append("令牌影响首页续页标记与总数不一致")
    elif kind == "tokenRevoke":
        if context is not None:
            if (data["previewState"] != "notProvided") != ("impactSnapshotId" in context):
                errors.append("撤销未提供预览却返回预览状态")
            if context.get("stopOwnedRuns") is False and data["stoppingRuns"] != 0:
                errors.append("保留运行的撤销请求返回停止运行数量")
    else:
        errors.append("未登记的管理快照检查类型")
    return errors
