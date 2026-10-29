# 视觉模型连接

## 配置与版本

模型连接保存 endpoint、model、凭据模式和 options。配置由可信本地设置界面提交，带 expectedVersion；成功返回新版本。WebView 只能写入本次新凭据和读取凭据是否存在，不能读取已保存密钥。

| 选项 | 含义 |
|---|---|
| provider | OpenAI 兼容消息接口，或使用自定义图像消息片段的同类接口 |
| systemPrompt / defaultPrompt | 系统内容与默认观察任务，单次观察可以覆盖任务内容 |
| temperature / maxOutputTokens | 采样参数与最大输出预算，范围见 Schema |
| imageEncoding / imageDetail | 图像 URL、直接 Base64 或自定义消息片段，以及图像细节等级 |
| imagePayloadTemplate | 自定义图像内容的 JSON 结构 |
| extraBody | 经白名单校验的附加模型参数 |

connection.read 可带 version 读取指定不可变配置，省略时读取当前头；不存在的版本明确返回 NOT_FOUND。返回内容保留原版本的端点、模型与选项，credentialPresent 表示该版本凭据当前是否可用。

模型连接的 options 见[视觉对象契约](../../contracts/vision.schema.json)。连接修改影响新观察；活动会话继续使用已固定版本，撤销凭据或目标授权则立即停止后续请求。

## 版本与凭据事务

连接身份和配置版本分别存储。每个 (connectionId, version) 唯一，版本中的端点、模型、选项、凭据引用和创建时间不可变；当前头通过 expectedVersion 比较后前移。活动会话、观察记录和运行快照固定具体版本，不能因头变化读取新配置。

connection.configure 的 revokePreviousCredential 默认 false。keep 复用当前凭据且只允许 false；replace 写入新的独立凭据引用，none 创建不携带凭据的新版本。替换默认允许已经运行的会话继续使用其固定旧版；replace/none 显式选择 true 时，撤销原凭据引用，停止仍使用它的后续观察。撤销标记与头更新在同一数据库事务生效，每次发请求前检查凭据状态与代次。

凭据材料保存于秘密存储，数据库只保存不含密钥的引用与生命周期。新密钥先暂存并核对可读取性，再提交配置意图；完成准备后，短事务比较旧头、登记新版本和引用、移动头并保存请求回执。任一步骤失败保留旧头；中断后按意图阶段核定，不能创建指向不存在凭据的生效版本。

旧凭据替换后标为 retired；当前头、活动会话和未决请求仍引用时不回收。历史记录保留非秘密配置及原引用身份，不要求永久保存可调用的旧密钥。retired 且无执行引用的材料可清除，历史查询返回 credentialPresent=false。revoked 状态不因重新配置、重连或恢复备份而复活。

工作空间备份不包含密钥材料。恢复后核对每个凭据引用与本机秘密存储的工作空间绑定、可用状态及撤销代次；缺失时保留配置与历史结果，返回 CREDENTIAL_UNAVAILABLE，重新配置后仅供新的观察使用。阶段、索引和补偿见[持久化事务](persistence-transactions.md)。

## 消息构造

图像模板是一段 JSON 数据，允许在完整字符串值中使用 {{imageDataUrl}}、{{imageBase64}} 和 {{detail}} 三个占位符。占位符不读取文件、环境、网络或运行上下文，不执行表达式，也不能定义 HTTP 目的地、请求头或凭据。

先解析模板，再按 JSON 树替换允许的占位符。未知占位符、重复 JSON 键、非有限数、结构过深或编码后过大均在保存前拒绝。图像内容只是消息的一部分，不能覆盖提示内容、模型或授权对象。

extraBody 禁止覆盖 model、messages、stream、tools、tool_choice、api_key、headers、base_url 和 timeout。对服务商扩展参数配置明确允许键及类型；不允许用扩展参数改变工具调用、图片来源或请求目标。

## 输出要求

每次观察选择 text 或 json。json 可附带受限 JSON Schema，仅允许本地定义和有界结构；拒绝网络引用、宿主扩展与无限递归。模型返回后执行结构校验；失败记录具体字段与原因，拒绝将未通过校验的文本作为结构化结果返回。

服务响应在提取业务结果、错误原因与用量后释放；观察记录保存这些结果及耗时。模型文本不能改变执行授权；进入流程条件时只作为普通值读取。

## 测试与错误

连接测试区分配置校验、端点连接和模型可用性。读取连接设置不发起网络请求；“测试连接”使用明确的小型合成请求，不附带真实屏幕。发送真实区域只能由观察操作发起。

端点默认 HTTPS；本机服务允许明确配置的回环 HTTP。重定向不能把凭据带到另一个源；DNS 或证书失败保留具体状态。网络错误、认证失败、模型不存在、消息不兼容、超时和结果结构失败使用不同原因。

自定义消息编辑器提供合成图像预览；保存前可查看实际 JSON 结构、占位符和大小。预览不包含密钥。对应界面与连接失败状态见[全界面原型](prototypes.md)，验收见 Q25。

## 连接发现与未保存测试

connection.list 提供本地分页发现，connection.configure 可保存显示名称。未保存配置通过 connection.testDraft 的可取消作业测试，不先改正式连接；editorId/editVersion 绑定测试结果。临时凭据、取消和保存规则见[制作事务](authoring-transactions.md)，列表见[运行检查](runtime-inspection.md)。
