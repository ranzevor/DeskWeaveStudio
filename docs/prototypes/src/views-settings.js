(() => {
"use strict";
const U=window.DW_UI,V=window.DW_VIEWS,{esc,icon,btn,iconBtn,pill,field,check,notice,table,section,progress,square,shell}=U;
const settingsLinks=[['settings-general','sun','通用与外观'],['settings-shortcuts','keyboard','快捷键'],['settings-connections','connect','模型连接'],['settings-mcp','link','MCP 接入'],['settings-storage','database','存储与保留'],['settings-diagnostics','shield','诊断与支持']];
function row(title,desc,control){return'<div class="setting-row"><div><strong>'+title+'</strong>'+(desc?'<p>'+desc+'</p>':"")+'</div>'+control+'</div>'}
const toggle=on=>'<button type="button" class="switch '+(on?'on':"")+'" role="switch" aria-checked="'+(on?'true':'false')+'" aria-label="切换设置" data-local="toggle"></button>';
function general(){
 return section('通用与外观','设置在当前用户下生效')+'<div class="settings-card"><h3>外观</h3>'+row('主题','跟随系统或选择固定主题','<div class="theme-options">'+[['亮色','',true,'editor-graph'],['暗色','dark-swatch',false,'editor-dark'],['跟随系统','auto-swatch',false,'settings-general']].map(([t,c,a,to])=>'<button type="button" class="theme-option '+c+' '+(a?'active':"")+'" data-go="'+to+'"><div class="theme-mini"><i></i><span></span></div>'+t+'</button>').join("")+'</div>')+row('界面密度','调整列表、表单与控件间距',btn('标准','','small primary')+btn('宽松','','small'))+row('界面语言','切换后保留打开的文档','<div style="width:180px">'+field('','简体中文','',{select:['简体中文','English']})+'</div>')+row('减少动态效果','遵循系统设置',toggle(true))+'</div><div class="settings-card"><h3>工作台</h3>'+row('启动时恢复上次工作空间','只恢复文档和布局，不自动运行流程',toggle(true))+row('关闭窗口时询问后台运行','分别处理草稿和活动运行',toggle(true))+row('主题预览','查看小窗口与键盘操作方案',btn('查看窄窗口','editor-narrow','small'))+'</div>';
}
function shortcuts(){return section('快捷键','工作台命令与系统热键分别配置',btn('快捷键帮助','shortcuts-help','','help'))+'<div class="panel">'+table(['命令','有效按键','范围',''],[
 ['全局急停','<kbd>Ctrl</kbd> + <kbd>Alt</kbd> + <kbd>Pause</kbd>',pill('系统热键','green'),btn('修改','','small')],
 ['录制 / 结束','<kbd>Ctrl</kbd> + <kbd>Alt</kbd> + <kbd>R</kbd>',pill('系统热键','blue'),btn('修改','','small')],
 ['保存版本','<kbd>Ctrl</kbd> + <kbd>S</kbd>','工作台',btn('修改','','small')],
 ['命令菜单','<kbd>Ctrl</kbd> + <kbd>K</kbd>','工作台',btn('打开','command-palette','small')],
 ['步骤单步','<kbd>F10</kbd>','调试时',btn('修改','','small')]
 ])+'</div><div style="height:20px"></div>'+notice('新快捷键注册失败','Ctrl + Shift + R 已被其他程序使用。录制热键继续使用 Ctrl + Alt + R。','amber')+'<div class="settings-card"><h3>修改录制热键</h3>'+row('按下新组合键','当前候选：Ctrl + Shift + R','<div class="hotkey-edit"><kbd>Ctrl</kbd> + <kbd>Shift</kbd> + <kbd>R</kbd>'+btn('重新录入','','small')+'</div>')+'</div>'}
function connections(validation=false){return section('模型连接','流程只使用这里保存的连接',btn('高级参数','connection-advanced')+btn('添加连接','connection-edit','primary','plus'))+
 '<div class="connection-card">'+square('connect')+'<div><h3>本地视觉服务</h3><p>127.0.0.1:11434 · 本地推理</p><div class="connection-note">模型：vision-local · 无密钥</div></div><span class="spacer"></span>'+pill('可连接','green')+btn('编辑','connection-edit','small')+iconBtn('more','连接操作','connection-edit')+'</div>'+
 '<div class="connection-card">'+square('globe')+'<div><h3>远程视觉服务</h3><p>已配置远程端点</p><div class="connection-note">密钥已保存 · 最近测试：昨天 16:30</div></div><span class="spacer"></span>'+pill('需要测试','amber')+btn('测试','connection-failed','small')+btn('编辑','connection-edit','small')+'</div>'+
 '<div style="height:13px"></div>'+notice('按目标范围发送图像','每次观察使用明确选定的窗口或区域。连接设置不会自行授予桌面输入。')+
 (validation?'<div class="panel"><div class="panel-head">本地连接配置</div><div class="panel-body stack">'+field('连接名称','本地视觉服务')+field('服务地址','127.0.0.1:11434','请输入包含 http:// 或 https:// 的完整地址。',{invalid:true})+field('模型','vision-local')+'<div class="row">'+btn('取消','settings-connections')+'<button class="btn primary" type="button" disabled>保存连接</button></div></div></div>':"");
}
function mcp(activity=false){return section(activity?'客户端活动':'MCP 接入','本地 stdio 连接 · 工作空间：个人自动化',btn('传输配置','mcp-transport')+btn('能力目录','mcp-tools')+btn(activity?'返回客户端':'查看活动',activity?'settings-mcp':'mcp-activity','','link'))+
 '<div class="panel"><div class="panel-head"><div class="row">'+square('connect')+'本地集成客户端 '+pill('已连接','green')+'</div>'+btn('管理授权','mcp-approval','small')+'</div><div class="panel-body"><div class="info-grid"><div class="info-item">绑定工作空间<strong>个人自动化</strong></div><div class="info-item">目标范围<strong>报表中心</strong></div></div><div class="permission-tags">'+pill('读取流程','blue')+pill('创建运行','blue')+pill('截图','blue')+pill('桌面输入未授权')+'</div></div></div>'+
 '<div style="height:20px"></div>'+(activity?'<div class="panel">'+table(['时间','操作','结果',''],[['09:42:10','运行月度报表下载',pill('已接收','blue'),btn('查看运行','run-running','small')],['09:41:58','读取流程版本 12',pill('完成','green'),btn('查看流程','editor-graph','small')],['09:41:50','请求单次输入',pill('未授权','amber'),btn('管理授权','mcp-approval','small')]])+'</div>':'<div class="settings-card"><h3>连接控制</h3>'+row('当前连接','客户端退出后释放直接输入许可',btn('撤销授权','mcp-approval','small danger'))+row('新客户端请求','首次执行前确认对象与能力',toggle(true))+row('接入说明','工具、资源、取消和权限设置',btn('查看说明','help-docs','small','help'))+'</div>');
}
function storage(){return section('存储与保留','个人自动化 · 本地工作空间',btn('从备份恢复','backup-restore','','history')+btn('创建备份','backup-create','primary','database'))+
 '<div class="settings-card"><h3>空间使用</h3><div class="between" style="margin-top:20px"><div><span class="muted" style="font-size:11px">当前工作空间</span><h2 style="margin-top:7px">2.4 GiB</h2></div><span class="muted" style="font-size:11px">所在磁盘可用 32.6 GiB</span></div><div class="quota-bar"><span style="width:15%;background:#4e7ccc"></span><span style="width:65%;background:#91ade0"></span><span style="width:12%;background:#cad7ec"></span></div><div class="quota-legend"><span><i style="background:#4e7ccc"></i>流程与资产</span><span><i style="background:#91ade0"></i>运行附件</span><span><i style="background:#cad7ec"></i>可重建缓存</span></div></div>'+
 '<div class="settings-card"><h3>运行附件</h3>'+row('保留时长','只清理符合条件的运行附件','<div style="width:170px">'+field('','30 天','',{select:['30 天','90 天','手动管理']})+'</div>')+row('附件容量上限','活动运行和未决效果引用不受自动清理影响','<div style="width:170px">'+field('','5 GiB')+'</div>')+row('图像缓存','可重建预览与匹配缓存 · 上限 256 MiB',btn('清理缓存','','small'))+'</div>'+
 '<div class="settings-card"><h3>备份</h3>'+row('最近一次备份','今天 08:30 · 已校验',btn('查看与恢复','backup-restore','small'))+row('用户流程与模板','默认不按时间删除',pill('保留'))+'</div>';
}
function diagnostics(){return section('诊断与支持','选择所需材料后导出',btn('预览支持包','support-export','primary','download'))+
 '<div class="settings-card"><h3>本地组件</h3>'+row('工作空间服务','最近连接：今天 09:42',pill('已连接','green'))+row('桌面控制','普通权限 · 急停热键可用',pill('可用','green'))+row('模型连接','本地视觉服务',btn('查看连接','settings-connections','small'))+'</div>'+
 '<div class="settings-card"><h3>导出范围</h3>'+row('应用与系统版本','帮助确认组件兼容性',toggle(true))+row('所选运行的摘要','流程名称、时间、状态与具体失败原因',toggle(true))+row('截图附件','导出前逐项预览',toggle(false))+row('工程诊断材料','在支持包中单独列出，先检查敏感字段',toggle(false))+'</div>'+
 notice('导出前可查看文件清单','密钥、键盘输入内容和无关截图不纳入默认范围。');
}
V.settings=s=>{
 const variant=s.variant;
 const active=['validation','connections'].includes(variant)?'settings-connections':variant==='mcp-activity'?'settings-mcp':s.id;
 let body=variant==='general'?general():variant==='shortcuts'?shortcuts():variant==='connections'||variant==='validation'?connections(variant==='validation'):variant==='mcp'||variant==='mcp-activity'?mcp(variant==='mcp-activity'):variant==='storage'?storage():diagnostics();
 return shell(s,'<aside class="settings-side"><h3>设置</h3>'+settingsLinks.map(([to,ic,label],index)=>([0,2,4].includes(index)?'<span class="settings-group">'+({0:'个性化',2:'连接与集成',4:'数据与支持'}[index])+'</span>':'')+'<button type="button" class="settings-link '+(active===to?'active':"")+'" data-go="'+to+'">'+icon(ic,17)+label+'</button>').join("")+'</aside><main class="settings-content"><div class="settings-limit">'+body+'</div></main>',{active:'settings'});
};
})();
