"""显示区域、录制范围和轨迹回放映射的语义检查。"""
from __future__ import annotations

from platform_contract_checks import local_topology_errors, local_source_errors, local_point_valid, capability_errors


def contains(rect, point):
    return rect["x"] <= point["x"] < rect["x"] + rect["width"] and rect["y"] <= point["y"] < rect["y"] + rect["height"]


def topology_errors(value):
    if value["coordinateSpace"] == "displayLocalPixels":
        return local_topology_errors(value)
    errors = []
    displays = value["displays"]
    ids = [item["displayId"] for item in displays]
    if len(ids) != len(set(ids)):
        errors.append("显示屏身份重复")
    if sum(item["primary"] for item in displays) != 1:
        errors.append("显示拓扑需要一个主屏")
    rectangles = [item["bounds"] for item in displays]
    left = min(rect["x"] for rect in rectangles)
    top = min(rect["y"] for rect in rectangles)
    right = max(rect["x"] + rect["width"] for rect in rectangles)
    bottom = max(rect["y"] + rect["height"] for rect in rectangles)
    if value["virtualBounds"] != {"x": left, "y": top, "width": right - left, "height": bottom - top}:
        errors.append("虚拟桌面范围与显示区域不一致")
    if right > 2147483648 or bottom > 2147483648 or right - left > 2147483647 or bottom - top > 2147483647:
        errors.append("显示拓扑超出坐标范围")
    bounds = [(rect["x"], rect["y"], rect["width"], rect["height"]) for rect in rectangles]
    if len(bounds) != len(set(bounds)):
        errors.append("镜像输出须合并到同一逻辑显示区域")
    return errors


def source_errors(value):
    if value["topology"]["coordinateSpace"] == "displayLocalPixels":
        return local_source_errors(value)
    errors = topology_errors(value["topology"])
    ids = {item["displayId"] for item in value["topology"]["displays"]}
    selected = set(value["displayIds"])
    if not selected <= ids:
        errors.append("来源引用了不存在的显示屏")
    if value["scope"] in {"allDisplays", "window"} and selected != ids:
        errors.append("来源未覆盖固定显示集合")
    if value["scope"] == "display" and len(selected) != 1:
        errors.append("单屏录制只能选择一个显示区域")
    point = value["initialPointer"]
    if point is not None and not any(contains(item["bounds"], point) for item in value["topology"]["displays"] if item["displayId"] in selected):
        errors.append("起始指针位于显示区域之外")
    return errors


def trajectory_source_errors(value):
    source = value.get("source")
    moves = [event for event in value["events"] if event["kind"] == "pointer.move"]
    relative = any(event["kind"] == "pointer.delta" for event in value["events"])
    local = source is not None and source["topology"]["coordinateSpace"] == "displayLocalPixels"
    errors = source_errors(source) if source else []
    if value["coordinateMode"] == "display" and (not local or relative):
        errors.append("屏内像素模式需要对应来源且不能包含相对位移")
    if local and value["coordinateMode"] == "physical":
        errors.append("屏内像素来源不能解释成全局物理坐标")
    if value["formatVersion"] == "1.1" and relative and (not source or source.get("inputProfile", {}).get("relativeUnit", "none") == "none"):
        errors.append("相对轨迹缺少来源单位")
    if local:
        displays = [d for d in source["topology"]["displays"] if d["displayId"] in source["displayIds"]]
        for event in moves:
            if not local_point_valid(event, displays):
                errors.append("轨迹屏内位置缺少显示身份或超出像素范围")
    else:
        if any("displayId" in event for event in moves):
            errors.append("带显示身份的位置需要屏内像素来源")
        if source:
            rectangles = [d["bounds"] for d in source["topology"]["displays"] if d["displayId"] in source["displayIds"]]
            for event in moves:
                if not any(contains(rect, event) for rect in rectangles):
                    errors.append("轨迹位置落在显示区域之外")
    return errors


def replay_errors(mapping, context):
    source = context["trajectory"].get("source")
    if context["topology"]["coordinateSpace"] == "displayLocalPixels" or source and source["topology"]["coordinateSpace"] == "displayLocalPixels":
        return display_replay_errors(mapping, context)
    errors = topology_errors(context["topology"])
    trajectory = context["trajectory"]
    errors += trajectory_source_errors(trajectory)
    source = trajectory.get("source")
    if source is None:
        return errors + (["映射需要轨迹来源布局"] if mapping["mode"] != "original" else [])
    if source["initialPointer"] is None:
        errors.append("请在轨迹编辑器设置起点")
    if source["scope"] == "allDisplays" and context["targetKind"] != "desktop":
        errors.append("全显示屏轨迹需要桌面输入目标")
    profile = source.get("inputProfile")
    relative = any(event["kind"] == "pointer.delta" for event in trajectory["events"])
    if profile and (mapping["mode"] == "original" or relative) and profile != context.get("inputProfile"):
        errors.append("原布局或相对输入回放需要相同输入后端与单位")
    source_displays = {item["displayId"]: item for item in source["topology"]["displays"] if item["displayId"] in source["displayIds"]}
    targets = {item["displayId"]: item for item in context["topology"]["displays"]}
    allowed = set(context["allowedDisplayIds"])
    if mapping["mode"] == "original":
        pairs = {key: key for key in source_displays}
        for key, item in source_displays.items():
            target = targets.get(key)
            if target is None or any(item[field] != target[field] for field in ("bounds", "rotation", "dpiX", "dpiY")):
                errors.append("来源与当前显示布局不同")
        if any(event["kind"] == "pointer.delta" for event in trajectory["events"]):
            full_source = {item["displayId"]: item for item in source["topology"]["displays"]}
            if set(full_source) != set(targets) or any(any(item[field] != targets[key][field] for field in ("bounds", "rotation", "dpiX", "dpiY")) for key, item in full_source.items() if key in targets):
                errors.append("相对轨迹需要原显示布局")
    else:
        rows = mapping["displays"]
        pairs = {row["sourceDisplayId"]: row["targetDisplayId"] for row in rows}
        if len(pairs) != len(rows) or set(pairs) != set(source_displays):
            errors.append("显示屏映射缺失或来源重复")
        if len(set(pairs.values())) != len(pairs):
            errors.append("多个来源不能映射到同一显示区域")
        if any(event["kind"] == "pointer.delta" for event in trajectory["events"]):
            errors.append("相对输入不能按屏内像素平移")
    if not set(pairs.values()) <= allowed or not set(pairs.values()) <= set(targets):
        errors.append("目标显示屏不存在或未获输入授权")
    points = [event for event in trajectory["events"] if event["kind"] == "pointer.move"]
    if source["initialPointer"] is not None:
        points.append(source["initialPointer"])
    for point in points:
        matches = [item for item in source_displays.values() if contains(item["bounds"], point)]
        if len(matches) != 1:
            errors.append("来源位置不能唯一对应显示区域")
            continue
        item = matches[0]
        target = targets.get(pairs.get(item["displayId"]))
        if target is None:
            continue
        if item["rotation"] != target["rotation"]:
            errors.append("映射屏幕方向不同")
        translated = {axis: point[axis] - item["bounds"][axis] + target["bounds"][axis] for axis in ("x", "y")}
        if not contains(target["bounds"], translated):
            errors.append("实际轨迹位置超出映射目标")
    return errors



def display_replay_errors(mapping, context):
    trajectory, topology = context["trajectory"], context["topology"]
    source = trajectory.get("source")
    errors = topology_errors(topology) + trajectory_source_errors(trajectory)
    if source is None:
        return errors + ["跨坐标空间回放需要轨迹来源"]
    source_local = source["topology"]["coordinateSpace"] == "displayLocalPixels"
    target_local = topology["coordinateSpace"] == "displayLocalPixels"
    sources = {d["displayId"]: d for d in source["topology"]["displays"] if d["displayId"] in source["displayIds"]}
    targets = {d["displayId"]: d for d in topology["displays"]}
    relative = any(e["kind"] == "pointer.delta" for e in trajectory["events"])
    if source["initialPointer"] is None:
        errors.append("请在轨迹编辑器设置起点")
    if source["scope"] == "allDisplays" and context["targetKind"] != "desktop":
        errors.append("全显示屏轨迹需要桌面输入目标")
    if mapping["mode"] == "original":
        pairs = {key: key for key in sources}
        if source_local != target_local or source.get("inputProfile") != context.get("inputProfile"):
            errors.append("原布局回放不能改变坐标空间或输入后端")
        for key, item in sources.items():
            target = targets.get(key)
            fields = ("pixelSize", "layoutBounds", "rotation", "pixelToNative") if source_local and target_local else ("bounds", "rotation", "dpiX", "dpiY")
            if target is None or any(item.get(field) != target.get(field) for field in fields):
                errors.append("来源与当前显示布局不同")
    else:
        rows = mapping["displays"]
        pairs = {row["sourceDisplayId"]: row["targetDisplayId"] for row in rows}
        if len(pairs) != len(rows) or set(pairs) != set(sources):
            errors.append("显示屏映射缺失或来源重复")
        if len(set(pairs.values())) != len(pairs):
            errors.append("多个来源不能映射到同一显示区域")
    if relative:
        if mapping["mode"] != "original" or source.get("inputProfile") != context.get("inputProfile") or source.get("inputProfile", {}).get("relativeUnit", "none") == "none":
            errors.append("相对输入需要相同后端、单位与原布局")
        if {k:v for k,v in source["topology"].items() if k != "version"} != {k:v for k,v in topology.items() if k != "version"}:
            errors.append("相对输入需要完整原显示拓扑")
    if not set(pairs.values()) <= set(context["allowedDisplayIds"]) or not set(pairs.values()) <= set(targets):
        errors.append("目标显示屏不存在或未获输入授权")
    points = [e for e in trajectory["events"] if e["kind"] == "pointer.move"]
    if source["initialPointer"] is not None:
        points.append(source["initialPointer"])
    for point in points:
        candidates = [sources[point["displayId"]]] if source_local and point.get("displayId") in sources else []
        if not source_local:
            candidates = [item for item in sources.values() if contains(item["bounds"], point)]
        if len(candidates) != 1:
            errors.append("来源位置不能唯一对应显示区域")
            continue
        item = candidates[0]
        target = targets.get(pairs.get(item["displayId"]))
        if target is None:
            continue
        x = point["x"] if source_local else point["x"] - item["bounds"]["x"]
        y = point["y"] if source_local else point["y"] - item["bounds"]["y"]
        size = target["pixelSize"] if target_local else target["bounds"]
        if item["rotation"] != target["rotation"] or not (0 <= x < size["width"] and 0 <= y < size["height"]):
            errors.append("屏内映射方向不同或实际位置越界")
        if target_local and target["pixelToNative"] is None:
            errors.append("目标缺少原生输入坐标映射")
    return errors


def recording_errors(value, kind, context=None):
    if kind == "topology":
        return topology_errors(value)
    if kind == "source":
        return source_errors(value)
    if kind == "startResult":
        return source_errors(value["source"]) + (["录制启动需要实际指针位置"] if value["source"]["initialPointer"] is None else [])
    if kind == "start":
        if context is None:
            return ["缺少录制目标上下文"]
        errors = topology_errors(context["topology"])
        if value["targetToken"] != context["targetToken"] or "record" not in context["permissions"]:
            errors.append("目标未授予此录制用途")
        if value["expectedTopologyVersion"] != context["topology"]["version"]:
            errors.append("显示拓扑已变化")
        if context["targetKind"] not in {"desktop", "display", "window"}:
            errors.append("此目标不支持录制")
        if context["topology"]["coordinateSpace"] == "displayLocalPixels" and value["coordinateMode"] == "physical":
            errors.append("当前拓扑需要屏内像素坐标")
        capabilities = context.get("capabilities")
        if capabilities is not None:
            errors.extend(capability_errors(capabilities))
            required = ["record.keyboard", "record.pointerRelative" if value["coordinateMode"] == "relative" else "record.pointerAbsolute"]
            if context["targetKind"] == "desktop":
                required.append("record.allDisplays")
            if any(capabilities["features"][key]["state"] != "available" for key in required):
                errors.append("当前会话缺少此范围的录制能力")
            if value["coordinateMode"] == "display" and "1.1" not in capabilities["trajectoryVersions"]:
                errors.append("当前后端未协商屏内坐标轨迹版本")
        return errors
    if kind == "snapshot":
        errors = source_errors(value["source"])
        if value["state"] in {"recording", "pausing", "paused"} and value["currentTopologyVersion"] != value["source"]["topology"]["version"]:
            errors.append("录制不能在两套布局之间继续")
        if value["state"] == "finished":
            if value["partial"] and value["reason"] not in {"DISPLAY_TOPOLOGY_CHANGED", "TARGET_UNAVAILABLE", "AUTHORIZATION_REVOKED", "SESSION_UNAVAILABLE", "EVENT_LIMIT", "PROCESS_INTERRUPTED", "RECORDING_SCOPE_LEFT", "INPUT_QUEUE_OVERFLOW", "INPUT_CAPTURE_LOST"}:
                errors.append("分段结束需要具体中断原因")
            if not value["partial"] and value["reason"] != "USER_FINISHED":
                errors.append("完整录制结束状态不一致")
        return errors
    if kind == "replay":
        return replay_errors(value, context) if context is not None else ["缺少回放目标上下文"]
    return ["未知录制对象类型"]
