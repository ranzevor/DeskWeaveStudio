# MCP 接入

## 协议与传输

主协议采用 [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)，兼容 [2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25)。默认提供 stdio 与显式启用的 Streamable HTTP；旧 HTTP+SSE 仅作为单独启用的旧客户端兼容入口。协议版本、传输与适配规则由 [MCP 目录](../../contracts/mcp-catalog.json)中的 protocolProfiles 定义。

Bridge 绑定一个工作空间 Engine。两代协议共用工具、资源、Prompt、输入校验、授权和结果模型，仅在协议适配层处理元数据、握手、订阅、取消与结果信封。Bridge 不另设存储或执行器；具体边界见[共享应用服务](../architecture/shared-services.md)。stdio 的 stdout 只输出协议消息，日志进入 stderr；网络监听默认关闭。

| 入口 | 主协议 2026-07-28 | 兼容协议 2025-11-25 |
|---|---|---|
| stdio | 每次请求声明版本与能力，可先调用 server/discover | initialize 与 notifications/initialized 完成后派发业务 |
| Streamable HTTP /mcp | 每次 POST 独立处理，不建立协议会话 | 初始化建立会话，后续请求绑定 MCP-Session-Id |
| 旧 HTTP+SSE /sse | 不提供 | 显式选择后启用，与消息提交端点使用同一授权 |

## 主协议请求与发现

按 [2026-07-28 版本协商](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)逐请求处理。params._meta 必须包含 io.modelcontextprotocol/protocolVersion 与 io.modelcontextprotocol/clientCapabilities，clientInfo 为可选实现信息。能力可为空对象；客户端不能继承此前请求声明的能力。缺少必填元数据返回 -32602，版本不支持返回 -32022，data.requested 与 data.supported 分别给出请求版本和支持集合。需要而未声明的客户端能力返回 -32021，data.requiredCapabilities 列出缺项。HTTP 对应以上错误均为 400。

server/discover 是必备方法，客户端也可以直接发送完整的业务请求。发现结果包含 resultType=complete、supportedVersions、capabilities、服务端实现信息及缓存字段。服务端实现名称固定为 DeskWeaveStudio，version 来自实际发行元数据。tools、resources、prompts 使用当前可提供的能力；未实现的扩展不进入结果。发现与工具目录按当前认证主体读取，clientInfo、serverInfo 和能力声明均不授予对象访问权。

请求 Schema 见 [modernRequest](../../contracts/mcp.schema.json)，元数据同时使用公共 requestMetadata 与 modernRequestMetadata。JSON-RPC ID 为非 null 字符串或整数，不能与同一请求发送者尚未完成的请求重号；它只关联本次协议往返。工作空间、稳定客户端、业务对象和可恢复请求身份由 Engine 独立维护。

## 兼容适配与入口分流

Bridge 从实际请求识别协议代际。携带新版协议元数据或新版 HTTP 版本头的请求进入新版校验；元数据缺失、格式错误、未知版本及头体冲突不得落入旧版执行路径。initialize 进入 2025-11-25 兼容适配，返回该受支持版本与实际能力；收到 notifications/initialized 后才处理业务，初始化阶段允许 ping。旧客户端不支持返回版本时结束连接。

stdio 的旧式初始化绑定本次进程连接；新版请求逐次携带上下文。同一 stdio 连接采用其已经选择的代际，改变代际先重新启动连接。HTTP /mcp 可同时服务两代客户端：新版 POST 独立处理，旧版请求使用经认证的旧会话。两代都转换为相同的 Engine 认证上下文与服务参数，不按代际分配第二套工作空间或运行状态。

新版客户端连接 DeskWeaveStudio 时可先发现能力；兼容客户端访问其他服务时，stdio 使用 server/discover 探测，HTTP 检查现代错误响应。-32020、-32021、-32022 表示服务端已经识别新版请求，应修正请求或选择受支持版本，不能因一个 HTTP 400 自动改为旧版。兼容分流、目录投影和错误映射由 Q28 验证。

## 网络认证与消息

Streamable HTTP 使用 /mcp。默认回环监听，非回环地址要求有效 TLS、明确 Host/Origin 白名单与客户端令牌；不接受通配来源、URL 查询令牌或命令行令牌。Origin 存在且不在允许列表时返回 HTTP 403；原生客户端没有 Origin 时仍验证 Host、认证和对象权限。

Bearer 令牌由可信本地入口签发，绑定工作空间、权限、目标和期限。配置结构见 [MCP 配置契约](../../contracts/mcp.schema.json)，示例见[回环 HTTP](../../examples/messages/mcp-http.json)、[TLS 监听](../../examples/messages/mcp-tls.json)和[客户端授权](../../examples/messages/mcp-token.json)。每次请求按当前令牌建立独立委托上下文；撤销停止新准入，并关闭相应订阅与直接输入许可。运行是否停止由本地撤销选项决定。

### 2026-07-28 Streamable HTTP

每个 POST 只包含一个 JSON-RPC 请求，声明 Content-Type: application/json，并接受 application/json 与 text/event-stream。客户端发送的 JSON-RPC response 不作为新版输入；服务端需要客户端补充信息时使用 MRTR 结果。传输规则见[官方 Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)。

| 请求头 | 对应正文 |
|---|---|
| MCP-Protocol-Version | params._meta.io.modelcontextprotocol/protocolVersion |
| Mcp-Method | method |
| Mcp-Name | tools/call、prompts/get 的 params.name，或 resources/read 的 params.uri |

头名称不区分大小写，值按字段语义核对。缺少必要头、非法头值或头体不符返回 HTTP 400 与 -32020；版本和方法校验完成前不派发 Engine。Mcp-Name 含非 ASCII、边缘空白、控制字符或本身匹配编码哨兵时，按官方 =?base64?...?= 格式编码 UTF-8；服务端解码后比较。首期工具不发布 x-mcp-header 参数映射，秘密与桌面内容不进入自定义路由头。

新版不签发或回显 MCP-Session-Id，不使用 Last-Event-ID；这两个旧头不作为新版身份或恢复依据。新版 GET/DELETE 返回 405，即使请求带有旧会话头也不改变代际；GET/DELETE 只在旧版兼容适配且绑定有效旧会话时处理。未知 RPC 方法返回 HTTP 404 与 -32601，资源不存在使用 -32602。新旧协议共用相同的帧大小、超时和限流预算。

请求响应可以是单个 JSON 对象，或仅承载该请求进度及最终结果的 SSE 流。响应流最终结果发出后结束；长通知流使用 subscriptions/listen。SSE 关闭按本次请求的取消信号处理；后台任务与已提交业务状态继续按下文的取消和恢复规则核对。

### 2025-11-25 兼容会话

初始化响应通过 MCP-Session-Id 签发加密随机生成的可见 ASCII 会话值，绑定当前认证客户端、工作空间和协商版本。后续 GET、POST 和 DELETE 携带该值并逐次认证；缺失返回 400，未知、已结束或属于其他客户端的会话返回 404。后续版本头为 2025-11-25；缺少头时只能从有效的已协商会话恢复，无法恢复或与会话冲突时拒绝。

旧版 GET 打开通知流，DELETE 结束会话并释放订阅。收到会话 404 后重新 initialize，再查询原业务状态。旧版 HTTP 流断开只结束等待；取消使用 notifications/cancelled。旧式通知接收后返回 202 且无正文。兼容入口同样不提供 SSE 事件重放，重连通过业务快照和游标恢复。

## 订阅与缓存

新版 subscriptions/listen 携带 notifications 条件，可选择 toolsListChanged、promptsListChanged、resourcesListChanged 和 resourceSubscriptions。服务端首先发送 notifications/subscriptions/acknowledged，返回实际接受的条件；之后只推送已确认类型和获准资源的通知。所有流内通知携带 params._meta.io.modelcontextprotocol/subscriptionId，值等于 listen 的请求 ID；stdio 的多个订阅按该身份分流。

每个客户端最多四个同时活动的 listen 请求，每个请求最多订阅 32 个去重资源 URI，超过容量明确拒绝。每个 URI 均按资源目录和当前对象权限检查。新增订阅条件通过新的 listen 请求建立，并取消原请求；进度只关联原业务请求，不混入目录与资源通知流。客户端断开、撤权或服务关闭后释放相应订阅；重新连接须重新 listen。

正常关闭时返回 resultType=complete 的 listen 终结响应，携带原 subscriptionId；服务端主动拆除订阅时发送引用原请求 ID 的 notifications/cancelled。非正常断开后重新订阅并重取业务快照，通知不充当完整状态。旧版 resources/subscribe、resources/unsubscribe 与 GET 通知流转换为同一 Engine 订阅管理，旧版通知不附加新版必须字段。

新版 server/discover、tools/list、resources/list、resources/templates/list、prompts/list 和 resources/read 返回 ttlMs 与 cacheScope。首期工作空间数据使用 ttlMs=0、cacheScope=private，HTTP 对应 Cache-Control: private, no-store；资源版本和 UI 已读取页面仍按各自契约管理。结果不能跨认证客户端、工作空间、授权版本或协议代际共享。旧版响应不附加新版缓存字段。

集合发现与业务集合均保留[分页查询](pagination.md)：服务端筛选、稳定排序、有期限快照、每页预算和逐页授权。新协议缓存字段不替代分页，也不扩大工具一次返回的数据量。

## 工具目录

完整的机器可读工具、资源和 Prompt 定义见 [MCP 目录](../../contracts/mcp-catalog.json)。目录逐条定义工具、资源模板和预设任务。工具 inputSchema/outputSchema 分别引用完整服务方法的 params/result；存在 publishedInputSchema 时，以该对象作为实际 tools/list 发布的参数结构，否则解析 inputSchema。Bridge 启动时编入共享定义，保留所选结构的对象类型、条件与字段限制，再执行验证。客户端收到的 Schema 不依赖本机文件路径或私有 URN 解析器；分页发现不得遗漏条目。公开方法与本地管理方法的边界由目录和能力检查共同确定。

| 工具组 | 提供能力 |
|---|---|
| 工作空间目录与流程 | 列出对象、读取、校验和保存固定流程版本 |
| 运行准备与控制 | 检查指定入口、创建运行、读取快照、暂停、继续和停止 |
| 桌面输入与急停 | 受限单次输入、组合键、轨迹与独立急停 |
| 采集与匹配 | 采集授予目标，使用 ROI、尺度和候选策略匹配 |
| 模板生产 | 提取候选、分页筛选、命名预览、提交批次及导出 |
| 视觉观察 | 单次观察、持续启停、会话快照、记录查询和导出 |
| 资源与作业 | 分页查询集合，读取授权资源，查询及取消作业，恢复请求状态 |

deskweave_input 映射 input.submit，仅接收一个输入节点，target=main、on 为空且无 store。Engine 构造临时运行，统一快照、租约、操作记录和清理；跨请求不保留按键。组合键使用 keyboard.chord，长时间组合使用完整轨迹或流程，桌面忙时明确拒绝。

工具不接受任意方法名、shell、宿主表达式或绝对路径。文件来源和导出位置使用先前由可信本地入口授予的能力；调用目录不能创建新权限。

## 分页查询

集合数据使用服务端筛选、稳定排序和游标续页。已有目录、候选、观察、引用、版本与变量查询默认每页 50 项，最多 100 项。运行调用、输入效果、诊断、命名、匹配候选、批次资产、导入对象与已安装语言通过 query.read 分页查询；query.close 按 snapshotId 释放查询快照，或按 collectionId 释放诊断集合及其全部查询。具体字段、来源权限、错误与容量见[分页查询](pagination.md)。

工具状态与作业回执只返回状态、数量和集合入口。operation.get、operation.cancel、request.status 及资源读取继续使用相同结构。tools/list、resources/list、resources/templates/list、prompts/list 每页 50 项，游标绑定授权与目录版本。

## 管理与授权边界

模型连接的创建、参数修改、读取与连接测试由可信本地设置负责；MCP 观察只使用已授权的固定连接。监听启动停止、令牌签发撤销、目标选择、系统设置、录制和急停解除同属本地管理；草稿和 expression.preview 属于可信本地编辑。目录的 localOnlyMethods 明确这些方法，客户端发现不能将其变成工具。

目录 permissions 中列出的能力必须同时满足；工具和资源还执行 objectAuthorization 及来源对象检查。对象目录根据 kind 分别要求 workflow.read、asset.read 或 run.read。作业查询和取消绑定原方法权限与所属客户端；资源读取沿用创建时的来源权限。空 permissions 表示使用上述对象权限策略，不能解释为匿名开放。

运行准备只检查并返回计划；启动时重新检查动作、目标、模型连接与依赖授权。具有 run.execute 不自动取得桌面输入或真实屏幕采集权限。组合键使用 keyboard.chord 的单次完整生命周期；不允许通过多个请求累积按住状态。

## 资源与预设任务

资源 URI 使用 deskweave://，覆盖流程目录与固定版本、资产目录与内容、运行列表/快照/事件、观察会话/记录及系统能力。流程与子流程共用可复用流程目录，引用时固定版本。截图使用已创建帧的资源 URI，读取历史截图不重新采集目标。

URI 路径绑定目录的 pathBindings，查询字段限定为 queryParameters，并按目录 defaults 补齐；路径参数进行单次百分号解码和字段校验，不能借 URI 切换工作空间。运行事件必须携带当前 instanceEpoch；新连接先取得运行快照。读取结果为资源目录声明的 JSON，图像内容通过资源身份继续分块读取。

资源支持分页、MIME、长度、摘要及过期状态，读取每一块仍核对对象授权。resource.read 每次最多读取 192KiB，Base64 编码后保持在控制消息预算内；sha256 对应完整资源原始字节，客户端收齐后验证。临时内容读取完成或取消后，通过 deskweave_content_discard 的 resourceId 分支释放；uploadId 与 resourceId 互斥。较大图像通过多个块或资源链接交付。系统能力返回版本、平台、允许动作、有界容量及已协商的 desktop 能力摘要，不包含私有目录、凭据、原生句柄或授权票据；原生能力状态不授予 MCP 新权限。

预设任务包括创建流程草稿、诊断指定流程和制作模板。每个 Prompt 有明确参数和返回消息，读取 Prompt 不保存草稿、不调用模型、不启动输入或提取。arguments 定义发现结果，argumentSchema 限制实际参数；可选参数按 defaults 填充。参数作为普通文本插入消息，不递归解析其中的占位符，不把参数解释成方法、路径或授权。模型生成的定义必须经过 workflow.validate/save 与显式运行路径，结果内容不能改变授权。

| 预设任务 | 必填参数 | 可选参数 |
|---|---|---|
| deskweave_create_workflow | description | targetName |
| deskweave_debug_workflow | workflowId、issue | — |
| deskweave_extract_templates | targetName | elementTypes |

## 结果、事件与取消

新版普通结果包含 resultType=complete；旧版结果不附加该字段。成功工具结果的 structuredContent 是符合已发布 outputSchema 的 JSON 对象，同时在 content 中提供同一对象的 JSON 文本，便于仅处理 TextContent 的客户端读取。两种表示来自同一次 Engine 结果，不分别查询或重新计算；序列化后核对完整消息预算，不能通过截断字段满足大小限制。实际输出不符合契约时返回内部错误，不交付伪造的成功对象。

工具业务失败和工具 arguments 校验失败使用 isError=true 与可处理的错误内容，不将失败对象塞入只描述成功结果的 outputSchema。未知工具、无效协议参数及消息格式错误使用 JSON-RPC 错误。accepted、作业状态、运行状态、输入效果与后置验证分别返回。运行完成前返回运行身份和当前状态。

运行使用 event.subscribe/read/unsubscribe；观察和模板作业使用 stream.subscribe/read/unsubscribe。资源订阅转成相同对象的更新通知，按客户端过滤；断线后使用快照及游标恢复，不能把传输请求 ID 当成持久对象身份。

取消绑定原请求或操作。新版 HTTP 断开响应流进入取消处理；stdio 及旧版 HTTP 使用 notifications/cancelled。Bridge 按协议尝试映射到原 Engine 请求，执行既定取消路径并继续核对清理结果。已经提交并返回 operationId/runId 的长任务由其明确的取消或停止工具控制，不能把关闭查询流等同于停止整个运行。已终结、不能取消或状态未确认的操作仍从原对象查询；输入停止与释放沿用 Agent 规则。

两代适配都使用相同的业务去重键。一个逻辑操作若有多个在途协议等待者，取消只移除当前等待者；所有等待者都取消且本次操作仍由请求拥有时，再对原 Engine 操作提出取消。明确 operation.cancel/run.stop 仍直接作用于指定对象。迟到结果不发送到已取消的协议流，也不改变新的请求上下文。

MRTR 的 input_required、inputRequests 与 requestState 属于协议多轮交互，不表示业务成功。首期业务方法不请求客户端采样、Roots 或 elicitation，本地恢复确认仍返回 LOCAL_CONFIRMATION_REQUIRED 并走工作台；不把确认能力改放 requestState。新增此类交互时须按真实客户端能力实现 MRTR 并扩展契约。Tasks 不在首期协商集合，长任务继续使用 Engine 的 operationId 和 runId；未知扩展和未知 resultType 不当作 complete。

## 互通验收

Q28 覆盖主协议和兼容协议的 stdio/HTTP 组合、显式旧 HTTP+SSE、逐请求元数据、初始化分流、头体一致、发现分页、缓存、订阅确认与关闭、结构化结果、取消和请求恢复。Q49 检查集合查询与快照；Q59 对比两代适配到同一 Engine 的权限、业务参数、结果和副作用次数。Q13/Q29 在应用实现后验证真实桌面输入与释放。

## 本地管理读取与配置版本

bridge.read、bridge.token.list 和 bridge.token.impact 仅供通过认证的可信本地管理客户端，要求 workspace.manage；不进入 MCP tools/resources，也不接受网络令牌授予该能力。结构见[本地接入管理契约](../../contracts/bridge-management.schema.json)。CLI 的 mcp config read、mcp tokens list 使用同一边界。

bridge.read 返回 savedVersion/savedConfig、activeVersion/activeConfig、requiresRestart 和实际 state。savedVersion 初始为 0，savedConfig 为明确的禁用监听默认配置；停止时 activeVersion/activeConfig 为 null。监听期间 activeVersion 指向本次启动成功采用的配置；保存新配置不修改已经运行的监听，requiresRestart 为两者有差异。监听失败也不把保存成功显示成正在监听。

读取配置的 tls 只返回 enabled、certificateState、certificateSha256 和 notAfter，不返回文件路径、证书/私钥选择能力、私钥正文或秘密引用。bridge.configure 仅在输入消费本次新选择的 certificateGrant/privateKeyGrant，结果 config 同样为无能力引用的公开快照；随后原选择能力不能被再使用。Engine 把核验材料登记为此监听配置专用的受保护绑定，重启重新校验；读取后再次保存 TLS 配置须重新选择证书与私钥，不能从读取结果逆造能力。

bridge.token.list 使用 cursor、pageSize（1..100）和 states（空数组代表全部），返回 items、nextCursor、snapshotAt。每项为 tokenId、名称、权限、授权目标、createdAt、expiresAt、lastUsedAt、revokedAt 与 active/expired/revoked。state 在 snapshotAt 计算，revoked 优先于 expired；未使用的 lastUsedAt 与未撤销的 revokedAt 为 null。成功认证后合并更新 lastUsedAt，不记录请求正文。

分页成员按 createdAt、tokenId 倒序固定到五分钟查询快照，游标绑定工作空间、过滤条件、认证管理客户端与快照，不续期；到期返回 CURSOR_EXPIRED 后重新查询。分页是浏览快照，撤销操作仍针对当前 tokenId 重新检查实际状态；点击撤销前可用 bridge.token.impact 查看当前影响范围；预览不可用时仍可直接撤销。已撤销项重复撤销为 revoked=true、影响数按本次实际变化返回。列表和任何历史查询均不返回 token 明文、散列验证材料或凭据句柄。

令牌签发以至少 32 字节加密随机数构造不透明 Bearer 值；验证材料使用受保护的本地密钥计算 HMAC-SHA-256 并作恒定时间比较。明文只在原签发请求的首次受保护返回中展示，签发回执仅保存非秘密元数据；重查原请求不能重新得到明文，客户端丢失首次回复时撤销该 tokenId 后重新创建。重启保留验证材料与撤销状态，工作空间导出/恢复不复制秘密或复活失效令牌。

监听配置保存、启动结果、令牌到期和撤销分别更新列表状态。创建明文关闭后不再提供“重新显示”；复制令牌只允许在该一次性本地界面显式操作，离开后清理其内存引用。撤销后关闭订阅和直接输入能力，所属运行仍按用户所选 stopOwnedRuns 收尾并可查询。

## 普通继续与本地恢复材料

run.resume 的公开参数只有 runId 与 expectedControlVersion。MCP 目录保留完整内部 inputSchema 的来源引用，publishedInputSchema 为工具发现使用的独立投影，additionalProperties=false，既不发布 resumePlanId/confirmationGrant，也不携带引用它们的 dependentRequired 条件。投影只移除这两个本地字段及其依赖，保留普通继续的必填项和范围。

Bridge 在向 Engine 派发前检查 forbiddenParams。业务 JSON 出现 resumePlanId 或 confirmationGrant 即拒绝整个请求，不删除字段后继续运行。未知字段继续按发布 Schema 拒绝。可信本地 Host 的确认路径不通过 MCP 工具转发。

没有持有输入和指针恢复需求时，已授权客户端可正常调用上述两个公开参数，Engine 仍核对同一 Agent 的实时检查点与当前授权。需要恢复按键、按钮或指针时，返回 LOCAL_CONFIRMATION_REQUIRED 和所属运行的本地继续入口；用户在本地准备、审阅并确认，Host 直接提交绑定当前计划的恢复材料。外部客户端读取运行快照获知实际结果，不能借重试、资源读取或请求状态查询取得本地确认能力。


## 按令牌读取撤销影响

bridge.token.impact 是可信本地查询，不加入 MCP tools/resources，也不提供外部客户端自查其他令牌的入口。workspace.manage 允许读取该工作空间令牌的总计；relatedRuns 的名称、状态、身份和控制版本还要求 run.read 及对应运行的对象读取权。看不到的关联运行仅计入 hiddenRunCount，不因管理权限自动取得业务详情。CLI 继续提供令牌元数据清单，影响预览由本地撤销界面调用。

请求提供 tokenId、cursor（首次为 null）、pageSize（1..100）。结果 tokenImpactPage 包含 tokenId、snapshotId、snapshotAt、expiresAt、impactVersion、tokenState、counts、relatedRuns、hiddenRunCount、nextCursor。counts 分别统计 activeSessions、resourceSubscriptions、directInputPermits、activeRuns；activeSessions 只计可关闭的旧版协议会话，新版请求不增加该计数。resourceSubscriptions 计两代实际资源订阅，目录通知流另按 listen 容量限制；运行只计该令牌创建且尚未终结的根运行（含单次输入临时运行），不把子调用帧重复计数。relatedRuns 每项为 runId、displayName、state、controlVersion，按 runId 稳定排序；activeRuns 包含不可见成员，hiddenRunCount 表示其中不能读取详情的成员。

Engine 保存逐令牌影响台账。Bridge 的旧版会话、两代订阅和直接输入许可在对外开放前登记其归属，关闭或失联清理经确认后撤下；运行准入/终结同样更新归属。在同一事务固定计数、tokenState、可见运行成员和 impactVersion，成员分页采用最多 60 秒的快照，游标绑定工作空间、令牌、管理客户端和读取权限。后续权限收紧逐页再次过滤，不能借旧快照读出已撤权详情；无法维持原过滤时返回 CURSOR_EXPIRED 重新查询。原始成员与计数不随页面翻动变化，实际台账变化推进 impactVersion。

bridge.token.revoke 仍只需 tokenId、stopOwnedRuns，可选 impactSnapshotId 关联先前预览。撤销事务重新确认本地权限及当前令牌，先阻止新准入并使授权代次失效，再按当前台账关闭旧版会话/两代订阅/直接许可，取消尚在接收或等待准入的新版请求，按 stopOwnedRuns 请求停止当前所属运行。预览不能限制本次撤销只处理先前看见的对象；期间新增的对象同样处理。

返回既有 revoked、affectedSessions、stoppingRuns，并增加 affectedSubscriptions、revokedDirectInputPermits、impactVersion、previewState。previewState 为 notProvided（未传）、current（仍对应当前台账）、stale（期间变化）、expired（超期）或 unavailable（不存在、归属不符或已回收）；判断在失权前取当前台账版本，返回的 impactVersion 为该次撤销提交后的版本。上述预览状态均不阻止实际吊销，也不要求先刷新才能撤销。原令牌不属于当前工作空间或本地管理者无权操作仍拒绝；未知令牌返回 TOKEN_NOT_FOUND。重复原请求返回原收据，新请求重复撤销按本次实际失效/停止对象计数，必要时可再选择停止先前保留的所属运行。

受影响计数表示本次已使访问失效或发出收拢请求的对象数，不把返回收据等同于所有网络连接或输入已经释放。界面分别呈现立即失权、运行停止与输入清理状态；运行详情通过授权快照继续查询。预览和回执均不返回令牌明文、验证材料或本地确认能力。

## 内容与制作工具

公开目录增加内容暂存、流程版本、脱敏变量和资产编辑工具。contentPurposePermissions 规定每种内容用途的写权限；后续块、封存与释放继续核对拥有者和用途。新查询与编辑仍逐对象授权，MCP不能取得未脱敏变量、临时模型凭据或确认材料。connection.list/testDraft仅供可信本地管理，未发布为工具。内容引用与字节预算见[内容传输](content-transfer.md)。

## 请求身份与恢复

MCP 的 JSON-RPC ID 只关联当前协议请求。新版仅要求未完成请求不重号，旧版按已协商会话规则使用；两者都不能凭协议 ID 定位持久操作。可恢复调用使用 deskweave/requestId，位置为 tools/call 的 params._meta；该键不属于工具 arguments，也不表示身份或权限。新版同时满足 modernRequestMetadata，旧版复用公共 requestMetadata；结构见[MCP 契约](../../contracts/mcp.schema.json)。

```json
{
  "jsonrpc": "2.0",
  "id": 17,
  "method": "tools/call",
  "params": {
    "name": "deskweave_run_stop",
    "arguments": {"runId": "run-example", "expectedControlVersion": 7},
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {},
      "deskweave/requestId": "request-stop-example"
    }
  }
}
```

客户端在发送前固定 1～128 字符的 Engine 请求 ID；Bridge 按 requestMetadata 校验，并映射到本机信封 id。省略时由 Bridge 在派发前生成。工具结果的 _meta 原样返回该请求 ID，structuredContent 保持原服务方法结果；业务失败同样带回请求定位信息。尚未通过协议或参数验证的调用不表示 Engine 已接收。

重连后，以同一稳定客户端重新认证，再用 deskweave_request_status 的 requestId 查询原操作。该查询本身使用新的 JSON-RPC ID 和新的 Engine 请求 ID。已接收请求返回原对象；只有明确 notReceived 且原权限仍有效时，调用者才可选择使用原 Engine 请求 ID、原方法和原参数再次提交。查询失败或传输断开不能视为 notReceived。

未预先提供请求 ID 且首次回复丢失时，客户端通过已知 operationId/runId 或有权访问的对象目录定位操作；没有定位结果时不自动重新发送修改。新令牌或新客户端不能仅靠旧请求 ID 读取原收据。传输取消按当前连接的 JSON-RPC ID 找到原 Engine 请求，之后沿用服务取消和清理状态。

元数据中的 clientInfo 只描述客户端实现，不决定稳定认证身份；工作空间切换和本地确认授权不能通过元数据传入；其他 MCP 元数据只按各自协议用途处理。进度令牌、网络会话 ID 与请求恢复键互不替代。Bridge 不把结果重算为成功，不将原操作正文或秘密附加到响应元数据。
