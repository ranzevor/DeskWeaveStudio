# DeskWeaveStudio 原型画册

共 254 个界面，按 14 个任务域组织。

- [交互入口](index.html)：用浏览器直接打开，无需安装依赖或启动服务。
- [完整图文目录](../design/prototypes.md)：按任务查看总览与每张原图。
- [全部画板总览](overview/all-screens.png)：定位所有画板。
- [界面数据](catalog.json)：画板、状态、需求与跳转关系。

## 浏览

需要在浏览器中预览时，在项目根目录运行 `python tools/serve_prototypes.py`，打开返回的本机地址。服务使用实时文件并禁用浏览器缓存。

左侧搜索页面名称或编号，使用前后按钮或 Alt+方向键切换。页面内按钮跳转到相关界面；Esc 返回触发界面，Ctrl+K 打开命令菜单。急停确认需要先勾选本地接管确认。

默认主画板 1440×900，V02 为 800×600。点击“查看设计图”可读取实际分辨率的 PNG。

## 维护

catalog/ 为界面清单源，src/ 为界面渲染与交互源，fixtures/ 为可校验的流程数据。catalog.json、src/data.js 和图文目录由构建工具生成。

在项目根目录执行：

```bash
python tools/build_prototypes.py
python tools/build_prototypes.py --check
python tools/check_prototypes.py
python tools/check_docs.py --contracts
```

重新生成设计图需要 Node.js/npm、Chrome，以及 requirements-prototypes.txt 中的 Pillow。安装工具后运行 python tools/render_prototypes.py，随后运行 python tools/compose_prototypes.py。渲染器使用固定版本 Playwright CLI，创建独立本机预览与浏览器会话，结束时关闭；合成工具生成图册总览。

运行 python tools/render_prototypes.py --check-only 检查全部页面与交互，或使用 --interactions-only 仅检查交互。--interaction-suite baseline、completion、authoring、recording、query 或 platform 可选择单组；交互结果在运行时校验。
