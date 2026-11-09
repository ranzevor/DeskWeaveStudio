# 独立 Engine 服务进程

`deskweave.process.EngineProcess` 为工作空间启动协调器提供独立进程的启动、授权登记、连接和排空接口。业务请求通过原生本机通道进入同一个 `Engine`，继续使用共享方法、事务、分页和请求恢复逻辑。进程生命周期的上层编排见[组件生命周期](../architecture/lifecycle.md)，连接格式见[原生本机通道](../interfaces/native-transport.md)。

## 启动配置

`PythonLaunch(launcher, installation, import_paths)` 固定 Python 启动解释器、实际运行映像、安装文件清单和应用模块目录。`launcher` 是带预期摘要的 `InstalledFile`；框架启动器与实际映像不同的平台，在清单中分别登记两者。进程入口 `_entry.py` 也属于安装清单。

启动使用绝对程序路径、独立参数数组及 `-I -S -X utf8`。工作目录固定为进程入口目录，环境保留系统目录、临时目录和区域设置；模块目录经专用继承管道传入。解释器的初始标准库搜索位置与应用目录分别处理。应用及第三方模块必须出现在安装清单中，导入前核对实际路径与内容摘要；Python 源码使用通过核验的字节编译。包搜索位置与资源加载器绑定已解析的实际目录。

发行启动配置由安装组合提供。[进程示例](../../examples/engine/process.py)从当前开发环境构造清单，包含应用、公共契约、依赖及包元数据。

## 创建与就绪

```python
from deskweave.process import EngineProcess
from deskweave.transport.protocol import ConnectionPolicy

process = EngineProcess(launch, host_binding, workspace_path, create=True)
try:
    credential = process.authorize(
        host_binding, ConnectionPolicy({"workflow.read", "workflow.write"}), lifetime=300,
    )
    with process.connect(credential, host_binding) as client:
        response = client.call(
            "request.status", {"requestId": original_request_id},
            request_id=status_request_id,
        )
finally:
    process.close()
    exit_code = process.wait(timeout=30)
```

`host_binding` 必须属于当前启动进程。`EngineProcess` 核对安装字节，启动候选进程并固定其真实进程身份，随后通过继承管道传递有界安装分块和工作空间配置。管道和后续端点均不允许被派生进程继承。

子进程完成模块来源校验、工作空间打开、唯一写锁、安装登记容量检查和连接接收线程启动后才发布就绪位置。父进程核对就绪身份，并建立双方身份匹配的原生启动控制连接；控制握手完成后构造函数返回。普通客户端另行使用带一次性票据的业务连接。

启动配置、清单分块、就绪交换与控制连接共用调用方的启动期限和取消信号。就绪前的失败或取消回收本次创建的候选进程，并释放标准流、线程和原生进程引用。已有工作空间由其他 Engine 持有时返回 `WORKSPACE_BUSY`；现有实例继续服务。

## 控制所有权与业务连接

| 接口 | 行为 |
|---|---|
| `authorize(peer, policy, lifetime=...)` | 为受控进程创建授权；同一安装清单及进程登记共享已有状态 |
| `issue(credential)` | 核对当前授权和进程后签发一次性票据，由控制面交付指定进程 |
| `connect(credential, local)` | 为当前进程取得票据，完成原生连接及认证，返回 `NativeClient` |
| `revise(..., expected_version=..., lifetime=...)` | 按授权版本更新策略，关闭旧连接并同步到期索引 |
| `revoke(credential)` | 撤销授权和票据，关闭对应连接并回收登记；重复调用幂等 |
| `status()` | 返回活动连接、授权、文件状态项及清单字节数量 |
| `close()` | 释放启动控制所有权，停止新的业务连接准入，保留已接入客户端的处理与收尾 |
| `poll()` / `wait(timeout=...)` | 查询或等待实际进程退出；`wait` 同时等待本地回收完成 |

权限签发通过受信启动控制连接执行，业务连接继续由 `ConnectionAuthority` 核对票据、策略、目标范围、期限和授权版本。控制连接使用顺序请求编号，分块登记占用同一串行事务；消息错误、超时和取消按实际传输边界清理。登记开始前的取消保留控制连接；服务端建立客户端进程登记时，目标退出或进程访问权限失效均使该次登记返回认证失败，其他客户端和启动控制连接继续工作。

控制方定期发送心跳，正在进行的控制事务共享已有连接与锁。控制方正常关闭或异常退出时，Engine 进入排空：停止接入新连接，已有连接继续按原授权和期限处理，最后一条连接结束后关闭数据库和进程。所属客户端退出和启动协调器的全局空闲策略分别由[组件生命周期](../architecture/lifecycle.md)规定。

`close()` 与 `wait()` 分别表示释放控制所有权和等待退出。业务客户端通过自己的 `close()` 释放连接。启动后的进程回收跟随实际退出，控制方不会为了缩短等待而终止仍有客户端使用的实例。

## 容量与回收

| 配额 | 默认值 | 配置范围 |
|---|---|---|
| 活动业务连接 | 64 | 1～64 |
| 客户端授权及进程登记 | 256 | 1～256 |
| 累计文件状态项 | 100,000 | 1～1,000,000 |
| 去重安装清单元数据 | 16MiB | 1 字节～64MiB |
| 单张清单组件 | 10,000 | 1～10,000 |
| 启动及控制帧 | 1MiB | 固定 |
| 单个清单分块 | 128 项且控制编码不超过帧预算 | 固定 |
| 诊断尾部缓冲 | 64KiB | 固定 |

前四项分别通过 `max_connections`、`max_clients`、`max_component_states` 和 `max_manifest_bytes` 配置。启动双方的文件状态及清单计入总量；清单元数据按规范编码后的实际路径计算，按安装摘要共享。每个新的受控进程登记承担自己的文件状态项；控制方已有登记直接复用。

组件数、累计状态项和累计字节在准入前检查。失败、撤权或到期后释放相应占用；关闭失败的资源继续保有登记和配额，供后续清理重试。连接仍持有的进程登记保持计费，直到连接处理完成。

PID 和客户端通过字典查找。授权到期使用最小堆；修订产生的旧索引达到 `2 × 当前授权数量 + 32` 后重建，避免长期修订累积历史索引。授权和连接关闭复用共享认证内核。文件摘要按实际文件字节处理，持续检查操作期限与取消。

## 错误与进程诊断

控制错误使用 `DeskWeaveError` 的错误码和原因；启动失败、工作空间占用、容量不足、授权变化、取消和通信截止分别返回对应结果。授权变更已经生效而传输清理失败时，登记同步当前权限和到期状态，关闭资源保持可重试。

排空先结束接收线程，再等待全部业务线程完成，随后关闭授权、进程登记和工作空间。某项清理失败仍继续其他清理，并向调用方返回实际异常。进程诊断写入专用 stderr 管道，持续读取并保留有界尾部；`diagnostics` 供进程运维和开发排障使用。业务结果仍由已认证的业务连接返回。

## 运行与测试

```bash
uv run --frozen python examples/engine/process.py
uv run --frozen python -m unittest tests.engine.test_process -q
```

测试使用临时工作空间与受控进程，覆盖共享连接、工作空间竞争、保存及重连、控制方退出、Engine 崩溃后的版本读取、授权修订与撤销、容量回收、模块遮蔽、路径别名、启动取消、线程失败和排空异常。三平台 CI 对源码和隔离安装的 wheel 执行同一组进程契约。
