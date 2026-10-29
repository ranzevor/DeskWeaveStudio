# 全显示屏录制与回放

## 范围与坐标

录制准备默认选择“全部显示屏”，覆盖当前交互桌面全部已启用显示区域；也可选择单个显示屏或指定窗口。一次录制使用一条输入事件流、一套按键状态及同一单调时间轴。跨屏移动、焦点切换和跨屏拖拽持续记录，不切换会话。跨屏跳转记录系统实际报告的位置，不向屏间空隙插补坐标。

全局物理像素拓扑按系统返回的矩形排列；跨平台屏内像素拓扑分别保存像素尺寸、系统布局和原生输入变换。两种表达均支持不同分辨率、横竖屏、混合缩放、负布局坐标和 L 形布局。全局坐标的有效范围为显示区域并集；屏内坐标的有效范围由 displayId 与该屏 pixelSize 决定。布局空隙不属于可操作区域。镜像输出合并为同一逻辑显示区域，outputNames 列出其实际输出，输入事件只记录一次。

原始相对增量保持设备输入单位。单次运动使用一种事件表达，避免把物理位置和同一运动的原始增量播放两次。默认 event 模式保留物理位置及需要的相对事件；关闭连续指针运动时，仅省略未按住按钮的普通移动；点击和滚轮的必要位置锚点、按钮按住期间的全部运动仍加入序列。relative 模式保留相对输入语义。

## 目标与接口

[录制 Schema](../../contracts/recording.schema.json)定义显示拓扑、录制来源、状态和回放映射。

| 对象或方法 | 内容与用途 |
|---|---|
| topology | 全局物理拓扑使用 version、desktopPhysicalPixels、virtualBounds；屏内拓扑使用 displayLocalPixels、nativeSpace、coverage 和逐屏映射 |
| display | 本机显示身份、名称、输出名称和方向；全局表示使用物理 bounds/DPI，屏内表示使用 pixelSize、layoutBounds、pixelToNative |
| source | scope、topology、displayIds、initialPointer；1.1 的屏内来源另含 inputProfile，固定后端及相对量单位 |
| desktop.topology | 读取当前交互桌面的显示拓扑；没有活动区域时返回 NO_ACTIVE_DISPLAYS |
| target.select / target.bind | 本地选择全部显示屏时返回 kind=desktop 的目标令牌及拓扑 |
| record.start | 带 targetToken、expectedTopologyVersion、coordinateMode 和 includePointerMotion；成功返回固定 source |
| record.snapshot | 读取录制身份、状态、控制版本、source、当前拓扑版本、事件数、时长、partial 及具体原因 |
| record.pause / resume / finish | 暂停、在同一布局继续、结束并封存轨迹或中断分段 |

目标能力绑定交互桌面、Agent epoch、用途、显示集合、拓扑版本和期限。全显示屏来源必须包含当时全部逻辑显示区域；单屏来源只包含所选区域，窗口来源固定窗口身份及当时可用显示集合。名称或显示身份数组不能自行创建目标能力。

Agent 在启动事务中重新枚举并比较拓扑；版本不一致返回 DISPLAY_TOPOLOGY_CHANGED，选择界面刷新后重新发起。录制目标只授予录制用途；输入回放及像素采集分别获取对应能力。desktop.topology 和 record.* 仅供本地客户端，MCP 不发布这些方法。kind=desktop 首期用于录制和输入；截图及视觉节点使用具体显示屏、窗口或区域目标。

## 事件与状态

全桌面使用同一原生录制服务和有序合流入口，过滤程序自身注入和控制热键，按接收顺序确定事件时间。桌面位置取每条事件携带的坐标，相对输入保留原始增量；不以处理包时的当前光标位置覆盖历史位置。同一时刻的事件保持原顺序；一屏按下、另一屏释放属于同一配对。全显示屏录制不因前台窗口改变而暂停。

开始与继续前等待现有物理按键和鼠标按钮释放，控制操作自身不加入轨迹。暂停停止追加事件并扣除暂停时长。输入队列及轨迹事件数有界；达到事件上限或无法写入时结束当前分段，提供编辑和保存入口。

| 情况 | 状态与处理 |
|---|---|
| 用户暂停 / 继续 | recording → pausing → paused；同一拓扑且授权有效时继续原时间轴 |
| 显示器插拔、排列、主屏、方向、分辨率或 DPI 改变 | 结束接收，进入 interrupted；reason=DISPLAY_TOPOLOGY_CHANGED，partial=true |
| 离开单屏或窗口录制范围 | 结束当前分段，reason=RECORDING_SCOPE_LEFT；未配对输入进入时间轴修复 |
| 目标关闭、撤权、锁屏、休眠或桌面切换 | 进入 interrupted，返回对应原因 |
| 用户结束 | record.finish 封存资源；完整轨迹形成资产，中断分段进入编辑草稿 |
| Agent 或 Engine 重启 | 恢复已写入分段；重新选择范围并创建新录制 |
| 输入队列溢出或确认接收通道失效 | 进入 interrupted，reason 分别为 INPUT_QUEUE_OVERFLOW / INPUT_CAPTURE_LOST，partial=true；保存缺口前的分段 |

拓扑变化后的旧会话不能通过 record.resume 继续。用户可保存分段，再刷新显示范围开始新会话；新接入显示屏不进入旧会话。record.snapshot 的控制版本随状态变化递增，事件计数更新不改变控制版本。

## 连续笔画

按住按钮期间，每个中间位置、转折、停顿和跨屏运动均属于笔画内容。录制、封存与回放保持完整顺序；不能只保存首尾或在界面抽样后使用预览数据回放。回放在按钮持续按住期间逐点发送，最后一个运动完成对应后端的原生提交后才释放。采集来源、原生调度、Windows 消息合并和验收详见[连续指针与笔画](pointer-motion.md)。

## 轨迹与编辑

新录制的 Trajectory 保存 source。1.0 使用全局物理坐标或整数相对量；1.1 另支持带 displayId 的屏内像素、小数位置和明确来源单位的相对量。source 包含显示几何与必要输入语义，不含令牌、凭据或窗口句柄。裁剪和编辑保持来源坐标上下文。裁剪起点取此前最后一个可确定的物理位置，若其后存在无法换算的相对事件则 initialPointer=null，回放前在轨迹编辑器设置起点。

不带 source 的轨迹沿用当前目标下的原坐标。带 source 的跨屏轨迹在包导入、裁剪、版本保存和编辑提交中保持来源布局，不能将其移除后按单屏轨迹解释。

## 回放映射

input.trajectory 的 displayMapping 省略时采用 original。有 source 的轨迹在输入前比较来源显示身份、后端和对应坐标几何：全局拓扑检查物理 bounds、方向和 DPI，屏内拓扑检查 pixelSize、layoutBounds、方向及变换；绝对位置必须属于授权显示区域。全显示屏轨迹绑定 kind=desktop 的输入目标；在授权屏幕间切换前台窗口属于同一目标。

布局不同的绝对轨迹可选择 displayLocal：逐一将来源屏绑定到当前屏，按屏内物理像素偏移平移。映射必须完整、一对一且处于当前输入权限内。屏幕方向不同、事件超出映射目标或落在空隙时拒绝启动。映射不缩放轨迹，也不把越界点夹到主屏边缘。包含 pointer.delta 的轨迹使用原布局，不按像素平移设备增量。

Engine 在 run.prepare 和正式启动时检查资源、映射与当前拓扑，并固定到运行快照。Agent 发送前再次核对目标代次；输入后端将已核对的位置转换到当前原生坐标；Windows 的全局绝对输入再按虚拟桌面原点和尺寸归一化，覆盖负坐标。回放中的拓扑变化进入暂停及输入释放流程，重新准备后再执行。各平台的权限、锁屏、桌面与会话检查覆盖全部显示屏。1.1 坐标、跨后端映射和 Wayland 全局录制条件见[跨平台架构](../architecture/platforms.md)。

## 界面与验收

录制准备展示录制范围、真实比例的显示屏布局、方向和缩放信息、坐标模式以及开始操作。浮条显示覆盖屏数和录制状态。时间轴展示来源布局并进入回放映射；布局变化页面提供保存分段、刷新屏幕和新建录制。

入口见[录制准备](../prototypes/index.html#record-config)、[屏幕布局](../prototypes/index.html#record-displays)、[布局变化](../prototypes/index.html#record-topology-changed)及[回放映射](../prototypes/index.html#trajectory-displays)。Q04、Q16、Q48、Q50～Q52 覆盖混合 DPI、负坐标、横竖屏、镜像、屏间空隙、跨屏按住与拖拽、拓扑变化和映射失败。

## 跨平台准入

全部显示屏录制要求实际后端能够覆盖完整桌面事件流。Portal 仅返回用户选择的部分屏幕时不能创建 allDisplays 来源。record.start 前读取 desktop.capabilities；屏内坐标需要已协商轨迹 1.1。跨屏采样不重置按钮或单调时钟，热插拔、缩放、方向、采集流或 EI region 变化使当前映射失效。

physical 与 display 坐标模式分别使用各自拓扑；event 根据来源坐标空间决定绝对事件表达，不能混用无显示身份的全局点和带显示身份的屏内点。relative 原样保存来源单位，跨后端不自动换算。displayLocal 映射允许全局物理来源与屏内来源之间按实际屏内像素偏移转换，目标当前变换与权限在发送前重新取得。
