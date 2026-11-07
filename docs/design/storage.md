# 数据与一致性设计

## 存储布局

程序目录只保存版本化二进制与资源。用户配置和 Agent 最小操作账位于用户应用数据目录。工作空间采用本地目录：

```text
workspace/
  workspace.json
  data/workspace.db
  objects/sha256/
  staging/
  recovery/
  exports/
```

workspace.json 保存格式版本、工作空间身份和创建信息，不保存凭据。活动 SQLite 数据库使用本地文件系统；网络盘和同步目录可作为导入导出位置，不作为首期活动库位置。

## 逻辑表

| 表 | 主要字段及约束 |
|---|---|
| workflow | id、name、head_revision、archived_at |
| revision | id、workflow_id、parent_id、definition_hash、dependency_manifest_hash、created_at、sequence、name_key、valid_until；目录版本有效区间与头版本同事务更新 |
| draft | editor_id、workflow_id、owner_principal、base_revision、edit_version、source_hash、model_hash、model_edit_version、validation_state、diagnostics_hash、saved_at；editor_id+workflow_id 唯一 |
| draft_tombstone | editor_id、workflow_id、discarded_version、request_key、discarded_at、retain_until；防止旧写入复活已放弃草稿 |
| editor_layout | editor_id、workflow_id、layout_version、layout_hash、updated_at；包含节点位置、视口和折叠状态 |
| asset | id、kind、display_name、current_version、deleted_at |
| asset_name_claim | namespace、name_nfc、asset_id；前两项唯一，登记与资产同事务 |
| asset_version | id、asset_id、content_hash、media_type、metadata；轨迹 source 随内容保存，用于坐标编辑与回放 |
| recording_session | id、owner_client_id、source_hash、topology_version、state、control_version、event_count、duration_us、partial、reason；控制录制与分段收尾 |
| object_ref | owner_kind、owner_id、content_hash；受引用对象不回收 |
| run | id、snapshot_hash、status、owner_session、active_ms、wall_deadline、cleanup_result |
| frame | id、run_id、parent_id、workflow_revision、state、cursor、outputs_hash |
| run_event | run_id、seq、kind、occurred_at、payload_hash；run_id+seq 唯一 |
| command_receipt | client_id、request_id、method、authorization_ref、payload_hash、operation_id、state、result_hash、retain_until；前两项唯一 |
| operation | id、method、owner_client_id、authorization_ref、params_hash、state、state_version、effect_stage、checkpoint_hash、result_hash、error_hash、cancel_requested_at、created_at、finished_at |
| object_event | scope、object_id、seq、kind、payload_hash、occurred_at；scope+object_id+seq 唯一 |
| effect_intent | segment_id、run_id、node_id、attempt、target_version、state、agent_receipt |
| model_connection | id、head_version、updated_at |
| model_connection_version | connection_id、version、endpoint、model、key_mode、options_hash、credential_ref、created_at；connection_id+version 唯一 |
| credential_binding | ref、workspace_id、generation、state、prepared_by_request、retired_at、revoked_at；仅存引用与状态 |
| connection_change | request_key、connection_id、expected_head、new_version、credential_ref、phase；记录配置准备与收尾 |
| observation_session | id、owner_client_id、owner_session_id、authorization_ref、detached、target_ref、target_generation、connection_id、connection_version、plan_hash、state、control_version、checked_count、active_operation_id、baseline_id、baseline_feature_hash、last_seq、started_at、deadline_at、stopped_at、reason |
| observation | id、session_id、operation_id、run_id、connection_id、connection_version、checked_at、captured_at（可空）、completed_at、frame_id、frame_hash、state、skip_reason、difference（可空）、result_hash、duration_ms、usage_hash、baseline_id、deleted_at |
| query_snapshot | id、owner_client_id、authorization_version、source_version、filter_hash、sort_order、created_at、expires_at；固定索引版本与查询范围 |
| query_cursor | token、snapshot_id、position；snapshot_id+position 唯一，position 为最后实际返回的排序键 |
| template_candidate_set | id、source_ref_hash、source_hash、roi_hash、options_hash、candidate_manifest_hash、version、total、truncated_count、state、expires_at |
| template_name_proposal | id、candidate_set_id、candidate_version、selection_hash、policy_hash、proposal_hash、items_hash、state、expires_at |
| template_batch | id、proposal_id、manifest_hash、committed_at；proposal_id 唯一 |
| run_variable | run_id、version、values_hash；按预期版本比较提交 |
| start_plan | id、snapshot_inputs_hash、target_generations_hash、expires_at |
| pause_record | run_id、pause_id、agent_epoch、generation、checkpoint_hash、input_target_token、held_inputs_hash、target_state_hash、expires_at；原生游标由存活 Agent 持有 |
| resume_plan | id、run_id、pause_id、control_version、restore_target_token、plan_hash、expires_at、state、consumed_by_request；状态为 prepared/consumed/cancelled/expired |
| breakpoint_profile | id、workflow_id、revision、version、entries_hash、updated_at；workflow_id+revision 唯一 |
| import_plan | id、source_manifest_hash、staged_input_hash、catalog_version、mapping_hash、expires_at、state |
| import_mapping | import_id、source_key、source_kind、source_id、source_version、source_hash、target_id、target_version、target_hash、disposition；import_id+source_key 唯一 |
| bridge_token | id、name、verifier_ref、workspace_id、permissions_hash、targets_hash、created_at、expires_at、last_used_at、revoked_at、impact_version |
| migration | version、input_version、state、checkpoint、completed_at |

历史版本和运行依赖不可变。head 指针和可变索引通过版本比较更新，修改失败不覆盖另一个编辑会话的结果。

## 提交顺序

1. 内容写到同盘 staging，检查结构、大小与 SHA-256，刷新文件。
2. 原子发布到对象存储，已存在摘要只复用相同字节。
3. 数据库短事务提交元数据、引用和关键事件。
4. 提交后发送回执与通知。UI 按已提交的编辑代次更新保存状态。

中断可能留下未引用对象，由延迟回收处理。缺失已引用对象时阻止相关加载/运行并保留故障，不创建空内容替代。文件替换结果不能确认时检查目标摘要再决定是否重试。

Engine 使用唯一写入队列和有限数据库等待，启用外键及唯一约束。WAL 仅在实际运行库完成版本核查后启用；构建矩阵固定含相关并发修复的 SQLite。重要版本和效果记录采用可靠持久化设置，普通进度可合批。

## 草稿与冲突

每个编辑会话保留 source、最后有效 model 和 editVersion。500ms 防抖调用受限本地 draft.write，源码与模型引用在同一事务提交；保存期间继续输入时，回执只确认旧代次。非法源码仍可保存为草稿，不进入 workflow.save 的正式版本路径。

draft.read 按工作空间、流程及授权的 editorId 读取草稿；editorId=null 分页发现可恢复分支。每份快照提供原文和模型的私有资源、已确认 editVersion 及 Engine 计算的诊断，读取不自动运行流程。draft.discard 按 expectedEditVersion 删除当前草稿引用并登记墓碑；旧保存请求不能复活它。

自动保存、显式版本和布局独立。第二个窗口默认只读或创建分支草稿。外部修改和服务重启后重新比较版本，文件监听通知不能替代内容比较。

关闭时取消防抖并冲刷当前编辑，等待相同文档和窗口代次的持久化确认；写入失败保留内存草稿并允许导出。保存正式版本、保留非法源码草稿与放弃草稿分别等待其对应回执，不能互相替代。满盘、权限变化和占用只有限重试，原有数据保持可读。

## 便携包

包由 manifest、流程、子流程和资产组成，结构见 [package.schema.json](../../contracts/package.schema.json)。manifest 为每个对象提供逻辑键、相对路径、类型、长度和摘要，entry 指向主流程逻辑键。

导入先在隔离 staging 完整验证：条目不重复，无绝对路径、上级路径、链接逃逸和大小写冲突；原始路径逐段拒绝空段、点段、冒号、控制字符、Windows 设备名和尾随点/空格，不依赖路径库归一化后再检查；解压总量最多 1GiB、10,000 项，单项压缩比最多 100:1；长度、摘要、Schema 与引用闭包通过后才注册正式对象。

导入前固定原始字节，逐项验证 sourceSha256，再分配整个闭包的目标身份。流程默认创建独立 workflowId 与正式 revision；图像和轨迹只在类型、实际字节及元数据相符时复用现有不可变版本。同名不同内容生成明确的新对象名称，不覆盖已有版本。导出固定版本，不导出凭据、无关运行历史或临时采集帧。

包内清单 key 是全局身份；每份流程 resources 中的 key 是该流程本地逻辑键。流程条目可声明 bindings，将本地逻辑键映射到包的全局 key；存在 bindings 时必须准确覆盖全部声明依赖且类型一致，省略时沿用同名全局 key 解析。多个流程可把相同本地键绑定到不同全局资源。节点、表达式和字符串中的本地资源键保持不变；只改写流程顶层 id 和正式依赖绑定，禁止全文件字符串替换。

sourceId、sourceVersion 表达来源身份而不是本地写入位置；声明的 workflow sourceId 必须与原始定义 id 相同。同一来源对象不同版本可在不同全局 key 下同时出现，映射以 sourceKey 区分。Engine 先为有限依赖闭包预分配目标 id/version，再写入带新身份的目标定义，重新计算 targetSha256；sourceSha256 始终保留原始字节摘要。目标版本依赖清单中的流程项使用 workflowId/revision，图像与轨迹项使用 assetVersion，均引用本次预分配或复用的目标身份与版本，不能意外指向同名现有流程。

本地 asset.import.prepare 冻结已验证源字节，返回限时计划、目录代次和完整映射资源；mappingResource 封存完整 importMapping 数组，mappingSha256 对应其实际字节；previewMappings 只提供最多 100 行，mappingCount 表示完整数量。正式导入使用同一计划与 staged 字节，不能重新读取可变路径替换已审阅内容。改选来源、目录代次变化、命名占用冲突或计划过期时重新预览，不静默调整映射。外部 asset.import 未指定计划时在同一作业内执行相同规则，并在完成清单返回实际映射。

所有目标定义、依赖、名称占用和 import_mapping 在一次短事务中提交；计划预分配不构成正式登记。失败只留下可回收 staging，不出现部分可见的已导入流程。结果 manifestResource 指向 package Schema 的 importManifest，包含完整来源到目标映射、源清单摘要、实际入口目标及完成时间；objects 按目标类型、身份和版本去重，entryWorkflow 是同一事务入口的投影；catalogVersion 记录本次准入核对的目录代次。

### 单文件导入

asset.import.prepare 与 asset.import 的 image、trajectory、workflow 模式接收一个原始文件。固定 sourceKey 为 input；sourceManifestHash 与唯一映射的 sourceSha256 都是原始文件字节 SHA-256，不能对重新序列化后的 JSON 计算。单文件图像与轨迹的 sourceId/sourceVersion 为 null；流程的 sourceId 取原始定义 id，sourceVersion 为 null。entryWorkflow 仅在 workflow 模式指向新目标流程，其余为 null。

独立流程文件只包含自身定义，resources 必须为空；声明外部依赖时返回 IMPORT_DEPENDENCIES_REQUIRED，并要求导入包含完整闭包的任务包。不能按本地同名对象猜测绑定。图像、轨迹仍执行实际内容与元数据核验及精确复用；流程修改顶层身份后重新计算目标字节摘要。两阶段计划、暂存源、目录版本比较与原子登记沿用相同规则。

## 备份与恢复

备份暂停回收，取得数据库一致快照，枚举其中引用的不可变对象，复制并校验后写入完成清单。不能单独复制活动数据库主文件充当完整备份。

恢复先到新目录，检查数据库完整性、对象摘要和工作空间身份，再切换注册位置。原件和未完成恢复目录保留到结果确认；恢复不会自动恢复 Agent 租约、已过期客户端令牌或活动输入。

## 演进与删除

迁移取得工作空间独占维护权，创建一致备份并登记输入版本；按可重入阶段执行，验证后提交新版本。旧程序遇到更高格式版本只提供兼容读取或拒绝写入。

程序回退与数据回退分别处理。无法向后读取时恢复到新目录，升级后新增内容保留可导出。禁止通过覆盖原数据库实现快速回滚。

用户流程和资产默认不按时间删除；运行附件默认建议保留 30 天且最多 5GiB，由用户确认。asset.delete 按 assetId 删除整个逻辑资产及全部版本，expectedCurrentVersion 拒绝过时确认。任一版本被不可变流程版本、草稿依赖、运行或调用帧、有效模板候选/命名计划/提交、观察基线/查询快照、导出、备份恢复、在途作业或有效读取句柄强引用时，返回 ASSET_REFERENCED，保留全部版本与名称；不能只删除无引用的部分版本。引用清单按 asset-management Schema 的 ownerKind 返回实际占用对象。

删除前使用 asset.references 的 assetId 分支，在一致事务中固定全部资产版本、当时 currentVersion 和引用成员；单版本分支仅供查看指定 assetVersion 的使用者。引用查询快照固定五分钟，游标绑定查询范围与当前管理客户端，头版本或引用后来改变时仍保留原快照供分页，但删除提交必须比较 currentVersion 并重查当前全部引用。元数据引用清单不为被查询资产创建新的内容使用 pin，不因查询自身造成永久不可删除。

相同内容摘要被另一个资产复用不等于当前逻辑资产仍被引用；没有逻辑引用时删除索引、全部版本关系和名称占用，在同一事务保存删除墓碑与请求收据。物理 blob 仅在全局强引用归零后回收。界面发起删除前释放自身预览句柄，其他使用方的句柄保持有效且继续阻止删除，不强行关闭。删除不附加回收区或恢复承诺。

## 模板与观察索引

候选集和命名预览是有期限的暂存对象，template_batch 与资产版本在同一事务登记。一个预览只能提交一次，重复请求读取原批次。资产名称在事务内按 NFC 归一化再次检查并占用唯一键；预览后的新增占用使原预览失效，不在提交时静默改名。过期或取消的暂存延迟回收，已登记资产不随候选集过期删除。

观察记录以 checked_at、id 建立稳定查询次序，按 session_id/state 与相同排序键建立组合索引。captured_at 只记录实际采集，不能作为无采集检查的排序替代。分页固定有期限的记录索引版本，通过 checked_at、id 排序键继续读取；记录内容与删除状态使用版本区间，不为每个查询物化成员，不跨请求持有 SQLite 读事务；截图与结果分别持有引用；持久化失败保留恢复对象并返回对应状态，不能创建没有实际内容的成功记录。

连接版本、运行共享状态及启动计划都使用内容摘要固定实际输入。新运行不读取另一运行的可变状态；令牌验证材料与模型凭据使用独立秘密存储，备份和导出按其实际权限处理。

草稿读写、冲突、关闭与恢复，以及作业状态与回执、模板占名、观察与基线、快照删除、连接凭据的事务边界和重启核定见[持久化事务](persistence-transactions.md)。

## 实时暂停与配置留存

pause_record 记录摘要和失效原因，原生事件游标只存在于存活 Agent。Engine 重启后将活动输入核定为 interrupted 或 uncertain，不能用持久化检查点恢复已发生的桌面事件。resume_plan 默认 60 秒过期；一次性确认能力不写入普通数据库、导出或日志。请求收据保留既有期限，计划被消耗后仍可供原请求读取结果，新请求不能重新使用。

断点配置按正式根版本独立保存，完整数组以 expectedVersion 比较更新；空初始配置版本为 0，首份持久更新为 1。运行准入复制精确配置档版本、有限子流程闭包和内容摘要并持有强引用；编辑配置只影响新运行。移除流程版本前检查配置与运行引用，历史运行的固定快照按运行保留周期释放。


## 引用查询与撤销影响台账

| 关系 | 字段与事务约束 |
|---|---|
| asset_usage | owner_kind、owner_id、asset_version、authorization_ref；前三项唯一，直接关联逻辑资产版本，创建/释放与所属对象同事务，不能仅以content_hash替代 |
| asset_reference_snapshot | id、owner_client_id、asset_id、scope、asset_version、head_version、version_count、total_refs、hidden_refs、created_at、expires_at；固定一次查询范围与五分钟期限 |
| asset_usage_version | owner_kind、owner_id、asset_version、display_name、valid_from、valid_until；共享引用元数据索引，各查询固定版本并按关系排序键续页，不新增资产内容使用 pin |
| bridge_token_object | token_id、object_kind、object_id、state；前三项唯一，关联会话、订阅、直接许可和根运行；对外开放前登记，确认收拢后移除活动计数 |
| bridge_token.impact_version | 对该令牌归属台账的注册、状态、删除及令牌状态变动递增；与对应变动同事务 |
| token_impact_snapshot | id、owner_client_id、token_id、impact_version、token_state、counts、hidden_run_count、created_at、expires_at；固定最多60秒 |
| token_impact_run_version | token_id、impact_version、run_id、display_name、state、control_version、valid_until；共享影响版本索引，按 run_id 续页，逐页检查读取权 |

资产引用页按 (owner_kind, owner_id, asset_version) 排序和去重，total_refs 包含所有可见与不可见关系；version_count 是该逻辑资产当时全部版本数，单版本查询也返回同一资产的当前头。相同所有者引用两个版本是两条关系。全对象删除事务检查 asset_usage 全集合与当前头，不能把分页查询截断或不可见成员当成不存在。

令牌影响页固定的counts与运行成员必须来自同一impact_version；资源许可计数表达登记的实际生命周期，不能从请求次数推算。撤销先使授权失效，后续网络收拢与输入清理分别确认；状态快照只用于展示，不持有控制权或延长任何令牌/租约。查询和其成员到期独立回收；快照缺失不能阻止对真实令牌发起撤销。具体权限、分页与失效语义见[MCP设计](../interfaces/mcp.md)。

## 制作与内容索引

content_upload 保存创建者、用途、声明长度/摘要、连续偏移、块摘要、期限和封存状态；实际暂存容量有界。asset_edit 固定来源字节和目标头，asset_version 保存源摘要、派生元数据、实际图像尺寸或轨迹时长。variable_inspection 固定dataVersion、脱敏投影根及权限；revision_query 与 connection_query 固定版本上界、条件及期限，复用已有版本索引。阶段、资源及事务见[内容传输](../interfaces/content-transfer.md)、[制作事务](authoring-transactions.md)及[运行检查](runtime-inspection.md)。

## 查询索引

result_collection 保存 collection_id、kind、来源对象、来源版本、版本根、所属客户端与期限；result_collection_row 使用 collection_id+ordinal 唯一，按类型记录状态、名称、对象类型、父调用或评分索引。query_snapshot 保存过滤条件、排序、读取者、授权版本和期限；query_cursor 绑定快照、页大小与最后返回的排序键；只有不可变结果来源保存一次稳定序号，各查询不复制来源行。查询仅固定索引版本和必要引用，容量和并发限制见[分页查询](../interfaces/pagination.md)。导入正式对象和 objectsQuery 的来源索引在同一事务登记，entryWorkflow 必须属于该集合。
