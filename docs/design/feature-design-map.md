# 功能设计与交付映射

设计版本：0.1.0  
更新日期：2026-10-29

本表将制作、执行、资产、配置和更新的操作路径对应到领域设计、机器契约、界面与验收用例。实现时以链接中的字段、状态及事务规则为准。

| 功能 | 关键行为 | 设计与契约 | 界面 | 验收 |
|---|---|---|---|---|
| 全显示屏录制 | 默认覆盖全部活动显示屏；跨屏事件连续，显示拓扑固定；布局变化结束原分段，按屏内物理像素映射回放 | [录制设计](recording.md)、[录制 Schema](../../contracts/recording.schema.json)、[轨迹 Schema](../../contracts/trajectory.schema.json) | [R01 录制准备](../prototypes/index.html#record-config)、[R22 屏幕布局](../prototypes/index.html#record-displays)、[R23 布局变化](../prototypes/index.html#record-topology-changed)、[R25 回放映射](../prototypes/index.html#trajectory-displays) | Q04/Q16/Q48 |
| 连续绘制与轨迹还原 | 逐事件位置、时间与按住状态保持；目标画布逐步画出折线、闭合曲线和跨屏笔画；积压不跳点 | [连续指针](pointer-motion.md)、[轨迹 Schema](../../contracts/trajectory.schema.json)、[录制 Schema](../../contracts/recording.schema.json) | [R01 录制准备](../prototypes/index.html#record-config)、[R18 时间轴](../prototypes/index.html#trajectory-trim)、[R25 显示映射](../prototypes/index.html#trajectory-displays) | Q50/Q51/Q52 |
| 平滑运动生成 | 正时长移动与拖拽连续加减速；相对量分步守恒，生成路径和已有录制分别处理 | [连续指针](pointer-motion.md)、[动作 Schema](../../contracts/actions.schema.json) | [动作库](../prototypes/index.html#editor-graph) | Q53 |
| 控制关系与入口编辑 | 替换出口先确认；失败保留原图；步骤重排不改变执行关系；更换入口可撤销 | [前端事务](studio.md)、[交互](experience.md)、[流程](workflow.md)、[Workflow Schema](../../contracts/workflow.schema.json) | [E23 连接与入口](../prototypes/index.html#topology-edit)、[E24 图环错误](../prototypes/index.html#topology-invalid) | Q36 |
| 复合表达式 | 结构树和 JSON 共用 Value AST；支持嵌套、短路与合成预览；错误编辑保留原文 | [表达式交互](experience.md)、[流程求值](workflow.md)、[Value Schema](../../contracts/values.schema.json) | [E25 复合条件](../prototypes/index.html#expression-builder)、[E26 类型错误](../prototypes/index.html#expression-invalid) | Q37 |
| 精确轨迹裁剪 | 左闭右开范围、多个输入配对、预览后应用和撤销；累计相对位移保持一致 | [轨迹设计](perception.md)、[Trajectory Schema](../../contracts/trajectory.schema.json) | [R18 裁剪](../prototypes/index.html#trajectory-trim)、[R19 多项配对修复](../prototypes/index.html#trajectory-repair-many) | Q38 |
| 键盘与数值选区 | 在目标图像像素中移动、缩放；越界保留待修正值；确认时重验捕获帧 | [采集交互](perception.md)、[无障碍](../../ACCESSIBILITY.md)、[ROI 契约](../../contracts/vision.schema.json) | [R20 键盘选区](../prototypes/index.html#capture-keyboard)、[R21 越界修正](../prototypes/index.html#capture-boundary) | Q39 |
| 正常暂停与继续 | 使用同一存活 Agent 的已确认检查点；恢复输入和指针前本地确认绑定目标与控制版本 | [执行设计](execution.md)、[Agent 协议](../interfaces/desktop-agent.md)、[执行控制 Schema](../../contracts/execution-control.schema.json) | [U19 恢复确认](../prototypes/index.html#resume-review)、[U20 计划失效](../prototypes/index.html#resume-stale) | Q30 |
| 断点管理 | 整组读取与保存；绑定固定流程和依赖版本；现有运行保留启动副本 | [执行设计](execution.md)、[本机协议](../interfaces/local-protocol.md)、[断点契约](../../contracts/execution-control.schema.json) | [U21 编辑断点](../prototypes/index.html#breakpoints-edit)、[U22 并发冲突](../prototypes/index.html#breakpoints-conflict) | Q31 |
| 导入身份与依赖映射 | 核验原始字节，固定来源与目标映射；独立流程身份、内容复用及整批原子提交 | [存储与导入](storage.md)、[任务包 Schema](../../contracts/package.schema.json)、[作业结果](../../contracts/operation-results.schema.json) | [A10 导入映射](../prototypes/index.html#package-target-map)、[A11 计划失效](../prototypes/index.html#package-target-invalid) | Q32 |
| 整对象资产删除 | 检查全部版本和完整引用；提交时重验；元数据删除与内容回收分别返回 | [存储设计](storage.md)、[资产管理 Schema](../../contracts/asset-management.schema.json) | [A12 删除检查](../prototypes/index.html#asset-delete-review)、[A13 版本变化](../prototypes/index.html#asset-delete-stale) | Q33 |
| 类型化设置与继承 | 23 个设置键；覆盖和恢复继承；整批版本校验；逐项显示生效时间；清理默认关闭 | [配置设计](../operations/configuration.md)、[设置目录](../../contracts/settings-catalog.json)、[设置 Schema](../../contracts/settings.schema.json) | [S11 设置范围](../prototypes/index.html#settings-overrides)、[S12 保存冲突](../prototypes/index.html#settings-conflict) | Q34 |
| MCP 管理查询 | 区分已保存与实际监听配置；分页筛选令牌元数据与读取撤销影响；查询不改变运行状态 | [MCP 设计](../interfaces/mcp.md)、[管理 Schema](../../contracts/bridge-management.schema.json)、[工具目录](../../contracts/mcp-catalog.json) | [C18 配置快照](../prototypes/index.html#mcp-config-read)、[C19 令牌查询](../prototypes/index.html#mcp-token-query) | Q35 |
| 更新信任与降级确认 | 验证真实签名与完整根链；校验吊销、通道、序号和时钟；降级保留最高信任状态 | [发行设计](../operations/release.md)、[更新 Schema](../../contracts/updates.schema.json)、[签名向量](../../examples/updates/signature-vectors.json) | [O14 更新信任](../prototypes/index.html#update-trust)、[O15 校验失败](../prototypes/index.html#update-verification-failed)、[O16 降级确认](../prototypes/index.html#update-downgrade) | Q40 |

| 运行变量投影 | 五种范围、固定快照与分页；敏感派生遮蔽，变更通知不携值；大值资源化 | [运行检查](runtime-inspection.md)、[检查 Schema](../../contracts/inspection.schema.json) | [U23 变量](../prototypes/index.html#variables-inspect)、[U24 到期](../prototypes/index.html#variables-expired)、[U25 敏感值](../prototypes/index.html#variables-redacted) | Q41 |
| 版本与连接发现 | 固定快照分页、到期重读与当前权限；清单不含密钥 | [运行检查](runtime-inspection.md)、[检查 Schema](../../contracts/inspection.schema.json) | [E27 历史版本](../prototypes/index.html#revision-browser)、[E28 查询到期](../prototypes/index.html#revision-query-expired)、[S13 连接清单](../prototypes/index.html#connection-browser) | Q42 |
| 未保存连接测试 | 可取消作业；绑定编辑代次；配置变化使测试失效，正式连接独立保存 | [制作提交](authoring-transactions.md)、[编辑 Schema](../../contracts/authoring.schema.json) | [S14 草稿测试](../prototypes/index.html#connection-draft-test)、[S15 过期结果](../prototypes/index.html#connection-test-stale) | Q43 |
| 手工资产提交 | 固定源像素或轨迹；新对象/新版本；头冲突与取消保留编辑 | [制作提交](authoring-transactions.md)、[编辑 Schema](../../contracts/authoring.schema.json) | [A14 保存](../prototypes/index.html#asset-edit-save)、[A15 冲突](../prototypes/index.html#asset-edit-conflict)、[A16 取消](../prototypes/index.html#asset-edit-cancelled) | Q44/Q47 |
| 大流程保存 | 分块、封存与引用；完整信封预算；失败保留草稿并查询原收据 | [内容传输](../interfaces/content-transfer.md)、[内容 Schema](../../contracts/content.schema.json) | [E29 保存](../prototypes/index.html#content-save)、[E30 传输失败](../prototypes/index.html#content-transfer-failed) | Q45 |
| 公共摘要一致性 | RFC 8785 编码与跨语言向量，原始字节资源独立核验 | [公共摘要](../interfaces/content-transfer.md)、[规范向量](../../examples/canonical/vectors.json) | 保存、计划确认和资源身份的共同契约 | Q46 |

## 实现顺序

1. M1 固定执行检查点、目标绑定、暂停和输入恢复，完成 Q30。
2. M2 实现控制关系事务、表达式会话和类型化设置，完成 Q34、Q36、Q37。
3. M3 完成轨迹裁剪、选区、导入映射与资产删除，完成 Q32、Q33、Q38、Q39。
4. M4 接入版本化断点、运行副本及 MCP 管理查询，完成 Q31、Q35。
5. M5 验证安装制品的签名、信任状态持久化和恢复，完成 Q40。

各阶段还需完成[路线图](../../ROADMAP.md)规定的其他能力和[需求追踪](../quality/traceability.md)中的关联用例。

## 评审与验证入口

- [完整系统文档](../README.md)：架构、领域设计、接口和发行约束。
- [全界面目录](prototypes.md)：正常、错误、冲突和恢复路径。
- [高清效果图](high-fidelity.md)：核心界面及上述操作的 2× 图像。
- [文档检查](../quality/document-validation.md)：契约、示例和边界检查入口。
- [原型检查](../quality/prototype-validation.md)：界面、交互与图像检查入口。
- [系统验收用例](../quality/verification.md)：实现后的端到端验收条件。

MCP 集合查询见[分页查询](../interfaces/pagination.md)：诊断、启动要求、运行调用、输入效果、命名项、匹配候选、批次资产、导入对象和已安装语言均通过服务端条件查询逐页读取；交互入口为 C20～C22，验收为 Q49。

## 跨平台功能

DW-030 使用[跨平台架构](../architecture/platforms.md)与[平台 Schema](../../contracts/platform.schema.json)统一后端能力、显示覆盖、屏内坐标和相对输入单位。目标选择、录制准备、时间轴、暂停恢复、设置和安装按当前平台能力提供相应操作；Windows UAC、macOS 系统隐私授权、Wayland 系统选择器分别处理。Q54～Q58 覆盖能力、格式、交互、身份和制品。

界面入口：[平台能力](../prototypes/index.html#platform-capabilities)、[macOS 系统权限](../prototypes/index.html#platform-macos-permissions)、[Wayland 屏幕与输入](../prototypes/index.html#platform-wayland-session)、[录制能力不可用](../prototypes/index.html#platform-wayland-recording)、[跨平台屏幕映射](../prototypes/index.html#platform-display-mapping)。
