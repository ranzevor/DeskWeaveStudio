# 持久化事务与恢复

## 对象与提交边界

工作空间数据库保存对象关系、状态和回执；不可变内容保存于摘要对象存储，凭据材料保存于本机秘密存储。本文细化[数据设计](storage.md)中模板、观察、作业、查询快照和连接版本的提交边界。

进入数据库事务前完成有界内容计算和同盘对象发布，事务中只登记引用、检查预期版本、更新状态并写入事件。文件已经写入不等于对象已经登记；只有事务提交后的回执确认正式结果。所有事件负载和结果按原方法的 Schema 校验。

| 对象 | 键、约束与索引 |
|---|---|
| operation | id 为主键；按 owner_client_id/created_at 和 state/created_at 查询；method 与 params_hash 在准入后不变 |
| command_receipt | (client_id, request_id) 唯一；绑定 method、authorization_ref、payload_hash 与 operation_id，按 retain_until 清理 |
| object_event | (scope, object_id, seq) 唯一；同对象状态和事件在一个事务提交 |
| template_candidate_set | id 主键；按 (state, expires_at) 回收；固定来源、区域、选项和候选清单摘要 |
| template_name_proposal | id 主键；外键引用候选集；candidate_version、选集、策略、实际名称和预览摘要共同固定 |
| template_batch | proposal_id 唯一；批次、资产版本、名称占用与引用在一个事务登记 |
| asset_name_claim | (namespace, name_nfc) 唯一；asset_id 外键关联正式资产 |
| observation | id 主键、operation_id 唯一；(checked_at, id)、(session_id, checked_at, id)、(state, checked_at, id) 支持过滤和稳定排序 |
| observation_session | id 主键；(state, deadline_at) 查待收尾会话；连接引用使用 (connection_id, connection_version) 外键 |
| query_snapshot | id 主键；按 owner_client_id/expires_at 查找和释放；filter_hash、sort_order 创建后不变 |
| query_cursor | (snapshot_id, position) 唯一；position 为最后实际返回的排序键，查询引用固定来源索引版本 |
| model_connection_version | (connection_id, version) 唯一且不可变；当前头通过外键引用已完成准备的版本 |
| credential_binding | ref 唯一；保存工作空间绑定、generation、state 和配置意图，材料不进入数据库 |
| connection_change | request_key 唯一；保存 expected_head、新版本、材料引用和 phase，供本地事务收尾 |

同一工作空间内的时间均保存为 UTC。运行中的耗时仍使用单调时钟；排序键不能使用可空的实际采集时间替代检查时间。执行调用栈的 frame 与图像采集帧分别处理，frame_id 的资源登记必须固定图像摘要、目标代次和采集时间。

## 草稿读写、关闭与恢复

草稿服务仅供通过认证的 Tauri Host 文档会话调用。editorId 由受保护的文档登记路径分配，不能单独作为授权；Engine 同时检查工作空间、流程写权限、窗口与连接代次。draft.read/write/discard 不加入 MCP 工具或 CLI 命令，也不允许借通用方法转发调用。

| 方法 | 参数与结果 |
|---|---|
| draft.read | workflowId、editorId（可空）、cursor、pageSize；返回 items 和 nextCursor。editorId=null 发现恢复分支，指定身份时只读该草稿 |
| draft.write | workflowId/editorId、expectedEditVersion、editVersion、baseRevision、source、lastValidModel、modelEditVersion；返回实际提交的 DraftSnapshot |
| draft.discard | workflowId/editorId、expectedEditVersion；返回 discarded 和确认的 editVersion，未存在时为 false/null |

字段见[草稿契约](../../contracts/draft.schema.json)。source 为原文，允许 JSON 语法或流程结构不合法。lastValidModel 的内容包含历史有效 source、definition 及 dependencies；Engine 重新解析该历史 source、拒绝重复键与非有限数，校验 definition 的流程结构，并比较两者的 JSON 值和 workflowId。不能仅凭客户端声明模型有效。这里的有效结构用于恢复编辑；图可达性、资源闭包和执行预算仍由正式保存与运行检查。

当前 source 能形成结构有效的定义时，lastValidModel 必须等于本次解析定义，modelEditVersion 等于 editVersion；当前 source 非法时，保留独立验证过的历史模型且 modelEditVersion 小于 editVersion。此前尚无有效模型时可为 null，此时 modelEditVersion 也为 null。已有有效模型不能被一次非法源码写入静默清空。validationState 和 diagnostics 由 Engine 计算，不接收客户端传入的 valid 字段。

### 内容通道与字节校验

小型文本或模型可 inline 传入；Host 按实际编码后大小决定是否装入控制帧。较大内容通过 Host 到 Engine 的私有 draft-content 资源连接暂存，返回绑定工作空间、editorId、窗口代次、角色（source/model）和当前连接的 contentRef。该通道不借用 Agent 轨迹上传，不接受网页提交任意文件路径。

资源头固定内容类型、实际长度与 SHA-256，分块上限 4MiB，source 与模型合计最多 32MiB；严格验证 UTF-8、累计实际字节、摘要和资源用途，全部完成后才封存为只读内容。闲置暂存默认 60 秒过期。draft.write 再检查引用所属文档及实际字节；别的窗口、工作空间或用途的句柄不能提交。快照的私有 sourceResource/lastValidModelResource 也只向对应恢复或编辑会话开放。诊断预览最多 100 条，diagnosticCount 保留实际总数，超过预览上限时必须提供完整 diagnosticsResource。draft.read 按实际控制消息字节预算返回不超过 pageSize 的成员，后续成员通过 nextCursor 继续读取；不把预览或单页误作完整结果。

### 写入事务与冲突

1. expectedEditVersion=null 只用于不存在且没有有效放弃墓碑的新草稿；已有草稿必须精确匹配其已提交代次。
2. editVersion 必须大于 expectedEditVersion，modelEditVersion 不能大于 editVersion；两者比较以整数值进行，不把字符串排序当版本比较。
3. 校验源码和独立历史模型，发布所需的不可变内容；随后在同一短事务内复查授权及预期代次，更新 source/model/模型代次/诊断引用、baseRevision、editVersion、saved_at 和请求回执。
4. 事务未提交时旧草稿和旧引用保持可用；提交后才释放不再使用的引用。相同请求重发读取原回执，同键异参或旧代次返回明确冲突，不静默覆盖另一个分支。

每个 DocumentSession 最多一个未确认写入，后续编辑合并为下一次跟随写。回执只确认其 editVersion 和内容摘要；当前文档已继续编辑时不能标成全部已保存。恢复时从原草稿继续增加代次，第二个活动窗口需要只读或独立 editorId 分支。

### 关闭、放弃与异常恢复

关闭开始时固定本次需要保留的 editVersion，取消 500ms 计时器并立即冲刷；Host 等待原请求状态及相应内容确认。选择保存正式版本时仍走 workflow.save 的有效 definition 路径；选择保留草稿可保存非法原文；选择放弃时走 draft.discard，不能以关闭窗口事件代替成功删除。

放弃事务比较 expectedEditVersion，删除该草稿的强引用并登记 draft_tombstone 与原回执；墓碑至少保留 24 小时。Host 撤销旧编辑写能力，重新创建文档使用新的 editorId。迟到写入不得把已放弃草稿重新登记；历史正式版本和活动运行不受放弃影响。

进程异常退出后，未提交内容成为延迟清理的暂存，已提交的 source/model 对保持同一代次。重新打开时通过 draft.read 分页列出可恢复草稿，由用户选择分支后重新取得编辑能力。非法原文按原字节恢复，画布使用已独立验证的最后模型；恢复编辑不自动保存正式版本或恢复桌面输入。

满盘、权限失败或资源缺失时显示对应错误；回执无法核定时显示结果未知，并提供原请求查询入口。保留当前内存原文及最后模型，使用单独授权的位置导出副本；关闭动作等待用户完成保留或明确处置。异常窗口丢失后恢复最后已确认草稿。

## 作业准入、取消与回执

所有修改命令的收据保存原 method 和经核验的授权范围引用。request.status 按原方法校验结果结构，并重新检查拥有者、对象权限及本地输出边界；payload_hash 覆盖方法与规范参数。收据不保存 Bearer 明文，也不因原调用曾成功而绕过当前撤权状态。

长作业准入事务同时登记 command_receipt、operation、授权对象引用及 queued 事件。operation 保存 method、owner_client_id、authorization_ref、params_hash、state_version、effect_stage、checkpoint_hash、result_hash、error_hash、取消请求时间和结束时间。原请求参数包含密钥时只保存不含明文的必要配置及摘要。

相同请求键和参数读取原 operation；同键异参返回 REQUEST_CONFLICT。开始执行、发送外部请求、进入本地提交、写入终态各自保存可核定检查点。调用外部模型或 Agent 前登记派发意图，派发回执不能替代业务结果。

operation.cancel 只登记取消意图并触发收尾。作业先进入 cancelling，清理完成后进入 cancelled；已经完成的原子提交保持 succeeded，不把成功回执改成取消。取消、重复回执与迟到结果按 operation 身份及状态版本裁决，不能覆盖另一作业。

succeeded、failed、cancelled 是已核定终态。uncertain 表示实际效果尚未核定，保留查询与清理信息。已结束作业及请求去重记录至少保留 24 小时；活动作业、未决效果和仍被引用的正式结果不因达到该时限删除。容量不足时拒绝新的修改请求，不提前清除未决记录。

### 中断核定

| 作业类型 | Engine 重启后的核定 |
|---|---|
| template.extract、vision.match、本地采集 | 核对已提交候选集或帧清单。已有完整结果则读取原结果；仅有计算中间状态则以 PROCESS_INTERRUPTED 结束，不自动重新采集 |
| template.commit | 按 proposal_id 查询唯一批次并核对对象摘要；已登记则返回原批次，未登记则保留有效暂存并报告中断，不自动补做一次提交 |
| template.export、observation.export | 在原输出授权仍有效时核对 exportId、全部文件及完成清单。完整匹配才确认成功；缺完成清单报告未完成；位置无法核查则保留 uncertain，不换目录重写 |
| vision.observe | 已提交记录读取原结果；仅有已派发标记时保留 uncertain，禁止自动重发模型请求。取消或迟到结果只能结束原操作，不推进新会话基线 |
| input.submit | 按原 segment 与 Agent 台账核对段收据及释放状态；不能把请求断开当作已释放，也不能自动发送新的输入段 |
| 备份与恢复 | 核对原操作检查点、数据库一致性、对象摘要和完成清单。仅完成已登记的本地阶段收尾，不把未完成目录当成可用副本 |

能够确认未派发、未提交的操作可以明确失败；已有外部派发且结果未知的操作使用 uncertain。核定完成后更新原 operation、原回执和对应事件，沿用原 operationId。

## 模板候选与名称提交

候选集内容包含 source、sourceSha256、区域、options、稳定排序的候选清单、total、truncatedCount 和版本。候选清单保存实际矩形、像素尺寸、来源算法、评分、缩略资源及裁剪摘要。sourceFrameId 仅在帧来源时有值，图像来源固定 assetVersion。

候选源图、候选清单和命名 items 通过 object_ref 固定；单个数据库字段的摘要必须能定位实际存储内容，不能只保存无法恢复内容的计算值。提取完成时同事务登记候选集、operation 终态、引用和事件；回执丢失后读取原结果。

候选集默认 30 分钟有效。打开、命名和保存持有有期限的使用引用以防内容在操作中被回收，但不修改 expires_at。过期后不再接收新的命名或提交；已准入提交的必要引用保留到收尾。客户端退出释放使用引用，Engine 重启不延长候选期限。

命名预览固定 candidate_version、selection_hash、policy_hash、items_hash、proposal_hash 和 expires_at。模板名称按 Unicode NFC 归一化后比较，保持大小写；图像资产使用同一 image 名称空间。文件导出另行检查目标文件系统的大小写、设备名和路径规则，不能以导出规则静默修改显示名称。

提交事务依次核对：

1. 原请求键及 proposal_id 是否已经产生批次；存在时读取原批次。
2. 候选版本、预览摘要、有效期与本次实际选集是否一致，且预览可提交。
3. 全部名称的 NFC 结果及 name claim 是否仍空闲；同批次内的名称也不能重复。
4. 登记全部资产、不可变版本、名称占用、来源引用、批次清单及 operation 成功结果。

任一条件失败则整体回滚。另一个预览先占名时返回 NAME_CONFLICT，原预览失效并重新生成命名预览；不能在提交时自行添加后缀而改变已确认名称。asset_name_claim 唯一约束是最终裁决，不依赖提交前的界面检查。整对象删除提交后释放该逻辑资产的名称占用；延迟内容回收不继续占用该名称，旧引用仍按不可变身份裁决。

## 观察记录、基线与会话

每次触发先在短事务核对会话 state、control_version、授权、截止时间及 maxRecords，并占用一个 checked_count 名额、登记本次检查 operation。一次会话最多一个在途采集或模型请求；busy 检查登记跳过结果，不创建第二个模型请求。名额在失败、取消或保存失败后不退还，重连不重置计数。

正常检查取得实际帧后记录 capturedAt；没有采集时该字段为 null。checkedAt 表示本次检查触发，completedAt 表示核定结束，必须满足 checkedAt ≤ capturedAt ≤ completedAt（有采集时）或 checkedAt ≤ completedAt（无采集时）。

| 结果 | 保存条件 |
|---|---|
| busy | state=skipped、skipReason=busy；无实际帧、截图、差异值、基线或输出；模型用量为零 |
| unchanged | state=skipped、skipReason=unchanged；有实际采集帧、成功基线和 difference，output=null，模型用量为零 |
| succeeded | 有本次实际帧和结果；skipReason、difference 为 null |
| failed / cancelled | 保留实际发生的采集和调用状态；skipReason、difference 为 null，没有采集时不补造帧 |
| persistenceFailed | 内存中存在本次模型结果，但正式持久化未完成；不推进成功基线，不算作已保存结果 |

正式记录、完整记录内容引用、最后记录指针、operation 终态和对象事件在同一事务提交。仅 succeeded 事务更新 baseline_id、baseline_feature_hash 及基线配置摘要。基线配置摘要覆盖目标代次、几何、区域、连接版本和提示；不一致时不能沿用旧基线。模型失败、取消、跳过和保存失败均不推进基线。

存储失败时结束后续触发并保留当前内存结果。若能够写入恢复区域，则保存有摘要的恢复对象；若该写入也失败，只报告内存仍保留，用户可另选已授权位置保存独立恢复文件。恢复文件写完并核对摘要后，显示该恢复文件保存成功。恢复内容不作为批量正式记录导出成员，重启也不自动导入或重发模型请求。

会话行固定 owner_client_id、owner_session_id、authorization_ref、detached、目标代次、连接版本、计划、计数和 deadline_at。owner 取认证上下文。owner_session_id 指 Engine 客户端上下文，新版 HTTP 不用该字段存储 MCP 会话或 HTTP 连接。detached=false 的所有者上下文结束触发停止；普通 HTTP 请求结束或事件连接断开不触发停止。授权撤销对两种 detached 模式都生效。Engine 重启将遗留活动会话转为 failed/PROCESS_INTERRUPTED，并核定在途作业；再次观察创建新的会话。

## 查询快照、删除与保留

查询使用 checked_at 左闭右开的时间范围，排序为 (checked_at, id) 升序或降序。首次查询在同一短事务中固定来源索引版本、筛选与排序，登记 query_snapshot 和版本根引用，并读取首屏。后续页面按最后返回的 checked_at、id 排序键继续读取，每页只取所需成员及一个后继项，不跨请求保持 SQLite 读事务。来源版本根保护该版本可见的记录和附件；筛选结果不逐项复制到各查询。

快照绑定工作空间、认证客户端、筛选摘要、排序和创建时的成员集。游标只定位该快照内的位置，每次读取仍检查当前对象授权。默认 TTL 为 5 分钟，分页不续期；过期返回 CURSOR_EXPIRED，新查询创建新快照。每工作空间最多 32 个活动查询快照，实际游标位置合计最多 1,000,000 条，超限返回 LIMIT_EXCEEDED，不截断成看似完整的结果。

快照、正在导出的固定记录集、备份和故障恢复分别使用 owner_kind/owner_id 的引用。成员在其快照有效期内保持原内容；新记录不插入旧分页。显式释放、到期、所有者会话结束或 Engine 重启后，释放查询引用。导出准入时单独固定其成员，不能依赖即将到期的浏览游标继续保留内容。

逻辑删除在新索引版本设置 deleted_at，使新查询不再选择该记录；旧快照和已准入导出继续读取其固定版本，记录在该版本区间内的内容和附件关系保持。物理删除等待所有强引用释放。截图清理只移除对应附件引用，保留实际采集事实和附件清理状态，不能把资源 URI 改为另一张图。

当前观察会话固定其最近成功基线特征及必要对象；历史记录中的 baselineRecordId 只保留来源关联，不无限延长全部历史截图寿命。记录 30 天与截图 2GiB 的用户确认策略分别计算；强引用导致暂时不能回收时显示实际占用，并在触及硬容量前停止新采集，不删除仍在使用的对象腾空。

## 连接版本与凭据收尾

model_connection 保存当前头，model_connection_version 保存不可变配置。相同 (connection_id, version) 只能登记一次，不能通过更新原行改变历史会话使用的端点、模型或 options。读取指定 version 时从该版本取内容；credentialPresent 依据其引用的当前可用性计算，不读取密钥到 WebView。

连接修改按 connection_id 串行处理，同时保留 expectedVersion 比较：

1. 校验端点、消息选项及 revokePreviousCredential。keep 复用原引用且不允许撤销该引用；replace 准备新材料引用；none 不分配材料。
2. 将新材料保存到秘密存储的准备位置并核对可读取性，登记 connection_change 及待提交版本；此时不移动当前头。
3. 完成本地材料准备，再在短事务内复查原头与工作空间绑定，登记生效版本、引用、头更新和请求回执。选择撤销时同时增加原凭据 generation 并标记 revoked，触发使用它的会话停止。
4. 清理配置意图及无引用暂存材料。配置回执只有在生效版本能读取其所需材料后返回 configured=true。

第二阶段后中断时按原请求意图核对材料、版本和预期头；条件完整则完成原本地提交，否则回收准备材料并保留旧头。最终事务后中断时读取原回执，不能另造配置版本。秘密材料准备与数据库提交之间不持有长数据库事务。

revokePreviousCredential=false 的替换将旧材料标为 retired，固定旧版的活动会话和未决请求继续持有执行引用。没有当前头、活动会话或未决请求引用的 retired 材料可清除；历史配置仍可读取且 credentialPresent=false。revoked 不再准入新请求，在途请求尽力取消并保留原身份和实际结果，不能因取消失败恢复调用资格。

配置版本、材料引用身份、撤销代次和记录引用按对象关系保留。工作空间备份不带密钥；本机秘密存储与备份中的引用不匹配时返回 CREDENTIAL_UNAVAILABLE，不改用当前头或其他工作空间的材料。恢复不清除已有撤销状态。

## 验收边界

| 对应场景 | 应核对的结果 |
|---|---|
| Q02 / Q03 | 非法源码、模型来源不匹配、代次冲突、迟到回执、关闭冲刷、放弃与重启恢复；原文/模型同代次且正式版本不变 |
| Q21 / Q22 | 两个预览并发占同名、提交中断及回执丢失；只产生一个名称占用和一个原子批次 |
| Q23 / Q24 | busy 无采集、unchanged 有真实差异、保存失败和重启；时间、基线、计数、归属与终态一致 |
| Q24 / Q18 | 分页期间新增与删除、快照过期、导出固定引用和容量清理；原成员稳定且最终释放引用 |
| Q25 / Q19 | 配置准备和最终事务各阶段中断、凭据替换/撤销及备份恢复；旧头或新版本可核定，不复活已撤销材料 |
| Q13 / Q19 | 外部请求派发后断开与重启；查询原作业，未决效果不自动重复执行 |

这些场景与[验证设计](../quality/verification.md)使用相同用例身份；记录实际制品和结果时分别标明结构检查、存储集成及外部效果验证范围。

## 制作与检查事务

内容暂存、资源化保存、编辑计划、临时连接测试及检查快照按[制作与测试事务](authoring-transactions.md)、[内容传输](../interfaces/content-transfer.md)和[运行检查](runtime-inspection.md)执行。asset_edit 保存源引用、来源摘要、目标头、编辑状态和期限；asset_edit_commit 对 editId 唯一，正式版本、头更新、名称占用与作业结果同事务提交。检查快照只固定已授权的脱敏值根，不持有输入控制权。

## 集合查询事务

结果集合按来源对象和版本登记稳定序号及查询字段；不可变作业重复读取复用同一集合。query_snapshot 保存客户端、工作空间、授权版本、来源版本、规范化条件、排序、页大小与期限。目录和运行查询通过版本化索引固定成员，单次 SQLite 读事务在本页读取后结束。每页重新核对权限，取消、主动关闭、到期和连接结束释放查询引用。查询不重新执行原作业，不持有桌面输入租约。具体容量与错误见[分页查询](../interfaces/pagination.md)。
