"""平台能力、屏内像素坐标与原生变换的语义检查。"""
from __future__ import annotations


def local_topology_errors(value):
    errors = []
    displays = value["displays"]
    ids = [d["displayId"] for d in displays]
    if len(ids) != len(set(ids)):
        errors.append("显示屏身份重复")
    if sum(d["primary"] is True for d in displays) > 1:
        errors.append("主显示区域重复")
    for display in displays:
        matrix = display["pixelToNative"]
        if matrix is not None:
            a, b, c, d, tx, ty = matrix
            if a * d - b * c == 0:
                errors.append("像素到原生坐标的变换不可逆")
    return errors


def local_point_valid(point, displays):
    display = next((d for d in displays if d["displayId"] == point.get("displayId")), None)
    return display is not None and 0 <= point["x"] < display["pixelSize"]["width"] and 0 <= point["y"] < display["pixelSize"]["height"]


def local_source_errors(value):
    topology = value["topology"]
    errors = local_topology_errors(topology)
    all_ids = {d["displayId"] for d in topology["displays"]}
    selected = set(value["displayIds"])
    if not selected <= all_ids:
        errors.append("来源引用了不存在的显示屏")
    if value["scope"] == "allDisplays":
        if topology["coverage"] != "all" or selected != all_ids:
            errors.append("全部显示屏录制需要完整桌面覆盖")
        if any(d["layoutBounds"] is None or d["pixelToNative"] is None for d in topology["displays"]):
            errors.append("全部显示屏录制缺少完整位置映射")
    if value["scope"] == "display" and len(selected) != 1:
        errors.append("单屏录制只能选择一个显示区域")
    point = value["initialPointer"]
    if point is not None and not local_point_valid(point, [d for d in topology["displays"] if d["displayId"] in selected]):
        errors.append("起始指针不在所选显示区域")
    expected = {"windows-win32": "windowsPhysicalPixels", "macos-quartz": "quartzPoints", "linux-x11": "x11RootPixels", "linux-wayland-portal": "eiRegions", "linux-wayland-compositor": "eiRegions"}
    if expected[value["inputProfile"]["backend"]] != topology["nativeSpace"]:
        errors.append("来源后端与原生坐标空间不符")
    return errors


def capability_errors(value):
    errors = []
    backend = value["backend"]
    os = "windows" if backend == "windows-win32" else "macos" if backend == "macos-quartz" else "linux"
    if value["os"] != os:
        errors.append("系统与桌面后端不匹配")
    features = value["features"]
    for item in features.values():
        if item["state"] == "available" and (item["reason"] != "NONE" or item["nextAction"] != "none"):
            errors.append("可用能力不能同时要求授权或恢复")
        if item["state"] != "available" and item["reason"] == "NONE":
            errors.append("不可用能力缺少具体原因")
        if item["state"] == "permissionRequired" and item["nextAction"] not in {"grantPermission", "selectSource"}:
            errors.append("授权状态缺少对应操作")
    if features["record.allDisplays"]["state"] == "available":
        if value["topologyCoverage"] != "all" or value["topologyVersion"] is None:
            errors.append("全部显示屏录制能力缺少完整拓扑")
        if any(features[k]["state"] != "available" for k in ("record.pointerAbsolute", "record.keyboard")):
            errors.append("全部显示屏录制缺少位置或键盘事件")
    if backend == "linux-wayland-portal" and any(features[k]["state"] == "available" for k in ("record.pointerAbsolute", "record.pointerRelative", "record.keyboard", "record.allDisplays")):
        errors.append("Portal 输入捕获不能作为普通桌面的被动录制")
    return errors


def platform_errors(value, kind):
    if kind == "capabilities":
        return capability_errors(value)
    if kind == "topology":
        return local_topology_errors(value)
    if kind == "source":
        return local_source_errors(value)
    return ["未知平台对象类型"]
