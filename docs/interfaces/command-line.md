# 命令入口设计

## 入口与参数来源

命令程序名为 deskweave，复用本机 Engine 服务，不解释另一种工作流。命令与现有方法的机器映射见[CLI 目录](../../contracts/cli-catalog.json)，参数与结果结构由其引用的 service Schema 定义。

以下为应用组件的实现契约；当前应用实现阶段与交付范围见[路线图](../../ROADMAP.md)。

```text
deskweave <组> <命令> --workspace <定位信息> [--params-file <文件>|--params-stdin]
          [--target <槽=本地目标别名>] [--source <本地来源>]
          [--output <本地输出位置>] [--format human|json|jsonl]
          [--wait] [--watch] [--request-access]
```

workspace 只定位已经授权的工作空间，不因传入一个路径就获得目录访问。客户端先按[组件生命周期](../architecture/lifecycle.md)发现和认证 Engine；缺少能力时，交互命令可通过 --request-access 选项打开可信本地审批流程，无交互环境直接返回权限错误。

CLI 只接受目录中的具名命令，不提供 method、shell 或脚本透传入口。目标选择、目录及文件批准通过 Host 原生入口完成；命令行字符串不能代替本地确认。

业务参数来自一个 UTF-8 JSON 对象。拒绝重复键、非有限数、尾随内容和超出本机协议容量的输入；文件和标准输入不能同时指定。参数文件只由 CLI 读取，不把其路径交给 Engine 执行。

## 命令分组

| 命令组 | 入口 | 既有服务 |
|---|---|---|
| 工作空间 | workspace open、backup、restore | workspace.open/backup/restore |
| 流程 | workflow list、read、validate、save、export | workspace.catalog、workflow.* |
| 运行 | run list、prepare、start、status、pause、resume、stop | workspace.catalog、run.* |
| 目标与采集 | target select、capture create | target.select、capture.create |
| 视觉 | vision match、observe、ocr status | vision.match/observe、ocr.status |
| 模板生产 | template extract、candidates、names、commit、export、discard | template.* |
| 观察 | observation start、stop、status、query、read、export | observation.* |
| 资产 | asset list、read、import、references | workspace.catalog、asset.* |
| 内容传输 | content begin、write、seal、discard | content.* |
| 状态与资源 | operation status/cancel、request status、resource read、capabilities | operation.*、request.status、resource.read、system.capabilities |
| 本地控制 | emergency stop、mcp status/start/stop/serve | emergency.stop、bridge.* |

内容传输沿用[内容引用契约](content-transfer.md)。`content discard` 通过参数对象中互斥的 uploadId 或 resourceId 释放临时内容；读取完成或取消时释放对应 resourceId。

list 的 kind 由具名命令固定，调用者不能把 workflow list 改成读取另一类对象。目录权限表列出基本能力，具体流程动作、资源来源、目标、模型与输出位置继续执行对象检查。localPermissions 中的 workspace.manage 只授予可信本地管理客户端，不属于 MCP tokenGrant 的 14 项权限，也不能通过网络令牌取得。

模型凭据、TLS、监听来源、客户端令牌和急停解除使用可信本地设置或确认入口。CLI 不提供修改这些内容的快捷参数。mcp start 只启动已经批准的配置，不能用启动命令自动修改监听策略。

## 本地能力绑定

目录中的 inputSchema 描述最终 Engine 参数。CLI 读取业务参数后先拒绝 forbiddenParams 指定字段，再以 defaults 补齐省略值，核对 fixedParams，合并通过 localBindings 取得的本地能力，最后按完整 Schema 校验；固定参数冲突、额外字段或缺失字段均拒绝。

| 参数来源 | 处理规则 |
|---|---|
| targetToken、targets | target 选项引用当前批准范围中的目标别名，Host 解析为令牌；同名歧义拒绝，不扫描任意窗口标题 |
| directoryGrant、selectionGrant | 通过本次原生选择和受保护通道注入 |
| sourceGrant、outputGrant、destinationGrant | 分别检查来源文件、输出位置和新目录，不互换用途 |
| 原生确认与连接材料 | 不从 JSON、argv、环境变量或普通配置文件接收；run resume 禁入 resumePlanId 和 confirmationGrant |
| 流程/版本、资源、运行、作业身份 | 可作为技术参数提交，每次仍核对所属对象和权限 |

调用者不能在参数文件中注入上述能力字段覆盖本地绑定。传入 source 或 output 路径只是向本地授权层提出对象定位请求；已有批准不匹配时重新审批，无交互环境返回拒绝。target select 兑换的结果由 Host/Engine 的已授权目标上下文持有，后续命令只引用其中的目标别名；该上下文不通过普通文件或 stdout 传递。目标令牌到期、Agent epoch 改变或系统目标失效时，不以显示名重新取得权限。

run prepare 与 run start 使用同一份固定业务输入和目标绑定。指定节点的 planId 与 initialVariables 遵循既有契约；参数、目标或预算改变后重新准备。CLI 不从另一个运行的现场复制调用句柄。

run resume 的业务 JSON 只接受 runId 与 expectedControlVersion。目录 forbiddenParams 明确禁止 resumePlanId、confirmationGrant，文件、标准输入、参数开关或环境值均不能注入；发现禁入字段直接拒绝，不剥离字段后继续，不通过 localBindings 代替本地确认。没有持有输入和指针恢复需求时保留普通继续能力，仍由 Engine 重新核对实时检查点和全部授权。

需要恢复按键、按钮或指针时，返回 LOCAL_CONFIRMATION_REQUIRED 及运行的可信本地继续入口。交互用户进入该入口准备、审阅和确认，Host 直接完成计划提交；无交互 CLI 保留暂停并返回具体处理动作。--request-access 只打开本地入口，不能代替确认或在其后自动重放带确认材料的命令。CLI 随后使用 run status/watch 观察实际状态。


## 请求、等待与幂等

每条修改命令在发送前固定经认证客户端身份、请求 ID、包含方法及规范参数的请求摘要和对应工作空间。必要的原请求方法与定位材料保存在当前用户受保护的客户端记录中，不包含目标令牌、密钥、截图或业务参数正文。

输出丢失或连接中断后先使用同一身份调用 request.status；已有 operationId 或 runId 时查询原对象。已被接收的输入、保存和模板提交不因 CLI 重启而自动重新发送。稳定身份不能恢复时，使用新认证和本地对象授权查询，不能假称找回原请求。

| completion | 默认命令结束条件 | wait 的处理 |
|---|---|---|
| result | 返回方法结果 | 对 run pause/resume/stop、observation stop、operation cancel 可继续查询到所请求状态与清理结果 |
| operation | 得到 accepted 与 operationId | operation.get 查询原作业终态；成功结果按作业结果目录验证 |
| run | 得到 accepted 与 runId | run.snapshot 及事件查询等待原运行终态 |
| listener | 取得 bridge 当前状态 | bridge.status 确认已监听或已停止 |
| stdio | 进入专用 MCP 协议会话 | 随协议会话生命周期结束，不打印 CLI 结果对象 |

foregroundPolicy 对客户端连接寿命作进一步约束：run.start 和 observation.start 的 detached 默认 false，此时前台命令保持连接直到对象结束或用户中断，不能返回回执后立即让所属客户端消失；detached=true 才能得到接收结果后退出，wait 可以继续观察已交由 Engine 的对象。input submit 默认保持连接直到单次输入作业及清理结束。

普通已接收的模板、导入、导出和计算作业由 Engine 持有到终态，取得回执后正常退出 CLI 不自动取消它们；主动中断等待仍使用下节的取消路径。目录 waitMethod 与 completionSchema 指定等待时的查询及最终结构，等待不增加服务方法。wait 或 foregroundPolicy 要求保持前台连接时，发送创建或控制命令前一并核对 waitPermissions；run.snapshot 需要 run.read，observation.session 需要 observation.read，监听查询使用本地 workspace.manage，作业查询继续核对原方法与所有者。缺少等待权限时先拒绝，不启动后再尝试扩大权限。

无 wait 且不要求前台持有时，退出码 0 只表示表中对应结束条件成立。创建运行、作业或观察的接收回执不表示任务已经完成。持续观察启动后独立产生记录；其结束、记录持久化与退出码不混为一项结果。

run status、observation status、operation status 可使用 watch。单次快照来自对应既有服务；连续模式使用已有订阅与补拉规则，jsonl 只允许连续查询，每行输出一个完整状态或事件，断线后先恢复快照再继续。watch 不创建新的业务执行。

## 标准输出与错误

human 输出使用流程、节点、目标、状态、原因和可执行入口。目录 outputSchema 校验收到的服务结果；设有 publishedOutputSchema 的命令先按 localResultBindings 把授权材料保留在本地上下文，再输出声明的投影。target select 的 JSON 返回 displayName、kind、expiresAt 及桌面目标的 topology，targetToken 不进入标准输出。

其余命令的 json 输出使用服务结果结构。前台等待或 wait 结束后按 completionSchema 或实际查询方法的结果结构输出；连续状态只在明确选择 jsonl 时输出多条对象。request status 根据本地原请求记录中的方法应用相同发布投影，不能借查询回执输出已经隐藏的能力。原方法无法恢复时不发布未分类结果，保留请求身份并转交可信本地恢复入口。

标准输出只承载所选结果；日志、连接过程、进度和字段错误写入标准错误。结果包含任务所需的技术身份，供后续状态命令引用；不输出连接票据、能力令牌、已保存密钥、完整请求头或默认展开的敏感输入。业务数据只有在显式读取或导出对应对象时输出。

参数和错误名称保持公共契约含义。workflow validate 的 valid=false 是校验不通过，不能把成功返回一个诊断对象当作流程有效。输入效果 uncertain、清理失败和保存失败分别保留实际状态。

## 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 命令对应的结果、接收或等待条件成功 |
| 2 | 命令语法、JSON 或字段结构错误 |
| 3 | 流程语义、前置检查或准备结果无效 |
| 4 | 认证、对象、目标、文件或能力期限不满足 |
| 5 | 连接、启动、协议兼容或读取预算失败，尚无已接收修改的未决效果 |
| 6 | 作业、运行或清理明确失败 |
| 7 | 等待对象明确取消或中断，且本次并非用户按 Ctrl+C |
| 8 | 已接收修改的结果无法核定，或退出预算内不能确认输入清理 |
| 130 | 用户中断当前命令；所属对象处置见下节 |

未决效果或清理不能确认时优先返回 8；已确认失败返回 6，不能因为用户同时中断就改成正常取消。只有原请求与清理都可核实时，用户中断返回 130。

MCP stdio 模式的业务错误通过 MCP 结果返回，不把某个工具调用失败变成整个服务进程退出。该进程退出码只说明协议会话或服务入口结束的原因。

## Ctrl+C、断线和退出

第一次 Ctrl+C 停止新的本地读取或发送。修改尚未发送时直接退出 130；已发送但未取得回执时，先查原请求。未决修改无法核定则输出可恢复的请求身份并退出 8，不创建新请求重做。

对于本客户端拥有且 detached=false 的运行，发送 run.stop；对于 detached=false 的观察会话，发送 observation.stop；当前作业使用 operation.cancel。控制版本冲突时先读取当前快照，再决定是否仍需同一停止动作。随后在有限关闭预算内等待清理与状态回执。

run.start 或 observation.start 明确 detached=true 时，Ctrl+C 只结束本次等待、订阅与客户端连接，不停止已交由 Engine 的对象。只读 watch 不是对象所有权；退出查看不会停止另一客户端的任务。

第二次 Ctrl+C 或关闭预算耗尽后可以结束 CLI，但不能把未确认清理写成已停止；保留恢复所需身份并返回 8。普通连接丢失不替代 emergency stop，需要立即停止输入时使用已有受信原生急停入口。

CLI 崩溃时由 Engine 按认证连接所属关系执行相同退出策略。恢复连接先重新授权，再读取 request.status、run.snapshot、observation.session 或 operation.get；不自动重发桌面输入。

## 秘密输入与专用 MCP 模式

模型密钥、Bearer 令牌、连接票据和原生确认能力不接受命令行参数。模型连接通过本地设置完成；标准输入中的业务敏感参数只在本次请求内使用，不进入普通客户端记录和诊断。

mcp serve 使用本地已批准的 stdio 配置与一次性连接材料，标准输出从进程启动起只承载 MCP 消息。版本、启动错误和日志进入标准错误；该模式不接受 params-file、format、watch 或 wait。

已有网络监听与请求的 stdio 配置不匹配时拒绝，不先停止或改写另一个监听实例。stdio 连接结束只关闭所属协议会话与引用，不停止其他网络客户端；对象继续或停止依其 detached 与原授权判断。

## 验证

实现验收使用合成工作空间和独立目标，覆盖命令目录发现、参数装配、所有权限拒绝、JSON 输出、wait/连续查询、Ctrl+C、启动竞争、原请求查询和 stdio 输出纯净性。运行结束还需验证真实输入释放，不能只检查 CLI 退出码。

相关验收沿用 Q01/Q09/Q10/Q13/Q19/Q23/Q27/Q28，具体故障与恢复顺序见[组件生命周期](../architecture/lifecycle.md)。

## 集合分页查询

query read 使用 collectionId、kind、filter、order、cursor、pageSize 和 includeTotal，默认每页 50 项、最多 100 项。query close 使用 snapshotId 释放本客户端查询快照，或使用 collectionId 释放诊断集合及其全部查询；两个参数互斥。目录、候选、观察、引用、版本与变量查询逐页返回，命令不会自动下载后续页面。完整参数与状态见[分页查询](pagination.md)。
