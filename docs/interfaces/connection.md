# Engine 连接认证

连接认证内核位于 `deskweave.transport`，在本机字节帧与业务消息之间完成双向挑战、票据消费、权限绑定和会话关闭。连接包结构见 [connection.schema.json](../../contracts/connection.schema.json)；业务方法继续通过 [Engine 共享服务](../architecture/shared-services.md)执行。

原生实现位于 `deskweave.transport.native`，接口与平台核验见[原生本机通道](native-transport.md)。

## 原生端点与可信输入

原生适配器创建受保护端点，核验实际对端后，向 `ConnectionAuthority.accept` 和 `ClientConnection` 传入 `PeerIdentity`。身份包含平台、用户、登录会话、进程号、进程启动标识、安装摘要和实例 epoch。服务端要求平台、用户与登录会话一致；票据同时固定双方全部身份字段。进程号复用、进程重启和安装替换均需重新取得票据。

| 平台 | 原生适配器职责 |
|---|---|
| Windows | 命名管道 DACL、双方实际进程、用户令牌及登录会话检查 |
| macOS | 私有 Unix socket、对端凭据、进程启动标识及安装签名检查 |
| Linux | 私有 Unix socket、对端凭据、登录会话、进程启动标识及安装来源检查 |

身份采集和票据交付走受保护的原生控制通道。JSON 握手只携带实例与协商资料，不能提供原生身份、权限或 `local` 标记。原生适配器还负责 I/O 超时、定时调用 `sweep`、在 EOF／取消／写出失败时关闭连接；端点与进程生命周期见[组件生命周期](../architecture/lifecycle.md)。

## 授权与票据

`ConnectionAuthority` 绑定一个 Engine、工作空间和服务端身份。可信控制面通过 `authorize(policy, lifetime=..., not_after=...)` 登记权限、流程范围及本地编辑权限，取得 `ClientCredential`。客户端身份由服务端随机生成，恢复密钥为 32 字节；登记表只保存恢复密钥的 SHA-256。可选的 `not_after` 是同一系统单调时钟上的绝对截止；创建和修订在授权锁内检查，实际到期取它与本次有效期的较小值。共享宿主的派生客户端使用宿主到期时间作为上限。

`issue(credential, peer)` 每次生成独立的票据身份和 32 字节密钥，并绑定：

- 固定用途 `engine.control`、工作空间与稳定客户端身份。
- 当前授权版本和双方完整原生身份。
- 由服务端单调时钟计算的到期时间，最多 60 秒，且不晚于授权到期。

票据密钥和恢复凭据由可信控制面交付给指定客户端。密钥不进入握手包或普通日志，凭据与票据的对象表示也不显示密钥。客户端取得恢复凭据后，用新票据建立新会话；已保存请求继续按稳定客户端身份查询和去重。

登记表随 `ConnectionAuthority` 实例存续；`close` 撤销所有授权、票据并关闭连接。默认最多 256 个授权、256 张票据和 64 个连接，各容量可配置为 1～4096。授权有效期由控制面明确指定为有限正数；票据有效期可缩短至 60 秒以内，握手有效期可缩短至 10 秒以内。容量满时返回 `LIMIT_EXCEEDED`，准入及定期 `sweep` 回收过期条目。

## 握手顺序

四类包均拒绝未知字段。双方 nonce 和 HMAC 应答使用 64 个小写十六进制字符。

| 顺序 | 方向 | 包与校验 |
|---|---|---|
| 1 | 客户端 → Engine | `connection.hello`：ticketId、nonce、peer |
| 2 | Engine → 客户端 | `connection.challenge`：nonce、peer、selected、proof；服务端核对已登记票据与实际对端，客户端验证服务端应答 |
| 3 | 客户端 → Engine | `connection.authenticate`：proof；服务端验证客户端应答，原子消费票据 |
| 4 | Engine → 客户端 | `connection.ready`：session、workspace、clientId、authorizationVersion、selected、proof；客户端核对会话绑定和最终应答 |

双方 profile 包含 protocolVersion、componentVersion、instanceEpoch、schemaDigest、capabilities 和 limits。主版本相同才可连接，次版本取较小值；能力取交集，帧预算取较小值。当前协议为 1.0，能力为 `contentRefsV1`。协商结果不能授予业务权限。

schemaDigest 要求相同。摘要输入由所有公共 Schema 和目录 JSON 组成：Schema 键为 `schema:` 加 `$id`，其他 JSON 键为 `catalog:` 加文件名，值为解析后的文档；整体按 RFC 8785 编码，再计算 SHA-256。源码与 wheel 使用相同集合。profile 的组件版本来自安装包元数据，安装真实性由原生端点核验。

帧上限为完整 JSON 的 UTF-8 字节数，不含 4 字节长度前缀；范围 1024～1048576。hello 使用接收端初始上限，从 challenge 起使用协商上限。业务请求和完整响应均受该预算约束；超限结果返回 `LIMIT_EXCEEDED`，通过分页或内容资源继续读取；查询事务同时回滚本次快照与游标。保存已提交时使用 `request.status` 查询原请求结果。

## 应答计算

令 `K` 为票据密钥，`J(x)` 为 RFC 8785 规范 JSON 编码。三类应答均计算：

```text
HMAC-SHA256(K, UTF8("DeskWeaveStudio/connection/v1/" + role) || 0x00 || J(value))
```

`binding` 的字段为 ticketId、purpose、workspace、clientId、authorizationVersion、clientPeer 和 serverPeer；`purpose` 固定为 `engine.control`。它由双方持有的票据构造，不包含秘密。`challengeBody` 为移除 proof 的 challenge；`readyBody` 为移除 proof 的 ready。

```text
transcript = {binding, hello, challenge: challengeBody}
server proof: role="server", value=transcript
client proof: role="client", value={transcript, serverProof}
ready proof:  role="ready",  value={transcript, clientProof, ready: readyBody}
```

应答按常量时间比较。角色前缀区分双向应答与就绪确认；双方 nonce、协商结果、工作空间、授权版本和进程身份共同参与认证。旧连接应答不能用于新挑战；同一票据并发认证只允许一个连接完成消费。

## 会话派发与撤权

服务端在客户端认证成功后调用 `Engine.open_session`，传入登记的权限、流程范围、本地属性、授权版本和协商能力。会话分配后再次检查授权与握手期限，随后发送 ready。请求中的 session 和 workspace 必须匹配该连接上下文。客户端校验响应时显式传入原请求，核对 replyTo、会话和工作空间，并按原方法验证成功结果契约。

`ServerConnection.receive` 直接调用 `Engine.dispatch`，将连接授权检查作为可信回调传入。参数解析、Schema 校验、表达式计算、内容处理、SQLite 查询、事务提交检查和响应编码都使用该检查。SQLite 进度中断恢复为原始授权或超时错误；事务回滚期间关闭进度中断，完成收尾后恢复外层预算。

`revise` 使用 expectedVersion 比较后更新策略并递增版本，旧票据和旧连接立即失效；版本冲突返回 `AUTHORIZATION_CONFLICT`。`revoke` 撤销授权和票据，并关闭关联连接；重复撤销幂等。登记失效在授权锁内完成，连接和 Engine 资源关闭在锁外执行，避免与在途请求互相等待。可信控制面的 `revoke_clients` 批量撤销一组已登记客户端：全部权限和票据在同一锁内失效，再关闭对应连接。宿主及其派生宿主的撤销复用这一边界。

撤权在提交检查前生效时，事务回滚；已通过提交检查并完成提交的保存保留结果和回执。连接中断后，恢复连接按当前权限查询原请求，不能依据传输失败直接判断保存结果。

关闭会话释放该会话的内容、分页快照和诊断集合。握手异常、业务调用中的取消及授权失效都会关闭连接；关闭自身也失败时，调用方收到同时保留原异常和清理异常的异常组。单个资源关闭被取消或失败时继续关闭其余资源，失败句柄转入关闭集合并禁止业务访问；重复关闭、定期回收或 Authority 关闭会重试，成功后释放归属和连接容量。

## 错误与恢复

| 错误 | 处理 |
|---|---|
| AUTHENTICATION_FAILED | 关闭连接；核对原生对端和票据，重新取得票据 |
| PROTOCOL_UNSUPPORTED / SCHEMA_UNSUPPORTED | 关闭连接；使用兼容组件与契约集合 |
| ACCESS_DENIED | 按当前授权判断请求；授权失效时关闭连接，控制面重新授权 |
| AUTHORIZATION_CONFLICT | 读取当前授权版本，重新提交策略变更 |
| INVALID_ARGUMENT | 握手包错误时关闭连接；业务参数错误按原业务响应修正 |
| LIMIT_EXCEEDED | 释放过期资源、调整单次内容或使用分页；不裁剪业务数据 |
| TRANSPORT_CLOSED | 取得新票据并建立新会话，按原请求身份恢复查询 |

运行入口见[连接认证示例](../../examples/engine/connection.py)与 [Engine 开发](../development/engine.md)。
