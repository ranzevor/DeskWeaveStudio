# 契约示例

这些文件用于实现公共格式、解析器和客户端。运行语义与[工作流设计](../docs/design/workflow.md)、[执行设计](../docs/design/execution.md)共同定义。

## 可运行服务示例

- [工作空间](engine/workspace.py)：流程保存、读取、校验与表达式预览。
- [内容传输](engine/content_transfer.py)：大于 1MiB 的模型分块上传、引用保存、资源化读取、整段摘要校验和临时资源释放。

运行方式见[Engine 开发](../docs/development/engine.md)。

## 合法示例

| 文件 | 用途与预期 |
|---|---|
| [hello.json](workflows/hello.json) | 赋值、等待、断言和返回；默认输出 message=Hello DeskWeave |
| [child.json](workflows/child.json) | 必填字符串输入 value，返回同名字段 |
| [parent.json](workflows/parent.json) | 启动后台 child，等待后返回 child.value=DeskWeave |
| [shortcut.json](trajectories/shortcut.json) | Ctrl+S 按键序列；总时间轴 30,000 微秒 |
| [request.json](messages/request.json) | 校验已保存流程版本的请求 |
| [response.json](messages/response.json) | 与校验请求对应的成功响应 |
| [event.json](messages/event.json) | UTC 时间、字符串序号和运行接收事件 |
| [input.json](messages/input.json) | 带演示目标令牌的单次输入请求 |
| [agent-lease.json](messages/agent-lease.json) | 原生租约准入请求 |
| [agent-segment.json](messages/agent-segment.json) | 带目标和参数摘要的已求值输入段 |
| [values.json](values.json) | 零、false、空串、保留键原文与表达式 |
| [manifest.json](manifest.json) | parent、child、hello 与 shortcut 的实际长度和摘要 |

执行轨迹或 input 请求前，为独立测试目标获取 targetToken 并替换样例中的值；执行后检查目标窗口收到的事件。

parent 使用资源逻辑键 child。导入时由包清单固定其对应文件；运行时固定为工作空间内的具体内容版本。

内容释放参数见[上传释放](authoring/content-discard-upload.json)和[读取资源释放](authoring/content-discard-resource.json)；[内容格式错误](messages/content-invalid.json)中的 contentPath 定位上传 JSON，区别于请求参数的 fieldPath。

## 拒绝示例

[用例清单](cases.json)指定文件、Schema、额外语义检查和预期。expected=pass 表示接受；schema-fail 表示结构、格式或方法参数拒绝；semantic-fail 表示结构合法但图、轨迹、包、视觉配置或网络接入规则拒绝。Schema 字段可通过 `#/$defs/` 选择具体对象。

[拒绝用例清单](cases.json)覆盖缺失节点、普通图环、未配对按键、未知表达式、无效日期、未知方法、错误参数、单次输入边界以及 Windows 设备路径和冒号路径。

## 模板、观察与接入样例

| 文件 | 用途与预期 |
|---|---|
| [template-options.json](vision/template-options.json) | 多算法权重、候选上限与超时边界 |
| [extraction-empty.json](vision/extraction-empty.json) / [extraction-image.json](vision/extraction-image.json) | 帧与固定图像来源、摘要、空结果和截断数 |
| [template-names.json](vision/template-names.json) / [template-name-conflict.json](vision/template-name-conflict.json) | 实际名称来源和禁止提交的名称冲突 |
| [observation-text.json](vision/observation-text.json) / [observation-json.json](vision/observation-json.json) | 最小周期、纯文本和结构化输出 |
| [observation-record.json](vision/observation-record.json) / [observation-run-record.json](vision/observation-run-record.json) / [observation-skipped.json](vision/observation-skipped.json) | 独立观察、关联运行和无变化跳过的记录 |
| [connection-default.json](vision/connection-default.json) / [connection-custom.json](vision/connection-custom.json) | 默认消息与完整字符串占位符 |
| [condition-loop.json](workflows/condition-loop.json) / [loop-body.json](workflows/loop-body.json) | 有界 while 循环、运行共享变量和所属循环退出 |
| [shared-state.json](workflows/shared-state.json) | 指定共享状态版本的条件写入 |
| [chord-boundary.json](actions/chord-boundary.json) | 组合按键和最长按住时长 |
| [run-prepare-node.json](messages/run-prepare-node.json) / [run-start-node.json](messages/run-start-node.json) | 指定节点检查与携带计划的启动请求 |
| [template-extract.json](messages/template-extract.json) / [observation-start.json](messages/observation-start.json) | 模板提取与持续观察请求 |
| [mcp-stdio.json](messages/mcp-stdio.json) / [mcp-http.json](messages/mcp-http.json) / [mcp-tls.json](messages/mcp-tls.json) | 本机进程、回环 HTTP 与带证书授权的 TLS 接入 |
| [mcp-token.json](messages/mcp-token.json) | 有期限的权限与目标范围 |

对应拒绝例覆盖全零算法、反向面积和宽高比、缺失来源摘要、过短观察周期、外域输出 Schema、未知图像占位符、消息覆盖、孤立循环信号、缺循环资源、非法共享版本、重复按键、缺启动计划和网络授权错误。

循环样例的 body 资源由工作空间固定到 loop-body 的具体版本，导入循环样例时一并提供该依赖。运行共享变量冲突和按键释放使用 Q26/Q29 的场景检查。

## 持久化边界样例

| 文件 | 用途与预期 |
|---|---|
| [observation-busy.json](vision/observation-busy.json) | 有检查时间、没有采集帧和模型结果的忙碌跳过 |
| [observation-unchanged-zero.json](vision/observation-unchanged-zero.json) | 实际零差异、成功基线及没有保留截图的无变化检查 |
| [observation-failed-before-capture.json](vision/observation-failed-before-capture.json) | 采集前失败，保留检查事实且不补造采集时间 |
| [observation-background.json](messages/observation-background.json) / [observation-background-session.json](vision/observation-background-session.json) | 创建时明确选择 detached，并在快照中固定该选项 |
| [observation-interrupted-session.json](vision/observation-interrupted-session.json) | 进程中断后的明确终态，不恢复原周期请求 |
| [connection-read-version.json](messages/connection-read-version.json) | 读取指定不可变连接版本 |
| [connection-keep-credential.json](messages/connection-keep-credential.json) / [connection-replace-revoke.json](messages/connection-replace-revoke.json) / [connection-no-credential.json](messages/connection-no-credential.json) | 复用、替换并撤销旧凭据及无凭据配置 |

反例覆盖无采集却提供帧、忙碌记录伪造采集或差异、沿用旧输出、无变化缺基线或差异、时间逆序、冒认所有者、未结束会话提供结束时间、非法连接版本以及 keep 模式撤销被复用的凭据。

事务中的并发占名、回执丢失、引用回收和秘密存储中断按[持久化事务](../docs/design/persistence-transactions.md)执行存储集成验收；JSON 样例检查验证负载结构及相应字段规则。

## 异步作业结果

[作业目录](../contracts/operation-catalog.json)覆盖 14 个异步方法，最终结果由[作业结果 Schema](../contracts/operation-results.schema.json)定义。[成功作业](operations/input-submit-succeeded.json)与[用例清单](cases.json)提供每个方法的 succeeded 快照，以及运行中、失败、取消和 uncertain 示例。

operation.get 与 operation.cancel 返回同一种快照。succeeded 的 result 必须属于其原 method，error 为 null；queued/running/cancelling 的 result 和 error 均为 null；failed/cancelled/uncertain 的 result 为 null，error 提供具体原因。uncertain 的 retry 为 afterQuery，查询原作业而非重新执行；cancelled 使用 CANCELLED 和 never。

[record-finish-partial.json](operations/record-finish-partial.json)保存可修复录制草稿，不能作为正式轨迹回放。[input-submit-succeeded.json](operations/input-submit-succeeded.json)记录系统输入发送与清理结果，目标应用结果由对应流程的后置检查确定。[vision-match-not-found.json](operations/vision-match-not-found.json)表示匹配计算完成但未找到目标。

反例覆盖 method/result 不匹配、把接收回执当完成结果、成功缺结果、未知方法、未完成却带结果、失败缺原因、uncertain 自动重发、输入清理未完成和不完整录制伪装成正式资产。录制资源的长度和摘要对应仓库中实际的完整或未配对轨迹字节；其余资源身份为协议演示对象。

## 文档草稿与恢复

[草稿快照](drafts/snapshot-invalid-source.json)与[用例清单](cases.json)包含有效源码、未完成源码、尚无有效模型、未连接节点和大内容资源引用；对应 `draft.read/write/discard` 的本地请求保留编辑分支和预期代次。

`write-invalid-source.json` 保存当前原文及上一有效结构，`write-unconnected.json` 保留可继续编辑的未连接节点，正式保存与执行另行校验流程语义。拒绝例检查倒退代次、模型代次超前、历史源码与模型不符、当前有效源码与模型不符、错流程和缺少完整诊断引用。

`write-resource.json` 和 `snapshot-invalid-source.json` 的资源声明分别对应 [source-invalid.txt](drafts/source-invalid.txt) 与 [model-content.json](drafts/model-content.json) 的真实长度和 SHA-256。实现阶段通过受限资源上传、事务竞争及关闭恢复用例检查相关行为。

## 执行控制、导入和管理

| 目录与入口 | 用途 |
|---|---|
| [暂停检查点](control/lease-timing-overrun.json) | 暂停检查点、目标绑定、恢复确认、断点版本及冲突反例 |
| [导入用例清单](cases.json) | 包与单文件的原始字节摘要、身份映射、依赖绑定及原子结果 |
| [设置更新结果](settings/mixed-result.json) | 用户与工作空间覆盖、恢复继承、混合生效及有效来源 |
| [资产引用页](management/asset-reference-page-all.json) | 全版本引用、头版本、隐藏引用数和有期限快照 |
| [令牌影响页](management/token-impact-page.json) | 按令牌隔离的影响计数与受限运行详情 |
| [过期预览撤销](management/token-revoke-expired-preview.json) | 预览过期仍按当前状态吊销，并返回实际处理数量 |
| [更新签名向量](updates/signature-vectors.json) | 真实 Ed25519 签名、根轮换、后续启动、吊销和防重放 |

## 执行检查

在仓库根目录运行：

```bash
python -m pip install -r requirements-docs.txt
python tools/check_docs.py --contracts
```

检查加载全部 Schema，验证每个用例的预期，匹配响应的方法结果，并重新计算包对象的长度与 SHA-256。修改包内样例后必须同步更新 manifest；摘要记录实际文件字节，包含 UTF-8 编码与 LF 换行。

更新样例的规范字节与真实签名另运行 `node tools/check_update_signatures.js`；该检查同时覆盖历史根链重验与非法 JSON 编码。

## 查询、编辑与大内容

[编辑与查询用例清单](cases.json)包含变量脱敏与快照、版本/连接清单、未保存测试、图像/轨迹提交及内容引用的接受与拒绝样例。[规范编码向量](canonical/vectors.json) 用于 Python 和 JavaScript 的共同字节检查。

    python tools/check_canonical_json.py
    node tools/check_canonical_json.js
    python tools/check_authoring_contracts.py

## 全显示屏录制

[显示拓扑](recording/topology.json)、[跨屏拖拽](recording/cross-screen-drag.json)及[启动参数](recording/start-all-displays.json)覆盖负坐标、混合 DPI 和 L 形布局。相应拒绝样例检查屏间空隙、缺少显示屏、过期拓扑、录制用途和不完整或未授权的回放映射；检查入口为 `python tools/check_docs.py --contracts`。

## 分页查询

[首页](queries/first-page.json)、[续页](queries/next-page.json)、[末页](queries/last-page.json)、[条件筛选](queries/conflict-filter.json)和[请求默认值](queries/default-request.json)使用固定集合与查询快照。queries/ 下同时覆盖九种记录结构、条件变化、跨客户端、撤权、到期、数量边界、空结果和关闭。集合摘要与逐页结果分别校验，命名、匹配和导入继续核对完整集合的状态。

## 连续笔画

[折线](trajectories/drawing-corners.json)、[闭合圆环](trajectories/drawing-loop.json)、[八字形](trajectories/drawing-eight.json)、[多笔画](trajectories/drawing-multistroke.json)、[相对折返](trajectories/drawing-relative.json)和[跨屏圆环](trajectories/drawing-cross-screen.json)保留全部中间事件。闭合与折返样例起终位置相同，用于区分完整路径和只有终点的输出；多笔画样例的抬笔移动不属于绘制区间。

录制快照同时包含队列溢出和接收中断的 interrupted、finished 分段，以及禁止将其标成完整录制的反例。[时序超限暂停](control/lease-timing-overrun.json)使用原有租约快照返回 INPUT_TIMING_OVERRUN 和实时检查点。结构检查不发送输入；目标窗口的逐步绘制、连续时序与跨屏结果按[连续指针设计](../docs/design/pointer-motion.md)和 Q50～Q53 验收。

## 跨平台

[后端能力](platform/compositor-unimplemented.json)与[平台用例清单](cases.json)包含四类运行后端能力、macOS 权限缺失、Wayland 合成器后端未实现、Retina 混合屏、1.1 跨屏笔画、小数相对量、跨后端像素映射、暂停位置及五种制品平台。拒绝例覆盖部分屏幕伪装全部、Portal 被动录制误报、奇异变换、缺少显示身份、旧格式小数、相对单位冲突和错误制品平台。

平台制品样例用于字段与平台选择检查；实际安装签名、公证、依赖和输入行为按 Q54～Q58 在对应环境执行。坐标和能力语义由 tools/platform_contract_checks.py 与录制检查共同校验。

## 多入口与表达式预览

[MCP 请求元数据](mcp/request-metadata.json)及边界样例核对可恢复请求编号、可选元数据、长度和类型。[表达式预览](expressions/preview-context.json)使用独立合成上下文；接受和拒绝样例覆盖字面量、false、根字段白名单、运算参数数目及结果结构。检查入口为 `python tools/check_docs.py --contracts`；运行求值及多入口状态验收见 Q06/Q37/Q59。

## MCP 协议与兼容

[新版发现](mcp/modern-discover-request.json)、[发现结果](mcp/modern-discover-result.json)、[工具调用](mcp/modern-tool-call.json)与[HTTP 请求头](mcp/modern-tool-headers.json)使用 2026-07-28。[订阅请求](mcp/modern-listen-request.json)、[确认](mcp/modern-listen-acknowledged.json)、[资源更新](mcp/modern-resource-updated.json)和[正常结束](mcp/modern-listen-complete.json)形成同一订阅序列。[旧版初始化](mcp/legacy-initialize.json)使用 2025-11-25；两代工具参数继续引用同一服务契约。

拒绝样例覆盖缺少版本/能力、错误类型、null 请求 ID、遗漏缓存字段、缺少结果类型、订阅身份缺失及容量超限。HTTP 头体冲突、Base64 名称、跨订阅通知和适配规则混用由 tools/mcp_protocol_checks.py 校验，入口为 `python tools/check_docs.py --contracts`。真实 SDK 互通与 Engine 副作用按 Q28/Q59 验证。

## 流程副本与固定依赖

[完整流程模型](authoring/workflow-model-dependencies.json)包含固定版本的子流程和两项图像资产绑定。原型在冲突另存成功、传输失败、提交失败和取消后分别核对依赖身份、版本及资源关联。
