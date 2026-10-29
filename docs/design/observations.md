# 观察会话与记录中心

## 对象与入口

主导航“观察”管理单次观察、持续会话和历史记录。单次观察使用 vision.observe；持续观察使用 observation.start，不需要创建桌面输入运行。配置和结果使用[视觉对象契约](../../contracts/vision.schema.json)。

会话固定目标授权、连接版本、提示内容、区域、输出要求、周期和容量预算。启动返回会话身份及 running 状态，第一条结果单独产生。更改连接或采集目标后需要结束当前会话并建立新会话；历史记录继续关联原配置。

## 周期与变化检测

周期范围为 5 秒至 24 小时。一个会话最多有一个在途模型请求，模型连接另有有界并发限制。一次周期到来时已有请求未结束，登记 busy 跳过，不采集图像、不追加模型请求，也不积压触发。首次启动立即检查一次；后续触发按单调时钟安排，休眠后不补发错过的所有周期。

变化检测在本地进行。先取得目标区域并生成颜色分块特征，与同一目标、几何、区域、模型配置和提示内容下最近一次成功持久化的基线比较。兼顾整体差异和局部控件变化；用户可设置阈值。目标代次、区域、尺寸或连接配置不同，必须重新建立基线。

无显著变化时保存 state=skipped、skipReason=unchanged 的检查记录、difference、原因及基线关联，结果内容为空；不复用旧模型文本作为本次回答。模型失败、取消或保存失败不推进成功基线。

每个会话同时受 maxRecords 和 maxDurationMs 限制。达到上限后停止触发并完成当前请求的收尾，记录具体停止原因。

## 状态与停止

会话状态为 running、stopping、stopped、failed。停止带 expectedControlVersion，进入 stopping 后不再接受新触发；当前请求取消或在剩余超时内完成收尾后才进入 stopped。旧控制版本返回冲突和当前快照。

目标失效、授权撤销、锁屏、采集失败或存储无法继续写入时停止会话并保留具体原因，不自动扩大截图范围。重新绑定后新建会话。observation.start 的 detached 省略时取 false；窗口关闭或所属客户端会话结束时停止该观察。用户明确选择 detached=true 时由 Engine 持有，仍受目标、时长和记录预算约束。

取消单次观察只影响该作业。取消、会话结束或配置变化后的迟到响应保留原关联，不覆盖其他记录，也不重新启动会话。

## 会话归属与重启

所有者由认证上下文中的稳定客户端身份、连接会话和授权记录确定，不能通过请求参数冒认。创建时固定 owner、detached、目标代次和连接版本；重连不改变原所有者，读取和控制仍重新核对对象授权。

普通网络请求完成或资源订阅断开不等于客户端退出。新版 HTTP 的所有者是经认证的 Engine 委托上下文，按[组件生命周期](../architecture/lifecycle.md)由 Bridge 服务与原授权期限管理，不建立 MCP 协议会话。默认观察在工作台窗口关闭、stdio EOF、网络协议会话明确结束或授权会话到期后进入 stopping；detached=true 只豁免对应客户端退出。撤销模型凭据、目标或客户端授权仍会停止后台观察。停止先禁止新触发，再完成当前请求的取消与持久化收尾。

Engine 重启不恢复周期计时或自动重发模型请求。启动核定将遗留 running/stopping 会话标为 failed，reason 为 PROCESS_INTERRUPTED，保留 checkedCount、原配置和已保存记录；在途 operation 按[持久化事务](persistence-transactions.md)核定。重新观察需要用户选择有效目标与连接后创建新会话。

每次触发先原子占用一个检查名额；检查计数包含成功、busy、unchanged、失败和取消。正式记录、最后记录、成功基线与状态事件在最终事务提交，失败保存不退还已占名额，也不推进基线。会话预算以创建时固定的截止时间和运行时单调时钟共同限制，重连不重置预算。

## 记录与查询

| 内容 | 用途 |
|---|---|
| checkedAt / capturedAt / completedAt | 本次检查、实际采集和结束时间；没有采集时 capturedAt 为 null |
| frameId / screenshotResource | 定位本次实际采集帧；截图按所选保留策略保存 |
| sessionId / operationId | 追踪持续会话或单次操作 |
| runId | 关联产生观察的运行；独立观察为 null |
| connectionId / connectionVersion | 确认实际使用的模型与配置版本 |
| state / skipReason / reason | 区分结果状态；跳过类型为 busy 或 unchanged，其余状态的 skipReason 为 null |
| difference | unchanged 检查的实际差异值，范围 0～255；其他状态为 null |
| baselineRecordId | 解释变化检测使用的基线 |
| output / usage / durationMs | 展示模型结果、用量和耗时 |

busy 记录的 capturedAt、frameId、screenshotResource、baselineRecordId、difference 和 output 均为 null，模型用量为零。unchanged 记录必须有本次实际采集时间、frameId、成功基线及实际 difference，output 为 null，模型用量为零。operationId 标识本次检查作业；busy 和 unchanged 作业均不发送模型请求。

checkedAt 不晚于 capturedAt（如有），completedAt 不早于二者。没有采集时不得提供 frameId 或截图资源；成功观察和保存失败的模型结果必须关联实际采集帧。截图清理后允许 screenshotResource 为 null，保留原 frameId 和采集事实。

成功、跳过、失败与取消均可查询。模型响应成功但截图或记录保存失败时显示 persistenceFailed；内存结果可导出到独立恢复文件，不能把尚未入库结果当作已保存记录。

observation.query 支持会话、开始时间、结束时间、状态、排序和分页。时间参数按 checkedAt 筛选并使用 UTC，开始包含、结束不包含；界面按所选时区将日期及时间换算后提交，并显示当前时区。from 必须早于 until。最近 N 条使用 newest 和 pageSize；跨日查询不依赖文件名。

查询游标绑定筛选、排序、所有者和固定成员快照，按 checkedAt、recordId 稳定排序。快照默认 5 分钟有效，翻页不延长寿命；期间新增记录不插入原分页结果。过期返回 CURSOR_EXPIRED 并重新查询，不静默换成新的记录集。记录详情显示实际截图、配置版本和保存状态。运行中产生的观察附加运行关联，独立观察不伪造 runId。

## 导出与保留

导出可选 JSONL 或 CSV，并可包含所选记录的截图。先预览数量、时间范围、字段、截图和输出位置；敏感参数及凭据不进入导出。checkedAt、可空 capturedAt、skipReason 和可空 difference 保留各自含义；不把检查时间填入缺失的采集时间。CSV 对电子表格公式前缀进行文本转义，原始值通过 JSONL 保留。

观察记录采用独立容量统计。默认建议记录保留 30 天、截图最多 2GiB，由用户确认；被运行、导出或故障恢复固定引用的内容按对应引用处理。删除记录和删除截图分别展示影响，不能使成功记录悄悄指向另一张图。正在查询、导出和恢复使用的正式对象以有期限引用保护；逻辑删除即时从新查询排除，物理回收等待引用释放。基线记录的历史关联不自动永久保留截图，当前活动会话单独固定其基线特征。具体成员、索引、引用和容量交点见[持久化事务](persistence-transactions.md)。

## 接口与更新

| 方法 | 用途 |
|---|---|
| observation.start / stop / session | 创建、停止及读取会话快照 |
| observation.query / read | 分页查询和读取记录 |
| observation.export | 导出固定记录集 |
| stream.subscribe / read / unsubscribe | 订阅观察或提取对象的状态和增量 |
| operation.get / cancel | 查询及取消单次作业 |

增量使用独立 streamId、instanceEpoch 和严格递增的序号。订阅返回一致快照，断线补拉按游标进行；缺口超出窗口则重新订阅。客户端退出释放订阅，不据此推断后台会话已停止。MCP 资源通知与 GUI 使用同一记录和授权检查。

界面提供概览、持续配置、运行中、停止中、历史过滤、记录详情、跳过原因、失败、保存失败、导出和空状态。画板见[全界面原型](prototypes.md)，验收见 Q23/Q24。
