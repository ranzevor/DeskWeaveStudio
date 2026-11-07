# 需求与验收追踪

| 需求 | 主要设计 | 接口或对象 | 用例 | 里程碑 |
|---|---|---|---|---|
| DW-001 | storage、lifecycle、command-line | workspace.open、备份集 | Q01 | M1 |
| DW-002 | workflow、experience、studio | Workflow、Draft | Q02/Q36/Q37 | M2 |
| DW-003 | storage、persistence-transactions、studio | 草稿读写、workflow.save、editVersion | Q03/Q42/Q45/Q46 | M1/M2 |
| DW-004 | perception、recording、pointer-motion | desktop.topology、record.*、Trajectory、displayMapping、连续笔画 | Q04/Q38/Q44/Q48/Q50/Q51/Q52 | M3 |
| DW-005 | perception | capture、vision.match | Q05/Q39/Q44 | M3 |
| DW-006 | workflow、studio、shared-services | Value、Expression、expression.preview、limits | Q06/Q37/Q59 | M1/M2/M4 |
| DW-007 | workflow、execution | call、join、repeat、Frame | Q07 | M1/M4 |
| DW-008 | storage、execution | Revision、RunSnapshot、asset.delete | Q08/Q33/Q42/Q47 | M1 |
| DW-009 | execution、desktop-agent | run.*、暂停检查点、恢复计划 | Q09/Q30 | M1/M4 |
| DW-010 | system、lifecycle、execution、desktop-agent | Desktop Lease | Q10 | M1 |
| DW-011 | experience、execution | debug.breakpoints.*、debug.step、RunTree | Q11/Q31/Q41 | M4 |
| DW-012 | storage | Package、导入映射、AssetVersion | Q12/Q32/Q33/Q44/Q47 | M3 |
| DW-013 | shared-services、mcp、pagination、command-line、persistence-transactions | MCP tools/resources、query.*、operation.get、请求身份元数据 | Q13/Q45/Q49/Q59 | M4 |
| DW-014 | perception、security-design | Observation、Connection | Q14 | M4 |
| DW-015 | execution、security-design | Agent、targetToken | Q15 | M4 |
| DW-016 | experience、perception、recording | 焦点、热键、输入法、选区、多屏布局 | Q16/Q36/Q39/Q48 | M2/M5 |
| DW-017 | release | 签名清单、可信根、吊销与恢复 | Q17/Q40 | M5 |
| DW-018 | configuration、storage | settings.*、设置目录、指标、容量 | Q18/Q34 | M1/M5 |
| DW-019 | execution、local-protocol、lifecycle、persistence-transactions | epoch、effect_intent | Q19 | M1/M5 |
| DW-020 | repository、engineering | 使用指南、接口文档、贡献与反馈入口 | Q20 | M0/M5 |
| DW-021 | templates、perception、persistence-transactions | template.extract/candidates/commit | Q21 | M3 |
| DW-022 | templates、persistence-transactions | template.names、NamingPolicy | Q22 | M3 |
| DW-023 | observations、lifecycle、command-line | observation.start/stop/session | Q23 | M4 |
| DW-024 | observations、storage、persistence-transactions | observation.query/read/export | Q24 | M4 |
| DW-025 | model-connections、persistence-transactions | connection.configure/read、ConnectionOptions | Q25/Q42/Q43 | M4 |
| DW-026 | workflow、execution | flow.loop/break/continue、runVariables | Q26/Q41 | M1/M4 |
| DW-027 | workflow、execution | run.prepare/start、startNode | Q27 | M1/M4 |
| DW-028 | shared-services、mcp、pagination、security-design | MCP 目录、query.*、bridge.*、stream.* | Q28/Q35/Q49/Q59 | M4 |
| DW-029 | execution、desktop-agent、pointer-motion | pressDurationMs、keyboard.chord、interruptPolicy、连续运动 | Q29/Q53 | M1/M4 |
| DW-030 | platforms、system、recording、pointer-motion、studio、release | desktop.capabilities、system.capabilities.desktop、Trajectory 1.1、逐屏拓扑与五种制品目标 | Q50/Q51/Q52/Q53/Q54/Q55/Q56/Q57/Q58 | M1/M2/M3/M5 |
| DW-N01 | system、perception | 本地功能与可选连接 | Q05/Q14 | M3/M4 |
| DW-N02 | security-design、storage | 秘密、授权、导出 | Q12/Q14/Q18/Q41/Q43 | M1/M4 |
| DW-N03 | configuration、pointer-motion | 限额、队列、缓存、采集与回放积压 | Q04/Q18/Q51 | M3/M5 |
| DW-N04 | engineering、release | 工具链、锁、构建清单 | Q17/Q20 | M0/M5 |
| DW-N05 | local-protocol、release | 格式兼容和迁移 | Q12/Q17/Q45/Q46 | M1/M5 |
| DW-N06 | verification | 性能预算和实测样本 | Q09/Q16/Q18 | M5 |

设计名对应[文档导航](../README.md)中的文件。用例定义见[验证设计](verification.md)，实施顺序见[路线图](../../ROADMAP.md)。

工作项和 PR 关联需求编号，使用对应的用例验证变更。新增公共字段同时更新契约、示例、文档与相关用例。

## 专用界面入口

| 需求 | 交互入口 | 正常与异常画板 |
|---|---|---|
| 批量模板制作（DW-021 / DW-022） | [批量模板制作](../prototypes/index.html#templates-source) | T 组：来源、候选、命名、冲突、OCR、批次、导出与失败 |
| 观察与历史（DW-023 / DW-024） | [观察中心](../prototypes/index.html#observations-center) | B 组：持续会话、停止、变化与忙碌跳过、记录、恢复和导出 |
| 模型高级配置（DW-025） | [模型高级配置](../prototypes/index.html#connection-advanced) | C01～C05：参数、图像消息、字段错误、消息预览和连接测试 |
| 循环与共享变量（DW-026） | [共享声明](../prototypes/index.html#run-shared-declarations) | E18、F22～F24、U16～U18：声明、循环控制、快照与并发冲突 |
| 指定节点启动（DW-027） | [指定节点启动](../prototypes/index.html#run-from-node) | E16、E19～E21：缺值、补值、就绪与准备过期 |
| 网络接入（DW-028） | [MCP 传输配置](../prototypes/index.html#mcp-transport) | C06～C17：监听、TLS、令牌、撤销、工具、资源与预设任务 |
| 输入与接管（DW-029） | [接管策略](../prototypes/index.html#run-interrupt-policy) | E22、F14、F25：物理操作策略、按住时长和组合键 |

分页查询的 DW-013 / DW-028 由[分页设计](../interfaces/pagination.md)、[查询契约](../../contracts/query.schema.json)与 Q49 覆盖；C20～C22 检查条件查询、续页、过期和失败恢复。
