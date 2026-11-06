# Engine 服务库

Python 包位于 `python/deskweave`。`Engine` 统一分发方法、权限校验和结果校验；工作空间存储、表达式求值和流程检查由独立模块提供。

## 安装与运行

使用 Python 3.12 和 uv 0.8.2，在仓库根目录运行：

```bash
uv sync --frozen
uv run --frozen python examples/engine/workspace.py
```

示例在临时目录创建工作空间，完成流程保存、读取、校验和表达式预览，退出时删除临时数据。

```bash
uv run --frozen ruff check python tests examples/engine
uv run --frozen ruff format --check python tests examples/engine
uv run --frozen python -m unittest discover -s tests -q
uv build
```

构建输出位于 `dist/`，wheel 包含运行模块和公共契约；源码包包含测试与契约源文件。

## 方法范围

| 方法 | 当前行为 |
|---|---|
| expression.preview | 复合值、全部已定义运算、短路、路径、类型和容量校验；限可信本地编辑会话 |
| workflow.save | 保存内嵌定义且不含资源依赖的流程；版本比较、不可变内容和重复请求去重 |
| workflow.read | 读取指定版本的内嵌定义、依赖和定义摘要 |
| workflow.validate | 校验格式、节点、出口、入口、可达性、图环、目标槽、资源类型和参数默认值 |
| workflow.revisions | 固定快照中的历史版本分页 |
| workspace.catalog | 流程目录的名称查询、排序和分页 |
| query.read / query.close | 诊断筛选、稳定序号、总数、快照和诊断集合释放 |
| request.status | 查询当前客户端已提交的保存请求；按当前权限读取结果 |

独立 Engine 进程、认证 IPC、内容资源传输、运行调度和原生输入按 M1 后续步骤实现。方法与参数的完整设计见[本机协议](../interfaces/local-protocol.md)。

## 服务调用

`Workspace(path, create=True)` 只在空目录创建工作空间；已有工作空间使用 `Workspace(path)`。实例持有操作系统文件锁，直到关闭才释放；同一目录的别名解析到相同位置。数据库事务与内容发布分开，文件写入失败或提交前超时不会更新流程头版本。

`Engine.open_session` 由受信启动器或适配器调用，传入已认证的稳定客户端身份、权限和流程范围。网络消息不能创建会话或覆盖这些字段。`dispatch` 使用会话身份和公共消息信封；返回值符合公共响应契约。

`local=True` 仅用于已经完成本地窗口身份核验的调用方。表达式预览保持本地准入限制；其他入口对相同业务参数调用相同方法处理器。`revoke_client` 关闭该客户端全部会话及查询，`close` 释放 Engine 与工作空间。

集合游标绑定会话、权限代次、筛选、排序和每页数量。目录与版本查询固定 SQLite 版本序号，使用索引和最后排序键读取 pageSize+1 条匹配记录；诊断页共享来源集合。快照保存查询条件、版本上界和期限，游标保存最后实际返回的位置。查询默认五分钟到期，诊断页不超过来源集合期限；每个会话最多八个快照，工作空间最多三十二个。

`workflow.validate` 的内嵌定义接受 JSON 对象；缺少必填字段、字段类型错误及未知字段通过 `WORKFLOW_SCHEMA` 诊断返回，`fieldPath` 定位字段；已知 action 使用对应动作分支，嵌套参数、缺失必填项和未知字段各自定位，未知动作定位 action。请求参数、JSON 类型、大小和深度仍在入口校验。合法流程身份继续检查流程访问范围；缺少或非法身份的草稿只检查客户端提交的内容，诊断归提交会话所有。

诊断来源集合独立限额为每会话八个、工作空间三十二个，包含空诊断结果。容量满时按创建次序淘汰没有活动查询快照的旧集合；会话达到限额时只淘汰本会话的旧集合。仍被查询占用的集合保持到期时间与成员，全部占用时返回 `LIMIT_EXCEEDED`。`query.close({collectionId})` 释放诊断集合及其全部分页；`query.close({snapshotId})` 只释放指定快照。两种关闭方式互斥且幂等。编辑器替换校验结果时按集合关闭旧结果，断开和撤权释放会话全部结果。

请求预算覆盖处理器、结果校验和完整响应编码。表达式预览取 `deadlineMs` 与一秒的较小值；JSON 检查、表达式求值和流程遍历在处理过程中检查剩余预算。超时返回 `DEADLINE_EXCEEDED`；保存已提交而响应超时时，通过原请求身份查询保存结果。完整响应超过 1MiB 返回 `LIMIT_EXCEEDED`，错误响应不携带原成功结果。

表达式预览的完整参数和结果各限 256KiB，目录、版本和诊断查询的完整结果也限 256KiB。预算从方法 Schema 的 `x-maxUtf8Bytes` 读取，保留 `$ref` 同层注解。分页在达到字节上限前结束本页，游标从最后返回项之后继续；单项放不入页面时返回 `QUERY_ITEM_TOO_LARGE`。实际游标位置合计最多一百万条。

工作空间数据库格式为 2；打开格式 1 时在单次事务中补齐目录名称键、版本有效区间和索引，已有版本、内容与保存回执保持。初始化失败回滚后可重新打开；临时查询在 Engine 重启后失效。

表达式计算错误统一为 `INVALID_ARGUMENT`，具体原因与相对 Value AST 的路径段分别位于 `details.reason`、`details.expressionPath`；容量错误为 `LIMIT_EXCEEDED`，时间耗尽为 `DEADLINE_EXCEEDED`。位置字段保留含斜线、波浪线和 Unicode 的对象键；客户端不按错误消息文本识别类型。

表达式相等按 JSON 类型和值比较，对象键顺序不影响结果；数值 `1` 与 `1.0` 相等，布尔值与数值分别比较。文本长度使用 UTF-16 代码单元，数组长度使用成员数量。路径读取与 `$literal` 返回的数据不会再次作为表达式执行。
