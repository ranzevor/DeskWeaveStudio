# 内容分块传输与公共摘要

## 能力与容量

控制帧继续保持 1MiB 上限，计入完整 JSON 信封的实际 UTF-8 字节。单个内容对象的原始字节与 RFC 8785 规范编码各不超过 32MiB。workflowModel 的容量对象是完整的 definition/dependencies 模型；workflowId、draftVersion、baseRevision 等保存命令字段计入控制帧，不能挤占模型额度。分块传输不能绕过内容上限。握手能力 contentRefsV1 启用内容引用与资源化结果；未协商的客户端只能使用能放入控制帧的原有内嵌格式，超限返回 LIMIT_EXCEEDED，不静默截断。

[内容契约](../../contracts/content.schema.json)定义短时上传和 contentRef。每工作空间最多 16 个暂存上传，包含已封存的上传，累计声明长度最多 64MiB；写入长度不能超过声明。资源化读取结果使用独立容量池，同样最多 16 项、64MiB，单项不超过 32MiB。达到任一容量上限返回 LIMIT_EXCEEDED，释放已有临时内容后可继续申请。只允许 workflowModel 与 trajectoryEdit 两种用途，分别检查 workflow.write 或 asset.write；每一步还核对创建者、工作空间、接收实例、用途和当前授权。

## 上传、封存与释放

1. content.begin 接收用途、媒体类型、总长度及 SHA-256，分配 uploadId、nextOffset=0、maxChunkBytes=196608 和最多 60 秒期限。按声明长度计入容量预留后才返回成功；临时文件按实际接收字节写入，磁盘不足通过 STORAGE_UNAVAILABLE 返回。
2. content.write 接收 uploadId、offset、严格 Base64 的 chunk 和 chunkSha256。解码后最多 196608 字节，只接受当前 nextOffset；与原始块边界及字节完全相同的重传可幂等返回当前 nextOffset，不能拆分或合并已接收块，内容变化或跳过偏移返回 CONTENT_OFFSET_CONFLICT。超出声明长度立即拒绝。块写入失败或超时回退到该块开始前的长度和偏移；回退失败则释放上传。
3. content.seal 重新计算实际长度与整段摘要，验证 UTF-8、严格 JSON 及用途对应的结构。workflowModel 必须是 definition 与 dependencies；trajectoryEdit 必须是完整 Trajectory。缺块、摘要不符或不合法内容不生成引用。解析 JSON 值、递归校验 Schema 及规范编码期间持续检查请求预算；超时返回 DEADLINE_EXCEEDED，不公布封存引用。格式结构错误返回 INVALID_CONTENT，details.contentPath 使用相对上传 JSON 根的 RFC 6901 JSON Pointer；请求字段错误使用 details.fieldPath，UTF-8 或严格 JSON 解析失败返回 INVALID_ARGUMENT。封存结果为不可变 contentRef 和期限，重复封存返回相同引用与期限。
4. content.discard 接受互斥的 uploadId 或 resourceId：前者释放上传及对应的封存引用，后者释放封存引用或临时读取结果。仅操作当前会话的内容，并检查来源权限和已知流程范围；不存在、已释放或其他会话的身份均返回 discarded=true，其他会话内容保持。正式版本及已准入作业持有的内容不因暂存释放而丢失。

contentRef 只含 kind=resource、resourceId、length 和 sha256，不提供通用路径。暂存期限不因分块读取、重查或重连延长；封存引用自首次封存起最多 60 秒。expiresAt 对齐 UTC 整秒，实际失效点与返回值一致；同时使用单调时钟限制剩余寿命，墙钟回退不能延长期限。到期内容立即失去访问资格，在后续资源准入或访问时回收；会话关闭、客户端撤销及 Engine 关闭释放所属临时内容。提交准入时固定必要引用到收尾，尚未准入且过期的内容不能继续使用。回复丢失先查询原请求或作业，不重新创建正式对象。

Engine 重启后不恢复临时上传、封存引用或读取结果；已成功的正式提交仍可通过原请求身份查询或重放，且重新检查当前流程权限。引用已经释放或过期不影响成功回执，同一请求键换引用返回 REQUEST_CONFLICT。上传和封存本身不保存流程、不生成正式资产，也不发送桌面输入。

## 流程保存和读取

工作台的“保存流程”自动完成内容上传、封存和版本保存。界面显示整体进度、失败原因和保存后的版本，提供取消及失败重试；分块顺序、重传和引用处理由客户端完成。取消或失败时保留当前草稿。

workflow.save 保留小内容 definition/dependencies，增加互斥的 modelRef 分支。两种形式解析为同一流程模型，使用同一静态校验、依赖授权、draftVersion/baseRevision 比较和原子保存。客户端不能同时传两种来源，服务端重验引用实际内容中的流程身份、依赖与摘要。

workflow.read 支持 contentMode=auto/inline/resource，省略为 auto。协商 contentRefsV1 后，模型的规范 JSON 字节大于 256KiB 时，以 modelResource 返回含 definition/dependencies 的不可变 JSON；definitionHash 覆盖实际流程定义。指定 inline 且超限时明确拒绝。

资源化结果通过 resource.read 分块读取，每次请求 length 为 1～196608 字节。响应 offset 是当前偏移，length 是本块解码后的实际字节数，totalLength 和 sha256 分别是完整资源的长度与原始字节摘要。offset 等于总长时返回空块和 length=0，超过总长返回 INVALID_ARGUMENT。客户端逐块核对资源身份、偏移、长度与总长，接收完整字节后校验整段 SHA-256，再解析并建立工作台模型。

同一会话对同一流程的相同模型重复读取复用临时资源，且不续期。读取完成或取消时调用 content.discard({resourceId}) 释放容量；取消仅丢弃未完成的新模型，原草稿继续保留。其他读取操作仍引用同一资源时，由客户端在最后一个使用者结束后释放。内容身份或摘要不匹配不能显示为加载成功。

## 运行快照和诊断

run.snapshot 返回 frameCount、effectCount、framesQuery 和 effectsQuery，两个集合固定到同一 dataVersion。调用帧与输入效果通过 query.read 执行条件筛选和游标分页，每页最多 100 项；详情与异常见[分页查询](pagination.md)。完整流程模型、轨迹和导出文件继续使用内容引用与字节分块。

变量另用 debug.variables.read 的固定分页和单值资源。诊断和其他有界列表在完整信封预算内输出；不能完整返回的结果必须采用其规定的资源分支或明确返回 LIMIT_EXCEEDED，不能仅用 pageSize 判断字节是否足够。草稿继续使用现有受限内容引用规则。

## RFC 8785 编码

公共参数、检查点与输入段摘要使用 RFC 8785 JCS 的 UTF-8 字节计算 SHA-256。数字按 ECMAScript 二进制双精度规则序列化，包括负零、指数阈值和最短往返表示；对象键按 UTF-16 代码单元排序，数组保持原顺序。重复键、孤立代理码点、非有限数和不合法 UTF-8 拒绝。业务格式中的安全整数范围仍单独验证。

Python 文档检查使用固定版本 rfc8785；JavaScript 独立使用严格解析、UTF-16 排序和原生数值序列化。两者重验[官方数值及边界向量](../../examples/canonical/vectors.json)。字节型内容资源和 PNG 的摘要始终基于原始字节，不能换成重序列化结果。更新签名继续使用其独立且限定整数的签名字节规范。

## 验收

Q45 覆盖 1MiB 边界、合法大模型、分块乱序/重发、缺块、篡改、到期、跨用途、取消与原子保存。Q46 覆盖 RFC 数值表、整值浮点、负零、指数、Unicode 键、重复键及三语言实现阶段的共同字节；当前工具验证 Python 与 JavaScript，Rust 在组件实现后接入。

官方规范：[RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html)。

## 正式资产范围

asset.read 的 offset/length 选择资产原始文件的字节区间，单次最多 4MiB；到达文件尾时返回剩余部分，offset 等于总长返回空资源，超过总长返回 INVALID_ARGUMENT。返回描述的 length 和 sha256 对应所选区间，resource.read 使用从 0 开始的区间内部偏移。媒体类型沿用原始资产，完整文件摘要取正式资产绑定的 sha256。

此方法要求 asset.read 和 contentRefsV1，输出沿用资源化结果的会话隔离、60 秒期限和容量池。资产读取与流程模型读取使用不同的用途及权限，不能互换为 workflow.save 的 modelRef。具体存储与依赖规则见[资产版本与流程依赖](../development/assets.md)。
