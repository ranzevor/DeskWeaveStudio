# 跨平台架构与桌面后端

## 平台范围

Studio、Engine、MCP 和工作空间格式面向 Windows、macOS 与 Linux 共用。桌面事件、采集、权限、原生会话与安装由独立后端实现；Linux 的 X11、Wayland 分别准入和验收。Windows 的实现顺序不改变公共模型与其他后端的设计范围。

| 目标 | 基线与发行架构 | 桌面入口 |
|---|---|---|
| Windows | Windows 11 x64；Windows 10 x64 按指定系统与 WebView2 组合验收 | Win32 输入、录制及采集后端 |
| macOS | macOS 14 及以上的指定系统组合；Apple Silicon 与 Intel 独立制品 | Quartz、ScreenCaptureKit、系统隐私授权 |
| Linux X11 | Ubuntu 24.04 LTS 为构建基线；x64、arm64 独立制品 | X11 RECORD/XInput2、XTEST、RandR 与截图适配器 |
| Linux Wayland | 按发行版、合成器、Portal、libei 与 PipeWire 的具体组合验收 | Portal 会话与 libei；普通桌面全局录制使用合成器专用后端 |

共同目标包括全部显示屏录制、持续按住绘制、原轨迹还原、输入释放与急停。每个后端对实际能力分别返回状态；不能将截图可用、输入可用或界面能启动视为全部显示屏录制已经可用。

## 核心与后端

Rust 的 platform-api 定义 DesktopSession、CapabilitySnapshot、DisplayTopology、TargetBinding、CaptureSource、InputRecorder、InputEmitter、InputClock、EmergencyControl 和 CredentialStore 端口。输入租约、完整事件队列、笔画配对、轨迹校验、逐事件调度、暂停游标及积压预算位于共享核心。

后端只负责原生事实转换及操作：枚举当前会话、读取权限、取得事件自身的位置、转换逐屏坐标、发送单个原生事件、取得帧、释放句柄。共享核心不依赖 HWND、CGEventRef、XID、PipeWire node、D-Bus object path 或 EIS 文件描述符；这些值不能成为任务包的可执行授权。

原生提交结果统一表示本次已提交、明确拒绝或提交状态不明。Windows 的返回计数、Quartz 的投递调用和 X11/EI 的连接及协议边界分别处理；原生接口未返回接收数量的后端按调用或协议边界确认提交。sent 表示本段原生提交完成，目标应用结果由实际收到的事件、绘图或流程后置条件核对。

原生模块分别为 platform-windows、platform-macos、platform-linux-x11、platform-linux-wayland。Wayland Portal 与合成器专用实现使用不同 backend 身份；进程输入互斥身份由真实登录会话、交互桌面和 seat 决定，不以 backend、安装版本或工作空间区分。XWayland 与原生 Wayland 指向同一交互会话时共用输入拥有者。

## 能力与运行准入

[平台契约](../../contracts/platform.schema.json)定义 desktop.capabilities 的 Agent 结果。Engine 通过 system.capabilities.desktop 提供当前能力摘要，包含系统、CPU 架构、后端、后端版本、能力代次、拓扑版本、显示覆盖范围和轨迹格式版本。没有启动原生后端时不构造 available；已登记但未实现的后端返回 unimplemented。

能力字段是固定的十二项：显示屏/窗口采集，物理位置/相对量/键盘/全部显示屏录制，绝对/相对/键盘/Unicode 输入，全局控制快捷键和目标激活。每项具有 state、reason、nextAction。state 区分 available、permissionRequired、denied、unsupported、unimplemented、sessionUnavailable；原因和操作对应具体能力。读取能力不自动触发系统授权弹窗。

录制开始、运行准备、实际发送和恢复分别核对当前代次。权限撤销、Portal 关闭、合成器重启、桌面切换、输入设备消失或拓扑变化使相关能力失效，停止接收或发送并进入既有中断/释放流程。能力可用不等于已取得某个窗口或显示屏的目标令牌。

record.allDisplays 只有在全部活动显示区域已明确覆盖、物理位置和键盘录制均可用时才可返回 available。topologyCoverage=selected 或 unknown 不满足该条件。用户在系统选择器中只选了部分屏幕时，界面显示实际选择的屏幕数，不能将它改名为“全部显示屏”。

## 坐标、时间与轨迹格式

保留 Trajectory 1.0 的全局物理坐标与整数相对量语义。Trajectory 1.1 增加 display 坐标模式；pointer.move 通过 displayId、x、y 表示当前方向下的屏内像素位置，支持小数。event 模式也可使用同一表达；来源拓扑为 displayLocalPixels 时，所有绝对位置必须带显示身份。relative 和 event 中的小数增量在 1.1 中原样保存，并由 source.inputProfile 指明后端及单位。

逐屏拓扑分别保存像素尺寸 pixelSize、用于布局显示的 layoutBounds、当前方向、主屏状态及 pixelToNative。layoutBounds 是系统布局坐标，不能拿来当作截图像素尺寸；系统没有提供位置时为 null，界面按屏幕列表选择，不推测桌面排列。主屏信息未知时为 null。

pixelToNative 为 `[a,b,c,d,tx,ty]`，定义 `nativeX=a*x+c*y+tx`、`nativeY=b*x+d*y+ty`，必须可逆。nativeSpace 区分 Windows 物理坐标、Quartz 点坐标、X11 根窗口坐标和 EI 区域坐标。变换由实际显示/采集/输入后端建立；包含缩放、平移及必要的方向变换。没有输入映射时为 null，截图仍可作为图像资产使用，输入在绑定阶段拒绝。原生句柄、授权票据及临时映射 ID 不进入持久来源。

录制将每个原生事件按其所属显示区域逆变换为屏内像素；回放将屏内像素映射到当前目标后端。跨屏时切换显示身份与对应变换，保持同一时间轴和按钮状态，不将两个屏内坐标直接当成全局直线。自动生成的跨屏移动先在已知的当前桌面布局中规划，再逐屏转换；布局位置未提供时不推测中间路径。已有录制点列始终按原事件回放。负原点、不同缩放、横竖屏、镜像和屏间空隙分别处理；整条路径不乘一个全局 DPI 系数。

原布局回放要求来源后端、单位与显示几何相符。跨系统或不同布局通过 displayLocal 明确绑定每个来源屏幕，按屏内像素偏移映射；不自动缩放笔画，不自动旋转。包含原始相对量时只能使用已核对的同后端、同单位和原布局；不同平台的设备计数、Quartz 位移、XI2 valuator 和 libei 增量不相加或按 DPI 转换。

动作 position/from/to 同时接受已有 desktopPhysical 点和新的 displayPixels 点。暂停 pointer、恢复 pointerToRestore 同样允许带 displayId 的屏内位置；恢复前核对显示身份、拓扑和实际输入目标。来源中的历史 pixelToNative 用于解释源坐标，不能绕过当前目标后端重新建立的授权映射。

调度使用共享单调时间轴。原生时钟只在后端入口换算一次，明确单位、起点和回绕；不能把不同平台时钟直接比较。完整点列、按钮顺序和积压处理沿用[连续指针设计](../design/pointer-motion.md)。

## Windows 后端

物理位置从每条低级鼠标事件取得，相对输入保留 Raw Input 原始量。SendInput 连续运动使用 MOVE_NOCOALESCE；绝对输入使用 ABSOLUTE 与 VIRTUALDESK，按整个虚拟桌面归一化。多显示屏坐标在每显示器 DPI 感知的线程中处理。

目标受登录会话、输入桌面、完整性级别与授权约束。UAC 提升仅切换受限 Agent，Studio 和 Engine 保持普通身份；拒绝提升保留编辑和其他已可用功能。受保护桌面不通过普通桌面目标令牌访问。

## macOS 后端

采用 Quartz event tap 读取事件自身的 location、timestamp、按钮及修饰键状态，覆盖 mouseMoved、leftMouseDragged、rightMouseDragged 和 otherMouseDragged；不能只监听未按住时的移动。[CGEvent](https://developer.apple.com/documentation/coregraphics/cgevent)提供事件、位置、时间和发送入口，[事件类型](https://developer.apple.com/documentation/coregraphics/cgeventtype)区分普通移动和持续按住的拖动。

发送时根据实际持有按钮构造对应 dragged 事件；左键持续按住时不能持续发送 mouseMoved 代替 leftMouseDragged。每条事件按共享期限投递，最后一个运动完成提交后才释放。[CGEvent.post](https://developer.apple.com/documentation/coregraphics/cgevent/post%28tap%3A%29)不返回 Windows 式的接收计数，段状态按原生投递边界处理。CGWarpMouseCursorPosition 不作为轨迹注入器；事件源的本地输入抑制也不能替代移动消息的连续交付。目标收到的笔画按相同原生用例验收。

全局屏幕位置通过各显示区域的实际映射转换，Retina backing pixel 与 Quartz 点分别保存；不硬编码 2×。屏幕采集使用 ScreenCaptureKit，每个输出的帧尺寸、方向与内容区域建立对应映射。镜像显示合并输入区域，采集内容与当前显示代次绑定。

输入监听、辅助功能/事件发送、屏幕录制权限分别检测。可信本地界面提供系统授权入口；拒绝或撤销只关闭受影响能力。event tap 超时被禁用或系统事件源中断时结束该录制分段，重新启用后创建新分段，不能把缺口前后连接成完整笔画。应用升级保持 bundle 身份与签名关系，权限检查以实际持有该权限的进程为准。

## Linux X11 后端

普通位置和按钮录制使用 [RECORD 的 device events](https://www.x.org/releases/current/doc/recordproto/record.html)及事件携带的根窗口位置；相对输入通过 XInput2 的原始 valuator 路径处理。设备事件与已分发到多个窗口的事件不能同时作为同一次操作录入，XQueryPointer 的当前结果不能回填历史位置。

发送使用 [XTEST](https://www.x.org/releases/current/doc/xextproto/xtest.html) 的运动、按钮和键盘事件。按原生期限发送并及时 flush，不能将整段运动留在 Xlib 输出缓冲中等到末尾才提交。XSync 只提供服务器请求处理边界，不代表目标应用已经绘图。避免为每个采样点执行阻塞往返；连接错误进入清理及未决效果处理。

XTEST 自带的 delay 会阻塞同一连接的后续请求，不能替代独立的可撤销调度。输入连接与急停控制连接分开；发送前校验屏幕和协议坐标范围，不依赖服务器自动夹取越界位置。RandR 变换、X11 根窗口像素与图像像素分别建模；Xft.dpi 不是指针变换依据。

X11 会话使用其当前授权，不以 root 运行 Studio。XWayland 连接的支持范围限于其可访问的窗口与输入。

## Linux Wayland 后端

屏幕采集通过 ScreenCast Portal 选择来源，再使用 PipeWire 读取帧。输入控制通过 RemoteDesktop Portal 的系统选择/授权和 ConnectToEIS 建立 libei sender；当前官方接口推荐 EIS，建立 EIS 后不能同时使用同一会话的 Notify* 路径。依据 [RemoteDesktop](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)及 [ScreenCast](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)的 mapping_id 关联图像流和输入区域，不能按数组次序或相同宽高猜测对应关系。

PipeWire 帧的像素尺寸、Portal 的布局尺寸和 EI region 的输入坐标分别保存。region、设备或流变化后重建映射并增加拓扑代次。输入只有在设备已恢复可用、会话仍授权且映射完整时发送；每个到期事件通过 libei 的事件及 frame 边界提交，不能在整段结束后才 flush。

[InputCapture Portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.InputCapture.html)的捕获由合成器激活，捕获时事件交给接收方，普通桌面指针不再按这些事件更新。因此它不能直接用作用户在普通画图窗口中操作的被动全局录制器，也不能以捕获后重新注入的方式悄悄替换用户输入链路。

普通桌面全显示屏录制由 linux-wayland-compositor 后端实现，要求合成器提供已授权的完整区域集合、每事件绝对位置、按钮/键盘事件和明确的事件顺序；组件单独锁定兼容版本。缺少该接口的组合以 NO_PASSIVE_CAPTURE 返回 unsupported；专用后端尚未实现时返回 unimplemented。全屏录制仍是该后端的交付与验收要求，Portal 输入控制或视频采集通过不能代替它。

不能自动读取 /dev/input、创建 uinput 设备、提升为 root 或改用 XWayland 以扩大权限。若采用需要系统安装的专用集成，安装目标、权限和桌面会话范围通过独立原生组件提供；其卸载、撤权、崩溃和升级同样使绑定失效。合成器拥有最终焦点与会话控制；不能取得焦点或无法核对目标时保持暂停。

## 键盘、控制与采集

物理键符号与 Unicode 文本保持分离；扫描码、Quartz keycode、XKB/evdev keycode 只在对应后端解释。Ctrl、Command、Alt、Super 不做隐式替换；跨平台流程使用显式参数或平台分支表达不同快捷键。文本发送预检目标后端和当前输入法可表达的字符，不能逐个把 Unicode 码点当成本机键码。剪贴板粘贴只由显式选择的操作触发。

全局快捷键按当前平台默认值和用户覆盖解析：Windows/Linux 急停默认为 Ctrl+Alt+Pause，macOS 为 Ctrl+Alt+F12；Meta 映射当前系统的 Meta 键，Win 仅用于 Windows 兼容。全局快捷键在各平台原生注册，Wayland 使用合成器/Portal 提供的对应机制。急停能力未就绪时阻止原生输入；不能以依赖窗口焦点的 React 按键事件代替。暂停、失焦、授权撤销、设备暂停和会话关闭均停止后续发送并释放已登记输入；来源无法可靠区分时，录制与回放互斥，不按坐标巧合去除物理事件。

截图和窗口选择按平台能力提供。被遮挡、最小化、受保护内容、Portal 源关闭、远程会话断开分别返回实际状态，不回退成未经选择的全屏。capture/window 结果统一保留实际帧像素尺寸和来源映射。

## 本机通信、文件与秘密

Windows 使用 DACL 命名管道。macOS 与 Linux 使用仅当前用户可访问的运行目录和 Unix domain socket；Linux 校验 SO_PEERCRED 与进程会话，macOS 使用本机对端身份接口及受保护启动票据。对端 UID 相同不能代替应用身份和一次性连接认证，端点路径不能成为授权凭据。禁止认证失败后改用无认证 TCP 回环服务。

发现记录、锁与临时资源按真实用户会话隔离，启动票据通过继承通道传递。Linux 运行目录验证所属用户和权限；macOS/Windows 的应用目录由原生目录接口取得。资源路径限制沿用对象身份检查，覆盖大小写、Unicode 文件名、符号链接、重解析点、文件替换与原子重命名的系统差异。

秘密存储分别使用 Windows 凭据设施、macOS Keychain 和 Linux Secret Service。服务不可用或密钥环锁定时返回具体状态；不回退为明文文件。凭据不随任务包跨系统导出，目标令牌、Portal restore token、窗口句柄和当前会话权限也不随工作空间复制。

## Studio 与平台交互

[Tauri 的 WebView](https://v2.tauri.app/reference/webview-versions/)分别为 WebView2、WKWebView 和 WebKitGTK。三套运行时分别覆盖 Monaco worker、CSP、拖放、文件对话框、输入法、画布、虚拟列表与资源释放；Chrome 中的原型检查只覆盖该原型运行环境。

菜单、窗口按钮、快捷键显示和目录选择遵循当前系统操作习惯；界面命令可使用平台主修饰键，但已录制流程的物理按键保持原样。Windows 使用 Narrator/NVDA，macOS 使用 VoiceOver，Linux 使用 Orca 检查键盘与读屏任务。

能力状态显示在当前录制、目标或运行准备入口，提供授权、重新选择或切换会话等对应操作。macOS 区分输入监听、辅助功能和屏幕录制；Wayland 区分共享屏幕、控制输入和全局录制。权限变化后刷新状态与显示范围，不能继续展示旧的可用状态。不可用动作的已有节点和资产仍可编辑、导出，运行准备定位具体不可执行节点。

## 构建、分发与更新

每个目标分别打包 Studio、Engine、Agent 和 Bridge，冻结该 OS/架构的 Python 原生扩展、Rust target、系统库与工具链。macOS arm64/x64、Linux x64/arm64 不混用解释器或二进制依赖。Linux 以目标发行版的 glibc/WebKitGTK 基线构建，动态库依赖须满足目标系统的最低版本。

Windows 安装器校验 Authenticode 身份；macOS 应用和内嵌组件执行 Developer ID 签名、Hardened Runtime、公证及票据处理，校验固定 Team ID；Linux 分别维护 AppImage 与包管理器发行渠道的签名身份。包管理器安装由该渠道更新，自包含安装由对应受信更新渠道更新，不能混用。所有平台均验证现有更新清单签名、平台/架构、真实字节摘要及组件兼容关系。

制品 platform 支持 windows-x64、macos-x64、macos-arm64、linux-x64、linux-arm64；publisher 根据平台指向签名发布者、Team ID 或发行渠道签名密钥指纹。不能只比较字符串而跳过平台签名和受信渠道核验。密钥轮换遵循现有受信根流程，跨平台身份不互相替代。

## 兼容与验收

连接握手协商 display-coordinates-v1、platform-capabilities-v1、platform-settings-v1 及轨迹版本。新字段和新联合分支仅向支持它们的客户端发送；1.0 的整数全局坐标继续按原语义读取。不能将 1.1 轨迹删除显示身份、截断小数或改写版本号后传给旧客户端。未知格式返回 SCHEMA_UNSUPPORTED，旧资产内容保持不变。

全平台文档与契约 CI 检查相同样例；原生验收分别在 Windows、macOS、X11 和具体 Wayland 合成器会话运行。Q54～Q58 覆盖能力与撤权、坐标格式、键盘和窗口交互、本机身份、平台制品与更新。Q50～Q53 的完整点列和逐步绘图在各后端分别执行；按每个显示区域的实际像素比较笔画，跨屏边界另外核对时序和持续按住状态。

虚拟显示和替身用于模型与错误路径；真实混合缩放、多显示屏、不同显示方向、独立目标画布与原生权限用于平台集成。X11 测试通过不替代 Wayland，WebView2 通过不替代 WKWebView 或 WebKitGTK，x64 制品通过不替代 arm64。
