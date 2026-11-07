"""整理原型截图，并合成目录总览。"""
from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docs/prototypes"
CACHE = ROOT / ".cache/prototype-browser"
CAPTURES = CACHE / "output/playwright/images"


def png_size(path):
    import struct
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", str(path)
    return struct.unpack(">II", raw[16:24])


def sheet(screens, source, destination, title, columns=3, tile_width=390):
    from PIL import Image, ImageDraw, ImageFont
    fonts = [Path("C:/Windows/Fonts/msyh.ttc"), Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
    font_path = next((p for p in fonts if p.is_file()), None)
    assert font_path, "请提供中文字体环境"
    font = ImageFont.truetype(str(font_path), 18)
    title_font = ImageFont.truetype(str(font_path), 26)
    sub_font = ImageFont.truetype(str(font_path), 13)
    gap, margin, top = 20, 28, 95
    image_height = round(tile_width * 900 / 1440)
    cell_height = image_height + 53
    rows = math.ceil(len(screens) / columns)
    image = Image.new("RGB", (margin * 2 + columns * tile_width + (columns - 1) * gap,
                              top + rows * cell_height + max(0, rows - 1) * gap + margin), "#eff3f8")
    draw = ImageDraw.Draw(image)
    draw.text((margin, 21), title, font=title_font, fill="#26364b")
    draw.text((margin, 60), f"DeskWeaveStudio  /  {len(screens)} 个界面", font=sub_font, fill="#73839a")
    for i, screen in enumerate(screens):
        x = margin + (i % columns) * (tile_width + gap)
        y = top + (i // columns) * (cell_height + gap)
        draw.rounded_rectangle((x, y, x + tile_width, y + cell_height), radius=7, fill="white", outline="#d8e1ed")
        draw.text((x + 11, y + 9), screen["code"] + "  " + screen["title"], font=font, fill="#34445b")
        thumb = Image.open(source / screen["image"]).convert("RGB")
        thumb.thumbnail((tile_width - 2, image_height), Image.Resampling.LANCZOS)
        dx = x + (tile_width - thumb.width) // 2
        dy = y + 43 + (image_height - thumb.height) // 2
        image.paste(thumb, (dx, dy))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, optimize=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    catalog = json.loads((BASE / "catalog.json").read_text(encoding="utf-8"))
    screens = catalog["screens"]
    if args.preview:
        selected = [s for s in screens if (CAPTURES / s["image"]).is_file()]
        preview = CACHE / "review"
        for group in catalog["groups"]:
            members = [s for s in selected if s["group"] == group["key"]]
            if members:
                sheet(members, CAPTURES, preview / (group["key"] + ".png"), group["name"])
        print(f"预览总览：{len(selected)} 个界面")
        return 0
    for screen in screens:
        original = CAPTURES / screen["image"]
        assert png_size(original) == (screen["viewport"]["width"], screen["viewport"]["height"]), screen["id"]
        target = BASE / "images" / screen["image"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
    for group in catalog["groups"]:
        members = [s for s in screens if s["group"] == group["key"]]
        sheet(members, BASE / "images", BASE / "overview" / (group["key"] + ".png"), group["name"])
    sheet(screens, BASE / "images", BASE / "overview/all-screens.png", "全界面总览", columns=6, tile_width=255)
    ids = ["editor-graph", "templates-candidates", "observations-center", "mcp-transport", "connection-advanced", "run-start-ready"]
    hero = [next(s for s in screens if s["id"] == key) for key in ids]
    sheet(hero, BASE / "images", BASE / "overview/key-screens.png", "主要界面", columns=2, tile_width=780)
    print(f"原型图：{len(screens)} 张；总览：{len(catalog['groups']) + 2} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
