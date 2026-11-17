# 纯逻辑执行内核

## 内部接口

`deskweave.engine.scheduler.Runtime` 消费已登记的不可变运行计划。工作台、CLI 与 MCP 的共享应用服务通过以下接口接入执行核心：

| 接口 | 行为 |
|---|---|
| `Runtime(workspace, clock=..., threaded=True, max_runs=17, ports=LOGIC_PORTS)` | 创建单线程调度器；可注入单调时钟和动作端口 |
| `attach(run_id, access)` | 核对计划归属、授权及完整依赖能力，建立执行帧并持久化初始状态；同一活动运行重复挂接返回当前状态 |
| `control(run_id, command, expected_control_version, access=...)` | 排队处理 pause、resume 或 stop，返回 Future；按预期控制版本提交 |
| `read_state(run_id, access=...)` | 读取已提交的运行状态、版本、步骤、主动时间和错误 |
| `read_value(run_id, scope, name, access=..., frame_id=None, version=None)` | 读取指定顶层值的脱敏投影；version 固定数据版本，省略时读取当前版本 |
| `result(run_id, access=...)` | 按同一数据版本读取状态与根调用输出 |
| `retry(run_id, access=...)` | 存储恢复后重新调度原来的未提交步骤 |
| `close()` | 结束活动调用、完成持久化收尾并关闭调度线程 |

按操作类别执行不同的权限、归属与流程范围校验：

- **挂接（`attach`）**：通过 `RunAccess.check_snapshot` 校验当前授权有效性、原运行计划记录的归属客户端与原授权引用及代次匹配、计划所需的全部权限（含 `run.execute`、`workflow.read` 及相关资产或动作端口权限）、流程访问范围覆盖全部依赖流程及目标授权校验；同一活动运行重复挂接亦按原计划校验上述条件。
- **重试（`retry`）**：通过 `ExecutionStore.check_access` 要求 `run.execute` 与当前授权有效性，并核对工作空间中该运行的创建者客户端、原授权引用与代次匹配，以及流程访问范围覆盖运行关联的全部依赖流程。
- **读取（`read_state`、`read_value`、`result` 及 `run.snapshot`、`debug.variables.read`、`event.subscribe`、`event.read`、`runFrames` / `runEffects` 查询）**：通过 `ExecutionStore.check_access` 要求 `run.read` 与当前授权有效性，并核对流程访问范围覆盖运行关联的全部依赖流程；满足上述条件时允许其他已授权客户端读取运行数据，不再要求与运行创建者身份、原授权引用或代次匹配。会话派生引用仍按会话隔离：`workspace.catalog(kind="run")` 的分页快照/游标、`debug.variables.read` 的 `snapshotId` / 游标及返回的 `resourceId`、`run.snapshot` 返回的 `runFrames` / `runEffects` 查询集合，以及 `event.subscribe` 的 `subscriptionId` 均归创建会话所有；其他客户端须通过自己的会话重新创建查询/检查快照、集合或订阅并取得相应资源引用，不能复用其他会话的标识。`event.read` 直接按运行 ID 校验 `run.read`，不依赖订阅标识。`workspace.catalog(kind="run")` 同样要求 `run.read` 并按 `workflowIds` 过滤；按 `requestId` 查询运行回执 `request.status` 时，还要求原请求客户端身份、对应原请求权限（`run.start` 对应 `run.execute`，控制请求对应 `run.control`）、当前授权有效、流程访问范围覆盖运行关联的全部依赖流程，并与回执中的授权引用及代次匹配。调用帧必须属于指定运行（`read_value` 经 `check_frame` 校验）；内部完整计划与结果存储由执行核心使用，面向调用方的值统一经过脱敏投影。
- **控制（`control` 及公共 `run.pause`、`run.resume`、`run.stop`）**：公共方法与调度控制路径通过 `ExecutionStore.check_access`（及事务检查点）要求 `run.control` 与当前授权有效性，并核对控制发起方的流程访问范围覆盖运行关联的全部依赖流程；满足上述条件时允许其他具备 `run.control` 的客户端发起暂停、继续或停止，不再要求控制发起方与运行创建者身份、原授权引用或代次匹配（携带 `requestId` 的控制回执写入与重放仍按控制发起方的客户端身份、`run.control` 及原授权引用与代次校验）。执行 `resume` 时，除校验控制发起方外，`Runtime` 与 `RunManager` 还会重新校验原运行拥有者的计划授权上下文（`check_snapshot`）仍然有效。

`threaded=False` 配合 `tick()` 或有步数上限的 `pump()` 用于可控时钟测试。`max_runs` 是内核容器容量；公共运行服务由 `RunManager`、`RunAPI`、`RunQueries`、`RunEvents` 与 `Inspection` 负责根运行名额、排队、查询投影、事件分发、变量脱敏资源化与客户端生命周期，接口范围见[Engine 开发](engine.md)与[实施路线图](../../ROADMAP.md)。

## 计划、节点与调用帧

每个固定流程版本加载、校验和编译一次，按 `(workflowId, revision)` 共享定义和节点字典。单次挂接的定义与依赖内容合计上限为 64MiB。节点定位为 O(1)；分支汇合点在编译阶段依据控制流的后支配关系建立，时间与空间为 O((V+E) log V)。

显式调用帧保存当前节点、父调用、局部变量、上一步结果、返回位置、循环上下文、控制标签和预算。Python 调用栈不承载流程调用深度。同步调用等待子调用；后台调用返回当前运行内的句柄。子流程应用自身声明的默认值与输入类型，初始化自身局部变量，同名且同类型的目标继承父调用的有效绑定。所有调用共享根运行的运行变量。

默认端口执行 time.wait、data.assign、logic.branch、logic.assert、flow.return、flow.call、flow.join、flow.repeat、flow.loop、flow.break 和 flow.continue。`ActionPorts` 将每个动作绑定到实际端口的 execute 方法，重复注册被拒绝。准备服务与 Runtime 使用同一端口集合检查完整计划。端口通过内核的完成、失败、等待及调用转换更新本步 Change；调度器和存储层统一提交。

## 调度与时间

调度器使用 FIFO 就绪队列，每次推进一个调用帧的一步。等待计时使用最小堆，更新为 O(log A)；完成通知通过反向等待索引唤醒相关帧。过期堆项按活动计时器数量压缩。空闲等待释放调度条件锁和工作空间存储锁。

主动时间取单调时钟减去暂停时间。暂停冻结整个调用树的等待、join 和循环计时，默认保留 30 分钟；到期结束运行。控制队列最多 64 项，停止优先于暂停和继续，提交时比较 controlVersion；已停止的运行不能被继续请求复活。

根预算约束整个调用树；每个子调用同时受祖先与自身的主动时间、步骤、调用深度和后台并发上限约束。表达式检查点及事务提交前检查取消、授权和时间预算。预算耗尽后结束业务推进并收拢子任务；等待完成和结果收拢不重复消耗节点步骤。

## 变量、控制依赖与结果

共享表达式求值器接收已校验表达式与不可变值根。编辑预览入口另行执行完整输入校验。不可变值记录编码大小、文本大小、嵌套深度与结构上限，短路分支通过这些元数据判断路径结果的校验风险。变量更新仅复制修改路径上的容器并更新沿途元数据；本步固定读取根，运行共享赋值比较整个共享状态版本，不匹配返回 VARIABLE_CONFLICT 与当前版本。标签变化推进 dataVersion，共享业务值赋值推进 sharedVersion。

敏感标签独立于业务 JSON，使用带父索引的平面节点表编码，存储深度不随业务值嵌套增长。实际读取、短路路径、条件控制、表达式错误、子调用和返回值传播标签；未执行的短路分支不增加标签。编译时以 O(V+E) 生成本地与共享写入摘要。敏感控制区间跳过无写入子图，按节点索引处理可能写入的目标，对旧值和缺失路径保留选择标签，包含被跳过子调用的共享写入；仅处理对应目标，不扫描全部变量。单次首次区间分析为 O(Vr+Er+W)，Vr/Er 是该区间及其共享写入依赖的节点与边，W 是写入路径集合。已覆盖区域的局部标签复用；共享写入清除旧标签时推进标签代次，并重新处理相关共享目标。分支控制标签在汇合点退出，非敏感来源的完整覆盖重新计算标签；局部修改保留其余部分标签。顶层值包含任意敏感子值时整体遮蔽，错误投影保留错误代码，事件只携带节点状态和版本信息。

`runtime.loop.index` 与 `runtime.loop.lastOutput` 提供当前迭代序号和前一轮输出。repeat 的次数求值一次，每轮输入重新求值；while 在轮前判断，until 在轮后判断。break/continue 作用于最近的所属循环，可经同步调用传播；后台调用建立独立控制边界。`calls.<nodeId>` 按本步数据版本从索引读取该调用节点最近完成的结果；父帧不缓存完整历史返回值。读取整个 calls 集合时按名称分页取值，并逐项执行表达式的字段数与字节上限。显式 store 和 prev 依各自语义保留当前值。

同步调用或 join 的失败经过父流程 on.error 成功处理后，允许整体成功。未处理的后台失败使所属调用失败。父调用退出前等待全部子任务收拢；join 超时先取消并收拢对应调用，再进入错误路径。重复 join 读取同一持久化结果；跨运行句柄、自等待及循环等待被拒绝。

## 格式 5/6 与原子提交

格式 5 在单次事务中建立以下执行表，保持格式 4 的运行计划、依赖强引用与原请求回执，随后在格式 6 中补齐运行视图与启动恢复索引：

| 表 | 用途 |
|---|---|
| execution_run | 提交版本、共享版本、累计步骤、主动时间、计时状态、后台数量和运行错误 |
| execution_frame | 当前执行阶段、循环、控制依赖、目标绑定、预算、控制信号和调用错误 |
| execution_value | 按运行、帧、范围、变量名和数据版本保存值与独立标签对象，valid_until 表示版本有效区间 |
| execution_unhandled | 尚未被显式错误路径处理的子调用结果 |

`frame.state_hash` 保存调用创建时的输入与局部初值；后续状态由 execution_frame 和 execution_value 的增量记录表示。循环前一轮输出和继承的循环值使用内部 runtime 范围持久化。每步只发布变化值对象，在短事务中原子更新调用状态、相关值版本、运行状态和事件序号；提交成功后再发布内存状态。失败回滚数据库并保留原内存状态，重试继续同一未提交步骤。

完成调用从活动内存移除，历史结果按运行与帧索引读取；历史事件在存储中分页消费。格式 6 增加 `run_catalog_version`、`run_frame_version`、`run_metrics` 及 `execution_inspection_names`、`run_state_sequence`、`frame_unfinished` 索引，工作空间打开时把残留未完成运行统一标记为 `interrupted` 并取消未完成帧。调度线程遇到存储错误隔离受影响运行，其余运行继续调度。格式升级失败回滚全部结构和版本号，重新打开后可再次升级。

## 验证入口

`uv run --frozen python -m unittest tests.engine.test_runtime tests.engine.test_runs tests.engine.test_run_service -q` 覆盖动作、显式错误恢复、调用收拢、控制竞争、敏感标签、预算、版本一致性、公共运行服务与升级回滚。三平台 Engine 工作流对源码及安装后的 wheel 运行相同契约测试。
