"""导出核心界面的 2× 图像与高保真展示板。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/"docs/prototypes"
DEST=BASE/"high-fidelity"
IDS=["welcome","editor-graph","editor-source","assets","templates-candidates",
     "observations-center","run-paused","connection-advanced","mcp-transport","run-start-ready",
     "topology-edit","topology-invalid","expression-builder","expression-invalid",
     "trajectory-trim","trajectory-repair-many","capture-keyboard","capture-boundary",
     "resume-review","resume-stale","breakpoints-edit","breakpoints-conflict",
     "package-target-map","package-target-invalid","asset-delete-review","asset-delete-stale",
     "settings-overrides","settings-conflict","mcp-config-read","mcp-token-query",
     "update-trust","update-verification-failed","update-downgrade","mcp-token-revoke",
     "variables-inspect","variables-expired","variables-redacted","revision-browser","revision-query-expired",
     "connection-browser","connection-draft-test","connection-test-stale","asset-edit-save","asset-edit-conflict",
     "asset-edit-cancelled","content-save","content-transfer-failed",
     "record-config","record-displays","record-topology-changed","trajectory-displays","trajectory-displays-invalid","mcp-query","mcp-query-expired",
     "platform-capabilities","platform-macos-permissions","platform-wayland-session","platform-wayland-recording","platform-display-mapping"]

def write(path,text):
    path.write_text(text,encoding="utf-8",newline="\n")

def board(screen,source,destination):
    from PIL import Image,ImageDraw,ImageFont,ImageFilter
    font_path=Path("C:/Windows/Fonts/msyh.ttc")
    if not font_path.is_file():
        raise RuntimeError("需要中文字体 Microsoft YaHei。")
    fonts={n:ImageFont.truetype(str(font_path),n) for n in (24,28,33,46)}
    canvas=Image.new("RGB",(3200,2200),"#eceef5")
    draw=ImageDraw.Draw(canvas)
    for y in range(2200):
        a=y/2199
        color=tuple(round(start+(end-start)*a) for start,end in zip((248,249,253),(229,232,242)))
        draw.line((0,y,3199,y),fill=color)
    draw.rounded_rectangle((154,81,218,145),radius=18,fill="#6260e6")
    draw.line((169,99,180,126,191,106,202,126,211,99),fill="white",width=5,joint="curve")
    draw.text((240,77),"DeskWeaveStudio",font=fonts[46],fill="#22283e")
    draw.text((242,138),"高保真界面设计",font=fonts[24],fill="#7b8299")
    draw.text((3040,97),screen["code"]+"  /  "+screen["title"],font=fonts[33],fill="#545c78",anchor="ra")
    shot=Image.open(source).convert("RGB")
    assert shot.size==(2880,1800),shot.size
    shadow=Image.new("RGBA",canvas.size)
    sd=ImageDraw.Draw(shadow)
    sd.rounded_rectangle((158,237,3042,2041),radius=24,fill=(33,43,80,28))
    shadow=shadow.filter(ImageFilter.GaussianBlur(28))
    canvas=Image.alpha_composite(canvas.convert("RGBA"),shadow)
    mask=Image.new("L",shot.size,0)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,2879,1799),radius=18,fill=255)
    canvas.paste(shot,(160,220),mask)
    draw=ImageDraw.Draw(canvas)
    draw.text((160,2090),screen["title"],font=fonts[28],fill="#343b55")
    draw.text((3040,2092),"2880 × 1800",font=fonts[24],fill="#7c849b",anchor="ra")
    canvas.convert("RGB").save(destination,optimize=True)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--check",action="store_true")
    parser.add_argument("--from-existing",action="store_true")
    args=parser.parse_args()
    catalog=json.loads((BASE/"catalog.json").read_text(encoding="utf-8"))
    screens=[next(s for s in catalog["screens"] if s["id"]==key) for key in IDS]
    outputs=[{"screen":s["id"],"image":s["code"]+"-"+s["id"]+"@2x.png",
              "board":s["code"]+"-"+s["id"]+"-board.png"} for s in screens]
    from PIL import Image
    if args.check:
        expected={item[key]:size for item in outputs
                  for key,size in (("image",(2880,1800)),("board",(3200,2200)))}
        assert len(expected)==len(IDS)*2
        assert {path.name for path in DEST.glob("*.png")}==set(expected)
        for name,size in expected.items():
            with Image.open(DEST/name) as image:
                assert image.format=="PNG" and image.size==size,name
                image.verify()
        print(f"高保真效果图：{len(IDS)} 个核心界面，{len(expected)} 个图像")
        return 0
    if not args.from_existing:
        subprocess.run([sys.executable,str(ROOT/"tools/render_prototypes.py"),"--ids",",".join(IDS),
                        "--scale","2","--skip-interactions"],cwd=ROOT,check=True)
    DEST.mkdir(parents=True,exist_ok=True)
    for screen,item in zip(screens,outputs):
        original=ROOT/".cache/prototype-browser/output/playwright/previews-2x"/screen["image"]
        with Image.open(original) as image:
            assert image.size==(2880,1800),(screen["id"],image.size)
            image.verify()
        target=DEST/item["image"]
        target.write_bytes(original.read_bytes())
        board(screen,target,DEST/item["board"])
    doc=["# 高保真界面效果图","",f"设计版本：{catalog['designVersion']}  ",f"更新日期：{catalog['updatedAt']}","",
         "采用精致浅色外壳、浅色点阵画布与适中信息密度。完整界面采用同一视觉系统；以下核心页面另外提供 2× 高清图像及展示板。",
         "","[视觉系统](visual-system.md) · [完整界面目录](prototypes.md) · [打开交互画册](../prototypes/index.html)","",
         "## 核心界面","","| 页面 | 高清界面图 | 展示板 |","|---|---|---|"]
    for screen,item in zip(screens,outputs):
        doc.append(f"| {screen['title']} | [2880 × 1800](../prototypes/high-fidelity/{item['image']}) | [3200 × 2200](../prototypes/high-fidelity/{item['board']}) |")
    for screen,item in zip(screens,outputs):
        doc+=["",f"## {screen['title']}","",f"![{screen['title']}高保真效果图](../prototypes/high-fidelity/{item['board']})","",
              f"[查看高清原图](../prototypes/high-fidelity/{item['image']}) · [打开此界面](../prototypes/index.html#{screen['id']})"]
    doc+=["","## 使用","","高清界面图用于评审文字、控件、图标与布局；展示板用于方案展示。全部页面、弹窗、抽屉、覆盖层与异常状态见完整目录。交互与视觉检查见[原型检查](../quality/prototype-validation.md)。",""]
    write(ROOT/"docs/design/high-fidelity.md","\n".join(doc))
    write(DEST/"README.md","# 高清界面图\n\n[效果图文档](../../design/high-fidelity.md)包含核心界面的 2880 × 1800 图像、3200 × 2200 展示板及交互入口。\n")
    print(f"高保真效果图：{len(screens)} 个核心界面，{len(outputs)*2} 个图像")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
