from __future__ import annotations

import math
import platform as _platform
import sys
from collections.abc import Callable

from deskweave.contracts import validate
from deskweave.errors import DeskWeaveError

_BACKEND_OS = {
    "windows-win32": "windows",
    "macos-quartz": "macos",
    "linux-x11": "linux",
    "linux-wayland-portal": "linux",
    "linux-wayland-compositor": "linux",
}

_BACKEND_NATIVE_SPACE = {
    "windows-win32": "windowsPhysicalPixels",
    "macos-quartz": "quartzPoints",
    "linux-x11": "x11RootPixels",
    "linux-wayland-portal": "eiRegions",
    "linux-wayland-compositor": "eiRegions",
}

_RECORD_FEATURES = (
    "record.pointerAbsolute",
    "record.pointerRelative",
    "record.keyboard",
    "record.allDisplays",
)


def current_platform_tag() -> str:
    if sys.platform == "win32":
        os_name = "windows"
    elif sys.platform == "darwin":
        os_name = "macos"
    else:
        os_name = "linux"
    machine = _platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    return f"{os_name}-{arch}"


def _contains(rect: dict, point: dict) -> bool:
    return (
        rect["x"] <= point["x"] < rect["x"] + rect["width"]
        and rect["y"] <= point["y"] < rect["y"] + rect["height"]
    )


def _local_point_valid(point: dict, displays: list[dict]) -> bool:
    display = next((d for d in displays if d["displayId"] == point.get("displayId")), None)
    return (
        display is not None
        and 0 <= point["x"] < display["pixelSize"]["width"]
        and 0 <= point["y"] < display["pixelSize"]["height"]
    )


def _validate_matrix(matrix: object) -> None:
    if (
        type(matrix) not in (list, tuple)
        or len(matrix) != 6
        or any(
            type(item) not in (int, float)
            or not math.isfinite(item)
            or not -1_000_000_000_000.0 <= item <= 1_000_000_000_000.0
            for item in matrix
        )
    ):
        raise DeskWeaveError("INVALID_ARGUMENT", "像素到原生坐标变换分量无效。")
    a, b, c, d, _tx, _ty = matrix
    det = a * d - b * c
    if not math.isfinite(det) or det == 0:
        raise DeskWeaveError("INVALID_ARGUMENT", "像素到原生坐标的变换不可逆。")


def pixel_to_native(matrix: list[float], x: float, y: float) -> tuple[float, float]:
    _validate_matrix(matrix)
    if (
        type(x) not in (int, float)
        or type(y) not in (int, float)
        or not math.isfinite(x)
        or not math.isfinite(y)
    ):
        raise DeskWeaveError("INVALID_ARGUMENT", "坐标必须为有限数值。")
    a, b, c, d, tx, ty = matrix
    nx = a * x + c * y + tx
    ny = b * x + d * y + ty
    if not math.isfinite(nx) or not math.isfinite(ny):
        raise DeskWeaveError("INVALID_ARGUMENT", "原生坐标变换溢出。")
    return (nx, ny)


def native_to_pixel(matrix: list[float], nx: float, ny: float) -> tuple[float, float]:
    _validate_matrix(matrix)
    if (
        type(nx) not in (int, float)
        or type(ny) not in (int, float)
        or not math.isfinite(nx)
        or not math.isfinite(ny)
    ):
        raise DeskWeaveError("INVALID_ARGUMENT", "坐标必须为有限数值。")
    a, b, c, d, tx, ty = matrix
    det = a * d - b * c
    dx = nx - tx
    dy = ny - ty
    px = (d * dx - c * dy) / det
    py = (-b * dx + a * dy) / det
    if not math.isfinite(px) or not math.isfinite(py):
        raise DeskWeaveError("INVALID_ARGUMENT", "逆向像素坐标变换溢出。")
    return (px, py)


def capability_errors(value: dict) -> list[str]:
    errors: list[str] = []
    backend = value["backend"]
    if value["os"] != _BACKEND_OS[backend]:
        errors.append("系统与桌面后端不匹配")
    features = value["features"]
    for item in features.values():
        if item["state"] == "available" and (
            item["reason"] != "NONE" or item["nextAction"] != "none"
        ):
            errors.append("可用能力不能同时要求授权或恢复")
        if item["state"] != "available" and item["reason"] == "NONE":
            errors.append("不可用能力缺少具体原因")
        if item["state"] == "permissionRequired" and item["nextAction"] not in {
            "grantPermission",
            "selectSource",
        }:
            errors.append("授权状态缺少对应操作")
    if features["record.allDisplays"]["state"] == "available":
        if value["topologyCoverage"] != "all" or value["topologyVersion"] is None:
            errors.append("全部显示屏录制能力缺少完整拓扑")
        if any(
            features[key]["state"] != "available"
            for key in ("record.pointerAbsolute", "record.keyboard")
        ):
            errors.append("全部显示屏录制缺少位置或键盘事件")
    if backend == "linux-wayland-portal" and any(
        features[key]["state"] == "available" for key in _RECORD_FEATURES
    ):
        errors.append("Portal 输入捕获不能作为普通桌面的被动录制")
    return errors


def local_topology_errors(value: dict) -> list[str]:
    errors: list[str] = []
    displays = value["displays"]
    ids = [d["displayId"] for d in displays]
    if len(ids) != len(set(ids)):
        errors.append("显示屏身份重复")
    if sum(d["primary"] is True for d in displays) > 1:
        errors.append("主显示区域重复")
    for display in displays:
        matrix = display["pixelToNative"]
        if matrix is not None:
            a, b, c, d, _tx, _ty = matrix
            det = a * d - b * c
            if not math.isfinite(det) or det == 0:
                errors.append("像素到原生坐标的变换不可逆")
    return errors


def topology_errors(value: dict) -> list[str]:
    if value["coordinateSpace"] == "displayLocalPixels":
        return local_topology_errors(value)
    errors: list[str] = []
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
    if value["virtualBounds"] != {
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
    }:
        errors.append("虚拟桌面范围与显示区域不一致")
    if (
        right > 2147483648
        or bottom > 2147483648
        or right - left > 2147483647
        or bottom - top > 2147483647
    ):
        errors.append("显示拓扑超出坐标范围")
    bounds = [(rect["x"], rect["y"], rect["width"], rect["height"]) for rect in rectangles]
    if len(bounds) != len(set(bounds)):
        errors.append("镜像输出须合并到同一逻辑显示区域")
    return errors


def source_errors(value: dict) -> list[str]:
    topology = value["topology"]
    if topology["coordinateSpace"] == "displayLocalPixels":
        errors = local_topology_errors(topology)
        all_ids = {d["displayId"] for d in topology["displays"]}
        selected = set(value["displayIds"])
        if not selected <= all_ids:
            errors.append("来源引用了不存在的显示屏")
        if value["scope"] == "allDisplays":
            if topology["coverage"] != "all" or selected != all_ids:
                errors.append("全部显示屏录制需要完整桌面覆盖")
            if any(
                d["layoutBounds"] is None or d["pixelToNative"] is None
                for d in topology["displays"]
            ):
                errors.append("全部显示屏录制缺少完整位置映射")
        if value["scope"] == "display" and len(selected) != 1:
            errors.append("单屏录制只能选择一个显示区域")
        point = value["initialPointer"]
        if point is not None and not _local_point_valid(
            point, [d for d in topology["displays"] if d["displayId"] in selected]
        ):
            errors.append("起始指针不在所选显示区域")
        if _BACKEND_NATIVE_SPACE[value["inputProfile"]["backend"]] != topology["nativeSpace"]:
            errors.append("来源后端与原生坐标空间不符")
        return errors

    errors = topology_errors(topology)
    ids = {item["displayId"] for item in topology["displays"]}
    selected = set(value["displayIds"])
    if not selected <= ids:
        errors.append("来源引用了不存在的显示屏")
    if value["scope"] in {"allDisplays", "window"} and selected != ids:
        errors.append("来源未覆盖固定显示集合")
    if value["scope"] == "display" and len(selected) != 1:
        errors.append("单屏录制只能选择一个显示区域")
    point = value["initialPointer"]
    if point is not None and not any(
        _contains(item["bounds"], point)
        for item in topology["displays"]
        if item["displayId"] in selected
    ):
        errors.append("起始指针位于显示区域之外")
    return errors


def trajectory_errors(value: dict) -> list[str]:
    source = value.get("source")
    moves = [event for event in value["events"] if event["kind"] == "pointer.move"]
    relative = any(event["kind"] == "pointer.delta" for event in value["events"])
    local = source is not None and source["topology"]["coordinateSpace"] == "displayLocalPixels"
    errors = source_errors(source) if source else []
    if value["coordinateMode"] == "display" and (not local or relative):
        errors.append("屏内像素模式需要对应来源且不能包含相对位移")
    if local and value["coordinateMode"] == "physical":
        errors.append("屏内像素来源不能解释成全局物理坐标")
    if (
        value["formatVersion"] == "1.1"
        and relative
        and (not source or source.get("inputProfile", {}).get("relativeUnit", "none") == "none")
    ):
        errors.append("相对轨迹缺少来源单位")
    if local:
        displays = [
            d for d in source["topology"]["displays"] if d["displayId"] in source["displayIds"]
        ]
        for event in moves:
            if not _local_point_valid(event, displays):
                errors.append("轨迹屏内位置缺少显示身份或超出像素范围")
    else:
        if any("displayId" in event for event in moves):
            errors.append("带显示身份的位置需要屏内像素来源")
        if source:
            rectangles = [
                d["bounds"]
                for d in source["topology"]["displays"]
                if d["displayId"] in source["displayIds"]
            ]
            for event in moves:
                if not any(_contains(rect, event) for rect in rectangles):
                    errors.append("轨迹位置落在显示区域之外")

    held: set[tuple[str, str]] = set()
    last = -1
    for event in value["events"]:
        if event["atUs"] < last:
            errors.append("轨迹时间逆序")
        last = event["atUs"]
        kind = event["kind"]
        if value["coordinateMode"] == "physical" and kind == "pointer.delta":
            errors.append("坐标模式冲突")
        if value["coordinateMode"] == "relative" and kind == "pointer.move":
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


def replay_errors(mapping: dict, context: dict) -> list[str]:
    trajectory, topology = context["trajectory"], context["topology"]
    source = trajectory.get("source")
    source_local = (
        source is not None and source["topology"]["coordinateSpace"] == "displayLocalPixels"
    )
    target_local = topology["coordinateSpace"] == "displayLocalPixels"
    errors = topology_errors(topology) + trajectory_errors(trajectory)
    if source is None:
        if source_local or target_local:
            return errors + ["跨坐标空间回放需要轨迹来源"]
        return errors + (["映射需要轨迹来源布局"] if mapping["mode"] != "original" else [])
    if source["initialPointer"] is None:
        errors.append("请在轨迹编辑器设置起点")
    if source["scope"] == "allDisplays" and context["targetKind"] != "desktop":
        errors.append("全显示屏轨迹需要桌面输入目标")

    sources = {
        d["displayId"]: d
        for d in source["topology"]["displays"]
        if d["displayId"] in source["displayIds"]
    }
    targets = {d["displayId"]: d for d in topology["displays"]}
    relative = any(e["kind"] == "pointer.delta" for e in trajectory["events"])

    if mapping["mode"] == "original":
        pairs = {key: key for key in sources}
        if source_local or target_local:
            if source_local != target_local or source.get("inputProfile") != context.get(
                "inputProfile"
            ):
                errors.append("原布局回放不能改变坐标空间或输入后端")
        else:
            profile = source.get("inputProfile")
            if profile and profile != context.get("inputProfile"):
                errors.append("原布局或相对输入回放需要相同输入后端与单位")
        for key, item in sources.items():
            target = targets.get(key)
            fields = (
                ("pixelSize", "layoutBounds", "rotation", "pixelToNative")
                if source_local and target_local
                else ("bounds", "rotation", "dpiX", "dpiY")
            )
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
        if source_local or target_local:
            if (
                mapping["mode"] != "original"
                or source.get("inputProfile") != context.get("inputProfile")
                or source.get("inputProfile", {}).get("relativeUnit", "none") == "none"
            ):
                errors.append("相对输入需要相同后端、单位与原布局")
            if {k: v for k, v in source["topology"].items() if k != "version"} != {
                k: v for k, v in topology.items() if k != "version"
            }:
                errors.append("相对输入需要完整原显示拓扑")
        else:
            if mapping["mode"] != "original":
                errors.append("相对输入不能按屏内像素平移")
            profile = source.get("inputProfile")
            if profile and profile != context.get("inputProfile"):
                errors.append("原布局或相对输入回放需要相同输入后端与单位")
            full_source = {item["displayId"]: item for item in source["topology"]["displays"]}
            if set(full_source) != set(targets) or any(
                any(
                    item[field] != targets[key][field]
                    for field in ("bounds", "rotation", "dpiX", "dpiY")
                )
                for key, item in full_source.items()
                if key in targets
            ):
                errors.append("相对轨迹需要原显示布局")

    if not set(pairs.values()) <= set(context["allowedDisplayIds"]) or not set(
        pairs.values()
    ) <= set(targets):
        errors.append("目标显示屏不存在或未获输入授权")

    points = [e for e in trajectory["events"] if e["kind"] == "pointer.move"]
    if source["initialPointer"] is not None:
        points.append(source["initialPointer"])
    for point in points:
        candidates = (
            [sources[point["displayId"]]]
            if source_local and point.get("displayId") in sources
            else []
        )
        if not source_local:
            candidates = [item for item in sources.values() if _contains(item["bounds"], point)]
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
        if item["rotation"] != target["rotation"] or not (
            0 <= x < size["width"] and 0 <= y < size["height"]
        ):
            errors.append("屏内映射方向不同或实际位置越界")
        if target_local and target["pixelToNative"] is None:
            errors.append("目标缺少原生输入坐标映射")
    return errors


def record_start_errors(request: dict, context: dict) -> list[str]:
    errors = topology_errors(context["topology"])
    if request["targetToken"] != context["targetToken"] or "record" not in context["permissions"]:
        errors.append("目标未授予此录制用途")
    if request["expectedTopologyVersion"] != context["topology"]["version"]:
        errors.append("显示拓扑已变化")
    if context["targetKind"] not in {"desktop", "display", "window"}:
        errors.append("此目标不支持录制")
    if (
        context["topology"]["coordinateSpace"] == "displayLocalPixels"
        and request["coordinateMode"] == "physical"
    ):
        errors.append("当前拓扑需要屏内像素坐标")
    capabilities = context.get("capabilities")
    if capabilities is not None:
        errors.extend(capability_errors(capabilities))
        required = [
            "record.keyboard",
            "record.pointerRelative"
            if request["coordinateMode"] == "relative"
            else "record.pointerAbsolute",
        ]
        if context["targetKind"] == "desktop":
            required.append("record.allDisplays")
        if any(capabilities["features"][key]["state"] != "available" for key in required):
            errors.append("当前会话缺少此范围的录制能力")
        if (
            request["coordinateMode"] == "display"
            and "1.1" not in capabilities["trajectoryVersions"]
        ):
            errors.append("当前后端未协商屏内坐标轨迹版本")
    return errors


def validate_desktop_capabilities(
    value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate("urn:deskweave:platform:1#/$defs/capabilities", value, checkpoint=checkpoint)
    assert isinstance(value, dict)
    issues = capability_errors(value)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return value


def validate_desktop_topology(
    value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate("urn:deskweave:recording:1#/$defs/topology", value, checkpoint=checkpoint)
    assert isinstance(value, dict)
    issues = topology_errors(value)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return value


def validate_recording_source(
    value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate("urn:deskweave:recording:1#/$defs/source", value, checkpoint=checkpoint)
    assert isinstance(value, dict)
    issues = source_errors(value)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return value


def validate_trajectory(value: object, *, checkpoint: Callable[[], None] = lambda: None) -> dict:
    validate("urn:deskweave:trajectory:1", value, checkpoint=checkpoint)
    assert isinstance(value, dict)
    issues = trajectory_errors(value)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return value


def validate_replay(
    mapping: object, context: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate("urn:deskweave:recording:1#/$defs/displayMapping", mapping, checkpoint=checkpoint)
    assert isinstance(mapping, dict)
    if type(context) is not dict or not {
        "trajectory",
        "topology",
        "allowedDisplayIds",
        "targetKind",
    } <= context.keys() <= {
        "trajectory",
        "topology",
        "allowedDisplayIds",
        "targetKind",
        "inputProfile",
    }:
        raise DeskWeaveError("INVALID_ARGUMENT", "回放上下文缺少必要字段或包含未知字段。")
    validate_desktop_topology(context["topology"], checkpoint=checkpoint)
    validate_trajectory(context["trajectory"], checkpoint=checkpoint)
    allowed = context["allowedDisplayIds"]
    if type(allowed) is not list or any(
        type(item) is not str or not 1 <= len(item) <= 256 for item in allowed
    ):
        raise DeskWeaveError("INVALID_ARGUMENT", "回放上下文允许的显示屏集合无效。")
    if type(context["targetKind"]) is not str or not 1 <= len(context["targetKind"]) <= 64:
        raise DeskWeaveError("INVALID_ARGUMENT", "回放上下文目标类型无效。")
    if "inputProfile" in context and context["inputProfile"] is not None:
        validate(
            "urn:deskweave:platform:1#/$defs/inputProfile",
            context["inputProfile"],
            checkpoint=checkpoint,
        )
    issues = replay_errors(mapping, context)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return mapping


def validate_record_start(
    request: object, context: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate(
        "urn:deskweave:service:1#/$defs/record.start/properties/params",
        request,
        checkpoint=checkpoint,
    )
    assert isinstance(request, dict)
    if type(context) is not dict or not {
        "targetToken",
        "targetKind",
        "topology",
        "permissions",
    } <= context.keys() <= {"targetToken", "targetKind", "topology", "permissions", "capabilities"}:
        raise DeskWeaveError("INVALID_ARGUMENT", "录制上下文缺少必要字段或包含未知字段。")
    if type(context["targetToken"]) is not str or not 1 <= len(context["targetToken"]) <= 256:
        raise DeskWeaveError("INVALID_ARGUMENT", "录制上下文目标令牌无效。")
    if type(context["targetKind"]) is not str or not 1 <= len(context["targetKind"]) <= 64:
        raise DeskWeaveError("INVALID_ARGUMENT", "录制上下文目标类型无效。")
    permissions = context["permissions"]
    if type(permissions) is not list or any(
        type(item) is not str or not 1 <= len(item) <= 64 for item in permissions
    ):
        raise DeskWeaveError("INVALID_ARGUMENT", "录制上下文权限集合无效。")
    validate_desktop_topology(context["topology"], checkpoint=checkpoint)
    if "capabilities" in context and context["capabilities"] is not None:
        validate_desktop_capabilities(context["capabilities"], checkpoint=checkpoint)
    issues = record_start_errors(request, context)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return request
