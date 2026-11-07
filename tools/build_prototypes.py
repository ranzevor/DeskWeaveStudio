"""从界面清单生成离线画册数据与 Markdown 索引。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOTYPES = ROOT / "docs/prototypes"


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def expression(*path):
    return {"$expr": {"op": "path", "path": list(path)}}


def node(key, title, action, params, on=None, store=None):
    result = {"id": key, "title": title, "action": action, "params": params, "on": on or {}}
    if store:
        result["store"] = store
    return result


def workflow(key, name, nodes, resources):
    return {
        "formatVersion": "1.0", "id": key, "name": name, "entry": nodes[0]["id"],
        "parameters": {"month": {"type": "string", "required": True, "sensitive": False, "default": "2026-10"}},
        "variables": {"matchResult": None, "checkResult": None, "downloadComplete": False},
        "targets": {"main": {"kind": "window", "required": True}},
        "resources": resources,
        "limits": {"activeMs": 600000, "steps": 10000, "callDepth": 8, "parallelCalls": 8, "inputEvents": 100000},
        "nodes": nodes,
        "extensions": {
            "studio.parameter-labels": {"month": "月份"},
            "studio.variable-labels": {"matchResult": "定位结果", "checkResult": "校验结果", "downloadComplete": "下载已完成"}
        },
    }


def fixtures():
    match = {"target": "main", "image": "downloadTemplate", "threshold": 0.92,
             "timeoutMs": 5000, "intervalMs": 200, "selection": "unique"}
    nodes = [
        node("wait", "等待报表加载", "time.wait", {"durationMs": 500}, {"next": "locate"}),
        node("locate", "定位下载按钮", "vision.match", match, {"next": "click", "error": "notReady"}, "matchResult"),
        node("click", "点击下载", "pointer.click", {
            "target": "main", "position": expression("vars", "matchResult", "selected", "position"),
            "button": "left", "clicks": 1, "intervalMs": 0}, {"next": "verify"}),
        node("verify", "校验下载结果", "flow.call", {
            "workflow": "checkDownload", "inputs": {"month": expression("inputs", "month")}, "mode": "wait"},
            {"next": "assert"}, "checkResult"),
        node("assert", "检查是否完成", "logic.assert", {
            "condition": expression("vars", "checkResult", "ready"), "message": "未发现下载完成提示"},
            {"next": "return", "error": "notReady"}),
        node("return", "返回下载结果", "flow.return",
             {"values": {"month": expression("inputs", "month"), "completed": True}}),
        node("notReady", "返回未就绪", "flow.return",
             {"values": {"month": expression("inputs", "month"), "completed": False}}),
    ]
    main = workflow("monthly-report", "月度报表下载", nodes,
                    {"downloadTemplate": {"kind": "image"}, "checkDownload": {"kind": "workflow"}})
    child = workflow("check-download", "校验下载结果", [
        node("locate", "定位完成提示", "vision.match",
             {**match, "image": "successTemplate"}, {"next": "return", "error": "notReady"}, "matchResult"),
        node("return", "返回校验结果", "flow.return",
             {"values": {"ready": {"$expr": {"op": "notEqual", "args": [expression("vars", "matchResult", "selected"), None]}}}}),
        node("notReady", "返回未就绪", "flow.return", {"values": {"ready": False}}),
    ], {"successTemplate": {"kind": "image"}})
    return main, child


def schema_root(value):
    definitions = {data["$id"]: data for data in (json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "contracts").glob("*.schema.json"))}
    while "$ref" in value:
        uri, _, pointer = value["$ref"].partition("#")
        current = definitions[uri]
        for part in pointer.lstrip("/").split("/") if pointer else []:
            current = current[part.replace("~1", "/").replace("~0", "~")]
        value = current
    return value


def outputs():
    groups = sorted((json.loads(p.read_text(encoding="utf-8")) for p in (PROTOTYPES / "catalog").glob("*.json")),
                    key=lambda g: g["order"])
    screens = []
    for group in groups:
        for original in group["screens"]:
            screen = {**original, "group": group["key"],
                      "image": original["code"] + "-" + original["id"] + ".png"}
            screens.append(screen)
    ids = [s["id"] for s in screens]
    codes = [s["code"] for s in screens]
    assert len(ids) == len(set(ids)) and len(codes) == len(set(codes)), "界面身份重复"
    for screen in screens:
        kind = screen.setdefault("primaryKind", "navigate")
        assert kind in {"navigate", "validate", "download", "local"}, screen["id"]
        if kind in {"navigate", "validate"}:
            assert screen["primary"] in ids, screen["id"]
        else:
            assert screen["primary"] is None and screen.get("primaryResult"), screen["id"]
        assert screen["parent"] is None or screen["parent"] in ids, screen["id"]
        if screen.get("completion"):
            assert screen["completion"] in ids and screen["completion"] != screen["id"], screen["id"]
    main, child = fixtures()
    catalog = {
        "designVersion": "0.1.0", "updatedAt": "2026-11-07",
        "groups": [{k: g[k] for k in ("key", "name", "order")} for g in groups],
        "screens": screens, "fixture": {"workflow": main, "child": child},
    }
    catalog["authoringFixtures"] = {name: json.loads((ROOT / ("examples/authoring/" + name + ".json")).read_text(encoding="utf-8"))
        for name in ("asset-image-plan", "asset-image-commit", "asset-image-result", "connection-test-draft", "workflow-model", "workflow-model-dependencies", "edited-trajectory")}
    catalog["recordingFixtures"] = {key: json.loads((ROOT / ("examples/recording/" + name + ".json")).read_text(encoding="utf-8")) for key, name in {"topology":"topology", "source":"source", "trajectory":"cross-screen-drag"}.items()}
    variable_value = [{"month": "2026-10", "amount": i} for i in range(200)]
    variable_bytes = json.dumps(variable_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    catalog["authoringFixtures"]["largeVariable"] = {"value": variable_value, "reference": {"mediaType": "application/json", "resourceId": "variable-rows", "length": len(variable_bytes), "sha256": hashlib.sha256(variable_bytes).hexdigest()}}
    catalog["authoringFixtures"]["trajectorySourceHash"] = hashlib.sha256((ROOT / "examples/trajectories/shortcut.json").read_bytes()).hexdigest()
    catalog["authoringFixtures"]["trajectorySource"] = json.loads((ROOT / "examples/trajectories/shortcut.json").read_text(encoding="utf-8"))
    action_schema = json.loads((ROOT / "contracts/actions.schema.json").read_text(encoding="utf-8"))
    action_catalog = json.loads((ROOT / "contracts/action-catalog.json").read_text(encoding="utf-8"))["actions"]
    groups_zh = {"time": "时间", "data": "数据", "logic": "逻辑", "flow": "流程", "vision": "定位与观察", "input": "输入"}
    catalog["actionModels"] = {
        a["id"]: {"label": a["label"], "category": groups_zh[a["group"]],
                  "parameters": action_schema["$defs"][a["id"]]["properties"]["params"]["properties"],
                  "required": action_schema["$defs"][a["id"]]["properties"]["params"].get("required", []),
                  "storable": "store" in action_schema["$defs"][a["id"]]["properties"],
                  "variable": "定位结果" if a["id"] == "vision.match" else ""}
        for a in action_catalog
    }
    mcp = json.loads((ROOT / "contracts/mcp-catalog.json").read_text(encoding="utf-8"))
    service = json.loads((ROOT / "contracts/service.schema.json").read_text(encoding="utf-8"))
    catalog["mcpModels"] = {
        "tools": [{**tool, "parameters": schema_root(tool.get("publishedInputSchema", service["$defs"][tool["method"]]["properties"]["params"])),
                   "result": service["$defs"][tool["method"]]["properties"]["result"]} for tool in mcp["tools"]],
        "resources": mcp["resources"], "prompts": mcp["prompts"],
        "protocolVersion": mcp["protocolVersion"], "supportedVersions": mcp["supportedVersions"],
        "protocolProfiles": mcp["protocolProfiles"], "defaultTransports": mcp["defaultTransports"],
    }
    catalog["queryFixtures"] = json.loads((ROOT / "examples/queries/datasets.json").read_text(encoding="utf-8"))
    catalog["settingModels"] = json.loads((ROOT / "contracts/settings-catalog.json").read_text(encoding="utf-8"))
    catalog["platformFixtures"] = {name: json.loads((ROOT / "examples/platform" / (name + ".json")).read_text(encoding="utf-8")) for name in ("macos-capabilities", "macos-permissions", "wayland-capabilities", "macos-cross-display", "context-cross-backend")}
    doc = [
        "# 全界面原型设计", "",
        "设计版本：0.1.0  ", "更新日期：2026-11-07", "",
        f"共 {len(screens)} 个界面，覆盖页面、属性面板、抽屉、弹窗、浮条、选区覆盖层、安装维护及关键异常状态。",
        "", "[打开可点击原型](../prototypes/index.html) · [原型使用说明](../prototypes/README.md) · [界面数据清单](../prototypes/catalog.json)",
        "", "## 主要界面", "", "![主要界面概览](../prototypes/overview/key-screens.png)", "",
        "## 画板与交互", "",
        "常规画板为 1440×900，窄窗口为 800×600。原型沿用工作台的导航、语义配色、可见焦点和三视图文档结构。",
        "画册左侧按任务分组，可搜索界面；右侧列出入口、主操作、画板和需求。图中的按钮可在原型中跳转，表单选择在当前页面预览。",
        "",
        "窗口选择器和权限提升由系统负责；相应图展示选择参考布局与应用交接状态。急停解除不恢复旧运行；保存失败另有源码原文与有效结构的副本入口。",
        "",
        "中文参数与变量名在表单中作为显示别名，分别保存到 extensions 的 studio.parameter-labels 和 studio.variable-labels；执行键与表达式 AST 保持公共契约格式。",
        "",
        "## 核心任务路径", "",
        "| 任务 | 界面路径 |", "|---|---|",
        "| 首次制作 | 首次启动 → 新建工作空间 → 新建流程 → 录制准备 → 时间轴 → 流程画布 |",
        "| 图像定位 | 目标窗口 → 截图选区 → 模板制作 → 匹配测试 → 节点属性 |",
        "| 运行与调试 | 运行准备 → 运行监控 → 暂停过程 → 断点与单步 → 完成或失败详情 |",
        "| 接管与恢复 | 急停锁定 / 输入清理失败 → 本地确认 → 保留已停止记录 → 新运行 |",
        "| 版本与草稿 | 流程版本 → 差异 → 恢复新草稿；保存失败 → 草稿副本 |",
        "| 任务包 | 导出固定版本 → 导入校验 → 同名处理 → 重新绑定目标 |",
        "| 批量模板 | 资产 → 提取条件 → 候选筛选 → 命名预览 → 冲突处理 → 保存批次 → 导出 |",
        "| 持续观察 | 观察中心 → 会话配置 → 运行与停止 → 历史过滤 → 记录详情 → 导出 |",
        "| 高级配置 | 模型参数 → 图像消息编辑与预览；MCP 传输 → TLS → 令牌 → 能力目录 |",
        "| 指定入口 | 缺失值检查 → 初始值 → 启动准备 → 新运行；目标改变后重新检查 |",
        "| 外部集成 | MCP 客户端 → 对象与能力授权 → 客户端活动 → 关联运行 |",
        "| 安装与更新 | 安装选项 → 组件安装 → 更新准备 → 数据检查 → 打开或恢复新目录 |",
        "",
    ]
    by_id = {s["id"]: s for s in screens}
    def outcome(screen):
        kind = screen["primaryKind"]
        if kind in {"local", "download"}:
            result = screen["primaryResult"]
        elif kind == "validate":
            result = "校验通过 → " + by_id[screen["primary"]]["title"]
        else:
            result = by_id[screen["primary"]]["title"]
        if screen.get("completion"):
            result += "；完成 → " + by_id[screen["completion"]]["title"]
        return result
    for group in groups:
        members = [s for s in screens if s["group"] == group["key"]]
        doc += [f"## {group['name']}", "",
                f"![{group['name']}原型总览](../prototypes/overview/{group['key']}.png)", "",
                "| 编号 | 界面与设计图 | 操作任务 | 主要操作与完成结果 |", "|---|---|---|---|"]
        for s in members:
            doc.append(f"| {s['code']} | [{s['title']}](../prototypes/images/{s['image']}) · [交互](../prototypes/index.html#{s['id']}) | {s['purpose']} | {s['primaryLabel']}：{outcome(s)} |")
        doc.append("")
    doc += ["## 需求覆盖", "", "| 需求 | 对应画板 |", "|---|---|"]
    import re
    reqs = re.findall(r"^\| (DW-(?:N)?\d+) \|", (ROOT / "docs/product/requirements.md").read_text(encoding="utf-8"), re.M)
    for req in reqs:
        covered = [s for s in screens if req in s["requirements"]]
        assert covered, "原型未覆盖 " + req
        doc.append("| " + req + " | " + "、".join(s["code"] for s in covered) + " |")
    doc += ["", "## 使用与验收", "",
            "逐图检查中文可读性、主操作位置、字段错误、状态含义与退出路径；分别验证页面切换、搜索、表单、键盘、急停确认、暗色和窄窗口。",
            "静态图与交互源保持同一版；图像、页面和交互的检查入口见[原型检查](../quality/prototype-validation.md)。",
            ""]
    readme = [
        "# DeskWeaveStudio 原型画册", "",
        f"共 {len(screens)} 个界面，按 {len(groups)} 个任务域组织。",
        "", "- [交互入口](index.html)：用浏览器直接打开，无需安装依赖或启动服务。",
        "- [完整图文目录](../design/prototypes.md)：按任务查看总览与每张原图。",
        "- [全部画板总览](overview/all-screens.png)：定位所有画板。",
        "- [界面数据](catalog.json)：画板、状态、需求与跳转关系。",
        "", "## 浏览", "",
        "需要在浏览器中预览时，在项目根目录运行 `python tools/serve_prototypes.py`，打开返回的本机地址。服务使用实时文件并禁用浏览器缓存。", "",
        "左侧搜索页面名称或编号，使用前后按钮或 Alt+方向键切换。页面内按钮跳转到相关界面；Esc 返回触发界面，Ctrl+K 打开命令菜单。急停确认需要先勾选本地接管确认。",
        "", "默认主画板 1440×900，V02 为 800×600。点击“查看设计图”可读取实际分辨率的 PNG。",
        "",
        "## 维护", "",
        "catalog/ 为界面清单源，src/ 为界面渲染与交互源，fixtures/ 为可校验的流程数据。catalog.json、src/data.js 和图文目录由构建工具生成。",
        "", "在项目根目录执行：", "",
        "```bash",
        "python tools/build_prototypes.py",
        "python tools/build_prototypes.py --check",
        "python tools/check_prototypes.py",
        "python tools/check_docs.py --contracts",
        "```", "",
        "重新生成设计图需要 Node.js/npm、Chrome，以及 requirements-prototypes.txt 中的 Pillow。安装工具后运行 python tools/render_prototypes.py，随后运行 python tools/compose_prototypes.py。渲染器使用固定版本 Playwright CLI，创建独立本机预览与浏览器会话，结束时关闭；合成工具生成图册总览。",
        "",
        "运行 python tools/render_prototypes.py --check-only 检查全部页面与交互，或使用 --interactions-only 仅检查交互。--interaction-suite baseline、completion、authoring、content、recording、query 或 platform 可选择单组；交互结果在运行时校验。", "",
    ]
    return {
        PROTOTYPES / "catalog.json": encode(catalog),
        PROTOTYPES / "src/data.js": "window.DW_CATALOG = " + json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) + ";\n",
        PROTOTYPES / "fixtures/workflow.json": encode(main),
        PROTOTYPES / "fixtures/check-download.json": encode(child),
        ROOT / "docs/design/prototypes.md": "\n".join(doc),
        PROTOTYPES / "README.md": "\n".join(readme),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = outputs()
    for path, content in output.items():
        if args.check:
            assert path.is_file() and path.read_text(encoding="utf-8") == content, str(path.relative_to(ROOT))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    print("原型数据与索引：" + ("一致" if args.check else "已生成") + f"；{len(output)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
