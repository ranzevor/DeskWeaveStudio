# 原型检查

## 检查入口

安装项目依赖并准备 Node.js、npm 与 Chrome 后，在项目根目录执行：

```bash
python -m pip install -r requirements-docs.txt -r requirements-prototypes.txt
python tools/build_prototypes.py --check
python tools/check_prototypes.py
python tools/export_design_showcase.py --check
python tools/render_prototypes.py --check-only
```

静态检查覆盖目录、需求映射、动作参数、页面资源及图像文件。浏览器检查逐页检查路由、边界、字段名称和按钮裁切，并执行六组交互；交互产生的操作结果在运行时检查结构及关联约束。

只检查交互可运行 `python tools/render_prototypes.py --interactions-only`；`--interaction-suite baseline`、`completion`、`authoring`、`content`、`recording`、`query` 或 `platform` 可选择单组。生成设计图和定向预览的步骤见[原型画册](../prototypes/README.md)。

## 交互范围

- 流程三视图、导航、搜索、暗色主题与窄窗口。
- 运行准备、暂停、急停、断点与本地恢复；变量分页与敏感值遮蔽。
- 控制关系、表达式、轨迹裁剪、截图选区、资源编辑和大流程保存。
- 全显示屏、单屏和窗口录制，跨屏拖拽与暂停、拓扑变化分段、来源布局与回放映射。
- 模板提取、筛选、命名、批量提交、冲突、取消与导出。
- 持续和单次观察、停止与失败、记录筛选、分页、截图和导出。
- 模型连接测试、消息模板、监听配置、令牌签发与撤销。
- macOS 分用途授权、Wayland 部分显示屏选择、全局录制不可用及跨平台像素映射。
- 集合条件查询、排序、每页数量、连续翻页、空结果、总数计算、过期和权限变化。
- 导入映射、资产引用与删除、设置继承与冲突、更新校验和降级确认。

## 视觉检查

查看[全界面目录](../design/prototypes.md)与[高清效果图](../design/high-fidelity.md)，核对文字、图标、布局、状态区分、焦点和主要操作；在窄窗口检查滚动、浮层与长内容。应用的原生输入、模型服务、读屏、安装和更新场景见[验证与验收设计](verification.md)。

原型页面检查在操作系统深色偏好下遍历全部画板，核对外层画册、应用窗口、表单和滚动区域的实际 color-scheme 与滚动条颜色。浅色轨道保持明亮，深色轨道跟随深色窗口，滑块与轨道对比度至少 3:1。主题切换、横向溢出和嵌套滚动沿用相同变量。
