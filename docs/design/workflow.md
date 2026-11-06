# 流程与文档设计

## 格式与对象

流程格式 1.0 的结构入口为 [workflow.schema.json](../../contracts/workflow.schema.json)，动作参数定义在 [actions.schema.json](../../contracts/actions.schema.json)。[动作目录](../../contracts/action-catalog.json)提供名称、分类、说明和桌面效果分类。

| 字段 | 含义 |
|---|---|
| formatVersion | 流程格式版本，首期为 1.0 |
| id、name | 工作空间内身份与显示名称 |
| entry | 明确的首节点 |
| parameters | 输入参数的类型、默认值、必填和敏感属性 |
| variables / runVariables | 调用局部变量与本次运行共享变量的初始值 |
| kind | workflow 或只在循环内使用的 loopBody |
| targets | 运行时需要绑定的目标槽 |
| resources | 流程依赖的图像、轨迹或子流程逻辑键，最多 10,000 项 |
| limits | 执行时长、节点执行数、调用深度、并行调用数和输入事件上限 |
| nodes | 带稳定身份、动作、参数、结果存储和出口的节点数组 |
| extensions | 带命名空间的非执行扩展数据 |

每次保存同时固定 resources 到具体内容版本的映射。运行快照包含完整依赖闭包、有效参数、预算和目标绑定策略。资源逻辑键不是任意文件路径。

## 节点与出口

节点由 id、title、action、params、on 组成，可用 store 将成功结果写入一个局部变量。变量名匹配标识符语法，不接受可执行路径。

普通动作成功后进入 on.next，失败后进入 on.error；省略出口表示结束当前流程或向上报告失败。logic.branch 使用 on.yes/on.no。flow.return 显式返回输出并结束本次调用。

图中每个静态出口必须存在，节点身份必须唯一，entry 必须可达。普通图不允许环；未连接节点可保留在草稿中，但运行校验要求作者处理不可达节点。布局顺序、节点位置和自动排列不参与执行。

## 动作集合

| 动作 | 行为 |
|---|---|
| time.wait | 等待指定主动时间 |
| data.assign | 计算值并写入局部或运行共享变量 |
| flow.loop | 按 while/until 条件执行有界子流程 |
| flow.break、flow.continue | 结束所属循环或进入下一轮 |
| keyboard.chord | 在一个输入段内完成组合按键 |
| logic.branch | 求布尔条件并选择出口 |
| logic.assert | 检查布尔条件，失败进入错误路径 |
| flow.call | 同步调用或启动后台子流程 |
| flow.join | 等待后台调用并取得输出 |
| flow.repeat | 按有限次数重复子流程 |
| flow.return | 返回明确的输出对象 |
| screen.capture | 采集目标并返回帧资源 |
| vision.match | 在目标中匹配图像资源 |
| vision.observe | 对明确区域执行已配置的模型观察 |
| pointer.move、pointer.delta | 物理位置移动、原始相对移动 |
| pointer.click、pointer.drag、pointer.scroll | 点击、拖拽、滚动 |
| pointer.button | 按下或释放鼠标按钮 |
| keyboard.down、keyboard.up | 按下或释放按键 |
| keyboard.text | 发送已确认文本 |
| input.trajectory | 执行固定轨迹资源 |

所有桌面动作必须包含目标槽；Agent 在发送输入前检查其实际绑定。vision.match 的匹配结果只有被后续明确使用时才产生输入。

动作目录的 effect 分为 none、capture、input、network 和 delegated。flow.call/join/repeat/loop 的权限需求取固定依赖闭包的并集，不能按调用节点本身归为只读。动作产出见目录的 outputs；mode=start 的调用结果为字符串句柄，mode=wait 与 join 返回子流程输出对象。输入产出只表示段收据和清理状态。

## 值与表达式

值可以是 JSON 常量、递归容器或 `{"$expr": ...}`。与保留键冲突的原始对象使用 `{"$literal": ...}` 包装。所有数值必须有限，普通整数限制在 JavaScript 安全整数范围；需要更大精度的数据使用声明为字符串的业务值。

表达式支持：

- path：访问 inputs、vars、run、prev、calls、runtime 下的 JSON 路径。
- add/subtract/multiply/divide、equal/notEqual、less/lessEqual/greater/greaterEqual。
- and/or/not、coalesce、length。

path 只查询 JSON 对象自有键和数组索引，不沿原型链查找，也不调用方法或宿主函数。读取的数据和动作输出不再次解释为表达式；只有定义中显式的表达式位置求值。缺失路径、除零、类型不匹配和超预算都产生明确的节点错误。and/or 短路求值，coalesce 只跳过 null 和缺失值，不跳过 0、false 或空串。

Value 数据的每个对象最多 1,000 个属性、每个数组最多 10,000 项。AST 最多 256 个运算节点、32 层嵌套；单值编码最多 1MiB，表达式输入、上下文与结果中的单个文本最多 256KiB UTF-8。编辑预览通过 expression.preview 调用同一 Engine 求值器；预览的消息预算见[本机协议](../interfaces/local-protocol.md#表达式编辑预览)。各入口使用共同的运算样例核对结果。

AST 预算按 Schema 声明的每个 Value 独立计算。Value 根深度为 0，进入数组成员、对象值或运算参数增加一层；动作 params、输入和返回值的映射容器、$expr 与 args 结构包装不额外占用深度。映射中的每个值分别计算 32 层深度和 256 个运算节点，预览、流程校验与保存使用同一规则。raw 字段及 $literal 内部按原始数据处理，遵守各自 Schema 和 JSON 深度、字节预算。

## 调用、重复与并发

flow.call 的 mode=wait 等待子流程并返回其输出；mode=start 返回仅属于本次运行的调用句柄。子流程只接收已声明输入，自有变量取子流程定义的初始值，不共享或隐式复制父变量。子流程的目标槽按同名、同类型从父调用的有效绑定继承，缺少必需目标时拒绝调用，不扩大授权范围。后台输出在成功 join 后写入该节点的 store。

flow.join 只接受本次运行的有效句柄。重复请求同一次 join 不重复提交 store；后续显式 join 可读取同一结果并写入本节点的 store，不重新启动子流程。等待超时请求取消对应子流程；父流程仍要完成收拢。

flow.repeat 在运行时把 count 解析为 0～10,000 的整数，按顺序调用固定子流程，提供从 0 开始的 iterationIndex。结果保存实际次数与最后一次输出，不默认保留所有中间大结果。

父调用正常退出时，在剩余预算内等待本层启动的子流程；失败或停止时取消并收拢。持有按键或鼠标按钮时不得等待子流程，以免相互阻塞。

## 编辑、保存与调试

文档分为有效结构、源码原文、布局和诊断。图编辑、步骤编辑、源码提交均转换为一个可撤销事务；运行事件和选择变化不进入业务撤销历史。

无效源码保留原文和最后有效结构，运行按钮要求修复。保存带预期草稿版本；新修改不被旧保存回执标成已保存。恢复历史版本生成新版本，不覆盖历史内容。

运行固定版本，调试断点绑定该版本。编辑产生下一次运行的草稿；“从节点运行”创建新运行，缺少所需前置变量、目标或调用句柄时拒绝。

静态校验至少检查结构、动作、类型、引用、出口、图环、资源类型与预算。运行时在表达式解析后再次检查实际参数。允许保留的扩展字段不能改变执行语义。

## 条件循环与控制信号

flow.loop 的 mode=while 在每次调用前检查 condition，首次为 false 时执行零次；mode=until 在一次调用结束后检查，首次至少执行一次。maxIterations 与 timeoutMs 始终有效，取值不能扩大父运行预算。达到上限仍未满足条件时报告 LOOP_LIMIT，不当作条件完成。

条件和 inputs 在所属调用中重新求值，可读取 runtime.loop.index、runtime.loop.lastOutput 和运行共享变量。首次 lastOutput 为 null；每轮结束后更新为该轮的显式输出。flow.repeat 保留固定次数行为，并使用相同的控制信号与预算处理。

含 flow.break 或 flow.continue 的定义标记 kind=loopBody，不能作为根运行启动。循环体中的同步调用可以传递最近一层循环的上下文，后台调用不能向另一个调用栈发出控制信号。普通流程定义中出现孤立循环信号时拒绝保存为有效版本。

break/continue 的 values 成为本轮 lastOutput；break 结束最近所属循环，continue 先完成本轮子调用与输入收拢，再检查条件或剩余次数。信号不越过异步边界，不绕过按键释放或子作业收尾。嵌套循环独立维护计数与最近所属身份。

## 运行共享状态

runVariables 的初始值在根运行启动时固定，所有同步及后台调用访问同一个运行内状态；新运行重新初始化，不与其他运行或工作空间共享。AST 的 run 根只读，修改只能通过 data.assign。

data.assign 的 scope 默认为 local；scope=run 访问运行共享状态。name 为根变量，path 为可选的对象键或数组索引，不调用属性访问器，不沿原型链创建对象。父路径不存在、数组越界或类型不兼容时返回明确错误，不隐式扩张数组。

每个节点开始时取得一致的变量快照及 runtime.runVariablesVersion。共享写入携带 expectedVersion，省略时使用本节点读取的版本；提交以整个共享状态版本比较。并发冲突返回 VARIABLE_CONFLICT 和当前版本，保留双方已提交内容。纯数据节点可通过显式错误路径重新读取与计算，不能连带重放已经发送的输入。

局部变量仍属于当前调用。变量查看器分别显示调用局部、运行共享和上一步输出；运行中的查看为只读，编辑定义影响下一次运行。

## 指定节点启动

run.prepare 接收固定版本、startNode、initialVariables、可选 runVariables、目标和预算，返回前置要求、诊断、有效期及 planId。仅检查从所选节点可达的执行路径，仍验证资源闭包与已声明参数；缺失的局部值必须由操作者填写。不得注入另一运行的后台调用句柄、定位令牌或输入收据。

run.start 带 startNode 时必须同时提供 planId 与 initialVariables。计划绑定流程摘要、依赖、参数、初始状态、授权目标代次、接管策略与预算，默认 60 秒有效；任何一项变化都重新检查。服务端启动时再次验证，不因前端显示“检查通过”而跳过验证。运行快照记录所选入口和实际初始值；它是一次新运行，不沿用旧运行的外部效果。
