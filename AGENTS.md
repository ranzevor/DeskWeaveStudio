# 项目工作指引

- 从 README.md 和 docs/README.md 恢复范围；修改前阅读对应设计和相关契约。
- 应用组件按 ROADMAP.md 创建，当前文档中的设计目标不视为已实现功能。
- 公共字段变更同步 Schema、动作目录、示例、设计和兼容处理；参数范围以 Schema 为准。
- 文档与契约检查入口为 python tools/check_docs.py --contracts。
- 输入、录制与恢复测试使用临时工作空间、合成资料和独立目标窗口，不提交用户数据或凭据。
- 项目自有材料采用 Apache-2.0；分发时保留根 LICENSE 和第三方材料必要的许可通知。
