(() => {
"use strict";
const U=window.DW_UI,V=window.DW_VIEWS,{esc,icon,btn,iconBtn,pill,field,notice,table,tabs,section,search,progress,square,shell,targetScene}=U;
const states={
running:['正在运行','blue','校验下载结果','00:04.8'],
paused:['已暂停','amber','定位完成提示','00:04.8'],
failed:['运行失败','red','点击下载','00:02.1'],
succeeded:['已完成','green','返回下载结果','00:06.2'],
stopping:['正在停止','amber','等待子调用退出','00:04.8'],
interrupted:['运行已中断','amber','核对下载结果','00:04.8'],
disconnected:['连接已中断','amber','断线前：校验下载结果','00:04.8'],
queued:['排队中','blue','等待当前桌面任务','—'],
pausing:['正在暂停','amber','等待调用树安全点','00:04.8'],
cancelled:['已取消','muted','已停止并释放输入','00:04.8'],
'cleanup-failed':['运行失败','red','输入释放未确认','00:04.8']
};
V.runlist=s=>{
 const empty=s.variant==='empty';
 const rows=[
 ['月度报表下载','版本 12',pill('正在运行','blue'),'今天 09:42','00:04.8','run-running'],
 ['账单附件整理','版本 7',pill('已完成','green'),'今天 09:20','00:18.3','run-succeeded'],
 ['月度报表下载','版本 11',pill('运行失败','red'),'昨天 16:25','00:05.1','run-failed'],
 ['截图巡检','版本 3',pill('已取消'),'昨天 15:10','00:03.6','run-cancelled'],
 ['批量数据录入','版本 4',pill('运行已中断','amber'),'10月27日 11:30','00:12.4','run-interrupted']
 ];
 return shell(s,'<main class="page">'+section('运行','查看固定版本的执行与结果',btn('选择流程运行','workspace','primary','play'))+tabs([['全部运行','runs'],['进行中','run-running'],['需要处理','run-interrupted']],'runs')+'<div class="filterbar">'+search('搜索流程或目标')+btn('全部状态　⌄','runs')+btn('最近 7 天　⌄','runs')+'<span class="spacer"></span>'+btn('清理附件','settings-storage','ghost','database')+'</div><div class="panel">'+(empty?'<div class="empty"><div class="empty-icon">'+icon('play',32)+'</div><h3>还没有运行记录</h3><p>选择一个已保存流程，确认参数和目标后开始。</p>'+btn('选择流程','workspace','primary')+'</div>':table(['流程','固定版本','状态','开始时间','耗时',''],rows.map(r=>['<div class="cell-main">'+square('flow')+'<strong>'+r[0]+'</strong></div>',r[1],r[2],'<span class="muted">'+r[3]+'</span>','<span class="mono">'+r[4]+'</span>',btn('查看',r[5],'small')])))+'</div><div class="list-footer">共 '+(empty?'0':'28')+' 次运行<span class="spacer"></span>'+btn('1','','small')+btn('2','','small')+iconBtn('chevron','下一页','runs')+'</div></main>',{active:'runs'});
};
function stateNotice(v){
 if(v==='failed')return notice('点击下载：当前目标未获得输入许可','系统拒绝本次发送，后续步骤未执行。输入已释放，请重新选择目标后创建新运行。','red');
 if(v==='interrupted')return notice('点击下载的结果尚未确认','原生控制进程中断。先查看目标应用的下载记录，再决定是否创建新的重试运行。','amber');
 if(v==='disconnected')return notice('与工作空间的连接已中断','显示最近一次同步的数据。重新连接后将按当前快照恢复。','amber');
 if(v==='cleanup-failed')return notice('输入释放未确认，新的输入已锁定','原生控制连接已中断，无法确认 Ctrl 左是否释放。请接管目标并完成本地确认。','red');
 if(v==='stopping')return notice('正在停止所有调用','不再提交新输入；正在等待输入释放和后台任务退出。','amber');
 if(v==='pausing')return notice('暂停请求已接收','正在停止后续输入并等待全调用树到达安全点。','amber');
 if(v==='queued')return notice('前方还有 1 个任务','当前桌面由“账单附件整理”使用。可以保留排队或取消。');
 if(v==='paused')return notice('已在断点暂停','所有调用已到达安全点，输入已释放。可查看变量或单步执行。','amber');
 if(v==='succeeded')return notice('流程已完成，后置检查通过','下载完成提示已出现，子流程已结束，输入已释放。','green');
 if(v==='cancelled')return notice('已按请求停止','保留已发生的结果，所有输入已释放。');
 return "";
}
V.run=s=>{
 const v=s.variant,st=states[v]||states.running;
 const terminal=['failed','succeeded','cancelled','interrupted','cleanup-failed'].includes(v);
 let actions=(v==='paused'?btn('单步','run-paused','','step')+btn('继续','run-running','primary','play'):terminal?btn(v==='succeeded'?'再次运行':'修改流程',v==='succeeded'?'run-config':'editor-graph',v==='succeeded'?'primary':""):v==='disconnected'?btn('重新连接','run-running','primary','refresh'):v==='queued'?btn('取消排队','run-cancelled','','close'):v==='running'?btn('共享变量','run-shared-variables')+btn('暂停','run-pausing','','pause'):'<button type="button" class="btn" disabled>'+esc(st[0])+'…</button>')+(!terminal?btn('停止','run-stopping','','stop'):"")+btn('急停','emergency','danger','stop');
 const events=[
 ['09:42:10','运行已接收','固定使用版本 12 和所选资源','check'],
 ['09:42:10','桌面控制已就绪','报表中心 · 主目标','target'],
 ['09:42:11','定位下载按钮','找到唯一候选，算法分数 0.97','image'],
 ['09:42:12','点击下载','输入已发送，等待后置检查','mouse'],
 ['09:42:13','校验下载结果',v==='succeeded'?'已发现下载完成提示':v==='paused'?'在定位完成提示前暂停':v==='cancelled'?'已取消，未返回结果':v==='stopping'?'正在取消子调用':v==='pausing'?'等待调用树安全点':v==='disconnected'?'最近同步：正在检查完成提示':'正在检查完成提示','flow']
 ];
 if(v==='failed')events.splice(3,2,['09:42:12','点击下载','本次输入被拒绝，后续步骤未执行','alert']);
 if(v==='interrupted')events.splice(3,2,['09:42:12','点击下载','发送过程中连接中断，目标结果待核对','alert']);
 if(v==='queued')events.splice(1,4,['09:42:10','任务已排队','等待桌面可用，尚未发送输入','clock']);
 const content='<main class="page run-page">'+section('月度报表下载','运行 / 今天 09:42 · 报表中心',actions)+
 '<div class="run-summary"><div><span>状态</span><strong>'+pill(st[0],st[1])+'</strong></div><div><span>固定版本</span><strong>版本 12</strong></div><div><span>当前步骤</span><strong>'+st[2]+'</strong></div><div><span>主动耗时</span><strong class="mono">'+st[3]+'</strong></div><div><span>输入清理</span><strong>'+pill(v==='cleanup-failed'||v==='interrupted'?'未确认':v==='disconnected'?'最近同步：运行中':v==='stopping'||v==='pausing'?'处理中':v==='queued'?'无需清理':v==='running'?'运行中':'已释放',v==='cleanup-failed'?'red':v==='interrupted'||v==='disconnected'?'amber':v==='stopping'||v==='pausing'?'amber':v==='running'||v==='queued'?'':'green')+'</strong></div></div>'+
 stateNotice(v)+'<div class="run-layout"><aside class="panel run-tree"><div class="panel-head">调用树 '+pill(v==='queued'?'0':'2')+'</div><div class="run-tree-body"><div class="tree-call root">'+icon('down',12)+icon('flow',15)+'月度报表下载</div><div class="tree-caption">主流程 · 版本 12</div><div class="tree-call">'+icon(v==='queued'?'clock':'check',13)+'等待报表加载</div><div class="tree-call">'+icon(v==='queued'?'clock':'check',13)+'定位下载按钮</div><div class="tree-call '+(v==='failed'?'active':'')+'">'+icon(v==='failed'||v==='interrupted'?'alert':v==='queued'?'clock':'check',13)+'点击下载</div><div class="tree-call '+(['queued','failed','interrupted'].includes(v)?'muted':'active')+'">'+icon('down',12)+icon('flow',14)+'校验下载结果</div><div class="tree-call child '+(v==='paused'?'breakpoint':"")+'">'+icon(v==='paused'?'pause':'image',13)+'定位完成提示</div><div class="tree-call child muted">'+icon('arrow',13)+'返回校验结果</div></div><div class="run-tree-foot">'+btn('查看固定流程','editor-graph','small','flow')+'</div></aside><section class="stack"><div class="panel"><div class="panel-head"><div class="row">执行时间线 '+pill(v==='succeeded'?'7 个步骤':'4 个步骤')+'</div>'+btn('查看附件','asset-image','small','image')+'</div><div class="event-list">'+events.map((e,i)=>'<div class="event-item"><span class="event-time">'+e[0]+'</span><span class="event-icon '+(v==='failed'&&i===3||v==='interrupted'&&i===3?'red':"")+'">'+icon(e[3],14)+'</span><div><strong>'+e[1]+'</strong><p>'+e[2]+'</p></div>'+(v==='succeeded'||v!=='queued'&&i<events.length-1?'<span class="spacer"></span>'+pill('完成','green'):"")+'</div>').join("")+'</div></div><div class="panel"><div class="panel-head">输入与后置检查</div>'+table(['动作','系统输入','目标核对'],[
 ['点击下载',pill(v==='interrupted'?'尚未确认':v==='queued'?'尚未发送':v==='failed'?'已拒绝':'已发送',v==='interrupted'?'amber':v==='failed'?'red':v==='queued'?'':'blue'),pill(v==='succeeded'?'已核对':v==='queued'||v==='failed'?'未执行':'等待核对',v==='succeeded'?'green':'amber')],
 ['定位完成提示','不发送输入',v==='succeeded'?'下载完成提示已出现':['queued','failed','interrupted'].includes(v)?'未执行':v==='cancelled'?'已取消':'当前步骤']
 ])+'</div></section><aside class="panel run-inspector"><div class="panel-head">'+(v==='failed'?'失败详情':v==='succeeded'?'流程输出':'变量与断点')+'</div><div class="panel-body stack">'+(v==='failed'?'<div class="alert-icon red">'+icon('alert',24)+'</div><h3>目标未获得输入许可</h3><p class="muted" style="font-size:12px;line-height:1.9">窗口的权限或身份已变化，本次点击被拒绝，尚未发送输入。</p>'+btn('查看目标','target-select','','target')+btn('重新绑定目标','target-select','primary')+'<div class="divider"></div><div class="info-item">停止位置<strong>点击下载</strong></div>':v==='succeeded'?'<div class="output-result">'+icon('check',28)+'<strong>下载已核对</strong></div>'+table(['输出','值'],[['月份','2026-10'],['完成状态',pill('已完成','green')],['报表数量','1 份']]):'<div class="section-label">所选调用 · 校验下载结果</div>'+table(['输入','值'],[['月份','2026-10'],['主目标','报表中心']])+'<div class="divider"></div><div class="between"><span class="section-label" style="margin:0">局部变量</span>'+icon('lock',13)+'</div>'+table(['变量','值'],[['候选数量',v==='queued'?'—':'1'],['匹配分数',v==='queued'?'—':'0.97'],['结果已就绪',v==='succeeded'?'是':'等待返回']])+'<div class="divider"></div>'+checkBreakpoint(v)+btn('管理断点','run-paused','','pause'))+'</div></aside></div></main>';
 return shell(s,content,{active:'runs'});
};
function checkBreakpoint(v){return'<div class="breakpoint-line"><span class="breakpoint-dot"></span><div><strong>定位完成提示</strong><small>'+ (v==='paused'?'已命中断点':'节点执行前暂停')+'</small></div></div>'}
})();
