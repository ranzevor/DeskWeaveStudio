# 工程实施

## 当前工作入口

当前已实现的 Python 服务库位于 `python/deskweave`，安装、方法范围和测试入口见[Engine 开发](engine.md)。组件按[架构目录](../architecture/system.md)和[路线图](../../ROADMAP.md)推进。

文档检查使用 Python 3.12 或更高版本。在仓库根目录创建独立环境后安装固定依赖：

```bash
python -m venv .venv-docs
```

Windows PowerShell：

```powershell
.\.venv-docs\Scripts\python.exe -m pip install -r requirements-docs.txt
.\.venv-docs\Scripts\python.exe tools/check_docs.py --contracts
```

Linux 或 macOS：

```bash
.venv-docs/bin/python -m pip install -r requirements-docs.txt
.venv-docs/bin/python tools/check_docs.py --contracts
```

不安装额外依赖时可运行 `python tools/check_docs.py` 检查文档链接、编码、JSON 和需求追踪。退出码 0 表示本次检查通过，1 表示检查失败，2 表示契约检查依赖缺失。原生应用的测试入口在对应组件建立时提供。

## 仓库检查

安装文档与原型检查依赖，并准备 Node.js 22：

```bash
python -m pip install -r requirements-docs.txt -r requirements-prototypes.txt
python tools/check_repository.py
python tools/check_docs.py --contracts
python tools/check_canonical_json.py
node tools/check_canonical_json.js
python tools/check_authoring_contracts.py
node tools/check_update_signatures.js
python tools/check_prototypes.py
python tools/export_design_showcase.py --check
```

`check_repository.py --staged` 检查 Git 索引中的实际内容；`--tracked` 检查已跟踪文件。检查覆盖许可、协作入口、路径、文件大小、访问凭据格式和个人绝对路径，输出只包含文件位置与规则名称。

## 组件入口与开发运行

Studio、Engine、Agent、Bridge 与命令客户端采用[生命周期设计](../architecture/lifecycle.md)中的发现、启动与退出顺序。各组件完成认证、数据加载及输入初始化后接受业务请求。

| 入口 | 实现边界 |
|---|---|
| Studio | 创建可信窗口与原生审批，按工作空间连接 Engine；WebView 崩溃与 Host 存活分别处理 |
| Engine | 单工作空间写入者，先完成格式和对象检查，再接受业务命令 |
| Agent | 当前交互桌面的唯一拥有者；普通/提升实例交接同一互斥身份 |
| Bridge | 连接既有 Engine，保留外部稳定客户端身份、授权及后台选择 |
| deskweave | 按 CLI 目录装配参数、绑定本地能力并调用既有服务，不增加执行解释器 |

CLI 的具名命令、参数、输出、退出码和中断处理见[命令入口](../interfaces/command-line.md)。目录的 input/outputSchema 指向现有服务契约；新增客户端入口不自动扩大其可调用方法和本地权限。

开发入口可以使用开发服务，但发行入口只加载制品清单中的代码与资源。进程角色、认证材料、目标和输出授权通过受保护启动上下文传递；不新增允许任意路径、脚本或能力字符串的发行快捷参数。

启动竞争、冷启动失败、最后窗口关闭、Engine/Agent/Bridge 中断及后台观察退出在同一纵向场景中验证。检查进程退出、状态持久化和输入释放。

## 构建输入

| 组件 | 版本与依赖记录 | 验证入口计划 |
|---|---|---|
| Studio | Tauri 2.12、React 19.3；Node、pnpm、TypeScript、Vite 和插件固定补丁，提交 pnpm-lock.yaml | 类型、组件、编辑事务、WebView2/WKWebView/WebKitGTK |
| Rust Agent | rust-toolchain.toml、Cargo.lock、target、features、平台 SDK | 领域单元、FFI、桌面隔离测试 |
| Python Engine/Bridge | CPython、SQLite 和全部直接/传递依赖锁 | 解释器、合同、存储与进程测试 |
| 发行 | 源提交、锁摘要、生成器、嵌入依赖和签名身份 | 安装、更新、数据恢复 |

插件版本单独核对，不能由宿主版本号推导。首次可构建组合在 M1 固定；依赖更新以独立变更核对兼容性、许可和安装包差异。

## 契约与代码生成

[JSON Schema](../../contracts/workflow.schema.json)定义公共结构，动作参数的唯一结构来源为 [actions.schema.json](../../contracts/actions.schema.json)。编辑表单、TypeScript 类型、Python 解析器和协议文档从同一契约生成或使用它验证。

动作目录保存名称、分组、效果类别和输出说明，不再复制参数范围。运行时表达式求值后仍需验证实际类型和数值范围。图可达性、资源闭包、取消和目标身份属于语义验证，不能仅靠 Schema 判断。

生成代码通过重生成差异检查。修改公共字段时同时提交契约、对应设计、合法/非法示例和兼容处理。不得让某个语言端私自容忍另一端拒绝的执行字段。

## 组件实现约束

TypeScript 开启 strict、严格空值和未检查索引访问检查；领域模型使用可区分联合。文档事务与运行投影分离，禁止组件直接修改底层流程数组。异步命令携带文档和连接代次，关闭视图时释放订阅与资源。

Python 的调度、存储和视觉适配器通过显式接口隔离。阻塞图像计算进入有界工作单元；取消后收拢子任务并关闭句柄，异常不通过空结果吞掉。流程值只有 JSON 数据，不执行表达式以外的宿主代码。

Rust 将系统调用集中于平台模块。unsafe 保持局部，注明指针寿命、线程和释放条件；每次原生输入前校验租约与目标。急停路径不等待图像、数据库或界面线程。

跨语言共享容量、时间、编码和错误样例；单调时钟只用于同一进程或已明确转换的时间域。大对象通过资源接口传输，不塞进持续事件流。

## 开发验证

功能从一个可验收纵向场景开始，按需求编号追到 Engine、Agent 与界面。使用临时工作空间、合成截图和独立目标窗口，不把个人桌面、凭据或运行记录作为仓库样例。

修改纯模型运行相关单元和契约测试；修改系统输入还需原生与释放检查；修改持久化覆盖中断和恢复；修改发行流程覆盖实际安装制品。验收场景按[验证设计](../quality/verification.md)执行。

## 变更审查

PR 说明用户可观察行为、公共字段、数据兼容、权限和验证结果。界面变更附必要的操作演示；性能变更给出相同输入下的测量。密钥、私有路径和用户内容不进入附件。

依赖、构建、契约或行为发生变化时同步更新相关文档和 CHANGELOG。

## 平台实现与构建

M1 同时建立平台端口和 Windows、macOS、X11、Wayland 模块边界。共享调度与录制模型只接收规范事件；各后端负责原生事件、坐标变换和权限，不在 UI 或 Python 服务中插入平台输入分支。Linux 的 X11 与 Wayland 按真实会话选择，不以是否存在 DISPLAY 环境变量独自判定。

原生二进制、Python 扩展和动态库按目标 OS/架构锁定与打包；跨平台编译通过后仍需对应会话的原生集成与安装验证。CI 与原生矩阵见[跨平台架构](../architecture/platforms.md)。

## 多入口业务复用

按[共享应用服务](../architecture/shared-services.md)建立单一 Engine 方法注册表，业务处理器与服务契约一一对应。TypeScript 客户端类型、Host 分发边界和 MCP 输入/结果结构从契约生成；前端、Bridge、CLI 只实现编辑交互与协议适配。表达式预览和正式运行复用同一个 Python 领域求值器，原生连续运动复用同一个 Rust 调度器。

入口一致性检查纳入 `python tools/check_docs.py --contracts`，核对资源与工具权限、URI 字段、默认值、结果结构和请求恢复元数据，并用目录变体检验漂移会被拒绝。应用实现后增加模块依赖检查及 Q59，分别验证处理器复用、同一工作空间状态、多客户端权限与真实输入。

Bridge 实现分别固定 2026-07-28 与 2025-11-25 的 SDK/传输组合；优先使用同一 SDK 的双代能力，需包装时将差异限制在协议适配层。准入、分页、授权、去重和运行控制始终调用相同 Engine 处理器。升级 SDK 时覆盖正常新版、正常旧版、混合错误字段、错误回退、订阅重建和断开取消。
