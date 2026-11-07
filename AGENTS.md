# 项目工作指引

- 依据当前任务确定修改范围；从 README.md 和 docs/README.md 查阅对应设计和契约。
- 应用组件按 ROADMAP.md 实施，接口与行为以当前代码及对应测试核对。
- 开发变更使用独立分支；合并 main 必须取得仓库所有者明确授权。
- 公共字段变更同步 Schema、动作目录、示例、设计和兼容处理；参数范围以 Schema 为准。
- Python 实现使用 uv.lock；运行 uv run --frozen python -m unittest discover -s tests -q。
- 文档与契约检查入口为 python tools/check_docs.py --contracts。
- 输入、录制与恢复测试使用临时工作空间、合成资料和独立目标窗口，不提交用户数据或凭据。
- 项目自有材料采用 Apache-2.0；分发时保留根 LICENSE 和第三方材料必要的许可通知。
