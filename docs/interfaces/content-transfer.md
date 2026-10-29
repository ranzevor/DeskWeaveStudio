# 内容分块传输与公共摘要

## 能力与容量

控制帧继续保持 1MiB 上限，计入完整 JSON 信封的实际 UTF-8 字节。逻辑请求展开后的内容最多 32MiB；控制帧分片传输不能绕过该上限。握手能力 contentRefsV1 表示支持本文的内容引用与资源化结果；未协商的客户端只能使用能放入控制帧的原有内嵌格式，超限返回 REQUEST_TOO_LARGE 或 RESPONSE_TOO_LARGE，不静默截断。

[内容契约](../../contracts/content.schema.json)定义短时上传和 contentRef。每工作空间最多 16 个暂存上传、累计声明长度最多 64MiB，实际字节同样计入。只允许 workflowModel 与 trajectoryEdit 两种用途，分别检查 workflow.write 或 asset.write；每一步还核对创建者、工作空间、接收实例、用途和当前授权。

## 上传、封存与释放

1. content.begin 接收用途、媒体类型、总长度及 SHA-256，分配 uploadId、nextOffset=0、maxChunkBytes=196608 和 60 秒期限。预留实际容量后才返回成功。
2. content.write 接收 uploadId、offset、严格 Base64 的 chunk 和 chunkSha256。解码后最多 196608 字节，只接受当前 nextOffset；已确认偏移的完全相同块可幂等返回，内容变化或跳过偏移返回 CONTENT_OFFSET_CONFLICT。超出声明长度立即拒绝。
3. content.seal 重新计算实际长度与整段摘要，验证 UTF-8、严格 JSON 及用途对应的结构。workflowModel 必须是 definition 与 dependencies；trajectoryEdit 必须是完整 Trajectory。缺块、摘要不符或不合法内容不生成引用。封存结果为不可变 contentRef 和期限。
4. content.discard 释放本人的未提交上传或封存引用。已被正式版本或在途作业固定的内容保留相应强引用，不因释放暂存而丢失。

contentRef 只含 kind=resource、resourceId、length 和 sha256，不提供通用路径。暂存期限不因翻页、重查或重连延长；封存引用自封存起最多 60 秒。提交准入时固定必要引用到收尾，尚未准入且过期的内容不能继续使用。相同请求键不允许换引用；回复丢失先查询原请求或作业，不重新创建正式对象。

Engine 重启后不恢复未封存上传；已经准入的正式提交按原事务和收据核定。上传和封存本身不保存流程、不生成正式资产，也不发送桌面输入。

## 流程保存和读取

workflow.save 保留小内容 definition/dependencies，增加互斥的 modelRef 分支。两种形式解析为同一流程模型，使用同一静态校验、依赖授权、draftVersion/baseRevision 比较和原子保存。客户端不能同时传两种来源，服务端重验引用实际内容中的流程身份、依赖与摘要。

workflow.read 支持 contentMode=auto/inline/resource，省略为 auto。协商 contentRefsV1 后，模型大于 256KiB 或完整回复不能放入控制帧时，以 modelResource 返回含 definition/dependencies 的不可变 JSON；definitionHash 仍覆盖实际流程定义，不是句柄或整个资源文件的摘要。指定 inline 且超限时明确拒绝。

资源化结果通过 resource.read 分块读取。每块核对 offset、实际块长度、总长和摘要，全部完整后解析并建立工作台模型；取消读取只丢弃未完成的新模型，原草稿继续保留。内容身份不匹配不能显示为加载成功。

## 运行快照和诊断

run.snapshot 返回 frameCount、effectCount、framesQuery 和 effectsQuery，两个集合固定到同一 dataVersion。调用帧与输入效果通过 query.read 执行条件筛选和游标分页，每页最多 100 项；详情与异常见[分页查询](pagination.md)。完整流程模型、轨迹和导出文件继续使用内容引用与字节分块。

变量另用 debug.variables.read 的固定分页和单值资源。诊断和其他有界列表在完整信封预算内输出；不能完整返回的结果必须采用其规定的资源分支或明确返回 RESPONSE_TOO_LARGE，不能仅用 pageSize 判断字节是否足够。草稿继续使用现有受限内容引用规则。

## RFC 8785 编码

公共参数、检查点与输入段摘要使用 RFC 8785 JCS 的 UTF-8 字节计算 SHA-256。数字按 ECMAScript 二进制双精度规则序列化，包括负零、指数阈值和最短往返表示；对象键按 UTF-16 代码单元排序，数组保持原顺序。重复键、孤立代理码点、非有限数和不合法 UTF-8 拒绝。业务格式中的安全整数范围仍单独验证。

Python 文档检查使用固定版本 rfc8785；JavaScript 独立使用严格解析、UTF-16 排序和原生数值序列化。两者重验[官方数值及边界向量](../../examples/canonical/vectors.json)。字节型内容资源和 PNG 的摘要始终基于原始字节，不能换成重序列化结果。更新签名继续使用其独立且限定整数的签名字节规范。

## 验收

Q45 覆盖 1MiB 边界、合法大模型、分块乱序/重发、缺块、篡改、到期、跨用途、取消与原子保存。Q46 覆盖 RFC 数值表、整值浮点、负零、指数、Unicode 键、重复键及三语言实现阶段的共同字节；当前工具验证 Python 与 JavaScript，Rust 在组件实现后接入。

官方规范：[RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html)。
