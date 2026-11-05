# 系统设计导航

设计版本：0.1.0  
更新日期：2026-10-29  
适用范围：桌面应用、执行引擎、原生桌面代理、MCP 接入及开源发行。

## 文档地图

| 文档 | 解决的问题 |
|---|---|
| [产品需求](product/requirements.md) | 为谁提供哪些能力，首期范围和完成条件 |
| [术语与状态](product/glossary.md) | 产品用语、对象身份、状态和计时口径 |
| [组件生命周期](architecture/lifecycle.md) | 启动、所有者、后台工作、关闭和异常恢复 |
| [命令行接口](interfaces/command-line.md) | 命令、授权、输出、退出与取消 |
| [持久化事务](design/persistence-transactions.md) | 作业、名称占用、查询快照和凭据版本的一致性 |
| [系统架构](architecture/system.md) | 组件职责、进程部署、数据所有权与依赖方向 |
| [共享应用服务](architecture/shared-services.md) | 工作台、MCP 与 CLI 的业务复用、认证上下文、请求恢复与状态同步 |
| [跨平台架构](architecture/platforms.md) | Windows、macOS、X11、Wayland 后端，坐标、权限、能力与分发 |
| [架构决定](architecture/decisions.md) | 关键方案的理由、成本和改变条件 |
| [流程设计](design/workflow.md) | 流程格式、动作、类型、表达式与静态校验 |
| [交互设计](design/experience.md) | 页面、编辑器、录制与运行操作、无障碍 |
| [视觉系统](design/visual-system.md) | 色彩、字体、布局、组件状态与高保真实现规范 |
| [高清效果图](design/high-fidelity.md) | 核心页面的 2× 界面图与展示板 |
| [全界面原型](design/prototypes.md) | 全页面、弹窗、覆盖层与状态图及交互路径 |
| [功能设计与交付映射](design/feature-design-map.md) | 制作、执行、资产、配置和更新的设计、原型及验收入口 |
| [前端结构](design/studio.md) | React、组件、状态、编辑事务、资源与渲染 |
| [运行变量与版本检查](design/runtime-inspection.md) | 变量脱敏快照、流程版本与模型连接发现 |
| [制作与测试事务](design/authoring-transactions.md) | 未保存连接测试、资产编辑计划和新版本原子提交 |
| [内容传输](interfaces/content-transfer.md) | 分块上传、资源化读写、帧预算与公共摘要 |
| [执行与输入](design/execution.md) | 调度、输入租约、并发、暂停、急停与恢复 |
| [采集、录制与视觉](design/perception.md) | 截图、坐标、轨迹、匹配、观察和模型调用 |
| [全显示屏录制](design/recording.md) | 全桌面连续录制、显示拓扑、分段与回放映射 |
| [连续指针与笔画](design/pointer-motion.md) | 事件位置、逐点回放、持续按住绘制、消息合并与平滑运动 |
| [模板生产](design/templates.md) | 候选提取、OCR 与规则命名、冲突、批次和导出 |
| [观察中心](design/observations.md) | 持续会话、变化检测、记录查询和导出 |
| [模型连接](design/model-connections.md) | 模型参数、图像消息模板、输出结构和连接测试 |
| [数据设计](design/storage.md) | 表结构、对象存储、事务、包交换与备份 |
| [本机协议](interfaces/local-protocol.md) | 帧格式、命令、事件、资源、兼容与重连 |
| [原生 Agent 协议](interfaces/desktop-agent.md) | 租约、已求值输入、资源封存、录制与急停 |
| [MCP 接口](interfaces/mcp.md) | 工具与资源、授权、取消和通知 |
| [分页查询](interfaces/pagination.md) | 服务端条件查询、集合结果、游标、排序、快照与过期 |
| [安全设计](security/security-design.md) | 信任边界、权限、秘密、外部输入与威胁处理 |
| [配置与可观测性](operations/configuration.md) | 配置生效、日志、指标、容量与诊断 |
| [安装、发行与恢复](operations/release.md) | 构建、签名、安装、更新、数据演进与回滚 |
| [验证设计](quality/verification.md) | 测试分层、故障注入、性能与端到端验收 |
| [文档检查](quality/document-validation.md) | 文档、契约和示例的检查入口 |
| [原型检查](quality/prototype-validation.md) | 画板覆盖、浏览器交互与视觉检查 |
| [需求追踪](quality/traceability.md) | 需求到设计、接口、验收用例与里程碑的对应 |
| [Engine 开发](development/engine.md) | 共享服务库的安装、方法范围、会话与测试 |
| [工程实施](development/engineering.md) | 代码组织、工具链、依赖与审查方式 |
| [公开仓库设置](development/repository.md) | 仓库权限、分支保护、报告入口与发行准备 |

## 机器可读资料

| 文件 | 用途 |
|---|---|
| [流程 Schema](../contracts/workflow.schema.json) | 工作流定义的结构约束 |
| [消息 Schema](../contracts/message.schema.json) | 请求、响应和事件信封 |
| [任务包 Schema](../contracts/package.schema.json) | 导入导出包清单 |
| [动作 Schema](../contracts/actions.schema.json) | 节点与动作参数 |
| [值与表达式 Schema](../contracts/values.schema.json) | 常量、保留键与 AST |
| [轨迹 Schema](../contracts/trajectory.schema.json) | 输入事件与来源坐标结构 |
| [录制 Schema](../contracts/recording.schema.json) | 显示拓扑、录制来源、状态与回放映射 |
| [平台 Schema](../contracts/platform.schema.json) | 能力状态、逐屏像素、原生坐标变换与输入单位 |
| [草稿 Schema](../contracts/draft.schema.json) | 源码原文、最后有效模型、编辑代次与恢复快照 |
| [执行控制 Schema](../contracts/execution-control.schema.json) | 暂停检查点、恢复确认及断点配置 |
| [资产管理 Schema](../contracts/asset-management.schema.json) | 引用分页、资产删除与结果 |
| [设置 Schema](../contracts/settings.schema.json) | 类型化设置、覆盖、重置及生效结果 |
| [设置目录](../contracts/settings-catalog.json) | 设置键、默认值、作用域和生效方式 |
| [接入管理 Schema](../contracts/bridge-management.schema.json) | 已保存/活动配置与令牌元数据 |
| [更新 Schema](../contracts/updates.schema.json) | 签名清单、可信根、吊销与信任状态 |
| [内容 Schema](../contracts/content.schema.json) | 上传、封存和不可变内容引用 |
| [检查 Schema](../contracts/inspection.schema.json) | 版本、连接列表与脱敏变量分页 |
| [编辑 Schema](../contracts/authoring.schema.json) | 临时连接测试和资产编辑提交 |
| [服务 Schema](../contracts/service.schema.json) | Engine 方法参数与结果 |
| [Agent Schema](../contracts/agent.schema.json) | 原生请求与方法结果 |
| [视觉对象 Schema](../contracts/vision.schema.json) | 模板、观察、命名与模型配置 |
| [MCP 接入 Schema](../contracts/mcp.schema.json) | 监听配置、客户端令牌、双代元数据与订阅消息 |
| [CLI 目录](../contracts/cli-catalog.json) | 子命令、参数契约、授权与完成结果 |
| [MCP 目录](../contracts/mcp-catalog.json) | 工具、资源与预设任务 |
| [查询 Schema](../contracts/query.schema.json) | 集合入口、筛选条件、类型化记录与分页结果 |
| [作业结果 Schema](../contracts/operation-results.schema.json) | 异步方法成功结果及作业快照状态约束 |
| [作业结果目录](../contracts/operation-catalog.json) | 成功作业与实际结果结构的对应 |
| [动作目录](../contracts/action-catalog.json) | 名称、分组、效果类别与产出 |
| [示例目录](../examples/README.md) | 最小流程、子流程、消息与失败样例 |

## 阅读路径

产品评审从需求与交互设计开始；工程实现从架构、流程和协议开始；发行评审从验证、追踪和发行设计开始。

流程数据以 Schema 和动作目录为结构入口，执行含义由流程与执行设计共同定义。变更公共字段时，同时更新对应设计、示例、校验和兼容处理。
