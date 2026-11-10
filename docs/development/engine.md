# Engine 服务库

Python 包位于 `python/deskweave`。`Engine` 统一分发方法、权限校验和结果校验；工作空间存储、表达式求值和流程检查由独立模块提供。

## 安装与运行

使用 Python 3.12 和 uv 0.8.2，在仓库根目录运行：

```bash
uv sync --frozen
uv run --frozen python examples/engine/workspace.py
uv run --frozen python examples/engine/content_transfer.py
uv run --frozen python examples/engine/connection.py
uv run --frozen python examples/engine/native_connection.py
uv run --frozen python examples/engine/process.py
uv run --frozen python examples/engine/coordination.py
```

示例均使用临时工作空间。`workspace.py` 完成流程保存、读取、校验和表达式预览；`content_transfer.py` 上传超过 1MiB 的合成模型，通过引用保存，再分块读取并校验完整模型。`connection.py` 使用合成对端身份，完成字节帧握手、流程保存和重连后的原请求查询。`native_connection.py` 使用实际本机端点，完成认证、保存和重连查询。`process.py` 在独立 Engine 进程中完成相同业务链路，接口见[独立进程](engine-process.md)。`coordination.py` 打开同一工作空间的两个宿主会话，关闭发起会话后继续查询，并在空闲后回收共享进程，接口见[工作空间启动协调](workspace-coordination.md)。原生接口、安装策略及登录会话要求见[原生本机通道](../interfaces/native-transport.md)。示例退出时关闭资源并删除临时数据。

```bash
uv run --frozen ruff check python tests examples/engine
uv run --frozen ruff format --check python tests examples/engine
uv run --frozen python -m unittest discover -s tests -q
uv build
```

构建输出位于 `dist/`，wheel 包含运行模块和公共契约；源码包包含测试与契约源文件。

## 测试时钟

服务功能测试通过 `EngineCase.make_engine()` 注入可控的墙上时间与单调时钟，重开工作空间沿用同一时钟来源。容量、分页、内容和持久化用例按数据与状态断言；超时用例显式推进单调时钟，检查截止前、截止点、回滚和恢复。并发用例通过锁竞争事件确认第二个请求已经等待，再推进第一个请求的预算。查询响应阶段的回归覆盖首屏与续页、结果校验与完整响应编码，并核对快照及游标回滚、配额恢复与原游标重试。

运行耗时按[性能预算](../quality/verification.md#性能预算)使用真实单调时钟单独测量，记录平台、数据量及耗时分布。

## 方法范围

| 方法 | 当前行为 |
|---|---|
| expression.preview | 复合值、全部已定义运算、短路、路径、类型和容量校验；限可信本地编辑会话 |
| workflow.save | 从内嵌模型或 modelRef 保存流程；版本比较、不可变内容和重复请求去重 |
| workflow.read | 读取指定版本；支持 auto、inline 和 resource，协商后大于 256KiB 的模型默认资源化 |
| workflow.validate | 校验格式、节点、出口、入口、可达性、图环、目标槽、资源类型和参数默认值 |
| workflow.revisions | 固定快照中的历史版本分页 |
| workspace.catalog | 流程与资产目录的名称查询、排序和分页 |
| query.read / query.close | 诊断筛选、稳定序号、总数、快照和诊断集合释放 |
| request.status | 查询当前客户端已提交的保存请求；按当前权限读取结果 |
| content.begin / content.write / content.seal | 预留容量、顺序分块上传、校验编码与摘要、按用途封存 JSON |
| content.discard | 按 uploadId 或 resourceId 释放当前会话的临时内容；参数互斥、重复释放幂等 |
| asset.read | 固定资产版本的范围读取，返回带摘要和期限的私有资源 |
| resource.read | 按偏移读取最多 192KiB；返回整段资源摘要与实际块长度 |

`workflow.save` 固定图像、轨迹和子流程的正式版本，核对依赖内容与完整授权范围；存储接口、复制规则和容量见[资产版本与流程依赖](assets.md)。`trajectoryEdit` 封存完整轨迹 JSON，由资产编辑应用服务进行时长与正式版本提交。方法与参数见[本机协议](../interfaces/local-protocol.md)。

## 服务调用

`Workspace(path, create=True)` 只在空目录创建工作空间；已有工作空间使用 `Workspace(path)`。实例持有操作系统文件锁，直到关闭才释放；同一目录的别名解析到相同位置。数据库事务与内容发布分开，文件写入失败或提交前超时不会更新流程头版本。

`Engine.open_session` 由受信启动器或适配器调用，传入已认证的稳定客户端身份、权限、流程范围和已协商的 `capabilities`。使用内容传输时传入 `capabilities={"contentRefsV1"}`；能力选择独立于用途权限。`ConnectionAuthority` 与 `ClientConnection` 实现[连接认证](../interfaces/connection.md)，验证票据和双向应答后调用此入口，并传入 `authorization_version`；原生适配器提供已核验对端身份。网络消息不能创建会话或覆盖这些字段。`dispatch` 使用会话身份和公共消息信封；连接的 `authorize` 回调覆盖解析、处理、提交检查和响应编码。返回值符合公共响应契约。

`local=True` 仅用于已经完成本地窗口身份核验的调用方。表达式预览保持本地准入限制；其他入口对相同业务参数调用相同方法处理器。`revoke_client` 关闭该客户端全部会话、查询与临时内容，`close` 释放 Engine 与工作空间。单个临时文件关闭失败时仍清理其余资源及会话，随后向调用方报告存储异常。

内容上传与读取结果分别使用每工作空间 16 项、64MiB 的容量池，单项原始字节和规范编码各不超过 32MiB；流程保存按完整模型计数，命令字段单独计入 1MiB 控制帧。暂存与封存各有最多 60 秒的期限。同一会话重复读取相同流程内容复用原资源和期限，读取完成或取消后调用 `content.discard({resourceId})`。会话关闭与撤权释放全部临时内容；过期内容不可访问，在后续资源准入或访问时回收。完整的权限、重传、期限和摘要规则见[内容传输](../interfaces/content-transfer.md)。

引用保存使用原始 `modelRef` 参数识别重复请求，已成功的保存可在引用释放、到期或 Engine 重启后重放；同一请求键换引用返回 `REQUEST_CONFLICT`。引用内容仍执行流程身份、静态校验、版本比较与原子保存，失败不会发布流程版本。

集合游标绑定会话、权限代次、筛选、排序和每页数量。目录与版本查询固定 SQLite 版本序号，使用索引和最后排序键读取 pageSize+1 条匹配记录；诊断页共享来源集合。快照保存查询条件、版本上界和期限，游标保存最后实际返回的位置。查询默认五分钟到期，诊断页不超过来源集合期限；每个会话最多八个快照，工作空间最多三十二个。

`workflow.validate` 的内嵌定义接受 JSON 对象；缺少必填字段、字段类型错误及未知字段通过 `WORKFLOW_SCHEMA` 诊断返回，`fieldPath` 定位字段；已知 action 使用对应动作分支，嵌套参数、缺失必填项和未知字段各自定位，未知动作定位 action。结构与语义诊断共用节点归属规则：节点身份符合公共 nodeId 定义且在当前流程中唯一时填写 nodeId；流程级诊断、节点集合类型错误以及缺失、无效或重复的节点身份均不填写 nodeId。fieldPath 始终定位实际出错位置。请求参数、JSON 类型、大小和深度仍在入口校验。合法流程身份继续检查流程访问范围；缺少或非法身份的草稿只检查客户端提交的内容，诊断归提交会话所有。

动作的 Value 根由公共 Schema 解析，使用与 expression.preview 相同的 AST 校验；参数对象和输入、返回值映射作为字段容器。深度或运算节点超限分别返回 AST_DEPTH、AST_NODES 诊断，fieldPath 由 Value 根位置和相对表达式路径共同组成。所有字段路径使用完整 RFC 6901 JSON Pointer，空字符串定位文档根；查询页放不下单条诊断时返回 QUERY_ITEM_TOO_LARGE。

诊断来源集合独立限额为每会话八个、工作空间三十二个，包含空诊断结果。容量满时按创建次序淘汰没有活动查询快照的旧集合；会话达到限额时只淘汰本会话的旧集合。仍被查询占用的集合保持到期时间与成员，全部占用时返回 `LIMIT_EXCEEDED`。`query.close({collectionId})` 释放诊断集合及其全部分页；`query.close({snapshotId})` 只释放指定快照。两种关闭方式互斥且幂等。编辑器替换校验结果时按集合关闭旧结果，断开和撤权释放会话全部结果。

通用 JSON 编解码检查数据类型、Unicode、数值、嵌套深度与字节预算，容器成员通过逐层迭代检查。字段容量使用对应 Schema：流程资源最多 10,000 项、变量映射最多 1,000 项；内嵌定义的容量错误通过 workflow.validate 返回字段诊断。表达式单个文本的 256KiB UTF-8 预算由求值入口和结果检查显式执行。

请求预算覆盖处理器、结果校验和完整响应编码。表达式预览取 `deadlineMs` 与一秒的较小值；JSON 文本处理、数组成员与对象字段解析、规范编码、Schema 关键字及跨资源引用验证均在处理过程中检查剩余预算；保存引用解析、工作空间对象读取及结果校验使用同一请求预算。表达式求值和流程遍历继续检查剩余预算，截止时间错误由入口直接返回。超时返回 `DEADLINE_EXCEEDED`；保存已提交而响应超时时，通过原请求身份查询保存结果。完整响应超过 1MiB 返回 `LIMIT_EXCEEDED`，错误响应不携带原成功结果。

表达式预览的完整参数和结果各限 256KiB，目录、版本和诊断查询的完整结果也限 256KiB。预算从方法 Schema 的 `x-maxUtf8Bytes` 读取，保留 `$ref` 同层注解。分页在达到字节上限前结束本页，游标从最后返回项之后继续；单项放不入页面时返回 `QUERY_ITEM_TOO_LARGE`。实际游标位置合计最多一百万条。

工作空间数据库格式为 3；打开格式 1/2 时在单次事务中补齐目录索引、资产版本和依赖引用表，已有版本、内容与保存回执保持。初始化失败回滚后可重新打开；临时查询在 Engine 重启后失效。

表达式计算错误统一为 `INVALID_ARGUMENT`，具体原因与相对 Value AST 的路径段分别位于 `details.reason`、`details.expressionPath`；容量错误为 `LIMIT_EXCEEDED`，时间耗尽为 `DEADLINE_EXCEEDED`。位置字段保留含斜线、波浪线和 Unicode 的对象键；客户端不按错误消息文本识别类型。

表达式相等按 JSON 类型和值比较，对象键顺序不影响结果；数值 `1` 与 `1.0` 相等，布尔值与数值分别比较。文本长度使用 UTF-16 代码单元，数组长度使用成员数量。路径读取与 `$literal` 返回的数据不会再次作为表达式执行。
