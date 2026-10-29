(() => {
"use strict";
const U=window.DW_UI,{esc,icon,btn,iconBtn,pill,field,check,notice,table,search,progress,square,targetScene}=U;
function details(items){return '<div class="info-grid">'+items.map(([k,v])=>'<div class="info-item">'+k+'<strong>'+v+'</strong></div>').join("")+'</div>'}
function choices(items){return '<div class="stack">'+items.map(([title,desc,selected])=>'<label class="choice '+(selected?'selected':"")+'"><input type="radio" name="dialog-choice" '+(selected?'checked':"")+'><span><strong>'+title+'</strong><p>'+desc+'</p></span></label>').join("")+'</div>'}
function dialogData(s){
 let body="",subtitle="",wide=false;
 switch(s.id){
 case 'create-workspace':
 subtitle='流程、资产和运行记录保存在独立目录。';
 body='<div class="stack">'+field('工作空间名称','个人自动化')+'<div class="field"><span class="field-label">保存位置</span><div class="inline-control"><input class="fake-input" aria-label="保存位置" value="文档 / 自动化">'+btn('选择目录','native-folder','small','folder')+'</div><small>将创建“个人自动化”目录。</small></div>'+notice('本地保存','无需连接模型服务即可编辑、录制和匹配模板。')+'</div>';break;
 case 'new-flow':
 subtitle='为这段操作命名，之后可以随时修改。';
 body='<div class="stack">'+field('流程名称','月度报表下载')+'<div class="section-label">起点</div>'+choices([['空白流程','从一个动作开始编排。',true],['从录制开始','选择目标，录制后进入时间轴。',false],['复制已有流程','保留动作与资源引用，创建独立草稿。',false]])+'</div>';break;
 case 'draft-recovery':
 subtitle='发现关闭前未保存为版本的编辑。';
 body='<div class="stack">'+table(['草稿','保存时间','内容'],[['<label class="check"><input type="checkbox" checked>月度报表下载</label>','今天 09:41','调整 2 个参数'],['<label class="check"><input type="checkbox">账单附件整理</label>','昨天 16:25','新增 1 个节点']])+notice('恢复为新草稿','已有不可变版本继续保留。')+'</div>';break;
 case 'expression':
 subtitle='只访问当前调用中可见的数据。';
 body='<div class="stack">'+btn('编辑复合表达式','expression-builder')+'<div class="token-input"><span class="muted">取值路径</span><br>变量 › 定位结果 › 已选候选 › 位置</div>'+field('数据路径','变量 / 定位结果 / 已选候选 / 位置')+'<div class="panel"><div class="panel-head">可用数据</div>'+table(['来源','名称','类型'],[['输入参数','月份','文本'],['局部变量','定位结果','定位对象'],['上一步输出','已选候选','位置对象']])+'</div>'+notice('表达式有效','结果类型：位置对象，可用于点击位置。','green')+'</div>';break;
 case 'flow-parameters':
 wide=true;subtitle='参数由运行时填写；局部变量属于本次调用。';
 body='<div class="stack"><div class="between"><h3>输入参数</h3>'+btn('添加参数','','small','plus')+'</div>'+table(['名称','类型','默认值','必填'],[['月份','文本','2026-10','✓'],['等待时长','整数','5000 ms','—']],'param-table')+'<div class="form-grid">'+field('参数名称','月份')+field('类型','文本','',{select:['文本','整数','数字','布尔','数组','对象']})+'</div>'+field('默认值','2026-10')+check('运行时必须提供',true)+check('敏感参数',false,'在观察与诊断中遮蔽显示')+'<div class="divider"></div><div class="between"><h3>局部变量</h3>'+btn('添加变量','','small','plus')+'</div>'+table(['名称','初始值'],[['下载已完成','否'],['定位结果','空']])+btn('运行共享变量','run-shared-declarations')+'</div>';break;
 case 'target-select':
 subtitle='绑定到本次选择的具体窗口。';
 body='<div class="stack"><div class="row">'+btn('窗口','target-select','small primary')+btn('屏幕或区域','target-display','small')+'<span class="spacer"></span>'+btn('刷新','','small','refresh')+'</div>'+search('搜索窗口')+[['报表中心','当前前台窗口 · 普通权限',true],['文件管理器','文档',false],['管理工具','需要更高权限',false]].map(([t,d,a])=>'<button class="target-card '+(a?'active':"")+'" type="button" data-go="'+(t==='管理工具'?'permissions-elevation':'target-select')+'"><span class="target-thumb"><div></div><div></div><div></div></span><span><strong>'+t+'</strong><small>'+d+'</small></span><span class="spacer"></span>'+icon(a?'check':'target',15)+'</button>').join("")+notice('输入目标','开始输入前重新核对窗口、焦点和显示位置。')+'</div>';break;
 case 'target-display':
 subtitle='图像与输入使用明确的显示范围。';
 body='<div class="stack"><div class="row">'+btn('窗口','target-select','small')+btn('屏幕或区域','target-display','small primary')+'</div><div class="monitor-grid"><div class="monitor active"><span>1</span><small>主显示器</small></div><div class="monitor"><span>2</span><small>扩展显示器</small></div></div>'+details([['主显示器','1920 × 1080 · 100%'],['扩展显示器','2560 × 1440 · 150%']])+choices([['仅选择区域','在所选显示器中框选图像范围。',true],['整个显示器','使用所选显示器的完整范围。',false]])+'</div>';break;
 case 'run-config':
 subtitle='这次运行固定版本与依赖，不受后续草稿编辑影响。';
 body='<div class="stack">'+details([['流程','月度报表下载'],['版本','版本 12'],['目标','报表中心'],['依赖','下载按钮 v3 · 校验下载结果 v2']])+'<div class="divider"></div><div class="form-grid">'+field('月份','2026-10')+field('等待时长（毫秒）','5000')+'</div><div class="between"><h3>目标与预算</h3>'+btn('更换目标','target-select','small')+'</div><div class="panel">'+table(['项目','本次生效值'],[['主目标','报表中心'],['主动时间上限','10 分钟'],['节点执行上限','10,000 次']])+'</div>'+check('窗口关闭后在后台继续',false)+btn('接管与中断策略','run-interrupt-policy','small')+'<div class="row">'+icon('shield',15)+'<span style="font-size:11px;color:var(--green)">目标有效 · 急停热键可用</span></div></div>';break;
 case 'run-from-node':
 subtitle='所选节点：点击下载。';
 body='<div class="stack">'+notice('缺少执行所需的前置结果','该节点需要“定位结果”，当前没有有效的定位对象。','amber')+table(['前置条件','状态'],[['主目标',pill('已绑定','green')],['流程版本',pill('已固定','green')],['定位结果',pill('缺失','amber')]])+'<p class="muted" style="font-size:12px">填写缺失的定位结果后重新检查，或返回定位步骤。</p></div>';break;
 case 'history':
 subtitle='不可变版本独立保留；恢复会创建新草稿。';
 body='<div class="stack">'+[['版本 12','当前版本 · 今天 09:42','调整匹配阈值与超时'],['版本 11','昨天 16:25','补充下载后置检查'],['版本 10','10月27日 11:30','创建校验子流程'],['版本 9','10月25日 15:20','首次完整流程']].map(([t,d,summary],i)=>'<div class="history-row"><span class="history-dot"></span><div class="history-main"><div class="between"><strong>'+t+'</strong>'+(!i?pill('当前','blue'):"")+'</div><p>'+d+'</p><p style="color:var(--text)">'+summary+'</p><div class="row">'+btn('比较','version-diff','small')+btn('恢复为新草稿','editor-graph','small')+'</div></div></div>').join("")+'</div>';break;
 case 'save-conflict':
 subtitle='另一个窗口提交了更新的版本。';
 body='<div class="stack">'+notice('当前草稿尚未覆盖','比较差异后选择保留当前编辑的方式。','amber')+table(['内容','状态'],[['当前草稿','2 处参数变化'],['草稿基于','版本 12'],['工作空间最新','版本 13']])+choices([['另存为分支草稿','保留当前编辑，与最新版本分别继续。',true],['查看并合并差异','对照节点与字段后决定如何保留。',false]])+'</div>';break;
 case 'close-unsaved':
 subtitle='当前文档有编辑，且存在活动运行。';
 body='<div class="stack"><div class="file-row">'+square('flow')+'<div><strong>月度报表下载</strong><small>当前草稿 · 2 处修改</small></div>'+pill('未保存','amber')+'</div>'+choices([['保存为新版本','确认保存成功后关闭。',true],['保留恢复草稿','下次打开时继续编辑。',false],['放弃本次草稿修改','保留最近已保存的版本。',false]])+'<div class="divider"></div><div class="row">'+pill('1 次运行中','blue')+'<span style="font-size:12px">月度报表下载 · 版本 12</span></div>'+check('让该运行在后台继续',false,'未选择时先停止并确认释放，再关闭窗口。')+'</div>';break;
 case 'delete-node':
 subtitle='定位下载按钮';
 body='<div class="stack">'+table(['影响','内容'],[['入边','等待报表加载 → 此节点'],['成功出口','点击下载'],['错误出口','返回未就绪']])+notice('删除将同时移除相关连线','模板资产仍被其他流程引用，继续保留。','amber')+check('重新选择前后步骤的连接方式',true)+'</div>';break;
 case 'asset-references':
 subtitle='下载按钮 · v3';
 body='<div class="stack">'+table(['引用对象','版本','位置'],[['月度报表下载','版本 12','定位下载按钮'],['下载检查','版本 2','查找下载入口'],['账单附件整理','版本 7','开始下载']])+notice('按固定内容版本引用','修改资产会产生新版本，不改变已经启动的运行。')+'</div>';break;
 case 'asset-import':
 subtitle='选择图像或输入轨迹文件。';
 body='<div class="stack"><div class="upload-area">'+icon('upload',28)+'<strong>拖入文件，或选择本地文件</strong><span>PNG、JPEG、WebP 或轨迹 JSON</span></div><div class="file-row">'+square('image')+'<div><strong>下载按钮.png</strong><small>126 × 40 px · 4.2 KB</small></div><span class="spacer"></span>'+pill('格式有效','green')+'</div>'+field('资产名称','下载按钮')+check('保留为新资产，不覆盖同名内容',true)+'</div>';break;
 case 'package-import':
 subtitle='月度报表下载任务包';
 body='<div class="stack"><div class="file-row">'+square('file')+'<div><strong>月度报表下载.dwpack</strong><small>2 个流程 · 2 个模板</small></div><span class="spacer"></span>'+pill('校验通过','green')+'</div>'+table(['检查项','结果'],[['内容长度与摘要',pill('一致','green')],['主流程与依赖',pill('完整','green')],['文件路径与类型',pill('有效','green')]])+notice('导入后重新绑定目标','工作空间凭据与活动运行不在任务包中。')+'</div>';break;
 case 'package-conflict':
 subtitle='检测到同名流程，内容与当前版本不同。';
 body='<div class="stack">'+table(['对象','当前内容','包内内容'],[['月度报表下载','版本 12','新内容'],['下载按钮','v3','不同图像']])+choices([['作为新对象导入','为不同内容建立新对象，保留现有版本。',true],['只复用相同内容','相同摘要的资源可复用，不同内容另建。',false]])+field('新流程名称','月度报表下载（导入）')+'</div>';break;
 case 'package-export':
 subtitle='固定流程版本与完整依赖闭包。';
 body='<div class="stack">'+field('流程版本','版本 12','',{select:['版本 12','版本 11','版本 10']})+table(['包含内容','版本'],[['月度报表下载','版本 12'],['校验下载结果','版本 2'],['下载按钮','v3'],['下载完成提示','v2']])+'<div class="field"><span class="field-label">保存位置</span><div class="inline-control"><input class="fake-input" value="文档 / 导出" aria-label="保存位置">'+btn('选择','native-folder','small')+'</div></div>'+check('包含流程依赖资产',true)+notice('导出范围','仅包含所选版本及依赖，不包含密钥或无关运行记录。')+'</div>';break;
 case 'draft-export':
 subtitle='保留当前源码原文与上一次有效结构。';
 body='<div class="stack">'+notice('草稿未保存到工作空间','选择另一个有空间的位置保存副本。','amber')+check('当前源码原文（.txt）',true,'包含尚未通过校验的输入')+check('最后有效流程（.json）',true,'保留可解析结构与资源逻辑引用')+'<div class="field"><span class="field-label">保存位置</span><div class="inline-control"><input class="fake-input" value="文档 / 草稿副本" aria-label="草稿副本保存位置">'+btn('选择目录','native-folder','small')+'</div></div>'+field('副本名称','月度报表下载-草稿')+'</div>';break;
 case 'asset-delete':
 subtitle='下载按钮 · v3';
 body='<div class="stack">'+notice('当前版本仍被 3 个流程引用','不能删除仍用于历史版本、运行或备份的内容。','amber')+table(['使用位置','版本'],[['月度报表下载','版本 12'],['下载检查','版本 2'],['账单附件整理','版本 7']])+btn('查看完整引用','asset-references','','link')+'</div>';break;
 case 'emergency':
 subtitle='所有新的桌面输入已锁定。';
 body='<div class="stack">'+notice(window.DW_RELEASE_UNCERTAIN?'已撤销桌面控制，输入释放尚未确认':'已撤销桌面控制，输入释放已完成',window.DW_RELEASE_UNCERTAIN?'请先接管目标，核对按键与按钮状态。原效果记录继续保留。':'相关运行已停止。解除锁定不会自动恢复原运行。','red')+details([['受影响流程','月度报表下载'],['当前输入',window.DW_RELEASE_UNCERTAIN?'待人工核对':'已释放'],['目标','报表中心'],['恢复方式','本地确认后重新创建运行']])+'<div class="divider"></div><label class="check"><input type="checkbox" data-confirm-guard><span>我已接管目标，并确认所有按键和按钮均已释放</span></label></div>';break;
 case 'target-lost':
 subtitle='报表中心窗口已关闭或发生变化。';
 body='<div class="stack">'+notice('运行已暂停，输入已释放','保留此前已执行的结果。重新绑定后仍需核对继续条件。','amber')+details([['当前流程','月度报表下载'],['停止位置','校验下载结果']])+choices([['重新绑定目标','选择同一任务的新窗口，再检查执行位置。',true],['结束本次运行','保留记录，之后创建新运行。',false]])+'</div>';break;
 case 'desktop-busy':
 subtitle='当前桌面正在执行另一项任务。';
 body='<div class="stack"><div class="file-row">'+square('play')+'<div><strong>账单附件整理</strong><small>财务工作台 · 正在录入表单</small></div>'+pill('运行中','blue')+'</div>'+notice('桌面输入按任务独占','可以查看当前任务或明确加入运行队列。单次输入请求遇忙即拒绝。')+'<div class="row">'+btn('加入运行队列','run-queued','','clock')+btn('返回编辑','editor-graph')+'</div></div>';break;
 case 'connection-edit':
 subtitle='连接仅供显式模型观察使用。';
 body='<div class="stack">'+field('连接名称','本地视觉服务')+field('服务地址','http://127.0.0.1:11434/v1')+field('模型','vision-local')+field('密钥模式','无密钥','',{select:['无密钥','保留现有密钥','替换密钥']})+field('新的 API 密钥','','只在替换密钥时填写。',{password:true})+'<div class="between"><span class="muted" style="font-size:11px">最近测试：可以连接，模型可用</span>'+btn('高级参数','connection-advanced','small')+btn('测试连接','connection-tested','small','connect')+'</div></div>';break;
 case 'connection-failed':
 subtitle='本地视觉服务';
 body='<div class="stack">'+notice('127.0.0.1:11434 拒绝连接','检查本地服务是否已启动，以及端点配置是否正确。','red')+details([['模型','vision-local'],['已保存配置','继续保留']])+btn('重试测试','settings-connections','','refresh')+'</div>';break;
 case 'mcp-approval':
 subtitle='本地集成客户端 · 个人自动化';
 body='<div class="stack">'+field('目标范围','报表中心','',{select:['报表中心','重新选择目标']})+'<div class="section-label">允许的操作</div>'+check('读取流程与版本',true)+check('创建与控制运行',true)+check('采集所选目标',true)+check('单次桌面输入',false)+check('使用远程模型连接',false)+'<div class="divider"></div>'+field('授权期限','本次连接','',{select:['本次连接','1 小时']})+notice('授权只作用于所选对象','断开或撤销后，旧权限不能重新生效。')+'</div>';break;
 case 'backup-create':
 subtitle='使用数据库一致快照和对应对象集合。';
 body='<div class="stack">'+field('工作空间','个人自动化')+field('备份名称','个人自动化-2026-10-29')+'<div class="field"><span class="field-label">备份位置</span><div class="inline-control"><input class="fake-input" value="备份 / 个人自动化" aria-label="备份位置">'+btn('选择目录','native-folder','small')+'</div></div>'+details([['流程与资产','12 个流程 · 28 份资产'],['最近备份','今天 08:30']])+check('完成后校验对象摘要',true)+'</div>';break;
 case 'backup-restore':
 subtitle='先校验备份，再恢复到独立的新目录。';
 body='<div class="stack"><div class="file-row">'+square('database')+'<div><strong>个人自动化-2026-10-29</strong><small>今天 08:30 · 一致备份</small></div>'+pill('可读取','green')+'</div>'+field('恢复后的工作空间名称','个人自动化（恢复）')+'<div class="field"><span class="field-label">新目录</span><div class="inline-control"><input class="fake-input" value="文档 / 个人自动化（恢复）" aria-label="恢复新目录">'+btn('选择目录','native-folder','small')+'</div></div>'+notice('保留原工作空间','恢复后重新绑定目标，不恢复桌面租约或活动输入。')+'</div>';break;
 case 'update-available':
 subtitle='DeskWeaveStudio 0.1.0 → 0.2.0';
 body='<div class="stack">'+details([['更新内容','工作台与本地组件'],['数据兼容','需要格式检查']])+'<div class="divider"></div><h3>本次更新</h3><ul class="release-points"><li>改进编辑器响应与键盘操作。</li><li>完善录制与恢复入口。</li><li>包含相应组件与依赖更新。</li></ul>'+notice('当前还有 1 次运行','安装前将确认草稿保存、运行停止、输入释放与备份。','amber')+'</div>';break;
 case 'permissions-elevation':
 subtitle='管理工具需要更高完整性级别的桌面控制。';
 body='<div class="stack"><div class="row"><span class="square-icon">'+icon('shield',21)+'</span><div><strong>等待 Windows 权限确认</strong><p class="muted" style="font-size:11px;margin-top:5px">请在系统权限提示中完成操作。</p></div></div>'+table(['准备项','状态'],[['原输入通道',pill('已停止','green')],['已有按键与按钮',pill('已释放','green')],['文档与运行记录',pill('已保留','green')]])+notice('未确认前不发送新输入','取消后可返回普通目标选择。')+'</div>';break;
 case 'command-palette':
 subtitle='当前工作空间 · 月度报表下载';
 body='<div class="stack">'+search('搜索命令','录制')+'<div class="action-list">'+[['record','开始录制','record-config','Ctrl + Alt + R'],['mouse','打开轨迹编辑','trajectory-editor',''],['target','选择录制目标','target-select',''],['keyboard','设置录制热键','settings-shortcuts','']].map(([ic,t,to,key],i)=>'<button type="button" class="command-row '+(!i?'active':"")+'" data-go="'+to+'">'+icon(ic,17)+t+(key?'<kbd>'+key+'</kbd>':"")+'</button>').join("")+'</div></div>';break;
 case 'shortcuts-help':
 subtitle='有效按键与键盘等价操作';
 body=table(['任务','快捷键或方式'],[['保存版本','<kbd>Ctrl</kbd> + <kbd>S</kbd>'],['打开命令','<kbd>Ctrl</kbd> + <kbd>K</kbd>'],['步骤单步','<kbd>F10</kbd>'],['全局急停','<kbd>Ctrl</kbd> + <kbd>Alt</kbd> + <kbd>Pause</kbd>'],['添加与连接步骤','步骤列表 → 操作菜单'],['离开弹窗','<kbd>Esc</kbd>，返回触发控件']]);break;
 case 'support-export':
 subtitle='导出前查看范围与文件清单。';
 body='<div class="stack">'+check('应用与系统版本',true)+check('所选运行摘要',true)+check('附带所选失败帧',false)+table(['文件','内容'],[['版本信息.txt','应用、系统和组件版本'],['运行摘要.json','流程名称、时间与状态'],['问题摘要.txt','已选择的具体错误与处理结果']])+notice('默认不包含敏感内容','密钥、键盘文本和无关截图不进入支持包。')+field('保存位置','文档 / 支持材料')+'</div>';break;
 case 'storage-error':
 subtitle='个人自动化 · 月度报表下载';
 body='<div class="stack">'+notice('保存失败：所在磁盘空间不足','当前草稿仍保留在编辑会话中，尚未确认为已保存。','red')+details([['未保存内容','2 处参数修改'],['最近有效版本','版本 12']])+'<div class="row">'+btn('存储设置','settings-storage','','database')+btn('重试保存','editor-graph','','refresh')+'</div><p class="muted" style="font-size:12px">可以将当前源码和最后有效结构保存到另一个位置。</p></div>';break;
 default:throw new Error("Missing dialog "+s.id);
 }
 return {body,subtitle,wide};
}
window.DW_DIALOG=s=>{
 const d=dialogData(s),drawer=s.view==='drawer';
 const target=s.id==='emergency'?'workspace':s.primary;
 let primary=btn(s.primaryLabel,target,'primary');
 if(s.id==='emergency')primary='<button type="button" class="btn primary" data-confirm-button data-go="workspace" disabled>确认接管并解除锁定</button>';
 if(s.id==='package-import')d.body+=btn('预览对象映射','package-target-map');

 if(s.id==='update-available')d.body+=btn('通道与信任校验','update-trust');
 if(s.id==='asset-delete')primary=btn('保留资产','assets','primary');
 if(s.id==='close-unsaved')primary=btn('保存并处理运行后关闭','workspace','primary').replace('data-go=','data-ve-close-studio data-go=');
 if(s.id==='run-from-node')primary=btn('填写前置值','run-start-values','primary');
 return '<div class="modal-shade '+(drawer?'drawer-shade':"")+'"><section class="modal '+(drawer?'drawer':d.wide?'wide':"")+'" role="dialog" aria-modal="true" aria-labelledby="dialog-title"><header class="modal-header"><div><h2 id="dialog-title">'+esc(s.title)+'</h2><p>'+esc(d.subtitle)+'</p></div>'+iconBtn('close','关闭对话框',s.parent||'editor-graph')+'</header><div class="modal-body">'+d.body+'</div><footer class="modal-footer">'+btn('取消',s.parent||'editor-graph')+primary+'</footer></section></div>';
};
})();
