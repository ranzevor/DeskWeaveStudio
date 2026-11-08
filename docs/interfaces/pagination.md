# MCP 分页查询

## 接口范围

对象目录、关联记录、诊断和批量作业结果使用服务端分页查询。筛选、排序和读取范围由 Engine 执行；每次请求只读取当前页。单个流程模型、输入轨迹和导出文件保持完整版本，通过内容引用和分块读取取得文件字节。

[查询契约](../../contracts/query.schema.json)定义集合入口、查询参数、类型化记录和查询快照；[MCP 目录](../../contracts/mcp-catalog.json)定义实际发布的工具及来源权限。

| 来源 | 返回的集合入口 | 查询类型 | 支持的筛选 |
|---|---|---|---|
| workflow.validate | diagnosticsQuery | diagnostics | 文本、诊断代码、节点 |
| run.prepare | requirementsQuery | runRequirements | 名称、参数/变量/目标、是否满足 |
| run.prepare | diagnosticsQuery | diagnostics | 文本、诊断代码、节点 |
| run.snapshot | framesQuery | runFrames | 调用状态、父调用、节点 |
| run.snapshot | effectsQuery | runEffects | 输入效果状态 |
| template.names | itemsQuery | templateNames | 名称、命名来源、是否冲突 |
| template.commit 的最终结果 | assetsQuery | templateAssets | 名称、候选 |
| vision.match 的最终结果 | candidatesQuery | matchCandidates | 最低分数、尺度 |
| asset.import 的最终结果 | objectsQuery | importedObjects | 对象类型、是否复用 |
| ocr.status | installedLanguagesQuery | ocrLanguages | 语言名称 |

以上来源返回数量、状态和集合入口。集合入口包含 collectionId、kind、itemCount、expiresAt。collectionId 定位已登记的结果集合；每次读取仍核对来源对象和实际客户端。

operation.get、operation.cancel、request.status 及订阅快照沿用同一结果结构。重复查询原作业返回原集合入口，不重新执行匹配、命名、导入或输入。

请求重查在返回查询页前重新检查读取权和快照期限。已关闭或过期的查询返回 CURSOR_EXPIRED，撤权返回 ACCESS_DENIED；不从请求结果缓存重新开放旧页面。来源对象更新的通知只携带身份与版本，客户端根据当前条件创建新查询。

## 既有查询

| 工具 | 查询范围 | 每页默认 / 上限 | 固定范围 |
|---|---|---|---|
| deskweave_catalog | 流程、资产、运行目录 | 50 / 100 | 工作空间、对象类型、名称、状态与排序 |
| deskweave_template_candidates | 指定版本的候选 | 50 / 100 | 候选集、版本与最低分数 |
| deskweave_observation_query | 观察记录 | 50 / 100 | 会话、时间、状态与排序 |
| deskweave_asset_references | 资产或单版本引用 | 50 / 100 | 原资产范围与可见引用 |
| deskweave_workflow_revisions | 流程历史版本 | 50 / 100 | 流程与版本元数据 |
| deskweave_debug_variables_read | 脱敏变量 | 50 / 100 | 运行、调用帧、范围与数据版本 |

workspace.catalog 的 nameContains 为 NFC 归一化后的名称子串，states 只筛选运行目录，允许运行状态枚举；流程与资产目录使用空数组。空数组查询全部允许状态；未知状态返回 INVALID_ARGUMENT。order 为 updatedDesc、nameAsc 或 nameDesc，相同排序值以对象身份作为稳定次序键。名称匹配忽略大小写，不解释通配符、表达式或 SQL。

template.candidates 在固定候选版本中先应用 minimumScore，再按评分、位置与候选身份排序。observation.query 的时间为 UTC，查询区间左闭右开，排序以时间和记录身份共同确定。记录查询每页返回同一 querySnapshot、snapshotAt 和 expiresAt。

## 集合查询工具

`deskweave_query_read` 对应 query.read：

| 参数 | 含义 |
|---|---|
| collectionId | 来源回复中的集合身份 |
| kind | 与来源入口相同的集合类型 |
| filter | 当前类型允许的条件，默认空对象 |
| order | asc 或 desc，默认 asc |
| cursor | 首次省略或 null；续页使用上一页 nextCursor |
| pageSize | 整数 1～100，默认 50 |
| includeTotal | 是否计算当前条件下的准确总数，默认 false |

filter 根据 kind 校验。未知字段、错误类型和不适用于当前集合的条件返回 INVALID_ARGUMENT。布尔条件 false 与省略条件分别处理。数组条件去重并规范排序；省略值与其默认值具有相同查询含义。

返回字段为 collectionId、kind、querySnapshot、items、matchedCount、countState 和 nextCursor。每条 items 为 index 与 value；value 按集合类型使用具体记录结构。index 是来源集合固定次序中的序号，筛选和翻页不重新编号。

asc 按固定序号递增，desc 递减。来源序号按以下规则建立：诊断按节点、字段和诊断代码；运行要求按类型和名称；调用帧按创建次序；输入效果按输入段接收次序；命名和资产批次按固定选集次序；匹配候选按评分降序、位置和尺度；导入对象按固定清单次序；语言按规范语言名称。

流程诊断中，nodeId 标识当前定义中合法且唯一的节点；nodes 数组成员下的结构错误和语义错误使用相同的归属规则。流程级错误、节点身份缺失、无效或重复时省略 nodeId，保留 fieldPath。filter.nodeId 只匹配已归属诊断，matchedCount 统计当前筛选条件下的匹配数量。

诊断 fieldPath 沿用[本机协议的字段路径](local-protocol.md#错误与兼容)约定，完整保留长属性名、空属性名、Unicode 和 JSON Pointer 转义。字段路径不设置独立字符截断；排序、定位与复制均使用完整路径。单条诊断无法放入页面时返回 QUERY_ITEM_TOO_LARGE，初次查询登记回滚；集合仍可主动关闭。

查询页最多 100 项，完整 result（包含查询元数据、数量与下一游标）按 RFC 8785 编码最多 256KiB UTF-8，由方法 Schema 的 x-maxUtf8Bytes 与共享分发器执行，并遵守 1MiB 控制帧预算。读取按页面预算提前结束时，nextCursor 指向最后实际返回成员之后。服务端使用索引和一个额外成员判断是否还有后续数据。

nextCursor 为 null 表示末页。空结果返回 items=[]、nextCursor=null。客户端根据 nextCursor 判断是否继续，不能用本页不足 pageSize 判断末页。

matchedCount 为当前条件与读取权限下的准确数量，countState=exact；未计算时返回 null 和 notComputed。includeTotal=true 要求在查询预算内完成准确计数，超时返回 DEADLINE_EXCEEDED。缓存计数必须绑定同一来源版本和规范化条件。首屏读取可以直接使用已有计数，或在不计算总数的情况下返回页面。

## 快照与游标

首次查询在一致读取点固定来源版本、读取者、筛选条件与排序，返回 snapshotId、snapshotAt、expiresAt 和 sourceVersion。普通查询最多有效五分钟；运行调用和输入效果查询最多六十秒，且均不超过来源集合的剩余期限。翻页不延长期限。

游标为不透明值，最多 256 个字符，绑定工作空间、实际客户端、授权版本、collectionId、kind、规范化 filter、order、pageSize、快照和最后返回序号。客户端不能用 offset、页码或自制序号替代游标。重复使用同一游标，在权限与期限有效时得到相同页面。

新增、更新和删除不会插入或重排既有查询的成员。筛选、排序、每页数量或来源改变后，客户端清空游标并创建新查询。已有页面保留自己的快照标识，不与新查询结果合并。

run.snapshot 的 framesQuery 和 effectsQuery 固定到同一 dataVersion 和 lastSeq。后续调用帧与输入效果查询使用该来源版本；当前运行继续推进时，旧查询保持原成员。调用帧列表只返回定位与状态字段，输出值通过 debug.variables.read 的 outputs 范围读取。

游标和快照逐页重新检查权限。撤权立即停止读取；授权版本变化使旧游标失效。不存在或不属于当前读取者的集合统一返回 COLLECTION_NOT_FOUND。失效查询不能退回为无条件查询或返回一个成功的空列表。

`deskweave_query_close` 对应 query.close，snapshotId 与 collectionId 必须二选一。snapshotId 释放当前客户端的指定快照，来源集合继续按原生命周期管理；关闭后的游标返回 CURSOR_EXPIRED。collectionId 释放当前客户端的诊断来源集合及其全部查询快照，后续读取该集合返回 COLLECTION_NOT_FOUND。两种关闭操作都幂等；不存在或属于其他客户端的目标返回 closed=true，不改变其他客户端资源，也不删除流程和业务对象。

## 服务端执行与容量

集合结果使用带稳定序号的索引记录，来源集合记录 kind、来源对象、版本、所有者、权限与到期时间。查询快照保存规范化条件、排序、来源版本和读取位置。大结果不在首次查询时整体加载到客户端或复制到每个查询。

不可变作业结果复用其原结果索引；运行与目录使用版本化索引，按读取版本固定成员。SQLite 读取事务只覆盖一次有界页面读取，查询快照通过不可变版本根或版本化记录维持，避免长期读事务阻止写入。索引至少覆盖集合/版本与稳定序号，以及状态、类型、父调用等主要筛选字段。

流程目录在创建快照的同一事务中读取版本序号上界。revision 的 sequence 与 valid_until 表示目录记录的可见区间：sequence 不大于上界，且 valid_until 为空或大于上界时可见。保存新版本在同一事务写入新记录、结束旧目录版本区间并更新流程头；定义内容和历史版本身份保持不变。name_key 保存名称的 NFC 与大小写折叠值，名称正序、倒序分别使用对应组合索引；更新顺序和版本历史使用 sequence 索引。

查询只登记版本上界、条件、授权范围及期限。每页按最后实际返回的排序键继续读取，最多取得 pageSize+1 条匹配记录，不使用 offset，也不为快照复制目录或版本成员。历史版本固定流程与头版本上界。诊断查询复用来源集合，按原始 index 筛选迭代，仅在 includeTotal=true 时计算准确总数；多个查询不复制来源条目。名称子串等条件可扫描索引中的候选项，扫描受请求时间预算约束。

每个客户端最多持有 8 个集合查询快照，每工作空间最多 32 个；同时在途的集合页查询每客户端最多 2 个。普通页面读取预算 2 秒，includeTotal=true 时总预算 5 秒。实际登记的游标位置每工作空间最多 1,000,000 条；相同快照和位置复用游标，容量按已分配的状态计算，不按查询范围内的虚拟成员数重复计费。容量不足返回 LIMIT_EXCEEDED，关闭、过期和连接结束释放所属查询及游标。首屏登记、页面读取、结果校验和完整响应编码共用一次请求事务，响应预算校验完成后提交。失败或超时回滚本次新建的快照与游标；续页失败保留既有快照和已交付游标，原游标可重试。源集合回收须先结束其查询引用；权限撤销不等待自然到期。

workflow.validate 产生的诊断集合最多有效五分钟，每会话最多八个、每工作空间最多三十二个，空诊断结果同样受限。创建前回收过期集合；达到会话限额时淘汰本会话最早创建且没有活动查询快照的集合，仅达到全局限额时从全局同类集合中淘汰最早创建者。活动查询保护其来源直到关闭或过期，全部候选均被占用时返回 LIMIT_EXCEEDED，调用方可按 collectionId 关闭旧诊断。淘汰后的集合返回 COLLECTION_NOT_FOUND；新校验创建新的集合身份。断开或撤权释放所属集合及快照。

查询页在序列化前检查记录数量、实际 UTF-8 字节、来源版本和授权。一个成员不能放入页面预算时返回 QUERY_ITEM_TOO_LARGE 及具体字段；完整文档和文件通过原内容接口读取，不把逻辑记录集合打包成资源来绕开分页查询。

## 各结果的完整性

- workflow.validate 的 valid 和 diagnosticCount 根据完整定义计算；诊断查询只改变读取范围。
- run.prepare 的 valid、requirementCount 和 diagnosticCount 根据完整启动计划计算；启动事务重验同一计划和当前目标。
- template.names 的 canCommit 和 conflictCount 覆盖完整选集；只查看无冲突页面不能使存在其他冲突的预览提交成功。
- vision.match 的 selectedIndex 是完整候选集合中的序号；selected 提供选中候选，供调用者使用对应定位结果。筛选后的页内序号不用于输入。
- asset.import 的 entryWorkflow 必须来自本次原子登记的完整导入结果；objectCount 和 objectsQuery 对应同一事务。
- template.commit 返回同一批次的 assetCount 和 assetsQuery；分页查询不再次生成资产或更改批次。
- ocr.status 的 missingLanguages 只对应本次最多 8 个请求语言，已安装语言目录通过 installedLanguagesQuery 查询。

## MCP 发现与资源

tools/list、resources/list、resources/templates/list、prompts/list 使用 MCP cursor 和 nextCursor，每页 50 项。发现游标绑定认证客户端、工作空间、授权版本、目录版本和发现方法，有效五分钟。最后一页按 MCP 结构省略 nextCursor。新版发现页增加 resultType=complete、ttlMs=0、cacheScope=private；旧版页使用原响应结构。游标同时绑定协议版本，不能在新旧适配间混用。目录版本改变或游标过期后从首页重新发现；重复页不遗漏或增加工具。

资源 URI 的 cursor、pageSize 和筛选参数传给同一 Engine 查询。读取较大文件仍使用 resource.read 的 offset/length 字节分块；每块核对原资源权限，JSON 资源引用不改变集合查询的来源权限。

## 客户端交互

查询页显示当前筛选、排序、每页数量和本页结果，提供查询、下一页、返回已读取页及重新查询操作。修改条件后保留输入并重置页位置。总数未计算时显示本页条数与是否有下一页；查询中禁用重复翻页，失败保留当前页和重试入口。

编辑器替换校验结果时，按 collectionId 关闭不再使用的诊断集合；未打开分页的结果也可直接关闭。

请求关联条件代次与查询快照。旧条件的迟到结果被丢弃；取消只结束当前页读取。查询过期后保持筛选输入，重新查询从第一页开始，并清空旧游标与页面缓存。界面只缓存已实际读取的页面，返回上一页时核对其快照与有效期。

## 验收

Q49 覆盖九种集合、既有列表、协议发现与资源查询，检查默认数量、最大页、首末页、空结果、布尔筛选、稳定排序、重复游标、条件改变、并发变更、权限撤销、过期、主动关闭、字节预算及迟到结果。批量命名、匹配选择和导入事务同时核对整体状态与分页内容。

结构与关联样例位于 [examples/queries](../../examples/queries/first-page.json)，检查入口为 `python tools/check_docs.py --contracts`；交互入口见[集合分页查询](../prototypes/index.html#mcp-query)。
