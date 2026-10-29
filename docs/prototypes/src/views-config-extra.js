(() => {
"use strict";
const U=window.DW_UI,V=window.DW_VIEWS,{esc,btn,pill,field,check,notice,table,section,shell}=U;
const panel=(title,body)=>'<section class="panel"><div class="panel-head">'+title+'</div><div class="panel-body stack">'+body+'</div></section>';
const json=value=>{
 const raw=typeof value==='string'?value:JSON.stringify(value,null,2);
 const color=line=>{
  const pattern=/("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null|-?\d+(?:\.\d+)?)\b/g;
  let out='',end=0;
  for(const m of line.matchAll(pattern)){
   out+=esc(line.slice(end,m.index));
   const kind=m[2]?'key':m[1]?'string':m[3]==='null'?'null':'number';
   out+='<span class="json-'+kind+'">'+esc(m[0])+'</span>';end=m.index+m[0].length;
  }
  return out+esc(line.slice(end));
 };
 return '<pre class="extra-json"><code>'+raw.split('\n').map(line=>'<span class="json-line">'+color(line)+'</span>').join('')+'</code></pre>';
};
const step=(text,done=true)=>'<div class="extra-check">'+pill(done?'通过':'需处理',done?'green':'amber')+'<span>'+text+'</span></div>';
const localButton=(label,action,kind='primary')=>'<button class="btn '+kind+'" type="button" data-extra="'+action+'">'+label+'</button>';
const formField=(label,value,name,options={})=>field(label,value,'',options).replace(/<(input|textarea|select)\b/,'<$1 data-extra-field="'+name+'"');
const DS=window.DW_DESIGN_STATE=window.DW_DESIGN_STATE||{};
const C=DS.connections=DS.connections||{version:3,connectionId:'local-vision',credentialGeneration:1,hasCredential:true,revokedCredentialGenerations:[],draft:{name:'本地视觉服务',endpoint:'http://127.0.0.1:11434/v1',model:'vision-local',temperature:.2,maxOutputTokens:2048,imageEncoding:'图像 URL',imageDetail:'自动',systemPrompt:'读取图像中的指定字段，按要求返回结果。',defaultPrompt:'读取报表月份、生成时间与下载状态。',extraBody:'{}',keyMode:'keep',revokePreviousCredential:false}};
const K=DS.tokens=DS.tokens||{items:[{id:'report-assistant',name:'报表助手',state:'valid',expires:'2026-10-30 09:00',permissions:['workflow.read','asset.read','run.read','observation.read'],targets:['报表中心']},{id:'research-client',name:'研究客户端',state:'expired',expires:'2026-10-28 18:00',permissions:['workflow.read'],targets:['报表中心']}],activeToken:'report-assistant',activeSessions:1,resourceSubscriptions:2,directInputPermits:0,lastRevocation:null,lastIssued:{id:'report-assistant',plaintext:'dws_example_4hY7G2wP9kM6vT8qR3sN5xJ1',displayOnce:true},sequence:0};
DS.runStateOverrides=DS.runStateOverrides||{};
const B=DS.mcpListener=DS.mcpListener||{transport:'streamableHttp',host:'127.0.0.1',port:8765};
const transportLabels={stdio:'stdio',streamableHttp:'Streamable HTTP',sse:'HTTP+SSE（旧客户端）'};
const transportKeys=Object.fromEntries(Object.entries(transportLabels).map(([key,label])=>[label,key]));
function protocolOverview(transport){
 const models=window.DW_CATALOG.mcpModels,profiles=models.protocolProfiles.filter(p=>p.transports.includes(transport));
 return table(['协议','版本'],profiles.map(p=>[p.era==='modern'?'主协议':'兼容协议',esc(p.version)]));
}
let pendingCredential='';
const permissionGroups=[['读取流程与资产',['workflow.read','asset.read']],['读取运行和观察记录',['run.read','observation.read']],['编辑流程',['workflow.write']],['保存模板与资产',['asset.write']],['创建及停止观察会话',['observation.control']],['创建及控制运行',['run.execute','run.control']],['采集指定目标',['capture.read']],['使用本地视觉计算',['vision.local']],['调用远程模型',['vision.remote']],['桌面输入',['desktop.input']],['停止桌面输入',['desktop.stop']]];
function credentialControls(){
 const mode=C.draft.keyMode;
 return formField('凭据处理',{keep:'保留现有凭据',replace:'替换凭据',none:'不使用凭据'}[mode],'key-mode',{select:['保留现有凭据','替换凭据','不使用凭据']})+
 formField('新凭据',pendingCredential,'credential',{password:true}).replace('<input','<input autocomplete="new-password"'+(mode!=='replace'?' disabled':''))+
 '<label class="check"><input type="checkbox" data-extra-revoke-credential'+(C.draft.revokePreviousCredential?' checked':'')+(mode==='keep'?' disabled':'')+'><span>同时撤销旧凭据并停止相关观察</span></label>';
}
function tokenFeedback(){
 const result=K.lastRevocation;if(!result)return '';
 return notice('令牌已撤销','已结束 '+result.affectedSessions+' 个网络会话、'+result.subscriptions+' 个资源订阅。','green')+
 table(['关联运行处置','结果'],[[result.stopOwnedRuns?'停止关联运行':'保留关联运行',result.stopOwnedRuns?'正在停止与清理输入':'保持已暂停状态']])+
 btn(result.stopOwnedRuns?'查看停止过程':'查看保留运行',result.stopOwnedRuns?'run-stopping':'run-paused');
}
function tokenView(s){
 const mode=s.variant,selected=K.items.find(x=>x.id===K.activeToken)||K.items[0];
 if(mode==='tokens')return section('客户端令牌','个人自动化',btn('创建令牌','mcp-token-create','primary'))+tokenFeedback()+panel('令牌清单',table(['名称','状态','到期时间','目标',''],K.items.map(item=>[esc(item.name),pill(item.state==='valid'?'有效':item.state==='revoked'?'已撤销':'已到期',item.state==='valid'?'green':item.state==='revoked'?'':'amber'),esc(item.expires),esc(item.targets.join('、')||'无采集目标'),item.state==='valid'?btn('撤销','mcp-token-revoke','small danger').replace('data-go=','data-extra-token="'+item.id+'" data-go='):btn('重新创建','mcp-token-create','small')]))+btn('返回 MCP 接入','settings-mcp'));
 if(mode==='token-create')return section('创建客户端令牌','选择工作空间内的能力与目标',btn('取消','mcp-tokens'))+'<div class="extra-columns">'+panel('名称与期限',formField('名称','报表助手','token-name')+formField('有效期','1 天','token-expiry',{select:['1 小时','1 天','7 天','30 天']})+formField('目标','报表中心','token-target',{select:['报表中心']})+'<div data-extra-feedback role="status"></div>'+localButton('创建令牌','token-issue'))+panel('能力范围',permissionGroups.map(([label,permissions],i)=>'<label class="check"><input type="checkbox" data-extra-permission="'+permissions.join(',')+'"'+(i<2?' checked':'')+'><span>'+label+'</span></label>').join(''))+'</div>';
 if(mode==='token-created'){
  const issued=K.lastIssued,item=K.items.find(x=>x.id===issued?.id)||selected,visible=!!issued?.displayOnce;
  return section('令牌已创建',esc(item.name)+' · 有效至 '+esc(item.expires),btn('完成','mcp-tokens','primary'))+'<div class="extra-columns">'+panel('保存令牌',(visible?notice('明文仅显示本次','关闭后只能撤销并重新创建。','amber')+json(issued.plaintext):notice('本次明文显示已结束','重新创建可取得新的令牌。'))+table(['项目','所选范围'],[['工作空间','个人自动化'],['目标',esc(item.targets.join('、')||'无采集目标')],['能力',esc(item.permissions.join('、'))]])+btn('已保存，返回清单','mcp-tokens','primary'))+panel('客户端配置',json({url:'http://127.0.0.1:8765/mcp',authorization:'Bearer <客户端令牌>'})+btn('查看工具目录','mcp-tools'))+'</div>';
 }
 return section('撤销客户端令牌',esc(selected.name),btn('取消','mcp-tokens'))+'<div class="extra-columns">'+panel('受影响对象',window.DW_COMPLETION.tokenImpactView(selected.id))+
 panel('关联运行',formField('运行处置','停止关联运行','token-stop-owned-runs',{select:['停止关联运行','保留关联运行']})+'<div data-extra-feedback role="status"></div>'+localButton('撤销令牌','token-revoke','danger'))+'</div>';
}
function navigation(active){
 return '<aside class="settings-side"><h3>设置与接入</h3>'+[['connection-advanced','模型高级配置'],['mcp-transport','MCP 传输'],['mcp-listener','监听与客户端'],['mcp-tokens','客户端令牌'],['mcp-tools','工具目录'],['mcp-resources','资源目录'],['mcp-prompts','预设任务']].map(([to,label])=>'<button class="settings-link '+(active===to?'active':'')+'" type="button" data-go="'+to+'">'+label+'</button>').join('')+'</aside>';
}
let previewTemplate=null;
const substitute=x=>typeof x==='string'?({'{{imageDataUrl}}':'data:image/png;base64,（内置图形）','{{imageBase64}}':'（内置图形编码）','{{detail}}':({'自动':'auto','低':'low','高':'high'}[C.draft.imageDetail]||'auto')}[x]||x):Array.isArray(x)?x.map(substitute):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,v])=>[k,substitute(v)])):x;
const template={type:'image_url',image_url:{url:'{{imageDataUrl}}',detail:'{{detail}}'}};
function connection(s){
 const bad=s.variant==='template-error',preview=s.variant==='message-preview',test=s.variant==='connection-tested',d=C.draft;
 if(s.variant==='advanced')return section('模型高级配置',esc(d.name)+' · 配置版本 '+C.version,btn('返回连接','settings-connections')+btn('消息模板','connection-template'))+
 '<div class="extra-columns">'+panel('模型与采样',formField('连接名称',d.name,'connection-name')+formField('端点',d.endpoint,'endpoint')+formField('模型',d.model,'model')+'<div class="form-grid">'+formField('采样温度',d.temperature,'temperature',{type:'number'})+formField('输出上限（Token）',d.maxOutputTokens,'max-output',{type:'number'})+'</div>'+formField('图像编码',d.imageEncoding,'image-encoding',{select:['图像 URL','直接 Base64','自定义片段']})+formField('图像细节',d.imageDetail,'image-detail',{select:['自动','低','高']})+credentialControls()+btn('编辑图像消息','connection-template'))+
 panel('提示与附加参数',formField('系统内容',d.systemPrompt,'system-prompt',{area:true})+formField('默认观察任务',d.defaultPrompt,'default-prompt',{area:true})+formField('附加参数',d.extraBody,'extra-body',{area:true})+'<div data-extra-feedback role="status"></div><div class="row">'+localButton('测试连接','connection-test','')+localButton('保存配置','connection-save')+'</div>')+'</div>';
 if(test)return section('连接测试',esc(d.name),btn('返回配置','connection-advanced'))+'<div class="extra-columns">'+panel('测试结果',notice('模型可用','已完成小型合成请求。','green')+step('配置结构有效')+step('端点连接成功')+step('模型已返回有效消息')+table(['项目','结果'],[['耗时','420 毫秒'],['图像消息','支持'],['测试材料','内置图形']])+localButton('保存连接','connection-save'))+panel('发送内容预览',json({model:d.model,messages:[{role:'user',content:'返回图形的颜色名称。'}],max_tokens:32})+btn('编辑图像消息','connection-template'))+'</div>';
 return section(preview?'图像消息预览':'图像消息模板',esc(d.name)+' · 自定义图像片段',btn('高级参数','connection-advanced')+btn('字段错误示例','connection-template-error'))+
 '<div class="extra-columns">'+panel('图像内容结构',formField('JSON 模板',bad?'{"type":"image_url","url":"{{unknown}}"}':JSON.stringify(previewTemplate||template,null,2),'template',{area:true})+formField('图像细节',d.imageDetail,'image-detail',{select:['自动','低','高']})+'<div data-extra-feedback role="status">'+(bad?notice('无法保存模板','占位符 unknown 未登记；请选择图像数据或细节占位符。','red'):'')+'</div><div class="row">'+localButton('校验并预览','template-preview')+btn('返回配置','connection-advanced')+'</div>')+
 panel(preview?'构造后的消息':'可用占位符',preview?json({role:'user',content:[{type:'text',text:d.defaultPrompt},substitute(previewTemplate||template)]}):table(['占位符','替换内容'],[['imageDataUrl','完整图像数据 URL'],['imageBase64','图像编码内容'],['detail','已选细节等级']])+notice('占位符占据完整字符串值','例如 "{{imageDataUrl}}"。')+localButton('查看消息预览','template-preview',''))+'</div>';
}
function transport(s){
 const bad=s.variant==='transport-error';
 return section('MCP 网络接入','工作空间：个人自动化',btn('返回客户端','settings-mcp')+btn('查看监听','mcp-listener'))+
 '<div class="extra-columns">'+panel('监听配置',formField('传输',transportLabels[B.transport],'transport',{select:Object.values(transportLabels)})+'<div class="form-grid">'+formField('监听地址',bad?'0.0.0.0':'127.0.0.1','host')+formField('端口','8765','port',{type:'number'})+'</div>'+formField('允许的 Host','127.0.0.1','hosts')+formField('允许的 Origin','https://client.example','origins')+'<label class="check"><input type="checkbox" data-extra-field="tls"><span>启用 TLS</span></label><div data-extra-feedback role="status">'+(bad?notice('无法启用监听','非回环地址需要 TLS 证书和私钥。','red'):'')+'</div>'+localButton('校验并启用','transport-enable')+'<div class="row">'+btn('证书配置','mcp-tls')+btn('查看配置错误','mcp-transport-error')+'</div>')+
 panel('客户端连接','<div data-mcp-protocols>'+protocolOverview(B.transport)+'</div>'+table(['传输','入口'],[['Streamable HTTP','/mcp'],['stdio','客户端进程连接'],['HTTP+SSE（旧客户端）','/sse']])+notice('网络访问使用客户端令牌','创建令牌时选择能力、目标和期限。')+btn('管理客户端令牌','mcp-tokens')+btn('工具与资源','mcp-tools'))+'</div>';
}
function toolInfo(name){
 const tool=window.DW_CATALOG.mcpModels.tools.find(x=>x.name===name)||window.DW_CATALOG.mcpModels.tools[0];
 return notice(tool.title,tool.objectAuthorization)+table(['项目','定义'],[['工具',esc(tool.name)],['方法',esc(tool.method)],['能力',esc(tool.permissions.join(', ')||'按请求对象检查')]])+json({inputSchema:tool.parameters,outputSchema:tool.result});
}
function resourceInfo(name){
 const r=window.DW_CATALOG.mcpModels.resources.find(x=>x.name===name)||window.DW_CATALOG.mcpModels.resources[0];
 return table(['项目','值'],[['资源',esc(r.name)],['媒体类型',esc(r.mimeType)],['订阅',r.subscribable?'支持':'不支持']])+json({uriTemplate:r.uriTemplate,queryParameters:r.queryParameters,defaults:r.defaults,resultSchema:r.resultSchema})+notice('对象授权',r.authorization);
}
function promptFields(name){
 const prompt=window.DW_CATALOG.mcpModels.prompts.find(x=>x.name===name)||window.DW_CATALOG.mcpModels.prompts[0];
 const labels={description:'任务描述',targetName:'目标名称',workflowId:'流程',issue:'待排查问题',elementTypes:'元素类型'};
 const values={description:'下载指定月份的经营报表并检查完成状态。',targetName:'报表中心',workflowId:'月度报表下载',issue:'目标绑定失效后如何恢复运行。',elementTypes:'按钮、选择框与状态图标'};
 return prompt.arguments.map(a=>formField(labels[a.name]+(a.required?'（必填）':''),values[a.name]||'',a.name,{area:['description','issue'].includes(a.name)})).join('');
}
function mcp(s){
 const mode=s.variant;
 if(['tokens','token-create','token-created','token-revoke'].includes(mode))return tokenView(s);
 if(['transport','transport-error'].includes(mode))return transport(s);
 if(mode==='tls')return section('TLS 证书','MCP 接入',btn('返回监听配置','mcp-transport'))+'<div class="extra-columns">'+panel('证书材料',field('证书','尚未选择')+btn('选择证书','','')+field('私钥','尚未选择')+btn('选择私钥','','')+field('证书域名','studio.example')+btn('应用证书','mcp-transport','primary'))+panel('有效性检查',step('证书与私钥配对',false)+step('证书有效期覆盖当前时间',false)+step('监听名称匹配证书',false))+'</div>';
 if(mode==='listener'||mode==='listener-error')return section('MCP 监听与客户端','个人自动化',btn('编辑配置','mcp-transport')+btn('停止监听','settings-mcp','danger'))+'<div class="extra-columns">'+panel('接入状态',notice(mode==='listener-error'?'监听启动失败':B.transport==='stdio'?'进程接入已配置':'正在监听',mode==='listener-error'?'端口 8765 已被占用，请更换端口后启用。':B.transport==='stdio'?'由客户端启动服务进程':B.host+':'+B.port+(B.transport==='sse'?'/sse':'/mcp')+' · '+transportLabels[B.transport],mode==='listener-error'?'red':'green')+protocolOverview(B.transport)+table(['项目','当前值'],[['绑定工作空间','个人自动化'],['TLS',B.transport==='stdio'?'不使用网络监听':'关闭 · 仅回环'],['旧版会话',mode==='listener-error'?'0':String(K.activeSessions)]])+btn('修改配置','mcp-transport')+btn('启动失败状态','mcp-listener-error'))+
 panel('客户端与操作',table(['客户端','协议','状态','操作'],[['报表助手','2025-11-25',pill(K.activeSessions?'已连接':'已断开',K.activeSessions?'green':''),btn('查看活动','mcp-activity','small')],['研究客户端','2026-07-28',pill('令牌已到期','amber'),btn('管理令牌','mcp-tokens','small')]])+btn('能力目录','mcp-tools'))+'</div>';
 if(mode==='tools')return section('MCP 工具目录',window.DW_CATALOG.mcpModels.tools.length+' 个工具',btn('分页查询','mcp-query')+btn('资源','mcp-resources')+btn('预设任务','mcp-prompts'))+
 '<div class="extra-columns">'+panel('选择工具','<label class="field"><span class="field-label">工具</span><select data-mcp-tool-select>'+window.DW_CATALOG.mcpModels.tools.map(x=>'<option value="'+esc(x.name)+'">'+esc(x.title)+'</option>').join('')+'</select></label><div data-mcp-tool-info>'+toolInfo()+'</div>')+
 panel('能力分组',table(['任务','能力'],[['流程','目录、读取、校验、保存和导出'],['运行','准备、启动、快照、暂停、继续、停止'],['桌面','截图、匹配、单次观察、输入与急停'],['模板','提取、候选、命名、提交与导出'],['观察','会话、记录、导出和取消'],['资源','内容读取、作业查询与请求恢复']])+btn('查看资源目录','mcp-resources'))+'</div>';
 if(mode==='resources')return section('MCP 资源目录','12 类资源模板 · 按对象授权读取',btn('工具','mcp-tools')+btn('预设任务','mcp-prompts'))+
 '<div class="extra-columns">'+panel('选择资源','<label class="field"><span class="field-label">资源</span><select data-mcp-resource-select>'+window.DW_CATALOG.mcpModels.resources.map(x=>'<option>'+esc(x.name)+'</option>').join('')+'</select></label><div data-mcp-resource-info>'+resourceInfo()+'</div>')+
 panel('内容预览',table(['字段','值'],[['类型','流程版本'],['名称','月度报表下载 · 版本 12'],['媒体类型','application/json'],['读取方式','分块读取 · 每块至多 192 KiB']])+json({name:'月度报表下载',revision:12,entry:'wait',parameters:['月份']})+btn('预览观察记录','observations-detail')+btn('打开对应流程','editor-source'))+'</div>';
 return section('MCP 预设任务','按已选任务填写对应参数',btn('返回目录','mcp-tools'))+
 '<div class="extra-columns">'+panel('任务参数','<label class="field"><span class="field-label">任务</span><select data-mcp-prompt-select>'+window.DW_CATALOG.mcpModels.prompts.map(x=>'<option value="'+esc(x.name)+'">'+esc(x.title)+'</option>').join('')+'</select></label><div class="stack" data-prompt-fields>'+promptFields()+'</div><div data-extra-feedback role="status"></div>'+localButton('预览消息','prompt-preview'))+
 panel('消息预览','<div data-prompt-preview>'+json({role:'user',content:'填写参数后预览消息。'})+'</div>'+btn('打开流程编辑','editor-graph')+btn('打开模板制作','templates-source'))+'</div>';
}
function execution(s){
 const mode=s.variant;
 if(mode==='shared')return section('运行共享变量','月度报表下载 · 草稿',btn('局部变量','flow-parameters')+btn('返回画布','editor-graph','primary'))+
 '<div class="extra-columns">'+panel('变量声明',table(['名称','类型','初始值','范围'],[['下载计数','数值','0','本次运行'],['下载已完成','布尔','否','本次运行'],['批次摘要','对象','{}','本次运行']])+field('显示名称','下载计数')+field('类型','数值','',{select:['数值','布尔','字符串','对象','数组']})+field('初始值','0')+btn('保存声明','editor-graph','primary'))+
 panel('读写行为',step('根流程与子流程访问同一次运行的共享值')+step('写入同时检查读取时的版本')+step('流程退出后保留在运行记录')+btn('查看赋值动作','action-data-assign')+btn('预览运行中的变量','run-shared-variables'))+'</div>';
 if(['start-values','start-ready','start-stale'].includes(mode)){
  const ready=mode==='start-ready',stale=mode==='start-stale';
  return section('从指定节点开始','月度报表下载 · 版本 12',btn('返回画布','editor-graph'))+
 '<div class="extra-columns">'+panel('入口与初始值',field('起始节点','点击下载','',{select:['点击下载','定位下载按钮']})+field('月份','2026-10')+formField('已定位的水平位置','1280','start-x',{type:'number'})+formField('已定位的垂直位置','210','start-y',{type:'number'})+field('下载已完成','否','',{select:['否','是']})+btn('选择目标','target-select')+'<div data-extra-feedback role="status"></div>'+localButton('检查前置条件','start-prepare'))+
 panel('检查结果',stale?notice('准备结果已过期','目标窗口已改变，请重新检查后启动。','red')+btn('重新检查','run-start-values','primary'):ready?notice('可创建新运行','入口、初始值与目标已固定。','green')+step('月份和定位结果已填写')+step('报表中心可采集与输入')+step('流程版本及依赖有效')+btn('开始新运行','run-running','primary')+btn('目标变化状态','run-start-stale'):notice('需要定位结果','此入口跳过了“定位下载按钮”，请填写本次位置并重新检查。','amber')+table(['项目','状态'],[['流程版本','版本 12'],['目标窗口','报表中心'],['定位结果','需要填写']]))+'</div>';
 }
 if(mode==='interrupt')return section('运行接管与中断','设置应用于新运行',btn('返回运行准备','run-config'))+
 '<div class="extra-columns">'+panel('物理操作策略',field('检测到鼠标或键盘操作','暂停运行','',{select:['暂停运行','忽略普通操作']})+notice('持有输入冲突仍会暂停','目标失焦、锁屏、权限变化、停止和急停继续生效。')+btn('应用到新运行','run-config','primary'))+panel('当前选择',table(['项目','行为'],[['普通物理操作','暂停并释放已登记输入'],['目标窗口失焦','暂停并重新确认'],['停止','释放并等待清理完成'],['急停','撤销租约、释放并锁定']])+btn('查看暂停状态','run-paused'))+'</div>';
 if(mode==='variables'||mode==='variable-conflict')return section('运行共享变量','月度报表下载 · 运行中',btn('返回运行','run-running')+btn('查看并发冲突','run-variable-conflict'))+
 '<div class="extra-columns">'+panel('变量快照',table(['变量','当前值','版本'],[['下载计数','3','8'],['下载已完成','否','2'],['批次摘要','3 份已下载','5']])+pill('只读','blue')+btn('刷新快照','run-shared-variables'))+
 panel(mode==='variable-conflict'?'共享写入冲突':'调用视图',mode==='variable-conflict'?notice('下载计数写入失败','其他调用已更新此变量，请重新读取再计算。','red')+table(['项目','值'],[['本次期望版本','7'],['当前版本','8'],['写入结果','未应用']])+btn('重新读取变量','run-shared-variables','primary'):table(['调用','当前动作'],[['主流程','等待下载完成'],['校验下载结果','读取共享变量']])+btn('查看循环','run-loop'))+'</div>';
 if(mode==='loop')return section('条件循环监控','等待下载完成',btn('返回运行','run-running')+btn('停止','run-stopping','danger'))+'<div class="extra-columns">'+panel('循环状态',pill('第 3 轮','blue')+table(['项目','当前值'],[['循环方式','直到条件成立'],['退出条件','下载已完成 为 是'],['最大轮数','30'],['剩余主动时间','47.2 秒']])+step('每轮先清理输入，再处理退出或继续')+btn('查看条件循环配置','action-flow-loop'))+
 panel('本轮调用',table(['轮次','结果'],[['第 1 轮','未完成 · 继续'],['第 2 轮','未完成 · 继续'],['第 3 轮','正在检查']])+btn('查看共享变量','run-shared-variables'))+'</div>';
 return matchConfig(s);
}
const M=DS.match=DS.match||{scope:'目标内矩形',x:80,y:100,width:720,height:480,scales:[.8,1,1.25],threshold:.92,selection:'仅唯一候选'};
function readMatch(app){
 const v=name=>app.querySelector('[data-extra-field="'+name+'"]')?.value||'',scope=v('match-scope');
 const fields=['match-x','match-y','match-width','match-height'],values=fields.map(x=>Number(v(x)));
 if(scope==='目标内矩形'&&(fields.some(x=>!v(x))||values.some(x=>!Number.isInteger(x))||values[0]<0||values[1]<0||values[2]<1||values[3]<1||values[0]+values[2]>1280||values[1]+values[3]>720))throw Error('搜索区域必须位于目标图像内。');
 const text=v('match-scales'),scales=text.split(/[,，\s]+/).filter(Boolean).map(Number),threshold=Number(v('match-threshold'));
 if(!scales.length||scales.length>128||scales.some(x=>!Number.isFinite(x)||x<.1||x>4)||new Set(scales).size!==scales.length)throw Error('尺度需为0.1～4之间互不重复的数值，最多128项。');
 if(!v('match-threshold')||!Number.isFinite(threshold)||threshold<0||threshold>1)throw Error('匹配阈值需在0～1之间。');
 return {scope,x:values[0],y:values[1],width:values[2],height:values[3],scales,threshold,selection:v('match-selection'),roi:scope==='整个目标'?null:{space:'targetPixels',x:values[0],y:values[1],width:values[2],height:values[3]}};
}
function matchConfig(s){
 const bad=s.variant==='match-invalid';
 return section('匹配范围与尺度','下载按钮 · 匹配测试',btn('返回匹配','match-test'))+
 '<div class="extra-columns" data-extra-match-form>'+panel('搜索范围',formField('范围',M.scope,'match-scope',{select:['整个目标','目标内矩形']})+'<div class="form-grid">'+formField('左边界（像素）',M.x,'match-x',{type:'number'})+formField('上边界（像素）',M.y,'match-y',{type:'number'})+formField('宽度（像素）',M.width,'match-width',{type:'number'})+formField('高度（像素）',M.height,'match-height',{type:'number'})+'</div>'+btn('从图像框选','capture-overlay')+formField('模板尺度',bad?'1.0, 1.0':M.scales.join(', '),'match-scales')+btn('范围错误状态','match-config-error'))+
 panel('测试与结果',formField('阈值',M.threshold,'match-threshold',{type:'number'})+formField('候选选择',M.selection,'match-selection',{select:['仅唯一候选','最高分候选']})+'<div data-extra-feedback role="status">'+(bad?notice('配置未应用','移除重复尺度后重新测试。','red'):notice('配置有效','可以执行匹配测试或应用到节点。','green'))+'</div>'+localButton('测试匹配','match-test').replace('<button','<button'+(bad?' disabled':''))+localButton('应用到节点','match-apply','').replace('<button','<button'+(bad?' disabled':'')))+'</div>';
}
function checkConnection(){
 const d=C.draft,temperature=Number(d.temperature),maxOutputTokens=Number(d.maxOutputTokens);
 if(!d.name.trim()||d.name.length>120||!d.model.trim()||d.model.length>256)throw Error('连接名称和模型需要有效值。');
 let endpoint;try{endpoint=new URL(d.endpoint);}catch{throw Error('请输入完整服务地址。');}if(!['http:','https:'].includes(endpoint.protocol)||d.endpoint.length>256)throw Error('服务地址需使用HTTP或HTTPS。');
 if(String(d.temperature).trim()===''||!Number.isFinite(temperature)||temperature<0||temperature>2||!Number.isInteger(maxOutputTokens)||maxOutputTokens<1||maxOutputTokens>32768)throw Error('采样温度需为0～2，输出上限需为1～32768的整数。');
 if(d.systemPrompt.length>32768||d.defaultPrompt.length>32768)throw Error('提示内容超过32768个字符。');
 let extraBody;try{extraBody=JSON.parse(d.extraBody);}catch{throw Error('附加参数必须是JSON对象。');}if(!extraBody||typeof extraBody!=='object'||Array.isArray(extraBody)||Object.keys(extraBody).length>32)throw Error('附加参数必须是最多32项的JSON对象。');
 if(d.keyMode==='replace'&&(!pendingCredential||pendingCredential.length>8192))throw Error('替换凭据时需填写1～8192个字符的新凭据。');
 if(d.keyMode==='keep'&&d.revokePreviousCredential)throw Error('保留现有凭据时不能撤销该凭据。');
 if(d.imageEncoding==='自定义片段'&&!previewTemplate)throw Error('请先校验自定义图像消息模板。');
 return {provider:'openaiCompatible',systemPrompt:d.systemPrompt,defaultPrompt:d.defaultPrompt,temperature,maxOutputTokens,imageEncoding:{'图像 URL':'imageUrl','直接 Base64':'base64','自定义片段':'custom'}[d.imageEncoding],imageDetail:{'自动':'auto','低':'low','高':'high'}[d.imageDetail],imagePayloadTemplate:previewTemplate,extraBody};
}
const connectionFields={'connection-name':'name',endpoint:'endpoint',model:'model',temperature:'temperature','max-output':'maxOutputTokens','image-encoding':'imageEncoding','image-detail':'imageDetail','system-prompt':'systemPrompt','default-prompt':'defaultPrompt','extra-body':'extraBody'};
document.addEventListener('input',event=>{
 const field=event.target.dataset.extraField;if(connectionFields[field])C.draft[connectionFields[field]]=event.target.value;
 if(field==='credential')pendingCredential=event.target.value;
 const form=event.target.closest('[data-extra-match-form]');
 if(form){let valid=true;try{readMatch(form);}catch{valid=false;}form.querySelectorAll('[data-extra="match-test"],[data-extra="match-apply"]').forEach(button=>button.disabled=!valid);}
});

V['config-extra']=s=>{
 const connectionMode=s.variant.startsWith('connection')||['advanced','template','template-error','message-preview'].includes(s.variant);
 const body=connectionMode?connection(s):mcp(s);
 return shell(s,navigation(connectionMode?'connection-advanced':s.id)+'<main class="page extra-page">'+body+'</main>',{active:'settings'});
};
V['execution-extra']=s=>shell(s,'<main class="page extra-page">'+execution(s)+'</main>',{active:s.id.startsWith('run-')?'runs':'workspace'});
document.addEventListener('change',event=>{
 const app=event.target.closest('.app-window');if(!app)return;
 if(event.target.dataset.extraField==='transport'){
  const transport=transportKeys[event.target.value];
  app.querySelector('[data-mcp-protocols]').innerHTML=protocolOverview(transport);
  return;
 }
 const connectionField=event.target.dataset.extraField;if(connectionFields[connectionField])C.draft[connectionFields[connectionField]]=event.target.value;
 if(connectionField==='key-mode'){C.draft.keyMode={'保留现有凭据':'keep','替换凭据':'replace','不使用凭据':'none'}[event.target.value];if(C.draft.keyMode!=='replace')pendingCredential='';if(C.draft.keyMode==='keep')C.draft.revokePreviousCredential=false;window.DW_PROTOTYPE.go('connection-advanced');return;}
 if(event.target.matches('[data-extra-revoke-credential]'))C.draft.revokePreviousCredential=event.target.checked;
 const matchForm=event.target.closest('[data-extra-match-form]');if(matchForm){let valid=true;try{readMatch(matchForm);}catch{valid=false;}matchForm.querySelectorAll('[data-extra="match-test"],[data-extra="match-apply"]').forEach(button=>button.disabled=!valid);}
 if(event.target.matches('[data-mcp-tool-select]'))app.querySelector('[data-mcp-tool-info]').innerHTML=toolInfo(event.target.value);
 if(event.target.matches('[data-mcp-resource-select]'))app.querySelector('[data-mcp-resource-info]').innerHTML=resourceInfo(event.target.value);
 if(event.target.matches('[data-mcp-prompt-select]')){
  app.querySelector('[data-prompt-fields]').innerHTML=promptFields(event.target.value);
  app.querySelector('[data-prompt-preview]').textContent='填写参数后预览消息。';
 }
});
document.addEventListener('click',event=>{
 const token=event.target.closest('[data-extra-token]');if(token)K.activeToken=token.dataset.extraToken;
 if(event.target.closest('[data-go]')&&event.target.closest('.app-window')?.dataset.screen==='mcp-token-created'&&K.lastIssued)K.lastIssued.displayOnce=false;
 const action=event.target.closest('[data-extra]')?.dataset.extra;if(!action)return;
 const app=event.target.closest('.app-window'),feedback=app.querySelector('[data-extra-feedback]');
 const value=name=>app.querySelector('[data-extra-field="'+name+'"]')?.value||'';
 const fail=message=>{if(feedback)feedback.innerHTML=notice('请修正配置',message,'red')};
 if(action==='connection-save'||action==='connection-test'){
  try{
   const options=checkConnection();
   if(action==='connection-test'){C.testedOptions=options;window.DW_PROTOTYPE.go('connection-tested');return;}
   const previous=C.credentialGeneration,mode=C.draft.keyMode,revoke=C.draft.revokePreviousCredential;
   C.lastSave={connectionId:C.connectionId,expectedVersion:C.version,keyMode:mode,revokePreviousCredential:revoke,credentialProvided:mode==='replace'&&!!pendingCredential,options};
   if(mode!=='keep'){C.credentialGeneration++;C.hasCredential=mode==='replace';}
   if(revoke){C.revokedCredentialGenerations.push(previous);DS.cancelObservationsForCredential?.(C.connectionId,previous);}
   C.version++;C.savedOptions=options;pendingCredential='';C.draft.keyMode='keep';C.draft.revokePreviousCredential=false;
   window.DW_PROTOTYPE.go('settings-connections');
  }catch(error){fail(error.message);}return;
 }
 if(action==='token-issue'){
  const name=value('token-name').trim(),permissions=[...new Set([...app.querySelectorAll('[data-extra-permission]:checked')].flatMap(x=>x.dataset.extraPermission.split(',')))],expiresInSeconds={'1 小时':3600,'1 天':86400,'7 天':604800,'30 天':2592000}[value('token-expiry')];
  if(!name||name.length>120||!permissions.length)return fail('名称需为1～120个字符，并至少选择一项能力。');
  const issuedAt=new Date(),expiry=new Date(issuedAt.getTime()+expiresInSeconds*1000),id='token-'+(++K.sequence),item={id,name,state:'valid',createdAt:issuedAt.toISOString().replace(/\.\d{3}Z$/,'Z'),expiresAt:expiry.toISOString().replace(/\.\d{3}Z$/,'Z'),permissions,targets:[value('token-target')],expires:expiry.toLocaleString('sv-SE',{timeZone:'Asia/Shanghai'}).slice(0,16)};
  K.items.push(item);K.activeToken=id;K.lastIssued={id,displayOnce:true,plaintext:'dws_example_'+String(K.sequence).padStart(4,'0')+'_N6mT4vR8kP2sW7xQ'};K.lastIssue={name,permissions,targets:item.targets,expiresInSeconds};K.lastRevocation=null;window.DW_PROTOTYPE.go('mcp-token-created');return;
 }
 if(action==='token-revoke'){
  const item=K.items.find(x=>x.id===K.activeToken);if(!item||item.state!=='valid')return fail('所选令牌已失效。');
  const stopOwnedRuns=value('token-stop-owned-runs')==='停止关联运行';
  try{K.lastRevocation=window.DW_COMPLETION.api.revokeTokenProjection(item.id,stopOwnedRuns);}catch(error){return fail(error.message);}
  window.DW_PROTOTYPE.go('mcp-tokens');return;
 }
 if(action==='match-test'||action==='match-apply'){
  try{Object.assign(M,readMatch(app.querySelector('[data-extra-match-form]')));const params=window.DW_CATALOG.fixture.workflow.nodes.find(x=>x.id==='locate')?.params;if(params)Object.assign(params,{roi:M.roi,scales:M.scales,threshold:M.threshold,selection:M.selection==='仅唯一候选'?'unique':'highest'});window.DW_PROTOTYPE.go(action==='match-test'?'match-test':'node-properties');}catch(error){fail(error.message);}return;
 }
 if(action==='template-preview'){
  try{
   const parsed=JSON.parse(value('template'));
   const valid=new Set(['imageDataUrl','imageBase64','detail']);
   const walk=(x,depth=0)=>{if(depth>16)throw Error('消息结构超过 16 层。');if(typeof x==='string'){const hits=[...x.matchAll(/\{\{([^}]+)\}\}/g)];for(const hit of hits)if(!valid.has(hit[1])||hit[0]!==x)throw Error('占位符需使用完整字符串值，且名称已登记。')}else if(x&&typeof x==='object')Object.values(x).forEach(y=>walk(y,depth+1))};
   walk(parsed);previewTemplate=parsed;window.DW_PROTOTYPE.go('connection-message-preview');
  }catch(e){fail(e.message)}
 }
 if(action==='transport-enable'){
  const transport=transportKeys[value('transport')];
  if(!transport)return fail('请选择可用的接入方式。');
  if(transport==='stdio'){B.transport=transport;window.DW_PROTOTYPE.go('mcp-listener');return;}
  const host=value('host'),port=Number(value('port')),tls=app.querySelector('[data-extra-field="tls"]')?.checked;
  if(!Number.isInteger(port)||port<1024||port>65535)return fail('端口范围为 1024～65535。');
  if(!['127.0.0.1','localhost','::1'].includes(host)&&!tls)return fail('非回环监听需要 TLS 证书和私钥。');
  if(tls)return fail('请选择并检查证书与私钥后启用。');
  if(value('hosts').includes('*')||value('origins').includes('*'))return fail('Host 与 Origin 需填写具体值。');
  Object.assign(B,{transport,host,port});
  window.DW_PROTOTYPE.go('mcp-listener');
 }
 if(action==='start-prepare'){
  if(!Number.isInteger(Number(value('start-x')))||!Number.isInteger(Number(value('start-y')))||!value('start-x')||!value('start-y'))return fail('定位结果需填写完整的整数坐标。');
  window.DW_PROTOTYPE.go('run-start-ready');
 }
 if(action==='prompt-preview'){
  const prompt=window.DW_CATALOG.mcpModels.prompts.find(x=>x.name===app.querySelector('[data-mcp-prompt-select]').value);
  const values={...prompt.defaults};
  for(const a of prompt.arguments){
   const input=value(a.name).trim();
   if(a.required&&!input)return fail(a.description+'需要填写。');
   if(input.length>(prompt.argumentSchema.properties[a.name].maxLength||4000))return fail(a.description+'超出长度范围。');
   if(input)values[a.name]=a.name==='workflowId'&&input==='月度报表下载'?'monthly-report':input;
  }
  app.querySelector('[data-prompt-preview]').innerHTML=json(prompt.messages.map(x=>({role:x.role,content:{type:'text',text:x.content.textTemplate.replace(/\{([A-Za-z]+)\}/g,(_,key)=>values[key]||'')}})));
 }

});
const configFixture={connections:JSON.parse(JSON.stringify(C)),tokens:JSON.parse(JSON.stringify(K)),match:JSON.parse(JSON.stringify(M))};
DS.resetConfigForScreen=()=>{
 Object.assign(C,JSON.parse(JSON.stringify(configFixture.connections)));Object.assign(K,JSON.parse(JSON.stringify(configFixture.tokens)));Object.assign(M,JSON.parse(JSON.stringify(configFixture.match)));pendingCredential='';previewTemplate=null;DS.runStateOverrides={};
};
})();
