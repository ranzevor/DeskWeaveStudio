# 原生本机通道

`deskweave.transport.native` 为 Engine 认证内核提供 Windows 命名管道和 macOS/Linux Unix socket。Studio、CLI 与 MCP Bridge 通过相同连接和方法调用 Engine；流程保存、权限检查及请求恢复继续由共享服务处理。消息格式见[连接认证](connection.md)与[本机协议](local-protocol.md)。

## 进程与安装登记

可信启动控制面先建立 `Installation`，再为它启动并管理的进程建立 `ProcessBinding`。安装策略包含：

- `executable`：实际运行映像的绝对路径与预期 SHA-256。
- `components`：应用模块、契约、运行依赖等组成该组件的文件及预期 SHA-256，最多 10,000 项。
- `macos_requirement`：macOS 运行映像必须满足的代码签名 requirement。

发行安装策略从受信安装清单读取。Python 组件的清单覆盖解释器之外的应用代码及运行依赖；运行映像路径以操作系统加载对象为准，虚拟环境启动器路径与该映像可能不同。文件清单身份参与 `installationDigest`，双方使用相同规范 JSON 和 SHA-256 算法。

`ProcessBinding(pid, installation, instance_epoch=..., timeout=30, cancel=None)` 在登记时打开原生进程引用，采集创建标识并核验安装内容。`instance_epoch` 由控制面为受控组件实例分配，通过进程引用与创建标识固定。清单、epoch、授权和恢复凭据通过受保护的启动控制通道或进程内调用传递。

普通连接消息中的 PID、用户、权限或安装信息不参与原生身份采信。接受连接时使用内核返回的对端 PID 查找预登记对象，再核对该对象的进程引用、创建标识、当前用户及会话、实际映像和安装文件。注册表由可信调用方维护，未登记进程在读取业务帧前被拒绝。

登记覆盖一个受控进程实例。控制面在进程退出、启动控制通道关闭或组件重启时关闭原登记；新实例重新登记 epoch 并取得票据。原生通道采用不可继承句柄或描述符。Linux 的 pidfd 固定进程存活对象，`/proc` 核对启动时刻及映像；同一进程执行 `exec` 的实例切换由启动控制通道关闭与重新登记处理。macOS 另外通过 kqueue 的退出和 exec 事件使登记失效。

## 平台核验

| 平台 | 端点与对端来源 | 会话与安装核验 |
|---|---|---|
| Windows | 显式登录 SID DACL、拒绝远程客户端的命名管道；`GetNamedPipeClientProcessId` / `GetNamedPipeServerProcessId` | 进程令牌用户、系统会话及 AuthenticationId；保留进程句柄，检查创建时间与实际映像、组件摘要 |
| macOS | 0700 私有目录、0600 Unix socket；`LOCAL_PEERTOKEN` | audit token 的 PID、有效 UID 和 audit session；kqueue 进程生命周期；Security.framework 按 audit token 获取动态代码并校验可信 requirement，组件清单单独核验 |
| Linux | 0700 私有目录、0600 Unix socket；`SO_PEERCRED` | pidfd、`/proc` 创建标识及映像；有效内核 audit session 或 systemd/elogind 登录会话；组件摘要 |

macOS 登记以控制方当前 audit session 为预期，连接后必须与内核对端 token 一致。Linux 登录会话取自 audit 或登录管理器，读取不到时拒绝登记；`XDG_SESSION_ID` 等环境值不参与身份核验。CI 在实际 PAM 登录会话中运行 Linux 原生测试。

端点权限与进程身份分别检查。随机端点名用于分配端点，安装策略和双方一次性票据共同约束应用接入。票据不写入普通发现记录、命令行或日志。端点目录与 socket 的文件身份固定，清理只移除该监听器创建的对象；清理失败可再次关闭重试。

## 接口与所有权

```python
from deskweave.transport.native import NativeListener, NativeClient, connect, serve_connection

with NativeListener(server_binding) as listener:
    channel = listener.accept(registered_processes, timeout=30, cancel=cancelled)
    # 可信控制面将 authority.issue(credential, channel.peer) 交付指定客户端。
    serve_connection(channel, authority, timeout=30, cancel=cancelled)
```

客户端持有控制面提供的服务端登记和一次性票据：

```python
with connect(address, client_binding, server_binding, timeout=10) as channel:
    with NativeClient(channel, ticket, timeout=10) as client:
        response = client.call(
            "request.status", {"requestId": saved_request_id},
            request_id=status_request_id, timeout=30,
        )
```

`NativeListener` 拥有监听端点，支持多个已连接客户端；每个接受的 `NativeChannel` 由调用方持有。`ProcessBinding` 可供多条连接复用，由控制面在实例结束时关闭；关闭绑定后现有通道身份检查失败。监听器关闭停止新连接，已接受通道分别关闭。

`NativeClient` 接管传入通道，构造、认证或调用失败时关闭通道和认证状态。`call` 显式接收调用方持有的 `request_id`，返回公共响应信封；业务失败读取其中的 `error`。同一客户端串行处理请求，排队占用同一次调用的时间预算。

`serve_connection` 接管通道，调用既有 `ConnectionAuthority.accept` 和 `ServerConnection.receive`。维护线程每 100ms 调用 `sweep`；撤权、到期和退出均联动关闭原生通道及 Engine 会话。该函数退出前停止并等待维护线程，清理异常与原处理异常共同返回。多连接调度由端点调用方负责，连接数量继续受 Authority 的容量约束。

## 帧、期限与恢复

帧由 4 字节小端长度和 UTF-8 JSON 对象组成。接收端先检查前缀，长度必须处于 1 至当前帧上限之间；不为超限长度读取或分配载荷。服务端第一个 hello 使用 Authority 初始上限，challenge 起使用协商上限。

`send`、`receive`、`connect`、`accept` 和客户端调用均使用单调时钟。帧锁等待、安装摘要分块检查、分片读写与解析共用一次操作的截止时间；握手的两次往返共用握手期限，客户端响应校验完成后再次检查期限。安装摘要每 64KiB 检查取消与期限。

Windows 使用 overlapped I/O。取消或关闭先请求 `CancelIoEx`，等待实际 I/O 完成后释放 OVERLAPPED、缓冲、事件与管道句柄。Unix 使用非阻塞 socket 和有界等待。原生错误和清理失败返回调用方；消息中不混入系统诊断。

| 结果 | 调用方操作 |
|---|---|
| AUTHENTICATION_FAILED | 核对受控进程登记、登录会话、安装清单及票据，重新连接 |
| LIMIT_EXCEEDED | 按协商上限拆分内容，集合使用分页 |
| TRANSPORT_TRUNCATED | 放弃残帧，建立新连接 |
| TRANSPORT_CLOSED | 取得新票据并恢复连接 |
| DEADLINE_EXCEEDED / CANCELLED | 关闭当前连接，按原请求编号查询保存结果 |

连接关闭只结束传输和会话资源。已提交保存继续由 `request.status` 查询，客户端沿用稳定身份及原请求编号处理重连。

## 运行与检查

[原生通道示例](../../examples/engine/native_connection.py)在临时工作空间建立实际本机端点，完成认证、保存和重连查询。示例从当前运行文件建立开发安装策略，通过进程内队列交付票据。

```bash
uv run --frozen python examples/engine/native_connection.py
uv run --frozen python -m unittest tests.engine.test_native -q
```

原生测试使用独立子进程与继承管道交付控制材料，覆盖保存恢复、假服务端、安装变化、进程退出、撤权与到期回收、并发连接、帧预算、部分 EOF、背压和取消。三平台 CI 运行源码与隔离安装 wheel 的测试；Linux CI 使用 `tools/test_engine.py` 建立 PAM 登录会话。

平台 API 依据：[命名管道权限](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)、[取消 I/O](https://learn.microsoft.com/en-us/windows/win32/fileio/canceling-pending-i-o-operations)、[动态代码校验](https://developer.apple.com/documentation/security/seccodecheckvalidity(_:_:_:))、[Unix socket 凭据](https://man7.org/linux/man-pages/man7/unix.7.html)、[Linux 登录会话](https://www.freedesktop.org/software/systemd/man/sd_pid_get_session.html)。
