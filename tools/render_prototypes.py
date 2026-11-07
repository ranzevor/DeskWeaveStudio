"""使用固定版本 Playwright CLI 在隔离会话中截图全部原型。"""
from __future__ import annotations

import argparse
from datetime import datetime
import functools
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docs/prototypes"
CACHE = ROOT / ".cache/prototype-browser"
CLI_PACKAGE = "@playwright/cli@0.1.22"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass



def check_interaction_outputs(value, *, suite="all"):
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
    from check_docs import trajectory_errors
    from design_contract_checks import settings_errors, management_errors
    schemas = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "contracts").glob("*.schema.json")}
    registry = Registry().with_resources((data["$id"], Resource.from_contents(data)) for data in schemas.values())
    formats = FormatChecker()
    @formats.checks("date-time")
    def utc_seconds(value):
        if not isinstance(value, str):
            return True
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
            return False
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return False
        return True
    outputs = value.get("contractOutputs", [])
    completion_required = {
        "workflow.schema.json", "values.schema.json#/$defs/value", "trajectory.schema.json",
        "vision.schema.json#/$defs/roi", "execution-control.schema.json#/$defs/resumePlan",
        "execution-control.schema.json#/$defs/breakpointProfile", "package.schema.json#/$defs/importMapping",
        "asset-management.schema.json#/$defs/deleteResult", "asset-management.schema.json#/$defs/referencesResult",
        "bridge-management.schema.json#/$defs/tokenImpactPage", "settings.schema.json#/$defs/updateResult",
        "bridge-management.schema.json#/$defs/bridgeSnapshot", "bridge-management.schema.json#/$defs/tokenPage",
    }
    content_required = {
        "content.schema.json#/$defs/contentRef", "content.schema.json#/$defs/writeResult",
        "service.schema.json#/$defs/workflow.save/properties/params", "service.schema.json#/$defs/workflow.save/properties/result",
        "service.schema.json#/$defs/request.status/properties/result",
    }
    recovery_required = {
        "service.schema.json#/$defs/workflow.save/properties/params",
        "service.schema.json#/$defs/workflow.save/properties/result",
        "service.schema.json#/$defs/request.status/properties/result", "message.schema.json",
    }
    authoring_required = {
        "inspection.schema.json#/$defs/variablesPage", "inspection.schema.json#/$defs/revisionPage", "inspection.schema.json#/$defs/connectionPage",
        "authoring.schema.json#/$defs/draftTestResult", "authoring.schema.json#/$defs/editPlan", "authoring.schema.json#/$defs/editResult",
        "content.schema.json#/$defs/contentRef", "content.schema.json#/$defs/writeResult",
        "service.schema.json#/$defs/workflow.save/properties/params", "service.schema.json#/$defs/workflow.save/properties/result",
    }
    authoring_required |= content_required
    query_required = {'query.schema.json#/$defs/readParams', 'query.schema.json#/$defs/page', 'query.schema.json#/$defs/closeParams', 'query.schema.json#/$defs/closeResult'}
    recording_required = {"recording.schema.json#/$defs/topology", "recording.schema.json#/$defs/source", "service.schema.json#/$defs/record.start/properties/params", "service.schema.json#/$defs/record.start/properties/result", "service.schema.json#/$defs/record.snapshot/properties/result", "trajectory.schema.json", "recording.schema.json#/$defs/displayMapping"}
    platform_required = {"platform.schema.json#/$defs/capabilities", "recording.schema.json#/$defs/displayMapping", "trajectory.schema.json"}
    required = {"completion": completion_required, "content": content_required, "content-recovery": recovery_required, "authoring": authoring_required, "recording": recording_required, "query": query_required, "platform": platform_required}.get(suite, completion_required | authoring_required | recovery_required | recording_required | query_required | platform_required)
    assert {row["schema"] for row in outputs} == required, "原型实际输出未覆盖要求的契约"
    assert outputs and len({row["name"] for row in outputs}) == len(outputs), "缺少独立契约输出或输出身份重复"
    for output in outputs:
        filename, _, fragment = output["schema"].partition("#")
        schema = {"$ref": schemas[filename]["$id"] + ("#" + fragment if fragment else "")}
        errors = list(Draft202012Validator(schema, registry=registry, format_checker=formats).iter_errors(output["value"]))
        assert not errors, output["name"] + ": " + "; ".join(error.message for error in errors[:3])
        if output.get("platformType"):
            from platform_contract_checks import platform_errors
            assert not platform_errors(output["value"], output["platformType"]), output["name"]
        if output.get("queryType"):
            from query_contract_checks import query_errors
            assert not query_errors(output["value"], output["queryType"], output.get("context")), output["name"]
        if output.get("recordingType"):
            from recording_contract_checks import recording_errors
            assert not recording_errors(output["value"], output["recordingType"], output.get("context")), output["name"]
        if filename == "trajectory.schema.json":
            assert not trajectory_errors(output["value"]), output["name"]
        if filename == "settings.schema.json":
            catalog = json.loads((ROOT / "contracts/settings-catalog.json").read_text(encoding="utf-8"))
            assert not settings_errors(output["value"], catalog), output["name"]
        if filename == "bridge-management.schema.json":
            kind = "tokenImpactPage" if "tokenImpactPage" in fragment else "tokens" if "tokenPage" in fragment else "bridge"
            assert not management_errors(output["value"], kind), output["name"]
        if filename == "asset-management.schema.json" and "referencesResult" in fragment:
            assert not management_errors(output["value"], "assetReferencePage"), output["name"]
        if filename in {"inspection.schema.json", "authoring.schema.json"}:
            from authoring_contract_checks import authoring_errors
            kind = fragment.rsplit("/", 1)[-1]
            if kind in {"variablesPage", "revisionPage", "connectionPage", "draftTestResult", "editResult"}:
                assert not authoring_errors(output["value"], kind), output["name"]
        if filename == "authoring.schema.json" and "editResult" in fragment:
            result = output["value"]
            if result["kind"] == "image":
                content = (ROOT / "examples/authoring/cropped.png").read_bytes()
                source = (ROOT / "examples/authoring/source.png").read_bytes()
            else:
                trajectory = json.loads((ROOT / "examples/authoring/edited-trajectory.json").read_text(encoding="utf-8"))
                content = json.dumps(trajectory, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                source = (ROOT / "examples/trajectories/shortcut.json").read_bytes()
            assert result["length"] == len(content) and result["sha256"] == hashlib.sha256(content).hexdigest(), output["name"]
            assert result["sourceSha256"] == hashlib.sha256(source).hexdigest(), output["name"]
            plans = [row["value"] for row in outputs if row["schema"] == "authoring.schema.json#/$defs/editPlan" and row["value"]["editId"] == result["editId"]]
            assert len(plans) == 1 and plans[0]["sourceSha256"] == result["sourceSha256"], output["name"]
        if filename == "content.schema.json" and "contentRef" in fragment:
            model = json.loads((ROOT / "examples/authoring/workflow-model.json").read_text(encoding="utf-8"))
            model["definition"]["variables"]["largeSample"] = ["样例" * 100] * 2048
            content = json.dumps(model, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            assert output["value"]["length"] == len(content) and output["value"]["sha256"] == hashlib.sha256(content).hexdigest(), output["name"]
    print(f"原型契约：{len(outputs)} 份结构与关联检查通过", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interactions-only", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="检查全部页面和交互，不生成截图")
    parser.add_argument("--interaction-suite", choices=("all", "baseline", "completion", "authoring", "content", "content-recovery", "content-rejections", "recording", "query", "platform"), default="all")
    parser.add_argument("--group", help="只预览指定任务组")
    parser.add_argument("--ids", help="逗号分隔的界面 ID，可与 --group 一起筛选")
    parser.add_argument("--skip-interactions", action="store_true", help="截图后不运行交互检查")
    parser.add_argument("--scale", type=int, choices=(1, 2), default=1, help="定向截图采用 1x 或 2x 像素密度")
    parser.add_argument("--with-interactions", action="store_true", help="在 2x 定向截图后明确运行交互检查")
    args = parser.parse_args()
    if args.check_only and (args.interactions_only or args.group or args.ids or args.skip_interactions or args.scale != 1 or args.with_interactions):
        parser.error("--check-only 不能与截图或交互筛选选项同时使用")
    if args.skip_interactions and args.with_interactions:
        parser.error("--skip-interactions 与 --with-interactions 不能同时使用")
    if args.interactions_only and (args.group or args.ids or args.skip_interactions or args.scale != 1 or args.with_interactions):
        parser.error("--interactions-only 不能与截图筛选或 --skip-interactions 同时使用")
    if args.interaction_suite != "all" and not args.interactions_only:
        parser.error("定向交互套件只用于 --interactions-only")
    npx = shutil.which("npx")
    if not npx:
        raise SystemExit("需要 Node.js/npm 提供 npx。")
    catalog = json.loads((BASE / "catalog.json").read_text(encoding="utf-8"))
    group_keys = {group["key"] for group in catalog["groups"]}
    if args.group and args.group not in group_keys:
        parser.error("未知任务组：" + args.group + "；可选：" + ", ".join(sorted(group_keys)))
    ids = list(dict.fromkeys(value.strip() for value in args.ids.split(",") if value.strip())) if args.ids else None
    if args.ids is not None and not ids:
        parser.error("--ids 至少需要一个界面 ID")
    known_ids = {screen["id"] for screen in catalog["screens"]}
    if ids and set(ids) - known_ids:
        parser.error("未知界面 ID：" + ", ".join(sorted(set(ids) - known_ids)))
    selected_screens = [screen for screen in catalog["screens"]
                        if (not args.group or screen["group"] == args.group)
                        and (not ids or screen["id"] in ids)]
    if not selected_screens and not args.interactions_only:
        parser.error("任务组与界面筛选没有共同结果")
    targeted = bool(args.group or ids)
    if args.scale == 2 and not targeted:
        parser.error("--scale 2 需要通过 --group 或 --ids 指定预览范围")
    subprocess.run([os.sys.executable, str(ROOT / "tools/build_prototypes.py"), "--check"], cwd=ROOT, check=True)
    skip_interactions = args.skip_interactions or (args.scale == 2 and not args.with_interactions)
    selected_groups = {screen["group"] for screen in selected_screens}
    groups = [] if args.interactions_only else [group for group in catalog["groups"] if group["key"] in selected_groups]
    output_directory = ("output/playwright/previews-2x" if args.scale == 2 else "output/playwright/previews") if targeted else "output/playwright/images"
    if not (args.check_only or args.interactions_only):
        (CACHE / output_directory).mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(BASE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    browser_directory = tempfile.TemporaryDirectory(prefix="deskweave-browser-")
    session = "deskweave-render-" + str(os.getpid())
    prefix = [npx, "--yes", "--package", CLI_PACKAGE, "playwright-cli", "-s=" + session]

    def command(*args):
        result = subprocess.run([*prefix, *args, "--json"], cwd=browser_directory.name, capture_output=True,
                                text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        envelope = json.loads(result.stdout)
        if envelope.get("isError") or envelope.get("error"):
            raise RuntimeError(str(envelope))
        return envelope

    try:
        url = f"http://127.0.0.1:{server.server_port}/index.html?capture=1#editor-graph"
        command("open", url, "--browser", "chrome")
        for group in groups:
            key = group["key"]
            assert key.isascii() and key.replace("-", "").isalnum()
            options = {"group": key, "outputDirectory": (CACHE / output_directory).as_posix(), "deviceScaleFactor": args.scale, "checkOnly": args.check_only}
            if ids:
                options["ids"] = ids
            command("eval", "window.DW_CAPTURE_OPTIONS=" + json.dumps(options, ensure_ascii=False))
            result = command("run-code", "--filename", str(ROOT / "tools/capture_prototypes.js"))
            value = json.loads(result["result"]) if isinstance(result["result"], str) else result["result"]
            assert not value["pageErrors"], value["pageErrors"]
            for screen in value["screens"]:
                issues = {key: value for key, value in screen["issues"].items()
                          if key not in {"width", "height"} and value}
                assert not issues, f"{screen['id']}: {issues}"
            expected_ids = {screen["id"] for screen in selected_screens if screen["group"] == key}
            assert {screen["id"] for screen in value["screens"]} == expected_ids
            assert value["count"] == len(expected_ids)
            print(f"{group['name']}：{value['count']} 个界面", flush=True)
        if not skip_interactions:
            suites = ["prototype_interactions.js", "prototype_completion_interactions.js", "prototype_authoring_interactions.js", "prototype_content_recovery_interactions.js", "prototype_recording_interactions.js", "prototype_query_interactions.js", "prototype_platform_interactions.js"] if args.interaction_suite == "all" else [{"baseline":"prototype_interactions.js", "completion":"prototype_completion_interactions.js", "authoring":"prototype_authoring_interactions.js", "content":"prototype_authoring_interactions.js", "content-recovery":"prototype_content_recovery_interactions.js", "content-rejections":"prototype_content_recovery_interactions.js", "recording":"prototype_recording_interactions.js", "query":"prototype_query_interactions.js", "platform":"prototype_platform_interactions.js"}[args.interaction_suite]]
            if args.interaction_suite == "content":
                command("eval", "window.DW_INTERACTION_OPTIONS={scope:'content'}")
            if args.interaction_suite == "content-rejections":
                command("eval", "window.DW_INTERACTION_OPTIONS={scope:'content-rejections'}")
            checks = []
            for suite in suites:
                interaction = command("run-code", "--filename", str(ROOT / "tools" / suite))
                value = json.loads(interaction["result"]) if isinstance(interaction["result"], str) else interaction["result"]
                assert value["checks"] and all(row["result"] == "pass" and row["name"] for row in value["checks"])
                if suite != "prototype_interactions.js":
                    check_interaction_outputs(value, suite="content-recovery" if suite == "prototype_content_recovery_interactions.js" else "content" if args.interaction_suite == "content" else "platform" if suite == "prototype_platform_interactions.js" else "query" if suite == "prototype_query_interactions.js" else "recording" if suite == "prototype_recording_interactions.js" else "authoring" if suite == "prototype_authoring_interactions.js" else "completion")
                checks.extend(value["checks"])
            assert len({row["name"] for row in checks}) == len(checks), "交互检查名称重复"
            print(f"原型交互：{len(checks)} 项通过", flush=True)
    finally:
        try:
            command("close")
        finally:
            server.shutdown()
            server.server_close()
            browser_directory.cleanup()
    if args.check_only:
        print("页面和交互检查完成。")
    elif args.interactions_only:
        print("交互检查完成。")
    elif targeted:
        print(f"定向 {args.scale}x 截图完成；结果位于 {output_directory}。")
    else:
        print("截图完成；运行 compose_prototypes.py 合成目录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
