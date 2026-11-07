"""检查界面目录、需求覆盖、图像尺寸与页面资源。"""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import runpy
from urllib.parse import urlsplit

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docs/prototypes"


class Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "script" and fields.get("src"):
            self.paths.append(fields["src"])
        if tag == "link" and fields.get("href") and not fields["href"].startswith("data:"):
            self.paths.append(fields["href"])


def main():
    generator = runpy.run_path(str(ROOT / "tools/build_prototypes.py"))
    for path, expected in generator["outputs"]().items():
        assert path.is_file() and path.read_text(encoding="utf-8") == expected, "生成资料过期：" + str(path)
    catalog = json.loads((BASE / "catalog.json").read_text(encoding="utf-8"))
    action_schema = json.loads((ROOT / "contracts/actions.schema.json").read_text(encoding="utf-8"))
    action_ids = {a["id"] for a in json.loads((ROOT / "contracts/action-catalog.json").read_text(encoding="utf-8"))["actions"]}
    action_screens = [s["action"] for s in catalog["screens"] if s.get("action")]
    assert len(action_screens) == len(action_ids) and set(action_screens) == action_ids
    assert set(catalog["actionModels"]) == action_ids
    for action, model in catalog["actionModels"].items():
        parameters = action_schema["$defs"][action]["properties"]["params"]
        assert model["parameters"] == parameters["properties"] and model["required"] == parameters.get("required", []), action
    ids = {s["id"] for s in catalog["screens"]}
    assert len(ids) == len(catalog["screens"])
    codes = {s["code"] for s in catalog["screens"]}
    assert len(codes) == len(ids)
    req_text = (ROOT / "docs/product/requirements.md").read_text(encoding="utf-8")
    requirements = set(re.findall(r"^\| (DW-(?:N)?\d+) \|", req_text, re.M))
    covered = {value for s in catalog["screens"] for value in s["requirements"]}
    assert covered == requirements, ("需求覆盖差异", covered ^ requirements)
    expected_images = set()
    for screen in catalog["screens"]:
        assert re.fullmatch(r"[A-Z][0-9]{2}", screen["code"])
        assert re.fullmatch(r"[a-z][a-z0-9-]*", screen["id"])
        if screen["primaryKind"] in {"navigate", "validate"}:
            assert screen["primary"] in ids
        else:
            assert screen["primaryKind"] in {"download", "local"} and screen["primary"] is None and screen["primaryResult"]
        if screen.get("completion"):
            assert screen["completion"] in ids
        assert screen["parent"] is None or screen["parent"] in ids
        expected_images.add("images/" + screen["image"])
    for screen in catalog["screens"]:
        path = BASE / "images" / screen["image"]
        assert path.resolve().is_relative_to(BASE.resolve()) and not path.is_symlink(), screen["id"]
        with Image.open(path) as image:
            assert image.format == "PNG", path.name
            assert image.size == (screen["viewport"]["width"], screen["viewport"]["height"]), screen["id"]
            image.verify()
    overview_images = {"overview/" + group["key"] + ".png" for group in catalog["groups"]}
    overview_images.update({"overview/all-screens.png", "overview/key-screens.png"})
    for name in overview_images:
        with Image.open(BASE / name) as image:
            assert image.format == "PNG" and image.width > 0 and image.height > 0, name
            image.verify()
    expected_images.update(overview_images)
    actual_images = {p.relative_to(BASE).as_posix() for folder in ("images", "overview") for p in (BASE / folder).glob("*.png")}
    assert actual_images == expected_images
    parser = Resources()
    parser.feed((BASE / "index.html").read_text(encoding="utf-8"))
    for path in parser.paths:
        assert not urlsplit(path).scheme and (BASE / path).is_file(), path
    for path in (BASE / "src").iterdir():
        if path.suffix in {".css", ".js"}:
            content = path.read_bytes()
            assert b"\r" not in content and not content.startswith(b"\xef\xbb\xbf")
            assert not re.search(r"\b(fetch\s*\(|XMLHttpRequest|WebSocket\b)", content.decode("utf-8"))
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    checker = runpy.run_path(str(ROOT / "tools/check_docs.py"))
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "contracts").glob("*.schema.json")]
    registry = Registry().with_resources((data["$id"], Resource.from_contents(data)) for data in schemas)
    workflow_schema = next(s for s in schemas if s["$id"] == "urn:deskweave:workflow:1")
    validator = Draft202012Validator(workflow_schema, registry=registry)
    for path in (BASE / "fixtures").glob("*.json"):
        definition = json.loads(path.read_text(encoding="utf-8"))
        validator.validate(definition)
        assert not checker["workflow_errors"](definition), path.name
    assert (BASE / "fixtures/edit-source.png").read_bytes() == (ROOT / "examples/authoring/source.png").read_bytes(), "原型裁剪来源与契约像素样例不一致"
    assert (BASE / "fixtures/edit-current.png").read_bytes() == (ROOT / "examples/authoring/cropped.png").read_bytes(), "资产当前版本与内容样例不一致"
    print(f"原型：{len(ids)} 个界面，{len(expected_images)} 张图，{len(requirements)} 项需求覆盖，{len(action_ids)} 类动作；错误 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
