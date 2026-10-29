# 运行变量与版本检查

## 运行变量快照

`debug.variables.read` 读取 `runId`、`frameId`、`scope`、`snapshotId`、`cursor` 和 `pageSize`。scope 为 inputs、local、shared、previous 或 outputs；shared 的 frameId 必须为空，其余范围必须指定本次运行的真实调用帧。结构见[检查契约](../../contracts/inspection.schema.json)。

首次调用的 snapshotId、cursor 均为 null。Engine 在一个一致读取点固定运行共享状态和各调用帧的不可变值根、敏感标签、dataVersion 和 controlVersion，生成最长 60 秒的只读快照。同一 snapshotId 可切换已授权帧与范围；游标同时绑定快照、范围、帧、排序和实际读取者，不能跨范围续页。排序按变量名的 UTF-16 代码单元序，分页不续期。每工作空间最多 32 个活动检查快照；超过存储或资源预算返回 LIMIT_EXCEEDED。

dataVersion 在输入、局部值、共享值、上一步或输出发生可见变化时递增；controlVersion 仍只用于运行控制，两者不能互相代替。快照过期返回 INSPECTION_SNAPSHOT_EXPIRED，重新读取可保留所选帧与范围，但不得把旧游标与新值混用。运行结束后可读取已实际封存的值；没有封存的值返回 unavailable/notCaptured，不从日志推测。

每页默认 50 项，最多 100 项，受完整控制帧字节预算约束。条目包含 name、valueType 和 visibility。visible 以 value 或 valueResource 二选一返回；单值超过 4KiB 时使用固定资源，仍绑定原快照和权限。redacted 不返回 value/valueResource；unavailable 附具体原因。不能把遮蔽值变成业务数据中的空串、null 或占位文字。

`run.snapshot` 返回运行控制投影、frameCount/effectCount 与同一 dataVersion 的 framesQuery/effectsQuery；调用帧和输入效果通过 query.read 分页。变量面板通过上述检查接口的 outputs 等范围读取脱敏值；重连先重取快照，不依赖从启动开始积累事件。`runtime.variables.changed` 事件只携带运行、帧、范围和两种版本，不携带变量值；其负载由 inspection Schema 严格限制。

## 敏感值投影

parameters.sensitive 标记加载后的运行输入，默认值同样适用。执行器保存值与敏感标签，标签由 Engine 计算，客户端不能通过请求字段清除。

表达式结果继承实际读取值的标签；局部赋值、运行共享写入、上一步结果、子调用输入及返回值继续传播。敏感条件控制的分支内写入同时继承该条件标签；嵌套调用不得丢掉控制依赖。部分修改仍保留未替换部分的标签；在不依赖敏感值或敏感条件的完整覆盖后，才按新来源重算。

检查、运行快照、事件、MCP、日志和支持材料使用同一脱敏投影。顶层变量只要包含敏感子值，就遮蔽整个变量；真实执行仍使用原值，不影响桌面输入或业务计算。流程定义和显式编写的源码保持原有内容含义；敏感标记不是存储加密或模型凭据接口。模型密钥仍仅由专用秘密存储管理。

`debug.variables.read` 要求 run.read 和对应运行、帧的读取权。MCP 只能得到相同的脱敏投影。变量资源不携带租约、目标能力、确认材料或秘密引用；授权收紧立即使旧检查句柄失效。

## 流程版本发现

`workflow.revisions` 按流程返回 headRevision 和分页的 revision、parentRevision、名称、创建时间与 definitionHash。首次查询固定五分钟的版本元数据成员，按创建时间倒序、revision 作为稳定次序键；后续保存不插入旧分页。每页重新核对读取权，快照到期返回 CURSOR_EXPIRED。

选择两个版本后分别调用 workflow.read，必要时分块取得模型，再按节点与参数比较。恢复以所选版本建立新草稿，保留其来源版本；保存仍与当前头做 CAS，不能把历史版本直接写成当前头或覆盖期间的新版本。

## 模型连接发现

`connection.list` 为可信本地管理读取，要求 workspace.manage，不进入 MCP。返回有期限的分页快照，条目含稳定身份、显示名称、当前版本、端点、模型、凭据是否可用及更新时间；不返回密钥、凭据引用或一次性能力。

connection.configure 可保存显示名称；旧连接在首次需要名称时采用已保存模型与端点主机形成的名称，不把内部身份包装成显示编号。connection.read 可读取既有不可变版本。列表、读取和翻页不发起网络测试。

## 集合查询

运行调用帧按状态、父调用与节点筛选；输入效果按状态筛选。两个来源入口固定到同一运行快照，翻页保持 sourceVersion、排序和授权。只读查询不改变 controlVersion。参数、页预算、到期和主动关闭见[分页查询](../interfaces/pagination.md)。

## 验收

Q41 检查帧与范围、跨页版本、重连、到期、权限收紧、大值、敏感数据和控制依赖。Q42 检查历史成员稳定性、版本差异、恢复后保存冲突，以及连接发现的分页、空结果和秘密排除。
