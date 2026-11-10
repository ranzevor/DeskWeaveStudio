from __future__ import annotations

from deskweave.contracts import validate
from deskweave.errors import DeskWeaveError


def _invalid(message):
    raise DeskWeaveError("INVALID_CONTENT", message)


def validate_trajectory(value, *, duration_us=None, checkpoint=lambda: None):
    validate("urn:deskweave:trajectory:1", value, checkpoint=checkpoint)
    events, mode = value["events"], value["coordinateMode"]
    duration = int(events[-1]["atUs"]) + 1 if duration_us is None else duration_us
    if type(duration) is not int or not 1 <= duration <= 86_400_000_001:
        _invalid("轨迹时长无效。")
    source = value.get("source")
    local, selected, displays = False, set(), {}
    if source:
        topology = source["topology"]
        local = topology["coordinateSpace"] == "displayLocalPixels"
        displays = {d["displayId"]: d for d in topology["displays"]}
        selected = set(source["displayIds"])
        if len(displays) != len(topology["displays"]) or not selected <= displays.keys():
            _invalid("轨迹来源包含重复或不存在的显示屏。")
        primary = sum(d["primary"] is True for d in displays.values())
        if primary > 1 or not local and primary != 1:
            _invalid("轨迹来源的主显示屏数量无效。")
        if source["scope"] == "display" and len(selected) != 1:
            _invalid("单屏轨迹需要一个来源显示屏。")
        if source["scope"] == "allDisplays" or not local and source["scope"] == "window":
            if selected != displays.keys():
                _invalid("轨迹来源未覆盖完整显示集合。")
        if local:
            for display in displays.values():
                checkpoint()
                matrix = display["pixelToNative"]
                if matrix is not None and matrix[0] * matrix[3] == matrix[1] * matrix[2]:
                    _invalid("轨迹来源的坐标变换不可逆。")
            if source["scope"] == "allDisplays" and (
                topology["coverage"] != "all"
                or any(
                    d["layoutBounds"] is None or d["pixelToNative"] is None
                    for d in displays.values()
                )
            ):
                _invalid("全部显示屏轨迹需要完整位置映射。")
            native = {
                "windows-win32": "windowsPhysicalPixels",
                "macos-quartz": "quartzPoints",
                "linux-x11": "x11RootPixels",
                "linux-wayland-portal": "eiRegions",
                "linux-wayland-compositor": "eiRegions",
            }
            if native[source["inputProfile"]["backend"]] != topology["nativeSpace"]:
                _invalid("轨迹来源后端与坐标空间不符。")
        else:
            rectangles = [d["bounds"] for d in displays.values()]
            left, top = min(r["x"] for r in rectangles), min(r["y"] for r in rectangles)
            right = max(r["x"] + r["width"] for r in rectangles)
            bottom = max(r["y"] + r["height"] for r in rectangles)
            if (
                topology["virtualBounds"]
                != {"x": left, "y": top, "width": right - left, "height": bottom - top}
                or max(right, bottom) > 2147483648
                or max(right - left, bottom - top) > 2147483647
            ):
                _invalid("轨迹来源的虚拟桌面范围无效。")
            if len({tuple(r[k] for k in ("x", "y", "width", "height")) for r in rectangles}) != len(
                rectangles
            ):
                _invalid("镜像显示区域需要合并为一个逻辑显示屏。")
    if mode == "display" and not local or local and mode == "physical":
        _invalid("轨迹坐标模式与来源不符。")

    def check_point(point):
        if local:
            display = displays.get(point.get("displayId"))
            if (
                display is None
                or display["displayId"] not in selected
                or not (
                    0 <= point["x"] < display["pixelSize"]["width"]
                    and 0 <= point["y"] < display["pixelSize"]["height"]
                )
            ):
                _invalid("轨迹位置缺少显示身份或超出所选屏幕。")
        elif "displayId" in point:
            _invalid("带显示身份的位置需要屏内像素来源。")
        elif source and not any(
            d["bounds"]["x"] <= point["x"] < d["bounds"]["x"] + d["bounds"]["width"]
            and d["bounds"]["y"] <= point["y"] < d["bounds"]["y"] + d["bounds"]["height"]
            for identity, d in displays.items()
            if identity in selected
        ):
            _invalid("轨迹位置落在所选显示区域之外。")

    if source and source["initialPointer"] is not None:
        check_point(source["initialPointer"])
    held, previous = set(), -1
    for event in events:
        checkpoint()
        if not previous <= event["atUs"] < duration:
            _invalid("轨迹事件时间逆序或超出时长。")
        previous, kind = event["atUs"], event["kind"]
        if kind == "pointer.move":
            if mode == "relative":
                _invalid("相对轨迹不能包含绝对位置。")
            check_point(event)
        if kind == "pointer.delta":
            if mode in {"physical", "display"}:
                _invalid("绝对轨迹不能包含相对位移。")
            if value["formatVersion"] == "1.1" and (
                not source or source.get("inputProfile", {}).get("relativeUnit", "none") == "none"
            ):
                _invalid("相对轨迹缺少来源单位。")
        if kind in {"key.down", "key.up", "pointer.button"}:
            key = ("key", event["key"]) if kind.startswith("key.") else ("button", event["button"])
            down = kind == "key.down" or event.get("state") == "down"
            if down == (key in held):
                _invalid("轨迹包含重复按下或未配对释放。")
            if down:
                held.add(key)
            else:
                held.remove(key)
    if held:
        _invalid("轨迹结束时仍有输入按住。")
    return {
        "eventCount": len(events),
        "durationUs": duration,
        "coordinateMode": mode,
        "formatVersion": value["formatVersion"],
    }
