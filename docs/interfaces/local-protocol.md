# 本机协议与命令

## 连接与握手

协议基线为 1.0，扩展通过共同能力协商。Windows 使用有显式 DACL 的命名管道；macOS/Linux 使用私有运行目录中的 Unix domain socket 和本机对端身份检查；均限制登录会话并验证客户端和服务端的实际身份。端点名称和消息中的 session 字段都不能单独授权。

握手交换 protocolVersion、componentVersion、instanceEpoch、schemaDigest、capabilities 和 limits。主版本不同拒绝执行，次版本取共同能力；新的必需字段或改变既有含义需要主版本变化。

Engine、Agent 与 Tauri Host 分别验证自己的调用边界。Engine 授予的工作空间权限不能自动换取 Agent 的桌面租约。

连接层使用相同的长度前缀，先完成挑战、认证与就绪确认，再接收业务消息信封。认证前拒绝所有服务方法；连接层包不混入 message.schema.json 的 request/response/event 分发。

| 连接层资料 | 验证规则 |
|---|---|
| protocolVersion、componentVersion | 协商协议与组件版本，不凭客户端自述代替二进制核验 |
| instanceEpoch | 来自实际对端进程，变化后废止旧连接状态 |
| schemaDigest | 固定公共契约集合的 SHA-256；差异只能按已知共同能力协商 |
| capabilities、limits | 功能和容量上限；不能解释为已授予对象权限 |
| 双方随机挑战 | 本次连接生成并绑定完整应答，拒绝旧应答重放 |
| 连接票据与身份恢复凭据 | 来自受保护启动/审批通道，绑定接收进程、用户、工作空间、用途、期限及稳定客户端身份 |
| 就绪确认 | 由服务端分配本次 session；认证和必要对象检查完成前不公布业务就绪 |

连接层字段白名单、长度和深度沿用控制帧上限；未知认证内容、无效挑战或超期票据直接关闭连接。挑战和票据不进入普通日志。票据必须已在接收方登记并核对本次对端；仅原样回显随机挑战不能通过认证。服务端把经核验的身份与权限保存在连接上下文，不接受 request 中的角色覆盖。

稳定客户端身份与一次性连接票据分开：重新连接要取得新的会话并重新检查权限，原请求归属只有在身份恢复凭据有效时才延续。具体签发、能力消费、撤销与退出规则见[组件生命周期](../architecture/lifecycle.md)。

## 字节帧

控制帧为 4 字节小端无符号长度，再跟随该长度的 UTF-8 JSON，最大 1MiB。接收端在解析前限制长度，JSON 解析深度最多 64 层，拒绝重复键、非有限数和不合法 UTF-8，支持分片和多帧连续读取；中途 EOF 作为传输失败，不解析半份对象。

请求参数、响应结果、错误详情、事件载荷及结果查询使用通用 JSON 定义；对象字段数、数组成员数和文本字符数由对应字段的 Schema 校验，字节预算按完整消息和方法参数、结果分别执行。流程 resources 最多 10,000 项；帧预算内的资源数量超限由 workflow.validate 返回 /resources 字段诊断。Value 数据的对象和数组限额适用于表达式、变量等值字段，其他结构使用各自契约。

控制、事件和资源连接独立。二进制资源经受控句柄、范围和摘要读取，单块上限 4MiB；不把任意路径交给 WebView。单个请求最多 32MiB 输入，超出时使用分块作业。

Engine 消息结构见 [message.schema.json](../../contracts/message.schema.json)，请求的方法与参数同时受 [service.schema.json](../../contracts/service.schema.json) 约束。成功响应按原请求方法的 result 校验；响应本身不携带可重新选择方法的字段。动作负载再按对应参数 Schema 校验。

## 请求和返回

```json
{
  "protocolVersion": "1.0",
  "kind": "request",
  "id": "request-demo-1",
  "session": "session-demo",
  "method": "workflow.validate",
  "workspace": "workspace-demo",
  "deadlineMs": 5000,
  "params": {"workflowId": "workflow-demo", "revision": "revision-demo"}
}
```

请求 deadlineMs 是接收端从准入开始计算的相对预算，不依赖客户端墙钟一致；覆盖处理、结果校验和完整响应编码，超时返回 DEADLINE_EXCEEDED。完整响应超过 1MiB 时返回 LIMIT_EXCEEDED 错误信封。返回响应分为 ok=true 的 result 和 ok=false 的 error；长作业先返回 operationId，运行先返回 runId 和 accepted 状态。

## 应用命令

| 方法 | 参数要点 | 结果 |
|---|---|---|
| workspace.open | 原生选择器授予的目录能力 | 工作空间身份、格式和权限 |
| workspace.catalog | kind、nameContains、states、order、cursor、pageSize | 固定查询中的授权对象与下一游标 |
| query.read / query.close | 集合、类型、筛选、排序、游标、每页数量 / snapshotId 或 collectionId | 类型化分页结果 / 快照或诊断集合释放 |
| workspace.backup/restore | 导出位置或备份来源及新目录能力 | 可取消作业 |
| workflow.read | workflowId、revision | 定义、依赖映射和摘要 |
| workflow.validate | 定义对象或固定版本；格式非法的定义进入诊断流程 | 字段/图/资源诊断 |
| workflow.save | draftVersion、baseRevision、定义 | 已提交版本和确认代次 |
| workflow.export | workflowId、revision、outputGrant | 导出作业和包清单 |
| run.start | revision、参数、目标绑定、预算 | runId、接收状态 |
| run.pause/resume/stop | runId、expectedControlVersion | 目标状态请求 |
| run.snapshot | runId | 状态、调用树、游标和事件序号 |
| debug.step | runId、frameId、控制版本 | 一步完成或具体失败 |
| record.start/pause/resume/finish | 目标、配置、录制身份 | 录制状态或轨迹资产 |
| target.select | selectionGrant 原生目标选择能力 | 受控目标令牌和有效期 |
| capture.create | 目标令牌、区域 | 帧资源 |
| vision.match/observe | 资产/连接、帧/目标、参数 | 可取消作业 |
| asset.read/import/references/delete | 对象身份、范围或导入能力 | 资源或作业 |
| settings.read/update | 类型化作用域、预期版本、set/reset 批次 | 配置快照与逐键生效范围 |
| operation.get/cancel | operationId | 已知状态、取消结果 |
| request.status | 原请求 ID | 原准入和操作结果 |
| connection.configure/test | 连接、端点、模型、密钥模式或连接身份 | 配置状态或可达性 |
| input.submit | intentId、targetToken、operation、可选轨迹版本映射 | 输入作业 |
| event.subscribe/read/unsubscribe | 运行、游标、epoch 或订阅身份 | 一致快照、补拉事件或关闭结果 |
| emergency.stop/rearm | 空参数或控制代次及本地确认能力 | 急停锁定/就绪状态和代次 |

workspace.open 与 workspace.restore 的启动器连接使用 workspace=bootstrap，实际目录能力决定打开或恢复对象；其余命令必须匹配已认证工作空间。恢复仅写入新目录，成功后再打开，不能覆写当前活动库。目录游标绑定工作空间、筛选与目录快照，过期时重新查询，不把重复条目当作新对象。

connection.configure 的 keyMode 为 keep、replace 或 none。replace 才接收 writeOnly 的 apiKey，keep 复用已有凭据，none 适用于无密钥的本地服务。服务端不得把密钥回写到结果、配置查询或诊断。connection.test 只返回连接及模型可用性。

input.submit 的 operation 是无出口、无 store 的单个输入节点，target 固定为 main，由 targetToken 绑定。轨迹操作还须提供一个 trajectoryVersions 映射，逻辑键与 operation.params.trajectory 对应，值为已授权资产版本。Engine 构造临时运行、固定依赖并统一登记和清理；请求结束后不跨请求保留按键。需要组合持有时使用完整轨迹或流程。桌面忙时准入即拒绝；deadlineMs 到期或取消仍要核对已发生的效果。

Agent 方法由 [Agent 契约](../../contracts/agent.schema.json)完整定义，覆盖平台能力、显示拓扑、租约、输入段、目标、截图、录制状态、资源与急停。公开客户端不直接调用 segment.submit；紧急停止入口只能撤销，不能扩大输入能力。Agent 参数与资源交接见[原生协议](desktop-agent.md)。

## 幂等与外部效果

去重键为工作空间、经认证的稳定客户端身份和请求 ID。消息 session 只绑定本次连接。客户端重连可查询原请求；同键异参返回 REQUEST_CONFLICT。

请求摘要覆盖 method 与经过确定性契约默认值及归一化处理的 params，采用 RFC 8785 JSON Canonicalization Scheme 的 UTF-8 结果计算 SHA-256；相同请求 ID 改变方法或规范参数均为 REQUEST_CONFLICT。环境配置在首次准入时固定，查询原请求不按当前配置重算；跨语言共用编码与摘要样例。修改命令保存请求摘要与接收结果，保留至少 24 小时；活动运行的记录保留到运行和未决效果结束。容量不足时拒绝新修改，不提前删除未决去重记录。

读取可有限重试。写入响应丢失时使用 request.status；不得创建新 ID 自动重做输入。Agent 的原生收据不等于目标应用的后置结果。

## 事件与恢复

事件信封 kind 固定为 event，事件类型字段为 event；其余字段包含 streamId、instanceEpoch、seq、occurredAt、payload。occurredAt 使用 YYYY-MM-DDTHH:mm:ssZ，校验真实日历日期。seq 为十进制字符串，每个流严格递增，运行、录制等流各有独立序号空间。

event.subscribe 返回订阅身份、epoch 和序号 S 的一致运行快照，并在事件连接缓冲 S 后事件；event.read 按运行和 afterSeq 补拉，resyncRequired=true 时重新订阅。event.unsubscribe 关闭对应连接资源。订阅返回后，客户端先应用快照再处理增量。重复序号丢弃，缺口补拉；超出保留窗口或 epoch 改变时重新取快照。过滤不同运行不共用一个连续序号空间。

关键状态与事件在同一数据库事务内提交。预览帧和普通进度可合并；客户端缓冲上限 8MiB 或 5,000 条，落后时转快照，不阻塞 Agent 急停。

关闭页面注销订阅。请求和回复同时检查文档、窗口和连接代次，迟到回复不能更新另一个任务。

## 错误与兼容

错误包含 code、message、retry、details；retry 为 never、sameRequest 或 afterQuery。字段位置 fieldPath 使用完整 RFC 6901 JSON Pointer，空字符串表示文档根，/ 表示名称为空的属性；对象键中的 ~ 和 / 分别编码为 ~0 和 ~1。消费端保留原路径并按 ~1、~0 的顺序解码，定位时根据实际容器区分对象键和数组下标。可可靠归属节点时附 nodeId；用户界面显示对应名称与处理入口，堆栈只进入诊断。

核心错误码：INVALID_ARGUMENT、SCHEMA_UNSUPPORTED、REVISION_CONFLICT、REQUEST_CONFLICT、RESOURCE_DENIED、TARGET_LOST、DESKTOP_BUSY、LEASE_EXPIRED、INPUT_RELEASE_FAILED、INPUT_TIMING_OVERRUN、EFFECT_UNCERTAIN、LIMIT_EXCEEDED、STORAGE_UNAVAILABLE、NAME_CONFLICT、CURSOR_EXPIRED、PROCESS_INTERRUPTED、CREDENTIAL_UNAVAILABLE、CANCELLED。

NAME_CONFLICT 指明当前名称占用；CURSOR_EXPIRED 要求以原筛选重新建立查询；PROCESS_INTERRUPTED 保留原作业与效果记录，不提示直接重放；CREDENTIAL_UNAVAILABLE 区分凭据已撤销或无法读取，不能以重输业务参数代替连接处理。

普通分页查询快照默认有效 5 分钟，每个 Engine 最多保留 32 个快照，实际登记的游标位置合计最多 100 万条，关闭或过期后释放；运行明细等更短期限及客户端限额见[分页查询](pagination.md)。游标绑定认证工作空间、查询条件与快照；到期或容量回收后返回 CURSOR_EXPIRED，不从游标猜测新分页位置。新查询取得新快照，结果更新时间不能被当成原快照的继续。

未知执行字段拒绝；extensions 中的命名空间内容可保留。新增非必需事件字段可忽略，未知事件类型触发能力协商或快照，不伪造成功状态。

## 模板、观察和接入方法

模板方法为 template.extract/candidates/names/commit/export/discard；观察方法为 observation.start/stop/session/query/read/export，参数与结果引用视觉对象 Schema。connection.read 返回无凭据的配置版本，可带 version 读取已保存历史。connection.configure 使用 expectedVersion 和 options；revokePreviousCredential 默认 false，keep 模式只允许 false。replace/none 显式撤销旧凭据时停止引用它的会话，历史记录继续关联原版本。

run.prepare 返回指定节点的前置检查计划；run.start 的 startNode、initialVariables、runVariables、planId 和 interruptPolicy 按[流程设计](../design/workflow.md)共同校验。旧运行身份不能作为初始调用上下文。

observation.start 的 detached 默认 false，是否脱离客户端与记录状态分开；会话快照保留该选择。记录的 checkedAt 标识本次周期，capturedAt 可空；busy 跳过没有采集帧，unchanged 跳过记录实际差异值，不复用模型文本。

非运行对象使用 stream.subscribe/read/unsubscribe，以 scope 与 objectId 取得快照；streamId 绑定实际对象和授权。运行事件继续使用 event.*。两者均遵循相同的 epoch、序号、缺口补拉和资源释放规则。

operation.get/cancel 的结果包含原作业 method。作业成功时 result 按[作业结果目录](../../contracts/operation-catalog.json)引用的结构验证；接收回执不能替代最终结果。模板保存或观察导出结果的资源仍需独立授权读取。

resource.read 接受授权资源身份、偏移与块长度，返回完整资源摘要和 Base64 块；读取完成或取消时通过 content.discard({resourceId}) 释放临时结果。content.discard 的 resourceId 与原 uploadId 分支互斥，旧上传释放请求保持有效。内容结构错误使用 INVALID_CONTENT 和 details.contentPath 定位上传 JSON，路径编码与 fieldPath 相同。bridge.* 为可信本地管理命令，不经 MCP 公开。system.capabilities 提供版本、平台、允许动作、有界容量和已协商的 desktop 平台能力摘要；各项状态、显示覆盖和原生坐标契约见[跨平台架构](../architecture/platforms.md)。

## 编辑草稿的持久化

草稿由可信 Studio 通过 draft.read/write/discard 管理，不公布为 MCP 工具或 CLI 子命令。正式 workflow.save 仍要求有效定义并创建不可变版本，不能替代非法源码的草稿保存。[草稿 Schema](../../contracts/draft.schema.json)约束独立数据格式。

| 方法 | 参数和结果 |
|---|---|
| draft.read | workflowId、editorId、cursor、pageSize；editorId=null 分页列出可恢复分支，指定 editorId 读取该分支；返回 items 和 nextCursor |
| draft.write | workflowId、editorId、expectedEditVersion、editVersion、baseRevision、source、lastValidModel、modelEditVersion；返回 Engine 确认的草稿快照 |
| draft.discard | workflowId、editorId、expectedEditVersion；返回是否已丢弃及实际代次，成功后保留墓碑并撤销该编辑会话的写能力 |

首次写入以 expectedEditVersion=null 创建分支，之后按已持久化代次执行 CAS；迟到写入不能覆盖较新版本。窗口与 editorId 的写权限单独核对，另一个窗口不能仅传入编辑身份取得写权限。丢弃后的迟到写入也不能重新创建旧分支。

source 保存本次源码原文；lastValidModel 保存其对应 source、definition 和 dependencies，首次尚无有效模型时可为 null，modelEditVersion 同时为 null。Engine 重新解析历史模型原文并核对定义与依赖闭包，不接受客户端自行声明 valid。modelEditVersion 不得大于 editVersion；当前原文有效时，模型代次与定义必须对应当前编辑代次；原文无效时仍原样保存，并保留最后有效模型。

小内容使用 inline 的 text 或 value。大内容通过 Host 原生受限内容通道先暂存，使用 resourceId、实际 UTF-8 字节长度和 SHA-256 的 contentRef 引用，绑定窗口、工作空间、用途、接收实例和期限。控制帧预算不因草稿较大而放宽；总请求仍不超过 32MiB，Engine 核验实际字节而非只相信声明长度，禁止任意路径或外域 URL。

源码和模型引用在一个 CAS 事务中提交。快照返回 sourceResource、lastValidModelResource、editVersion、baseRevision、modelEditVersion、由 Engine 计算的 validationState、前 100 条 diagnostics、总数 diagnosticCount、必要的 diagnosticsResource 与 savedAt；diagnosticCount 超过 100 时必须提供完整诊断的私有 diagnosticsResource；未超过时可为 null。draft.read 按实际控制帧字节预算分页，items 不超过 pageSize，尚未输出的分支通过 nextCursor 继续读取。这些资源只供已授权本地草稿恢复，不因 asset.read、资源 URI 或 CLI resource read 获得读取权。

关闭窗口等待当前 draft.write 的确认代次。确认期间发生的新编辑继续保持未确认状态；失败保留原文和有效模型的导出入口。恢复时通过 draft.read 选定分支并读取私有资源，原文与模型分别恢复，不能把最后有效模型反写成用户尚未修正的源码。

## 组件和命令入口

业务方法在完成连接层认证后按 service Schema 派发。启动锁、选择审批和本地确认属于受信本地控制路径，不增设可供外部客户端任意调用的授权方法。参数能力由登记方验证，字符串格式通过不能替代授权。

图形工作台、Bridge 和[命令客户端](command-line.md)共用本机方法及请求去重。进程排空时保留停止、急停、必要收尾和原请求查询；新运行与新作业不再准入。完整关闭与恢复顺序见[组件生命周期](../architecture/lifecycle.md)。

## 资产删除、设置与接入管理

| 方法 | 参数 | 成功结果与授权 |
|---|---|---|
| asset.references | assetId 或 assetVersion 二选一；可选 cursor/pageSize，缺省 null/50 | 固定头版本、查询范围、快照与分页引用；asset.read 与引用者读取权限 |
| asset.delete | assetId、expectedCurrentVersion | deleted、removedVersionCount、contentCleanup=deferred；asset.read+asset.write，全部版本与强使用引用重查 |
| settings.read | scope | 类型化 snapshot；settings.read，可信本地 |
| settings.update | scope、expectedVersion、expectedBaseVersion、set/reset 批次 | snapshot、逐键 changes 和 activation；settings.write，可信本地 |
| bridge.read | 空对象 | 已保存/活动公开配置与版本、requiresRestart、state；workspace.manage，可信本地 |
| bridge.token.list | cursor、pageSize、states | 元数据 items、nextCursor、snapshotAt；workspace.manage，可信本地 |
| bridge.token.impact | tokenId、cursor、pageSize | 逐令牌计数、受限运行详情与分页快照；workspace.manage，可信本地 |

asset.references 的 assetId 分支查询全部版本；assetVersion 分支保持单版本含义，不得把当前版本的零引用解释为整个资产可删除。结果包含 assetId、currentVersion、versionCount、scope（asset/version）、assetVersion（asset范围为null）、snapshotId、snapshotAt、expiresAt、references、totalReferences、hiddenReferences、nextCursor。引用按 (ownerKind, ownerId, assetVersion) 去重计数并稳定排序；不可读取引用仅计入hiddenReferences。首请求在一致事务固定成员与头版本，后续cursor必须匹配同一分支/身份；五分钟到期返回CURSOR_EXPIRED，新查不续用旧计数。删除使用本页currentVersion作为expectedCurrentVersion，并在提交时重查，不把分页快照当作删除许可。

asset.delete 以逻辑 assetId 为整个删除单位，删除全部资产版本，不接收单个 assetVersion 作为目标。expectedCurrentVersion 必须等于提交时当前版本；任一版本被使用则整笔返回 ASSET_REFERENCED，不级联删除引用者。引用表覆盖固定版本、草稿、运行/调用帧、模板暂存/提交、活动观察基线、查询快照、导出、备份/恢复、作业和有效资源句柄。判断基于逻辑版本关系，不以另一资产复用相同 blob 判定本资产仍被使用。可读取引用者分页返回名称和版本；不可读取引用只返回合计 hiddenReferences，不泄露其身份或内容。totalReferences 统计全部使用引用，空可见页不表示可删除。

删除事务比较当前版本、复查引用与访问权、删除索引/版本关系/名称占用，并保存请求回执及逻辑删除墓碑；内容按全局强引用归零后的延迟回收处理。success 仅表示逻辑删除已提交，不承诺所有副本或备份字节已物理抹除。重复同请求返回原回执；新请求引用已删对象返回 ASSET_NOT_FOUND。提交前新增引用使删除拒绝，提交后的新引用注册必须拒绝已删版本。

settings 使用[设置契约](../../contracts/settings.schema.json)及[配置规则](../operations/configuration.md)。整批 CAS 在外部资源准备后重新比较本层和合并基础版本；SETTINGS_CONFLICT 返回本层与基础实际版本，不返回秘密或静默改写客户端编辑。作用域不允许、重复键和跨键条件错误为 INVALID_ARGUMENT。所有字段错误指向具体键与对应业务名称。

本地接入管理读取及 TLS 脱敏见[MCP 管理设计](mcp.md)。bridge.configure 消费输入文件能力后返回公开配置快照，不把 certificateGrant/privateKeyGrant 回写。新增 ASSET_REFERENCED、ASSET_NOT_FOUND、SETTINGS_CONFLICT、UPDATE_SIGNATURE_INVALID、UPDATE_METADATA_EXPIRED、UPDATE_REPLAYED、UPDATE_REVOKED、UPDATE_TRUST_UNAVAILABLE 错误只表达各自具体失败；后四项由更新器使用，不通过 Engine 扩充可调用更新权限。

## 制作与检查扩展

workflow.revisions、connection.list、debug.variables.read 分别提供版本、连接和变量的固定分页，见[运行检查](../design/runtime-inspection.md)。connection.testDraft 创建本地临时测试作业，asset.edit.prepare/commit/discard 固定编辑来源并提交新资产版本，见[制作事务](../design/authoring-transactions.md)。content.begin/write/seal/discard 及workflow.save/modelRef、workflow.read/modelResource、快照数组资源见[内容传输](content-transfer.md)。完整控制信封仍受1MiB约束，不能通过内嵌大对象绕过。

## 全显示屏录制

本地方法 desktop.topology 返回显示区域、逐屏像素、可用的布局与坐标变换信息；record.start 要求预期拓扑版本，record.snapshot 返回固定来源及当前录制状态。全部显示屏录制须由目标授权、显示覆盖及后端被动录制能力共同确认，不能由 target.kind=desktop 单独推定；source 随轨迹内容保存。接口结构见[录制 Schema](../../contracts/recording.schema.json)，状态和坐标规则见[全显示屏录制](../design/recording.md)。

## 集合查询

诊断、运行要求、调用帧、输入效果、命名项、匹配候选、批次资产和导入结果使用类型化集合入口。query.read 默认读取 50 项，最多 100 项；来源权限、条件、快照与页大小绑定游标。run.snapshot 的 frameCount/effectCount、framesQuery/effectsQuery 对应同一 dataVersion；调用输出通过变量查询读取。具体协议与错误见[分页查询](pagination.md)。

## 表达式编辑预览

expression.preview 属于可信本地编辑方法，沿用当前编辑会话的 workflow.read 权限。参数 value 使用公共 Value AST；context 只接收 inputs、vars、run、prev、calls、runtime 六个可选 JSON 根；expectedType 为 any、null、boolean、number、string、array 或 object，缺省 any。context 缺省空对象，缺少根或路径按同一求值器的缺失值规则处理，不补入真实运行变量。

Engine 使用流程运行的领域求值器，单次仅计算所传合成数据，不创建运行、持有租约、读工作空间对象或调用外部连接。成功返回 value；缺失路径、除零或类型不匹配返回 INVALID_ARGUMENT。details.expressionPath 是相对 value 的字段路径段数组，字符串表示对象键、整数表示数组下标，空数组表示 value 根；details.reason 分别为 PATH_MISSING、DIVISION_BY_ZERO 或 TYPE_MISMATCH。数值范围、AST 深度及运算节点超限返回 LIMIT_EXCEEDED，reason 分别为 NUMERIC_RANGE、AST_DEPTH、AST_NODES，并携带同样的位置字段。请求时间耗尽返回 DEADLINE_EXCEEDED。布尔字段使用 expectedType=boolean，不将数字、字符串或 null 转换为布尔值。

完整参数与成功结果各不超过 256KiB UTF-8，Schema 中以 x-maxUtf8Bytes 声明，由分发器统一执行。参数按补默认值前的 params、结果按包含 value 字段的完整 result 计数，均使用 RFC 8785 规范编码后的实际字节；超限返回 LIMIT_EXCEEDED。完整消息仍受 1MiB 控制信封限制；AST 使用共同的 256 运算节点、32 层嵌套上限，执行预算取 deadlineMs 与 1 秒的较小值。每客户端最多一个在途预览；新编辑取消旧等待，服务收拢原计算，前端按编辑代次丢弃迟到回复。超限直接返回具体错误，不裁剪值、抽样数组或返回部分运算结果。

计算错误详情见 [expressionErrorDetails](../../contracts/values.schema.json)；请求结构错误使用 INVALID_ARGUMENT 和 details.fieldPath 定位参数。客户端按 reason 处理计算错误，按 expressionPath 定位字段，message 用于显示具体原因。coalesce 只处理内部的缺失路径，不吞掉除零、类型或预算错误。

该方法与草稿共用本地编辑入口；MCP 的 workflow.validate、run.prepare 和 run.start 调用同一领域校验与执行逻辑。入口分工及处理器约束见[共享应用服务](../architecture/shared-services.md)。
