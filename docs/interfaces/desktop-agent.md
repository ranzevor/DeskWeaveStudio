# Desktop Agent 协议

## 端点与身份

Agent 使用与 Engine 相同的字节帧、UTF-8、时限和错误信封。请求结构为 [agent.schema.json](../../contracts/agent.schema.json)；Engine 方法使用独立的 service Schema。共享 requestEnvelope 定义消息字段，接收端按自己的方法集合校验，不能只验证信封后直接派发。

连接验证登录会话、交互桌面、实际对端与随机 epoch。Agent 只接收已认证 Engine 和受限本地控制入口；MCP、WebView 和视觉工作单元不能直接提交输入段。普通与提升实例共用桌面互斥身份，握手不兼容时保留已有拥有者并拒绝新输入。

workspace 与 run 标识用于所有权核对，不替代连接授权。能力、租约和资源均绑定当前 Agent epoch；Agent 重启后旧代次失效。目标选择能力来自可信原生选择与受保护登记路径，target.bind 只兑换已登记记录。租约续期不延长目标权限，expiresAt 到期后停止新的输入和采集。

## 方法

公开原生分发共 22 个方法；Schema 的 resolvedValue 是已求值数据的辅助定义，不作为命令派发。

| 方法 | 参数 | 结果与处理 |
|---|---|---|
| lease.acquire | runId、目标令牌集合、waitMs、可选 interruptPolicy | leaseId、generation、agentEpoch、expiresInMs；waitMs=0 表示遇忙立即拒绝 |
| lease.renew | leaseId、generation | 本运行的新到期预算；不改变目标授权 |
| lease.release | leaseId、generation、reason | 撤销输入许可、清理状态与新代次 |
| lease.pause | leaseId、generation | pausing/paused/uncertain 租约快照；完成释放后提供实时检查点 |
| lease.status | leaseId | 当前拥有者的实时状态、检查点或失效原因 |
| lease.resume | leaseId、generation、pauseId、resumeAttemptId、resumePlanHash、可选 confirmationGrant | 校验同一存活 Agent 的检查点，受控恢复后返回租约快照 |
| target.bind | 原生目标选择能力 | targetToken、显示信息和有效期 |
| capture.create | targetToken、可选 roi | 帧、物理原点、尺寸、拓扑与资源 |
| desktop.capabilities | 空参数 | 当前系统、架构、后端、能力代次、逐项状态、覆盖范围及轨迹版本 |
| desktop.topology | 空参数 | 已协商的全局物理或屏内像素拓扑，包含实际显示集合与坐标映射 |
| record.start | 目标、预期拓扑版本、坐标模式、运动事件选项 | recordingId、状态、控制版本及 source |
| record.snapshot | 录制身份 | 来源、状态、事件数、时长、当前拓扑版本和中断原因 |
| record.pause/resume | 录制身份、预期控制版本 | 实际状态与新控制版本 |
| record.finish | 录制身份、预期控制版本 | 轨迹资源和 partial 标志 |
| segment.submit | 租约、代次、段身份、摘要、目标、已求值操作 | accepted/running/paused/sent/rejected/uncertain 与清理结果 |
| segment.status | 段身份、原参数摘要 | 本调用者的既有收据 |
| resource.prepare | 类型、长度、摘要、purpose | uploadId 和允许块大小 |
| resource.seal | uploadId | 已验证、不可变的资源句柄 |
| resource.open | resourceId、offset、length | 限定范围的只读资源连接 |
| emergency.stop | 空参数 | 全桌面锁定代次与清理状态 |
| emergency.rearm | 预期代次、本地确认能力 | 就绪代次；不恢复旧租约 |

结果字段以 Schema 对应方法的 result 为准；接收后状态与最终输入效果分开。record.finish 的 partial=true 表示中断或不完整分段，Engine 保存为可修复草稿，不能自动视为可回放轨迹。

## 输入负载

kind=desktop 的目标覆盖授权显示区域并集；全显示屏录制不因跨屏焦点变化而切分会话。录制与回放分别获取用途授权，拓扑变化结束原录制分段。来源与映射见[全显示屏录制](../design/recording.md)。

Engine 完成表达式求值、类型与预算校验后，提交单个输入操作。节点 target 固定 main、on 为空且没有 store。Agent 参数中禁止残留 $expr 或 $literal，也不接受工作流、模型连接、脚本和任意文件路径。

轨迹通过 trajectoryRef 指向 Agent 已封存资源，逻辑键仍与原运行快照关联。Agent 在封存和播放前验证轨迹结构、时间、配对、事件数量与内容摘要，不读取 Engine 任意路径。

payloadHash 覆盖含 targetToken、operation 以及可选 trajectorySha256 的对象，采用 RFC 8785 的 UTF-8 确定编码后计算 SHA-256。资源句柄只用于传输，同一轨迹换用新句柄不改变内容身份；摘要与实际资源不符则拒绝。收据另外绑定经认证拥有者、运行、租约和代次。

同段同摘要返回已知收据，同段不同摘要返回冲突。Agent 对整段登记准入与结果；轨迹逐事件台账只服务实时调度和释放，不能用持久日志推测崩溃发生时的精确播放位置。

轨迹中的 pointer.move 和 pointer.delta 固定为已有事件，不按当前光标状态切换语义。Agent 保留所有中间点和原始增量，按事件期限通过同一原生输入流发送；按钮按下、持续移动与释放顺序不可拆散。Windows 使用 MOVE_NOCOALESCE 防止系统合并移动消息，绝对输入同时使用 ABSOLUTE 和 VIRTUALDESK。时序超限后的段状态为 paused 并返回 pauseId，lease.status.reason=INPUT_TIMING_OVERRUN，pauseSnapshot 仍使用同一实时游标；累计顺延作为 Agent 内部段调度状态随暂停冻结，不能通过持久摘要重建。具体调度与时序超限见[连续指针与笔画](../design/pointer-motion.md)。

record.start 的 includePointerMotion=false 只省略未按住时的普通运动，按钮按住期间仍保存完整运动。INPUT_QUEUE_OVERFLOW 和 INPUT_CAPTURE_LOST 对应 partial=true 的中断分段；客户端分别呈现输入积压或接收中断，并提供保存分段及新建录制操作。 原因枚举与接口契约随组件协议版本一同分发；客户端不能校验新状态时按协议不兼容处理并禁止继续，不将未知原因转换为完整录制。

## 资源连接

大轨迹先 prepare，经独立二进制连接按 uploadId、偏移和长度上传，全部块在允许范围内接收后 seal。每块最多 4MiB，每个对象最多 32MiB；密封前不可输入，摘要、Schema 或配对失败则销毁该上传暂存。

上传会话只允许写入 Agent 自有暂存，不接受目录参数；闲置上传默认 60 秒过期。已封存资源按调用者和用途授权，运行使用期间固定引用，退出或撤权后释放；不能把写入句柄转成采集或输入权限。读取同样使用受限范围连接，退出时关闭句柄。

采集和录制产生的资源由 Engine 验证并转存工作空间对象。原始录制片段、规范化轨迹和编辑调整分别保留必要关联；未配对事件不伪造为原始捕获结果。

## 调度与撤销

心跳、到期和回放使用 Agent 单调时钟。每段前验证租约、目标、焦点和显示代次；开始后的失焦、锁屏、会话切换、用户接管和急停均能撤销后续输入。记录/回放互斥，原生控制路径不等待资源上传或图像计算。

急停先锁定并增加代次，再清队列与释放。返回 cleanup=pending 或 failed 时，客户端不能显示停止已完成。rearm 需要可信本地交互产生的确认能力，不能由 Engine 自行拼接一个字符串获得。

到期或崩溃不自动重放。新 Agent 对旧段只能返回可核实收据或 uncertain；需要人工接管时保留原效果状态。具体运行规则见[执行设计](../design/execution.md)。

## 验证入口

[租约请求](../../examples/messages/agent-lease.json)、[输入段](../../examples/messages/agent-segment.json)为合法结构样例，[残留表达式](../../examples/invalid/agent-expression.json)应被结构校验拒绝。演示令牌只用于合成数据。

原生验收覆盖同段重发、异参冲突、伪拥有者、旧 epoch、资源替换、逐块中断、租约到期、正在拖拽时急停和 Agent 中途退出。原生验收由目标应用检查收到的事件、输入释放与窗口状态。

## 启动、排空与本地确认

Agent 发现与启动以用户、系统会话和交互桌面为互斥边界。验证实际进程后先握手，已有实例尚未就绪或协议不兼容时不启动另一个发射端。原生急停注册和输入清理核对完成前不开放输入能力。

排空时拒绝新租约和新录制，保留 lease.release、segment.status、record.finish、资源收尾和紧急停止。只有租约、录制、受信连接、上传、资源交接及清理全部结束后才退出。其他工作空间仍连接时，单个 Engine 退出不结束共享 Agent。

受限提升先停止派发、释放并确认旧输入，再交接桌面互斥拥有者。新实例取得新的 epoch，旧目标和确认能力重新登记；UAC 拒绝或交接失败保持输入关闭，不能通过启用两个实例恢复。

emergency.rearm 的 confirmationGrant 只能由受信本地确认路径产生，绑定本次锁定代次和用途，使用后失效。Agent 重启后旧 confirmationGrant 失效；原输入效果无法确定时保持 uncertain，并进入本地接管确认。组件故障、能力和关闭顺序见[生命周期设计](../architecture/lifecycle.md)。

## 暂停与继续协议

检查点、租约快照与继续计划结构见 [execution-control.schema.json](../../contracts/execution-control.schema.json)。`lease.pause` 是正常控制路径，不能通过 lease.release 再申请租约代替；释放会使实时游标失效。Agent 在可确认的原生事件边界冻结源游标和剩余主动等待，再释放实际登记输入。单个 Unicode 标量的原生批次不拆分；系统调用部分接受、精确位置不明时进入 uncertain，不创建推测检查点。

paused 必须同时具有有效 pauseSnapshot 和成功或无须清理的 cleanup。检查点的 leaseId、generation、agentEpoch 与租约一致，nextEventIndex 不超过 eventCount；heldInputs 是暂停清理前集合，实际释放结果另计。inputTargetToken 由 Agent 独立保存最后实际输入目标，不依赖 cursor 是否存在；显式 down 节点成功后保留租约持有集合，不能因段完成而误清理，停止或运行退出仍释放。segment.status 的 paused 及 pauseId 只表达当前仍存活的暂停，不把整段标为 sent。段结束且完成清理后才按原有收据规则报告完成。

Engine 把实时检查点与运行控制版本绑定。无恢复输入和指针需求时可在同一 Agent 内直接继续；需要恢复时只接受可信 Host 签发、当前计划绑定的一次性 confirmationGrant。resumePlanHash 必须等于 Engine 审阅计划摘要，Agent 核对 pauseId、epoch、generation、目标与拓扑、持有集合、restoreTargetToken 及恢复指针目的地。确认交互期间允许 Studio 处于前台，确认后的目标激活只针对 restoreTargetToken，且必须等于检查点 inputTargetToken 并属于已固定目标集合；cursor=null 的跨节点持有使用同一规则，实际前台核对成功后才恢复；物理输入冲突不能通过激活目标绕过。确认后发送的指针与按住事件独立记账，不推进源事件游标。

`resumeAttemptId` 同拥有者、同参数可重复查询；同标识异参拒绝。首次恢复部分失败须释放该次已登记输入，无法确认实际效果则返回 uncertain；不能用新 attempt 重发源事件。状态轮询不延长暂停或计划时限，Engine 失联、停止、急停和会话撤销可以打断恢复事务，提交前再次检查代次。

同一个 Agent 仍存活、检查点未到期且授权事实未变才可正常继续。重启、失去精确游标或旧 epoch 的检查点不接受恢复，持久日志只供核对。拖拽暂停的释放已真实发生，后续重新按下构成新的输入，目标应用是否延续同一业务手势需要流程后置检查。

## 跨平台协议能力

platform-capabilities-v1 增加 desktop.capabilities，结果由 [platform.schema.json](../../contracts/platform.schema.json) 定义。display-coordinates-v1 增加屏内像素拓扑、display 模式与带显示身份的位置；Trajectory 1.1 使用同一结构。Engine 的 system.capabilities.desktop 转发该有界能力摘要，实际目标授权仍在 Agent 核对。

双方握手取得共同能力后才使用新联合分支；不支持的格式返回 SCHEMA_UNSUPPORTED。源输入单位、坐标变换、平台权限与中断规则见[跨平台架构](../architecture/platforms.md)。desktop.capabilities 是 Agent 本机方法，MCP 只通过现有系统能力资源读取摘要。
