(() => {
"use strict";
const U=window.DW_UI,V=window.DW_VIEWS,{esc,icon,btn,pill,field,check,notice,table,section,progress,shell,targetScene}=U;
const DS=window.DW_DESIGN_STATE=window.DW_DESIGN_STATE||{};
const T=DS.templates=DS.templates||{selected:[0,1,2,3],detail:0,version:1,expiresAt:Date.now()+1800000,policy:{mode:'ocrThenRule',prefix:'报表',startIndex:1,onCollision:'suffix',ocrUnavailable:'rule',languages:['chi_sim','eng']},overrides:{},proposal:null,operation:null,assets:null,batches:0,sequence:0,ocrAvailable:true};
const O=DS.observations=DS.observations||{sessionState:'running',sessionKey:'report-status',sessionName:'报表可用状态',connectionVersion:2,detached:false,stopReason:null,records:null,single:{state:'idle',sequence:0,record:null},query:{session:'all',sort:'newest',from:'2026-10-29T00:00',until:'2026-10-30T00:00',state:'全部状态'}};
const formField=(label,value,name,options={})=>field(label,value,'',options).replace(/<(input|textarea|select)\b/,'<$1 data-ve-field="'+name+'"');
const templateSelection=()=>T.selected.map(index=>({index,...candidates[index]}));
const templateExpired=()=>T.expiresAt<=Date.now();
function templateSignature(){return JSON.stringify({selected:T.selected,version:T.version,policy:T.policy,overrides:T.overrides});}
function buildNames(){
 const names=new Set(),items=templateSelection().map((c,index)=>{
  const mode=T.policy.mode,hasOCR=T.ocrAvailable&&mode==='ocrThenRule'&&c.index<3;
  const manual=T.overrides[c.index];
  let name=manual??(mode==='manual'?c.name:hasOCR?c.name:[T.policy.prefix,{button:'按钮',select:'选择框',status:'状态',icon:'图标'}[c.kind]||'控件',c.x+'x'+c.y,String(T.policy.startIndex+index).padStart(3,'0')].filter(Boolean).join('_'));
  const source=manual!==undefined||mode==='manual'?'manual':hasOCR?'ocr':'rule';
  name=name.normalize('NFC').trim();
  let conflict=!name||name.length>120||names.has(name)||name==='下载按钮';
  if(conflict&&T.policy.onCollision==='suffix'&&name){let suffix=2;while(names.has(name)||name==='下载按钮'){name=(manual||name).slice(0,110)+'_'+suffix++;}conflict=name.length>120;}
  names.add(name);
  return {...c,name,source,conflict,reason:source==='rule'&&mode==='ocrThenRule'?(T.ocrAvailable?'未识别到文字':'OCR 不可用，使用规则命名'):null};
 });
 T.proposal={signature:templateSignature(),version:T.version,items,canCommit:items.length>0&&!items.some(x=>x.conflict)&&!templateExpired(),id:'names-'+(++T.sequence)};
 return T.proposal;
}
function validNames(){return !!T.proposal&&T.proposal.signature===templateSignature()&&T.proposal.canCommit&&!templateExpired();}
function invalidateNames(){
 T.proposal=null;T.namingDirty=true;
 document.querySelectorAll('[data-ve-template-confirm]').forEach(button=>button.disabled=true);
 const box=document.querySelector('[data-ve-naming-feedback]');if(box)box.innerHTML=notice('名称预览已失效','重新生成预览后再确认。','amber');
}
function finishTemplateCommit(){
 if(!T.operation)beginTemplateCommit();
 if(T.operation?.state==='failed')return T.operation;
 if(T.operation&&T.operation.state!=='succeeded'){
  T.assets=T.operation.items.map(item=>({...item,assetVersion:'版本 1'}));
  T.operation.state='succeeded';T.operation.receiptKnown=true;T.operation.batch=++T.batches;
 }
 return T.operation;
}
function beginTemplateCommit(){
 if(!validNames())return null;
 if(T.operation&&T.operation.proposalId===T.proposal.id&&T.operation.state!=='failed')return T.operation;
 T.operation={id:'template-save-'+(++T.sequence),proposalId:T.proposal.id,state:'running',receiptKnown:true,items:T.proposal.items.map(x=>({...x}))};
 return T.operation;
}
function templateAssets(){return T.assets||[0,1,2,3].map((index)=>({...candidates[index],index,name:['下载按钮','月份选择','类型选择','可下载状态'][index],source:index===3?'rule':'ocr',assetVersion:'版本 1'}));}
const candidates=[
 {name:"下载所选",kind:"button",w:126,h:40,x:1040,y:172,score:.97,source:"轮廓 · 颜色",view:"download"},
 {name:"月份选择",kind:"select",w:150,h:36,x:230,y:172,score:.95,source:"轮廓 · 角点",view:"month"},
 {name:"类型选择",kind:"select",w:132,h:36,x:398,y:172,score:.94,source:"轮廓 · 角点",view:"type"},
 {name:"可下载状态",kind:"status",w:78,h:28,x:1028,y:272,score:.91,source:"颜色 · 轮廓",view:"ready"},
 {name:"报表图标",kind:"icon",w:32,h:32,x:242,y:270,score:.88,source:"角点 · 颜色",view:"file"},
 {name:"下一页",kind:"button",w:30,h:30,x:1136,y:490,score:.82,source:"轮廓 · 角点",view:"next"}
];
const num=(label,value,min,max,step="1")=>'<label class="field"><span class="field-label">'+esc(label)+'</span><input type="number" value="'+value+'" min="'+min+'" max="'+max+'" step="'+step+'" required></label>';
const action=(label,to,kind="",validate=false)=>'<button type="button" class="btn '+kind+'" data-go="'+to+'"'+(validate?' data-ve-validate':'')+'>'+esc(label)+'</button>';
const point=(label,value)=>'<div class="ve-point"><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong></div>';
const pair=(label,value)=>'<div class="ve-pair"><span>'+esc(label)+'</span><strong>'+value+'</strong></div>';
const panel=(title,body,extra="")=>'<section class="panel"><div class="panel-head"><span class="ve-panel-name"><span class="ve-panel-icon">'+icon(/来源|图像|截图|采集/.test(title)?'image':/候选|参数|策略|选项/.test(title)?'settings':/记录|结果|预览|检查/.test(title)?'list':/范围|目标|变化/.test(title)?'target':/连接|请求/.test(title)?'connect':'grid',14)+'</span>'+esc(title)+'</span>'+extra+'</div><div class="panel-body">'+body+'</div></section>';
const statusSteps=(active)=>'<nav class="ve-steps" aria-label="制作阶段">'+["固定来源","检测候选","筛选与命名","保存批次"].map((x,i)=>'<span class="'+(i===active?'current':i<active?'done':'')+'"><b>'+(i<active?'✓':i+1)+'</b>'+x+'</span>').join("")+'</nav>';
const blank=(ico,title,desc,actions)=>'<div class="ve-empty">'+icon(ico,46)+'<h3>'+esc(title)+'</h3><p>'+esc(desc)+'</p><div class="row">'+actions+'</div></div>';
function sample(c,large=false){
 const cls='ve-sample '+(large?'large ':'')+c.view;
 if(c.view==='download')return'<span class="'+cls+'">下载所选</span>';
 if(c.view==='month')return'<span class="'+cls+'">2026 年 10 月　⌄</span>';
 if(c.view==='type')return'<span class="'+cls+'">全部类型　⌄</span>';
 if(c.view==='ready')return'<span class="'+cls+'">可下载</span>';
 if(c.view==='file')return'<span class="'+cls+'">'+icon('file',large?52:28)+'</span>';
 return'<span class="'+cls+'">›</span>';
}
function header(s,sub,buttons){return '<div class="page-header"><div class="ve-heading"><div class="ve-path">'+icon(s.view==='templates-extra'?'image':'eye',13)+esc(sub)+'</div><h2>'+esc(s.title)+'</h2></div><div class="row">'+buttons+'</div></div>';}
function templateShell(s,content,stage=2,buttons=""){
 return shell(s,'<main class="page ve-page ve-templates ve-template-'+esc(s.variant)+'">'+header(s,'资产 / 模板生产',buttons||btn('返回资产','assets'))+statusSteps(stage)+content+'</main>',{active:'assets'});
}
function extractionConfig(s){
 const values=T.extractValues||[0,0,1280,720,.5,.3,.2,200,40000,.2,8,.75,.65,6,200,30000];
 return templateShell(s,'<form class="ve-layout" data-ve-form novalidate><div class="stack">'+
 panel('图像来源','<div class="ve-source-controls">'+field('来源类型','图像资产','',{select:['图像资产','本次授权采集帧']})+field('源图版本','报表中心截图 · 版本 3')+'</div><div class="ve-source-preview">'+targetScene('preview')+'<span class="ve-source-box"></span></div><div class="ve-metrics">'+point('原图尺寸','1280 × 720 px')+point('提取区域','整个图像')+point('来源时间','今天 09:35')+'</div>')+
 panel('区域范围','<div class="ve-form4">'+num('左侧（px）',values[0],0,1279)+num('顶部（px）',values[1],0,719)+num('宽度（px）',values[2],1,1280)+num('高度（px）',values[3],1,720)+'</div><div class="ve-inline-actions">'+btn('从授权帧选择区域','capture-overlay','','crop')+btn('选择其他图像','assets','','image')+'</div>')+
 '</div><aside class="stack">'+panel('候选检测参数','<div class="ve-param-group"><h4>特征融合权重</h4><div class="ve-form3">'+num('轮廓权重',values[4],0,1,"any")+num('颜色权重',values[5],0,1,"any")+num('角点权重',values[6],0,1,"any")+'</div></div><div class="ve-param-group"><h4>候选几何范围</h4><div class="form-grid">'+num('最小面积（px²）',values[7],4,67108864)+num('最大面积（px²）',values[8],4,67108864)+num('最小宽高比',values[9],.01,100,"any")+num('最大宽高比',values[10],.01,100,"any")+'</div></div><div class="ve-param-group"><h4>筛选与去重</h4><div class="form-grid">'+num('最低融合评分',values[11],0,1,"any")+num('重叠阈值',values[12],0,1,"any")+num('图像相似距离',values[13],0,64)+num('最多候选',values[14],1,2000)+num('检测超时（毫秒）',values[15],100,120000)+'</div></div>'+check('排除独立正文块',true)+'<p class="ve-field-error" data-ve-error aria-live="polite"></p>')+'</aside></form>',0,btn('取消','assets')+action('开始提取','templates-progress','primary',true));
}
function candidateGrid(){
 return '<div class="ve-candidate-toolbar"><div class="row">'+action('全选','templates-candidates').replace('data-go="templates-candidates"','data-ve-select="all"')+action('反选','templates-candidates').replace('data-go="templates-candidates"','data-ve-select="invert"')+action('取消所选','templates-candidates').replace('data-go="templates-candidates"','data-ve-select="none"')+'</div><label class="ve-score-label">最低评分 <select data-ve-score aria-label="最低候选评分"><option value="0">全部候选</option><option value="0.9">0.90 及以上</option><option value="0.95">0.95 及以上</option></select></label><span class="spacer"></span><strong data-ve-selection>已选 '+T.selected.length+' 项</strong></div><div class="ve-candidates">'+candidates.map((c,i)=>'<article class="ve-candidate" data-ve-card data-candidate="'+i+'" data-score="'+c.score+'"><div class="ve-candidate-top"><label><input type="checkbox" data-ve-candidate value="'+i+'" '+(T.selected.includes(i)?'checked':'')+' aria-label="选择'+c.name+'"> '+esc(c.name)+'</label>'+pill(c.score.toFixed(2),'green')+'</div><button class="ve-thumb checkerboard" type="button" data-go="templates-candidate" data-ve-candidate-open="'+i+'" aria-label="查看'+c.name+'原图边界">'+sample(c)+'</button><div class="ve-candidate-caption"><span>'+c.w+' × '+c.h+' px</span><span>'+c.source+'</span></div><div class="ve-candidate-score" aria-label="融合评分 '+c.score+'"><i style="width:'+(c.score*100)+'%"></i></div></article>').join("")+'</div><div class="ve-bottom"><span>显示 6 / 6 项 · 按评分与位置排序</span><span class="spacer"></span>'+'<button type="button" class="btn small" disabled>上一页</button>'+pill('第 1 页')+'<button type="button" class="btn small" disabled>下一页</button>'+'</div>';
}
function candidatesView(s){
 return templateShell(s,panel('报表中心截图 · 检测结果',candidateGrid(),pill('6 个候选','blue'))+
 '<div class="ve-bottom">'+point('提取用时','0.84 秒')+point('候选有效期',new Date(T.expiresAt).toLocaleTimeString('zh-CN',{hour12:false}))+point('选集来源','图像版本 3')+'<span class="spacer"></span>'+btn('重新设置','templates-source')+action('为所选命名','templates-naming','primary').replace('data-go=','data-ve-name-open data-ve-require-selection data-go=')+'</div>',2,btn('返回来源','templates-source')+action('命名预览','templates-naming','primary').replace('data-go=','data-ve-name-open data-ve-require-selection data-go='));
}
function namingRows(conflict=false){
 const proposal=T.proposal||{items:templateSelection().map(c=>({...c,name:T.overrides[c.index]??c.name,source:'manual',conflict:false,reason:null}))};
 return proposal.items.map(c=>[sample(c),'<input maxlength="120" minlength="1" required class="ve-name-input'+(c.conflict?' invalid':'')+'" aria-label="'+esc(c.name)+'的模板名称" value="'+esc(c.name)+'" data-ve-name="'+c.index+'">',pill({ocr:'OCR',rule:'规则命名',manual:'手工名称'}[c.source],c.source==='ocr'?'blue':''),c.conflict?pill('名称已占用','red'):c.reason?esc(c.reason):pill('可保存','green')]);
}
function namingView(s){
 if(!T.proposal&&s.variant==='naming'&&!T.namingDirty)buildNames();
 if(s.variant==='conflict'){T.policy.onCollision='reject';T.overrides[T.selected[0]??0]='下载按钮';buildNames();}
 const conflict=!!T.proposal?.items.some(c=>c.conflict),valid=validNames();
 const sources=T.proposal?.items||[],ocr=sources.filter(c=>c.source==='ocr').length,rule=sources.filter(c=>c.source==='rule').length,manual=sources.filter(c=>c.source==='manual').length;
 return templateShell(s,'<form class="ve-layout ve-naming" data-ve-form novalidate><div class="stack"><div data-ve-naming-feedback>'+(templateExpired()?notice('候选已过期','重新提取候选后生成名称。','amber'):conflict?notice('名称存在冲突','修改名称或选择追加后缀，再生成预览。','amber'):!valid?notice('名称预览已失效','重新生成预览后再确认。','amber'):"")+'</div>'+
 panel('所选模板的名称预览',table(['裁剪预览','模板名称','实际来源','检查结果'],namingRows(conflict))+'<div class="ve-inline-actions">'+btn('返回候选','templates-candidates')+btn('查看冲突示例','templates-conflict','','alert')+'</div>')+
 '<div class="ve-metrics">'+point('本次选集',T.selected.length+' 个模板')+point('命名来源','OCR '+ocr+' · 规则 '+rule+' · 手工 '+manual)+point('名称检查',valid?'全部通过':conflict?'存在冲突':'需重新预览')+'</div></div><aside class="stack">'+panel('命名策略','<div class="stack">'+
 formField('命名模式',{ocrThenRule:'OCR 后规则命名',rule:'仅规则命名',manual:'手工名称'}[T.policy.mode],'naming-mode',{select:['OCR 后规则命名','仅规则命名','手工名称']})+
 formField('OCR 语言','简体中文 + English','naming-languages')+formField('名称前缀',T.policy.prefix,'naming-prefix').replace('<input ','<input maxlength="80" ')+
 num('规则起始序号',T.policy.startIndex,0,999999).replace('<input ','<input data-ve-field="naming-index" ')+
 formField('名称重名时',T.policy.onCollision==='suffix'?'追加后缀':'返回冲突','naming-collision',{select:['追加后缀','返回冲突']})+
 formField('OCR 不可用时',T.policy.ocrUnavailable==='rule'?'使用规则命名':'停止命名','naming-unavailable',{select:['使用规则命名','停止命名']})+
 '<button class="btn primary" type="button" data-ve-names-preview>重新生成预览</button>'+btn('检查 OCR 环境','templates-ocr','','settings')+'</div>')+'</aside></form>',2,
 btn('返回候选','templates-candidates')+'<button class="btn primary" type="button" data-ve-template-confirm data-ve-validate'+(!valid?' disabled':'')+'>确认名称</button>');
}
function candidateDetail(s){
 const current=candidates[T.detail]||candidates[0];
 return templateShell(s,'<div class="ve-layout"><div class="stack">'+panel(current.name+' · 原始像素','<div class="ve-large-crop checkerboard"><div class="ve-crop-border">'+sample(current,true)+'<span class="ve-crop-size">'+current.w+' × '+current.h+' px</span></div></div><div class="ve-bottom">'+btn('−','','small')+pill('200%')+btn('+','','small')+'<span class="spacer"></span><span>裁剪边界已固定</span></div>')+
 panel('源图位置','<div class="ve-crop-context">'+targetScene('preview')+'<span class="ve-context-box"></span></div>')+'</div><aside class="stack">'+panel('候选属性',pair('候选显示名称',esc(current.name))+pair('融合评分',pill(current.score.toFixed(2),'green'))+pair('算法来源',current.source)+pair('图像区域','左 '+current.x+' · 上 '+current.y)+pair('像素尺寸',current.w+' × '+current.h+' px')+pair('来源版本','报表中心截图 · 版本 3')+'<div class="ve-inline-actions">'+btn('调整来源范围','templates-source','','crop')+'</div>')+
 panel('选集',check('保留在本次选集',T.selected.includes(T.detail)).replace('<input','<input data-ve-candidate-keep="'+T.detail+'"')+'<div class="ve-inline-actions">'+btn('返回候选','templates-candidates')+'</div>')+'</aside></div>',2,btn('返回候选','templates-candidates')+btn('保留此候选','templates-candidates','primary').replace('data-go=','data-ve-keep-current data-go='));
}
function reviewView(s){
 const proposal=T.proposal||(!T.namingDirty?buildNames():{items:templateSelection().map(c=>({...c,name:T.overrides[c.index]??c.name,source:'manual',conflict:false}))}),valid=validNames();
 const body=table(['模板名称','尺寸','命名来源','结果'],proposal.items.map(c=>[esc(c.name),c.w+' × '+c.h+' px',{ocr:'OCR',rule:'规则',manual:'手工'}[c.source],pill(valid?'可提交':'需重新预览',valid?'green':'amber')]))+
 '<div class="ve-metrics">'+point('来源','报表中心截图 · 版本 3')+point('批次内容',proposal.items.length+' 个模板及清单')+point('名称冲突',proposal.items.filter(c=>c.conflict).length+' 项')+'</div>';
 return templateShell(s,(!valid?notice(templateExpired()?'候选已过期':'名称预览已失效','重新检查候选和命名后保存。','amber'):'')+'<div class="ve-layout">'+panel('确认保存内容',body)+'<aside class="stack">'+panel('保存到资产库','<div class="stack">'+field('工作空间','个人自动化')+pair('模板总数',proposal.items.length+' 个')+pair('候选与命名预览',valid?'有效':'不可提交')+btn('返回修改名称','templates-naming')+'</div>')+'</aside></div>',3,
 btn('返回命名','templates-naming')+'<button class="btn primary" type="button" data-ve-template-commit'+(!valid?' disabled':'')+'>'+icon('save',15)+'保存整个批次</button>');
}
function batchView(s){
 if(T.operation&&T.operation.state==='running')finishTemplateCommit();
 const assets=templateAssets();
 return templateShell(s,notice('已保存 '+assets.length+' 个模板','批次清单与图像版本已登记，可从资产库使用。','green')+
 '<div class="ve-batch-summary">'+point('批次名称','报表中心 · 本次保存')+point('保存数量',assets.length+' 个模板')+point('来源','报表中心截图 · 版本 3')+'</div>'+
 panel('已保存资产',table(['模板','版本','命名来源','像素尺寸',''],assets.map(c=>['<div class="row">'+sample(c)+esc(c.name)+'</div>',c.assetVersion,{ocr:'OCR',rule:'规则',manual:'手工'}[c.source],c.w+' × '+c.h+' px',btn('查看模板','asset-image','small')]))),3,btn('返回资产','assets')+btn('导出批次','templates-export','primary','download'));
}
function templateExport(s){
 const assets=templateAssets();
 return templateShell(s,'<div class="ve-layout">'+panel('导出文件预览',table(['文件','内容'],assets.map(c=>[esc(c.name)+'.png','模板原图']).concat([['manifest.json','模板名称、来源和图像信息']]))+'<div class="ve-metrics">'+point('资产范围','已保存的 '+assets.length+' 个模板')+point('总计',(assets.length+1)+' 个文件')+'</div>')+
 '<aside class="stack">'+panel('导出选项','<div class="stack">'+field('输出位置','文档 / 模板批次 / 报表中心')+btn('选择文件夹','','','folder')+field('图像格式','PNG','',{select:['PNG']})+pair('导出内容','图像与批次清单')+pair('同名目录','创建新目录')+'</div>')+'</aside></div>',3,btn('返回批次','templates-batch')+btn('开始导出','templates-exporting','primary','download'));
}
function templateJob(s){
 if(s.variant==='saving'){
  if(!T.operation){if(!T.proposal)buildNames();beginTemplateCommit();}
  return templateShell(s,'<section class="panel ve-progress-card" data-ve-complete="templates-batch"><div class="panel-head">正在保存模板批次 '+pill('处理中','blue')+'</div><div class="panel-body"><div class="ve-operation-icon">'+icon('save',34)+'</div><h3>核对图像并登记整个批次</h3><p class="muted">'+(T.operation?.items.length||0)+' 个模板 · 保留本次保存请求</p>'+progress(65)+'<div class="ve-stage-list"><div>'+icon('check',16)+'<span>裁剪、编码与名称核对</span><strong>已完成</strong></div><div>'+icon('clock',16)+'<span>写入对象并登记批次</span><strong>进行中</strong></div></div><div class="ve-inline-actions">'+btn('查询保存结果','templates-receipt-unknown')+'</div></div></section>',3,btn('返回资产','assets'));
 }
 if(s.variant==='receipt-unknown'){
  if(!T.operation){if(!T.proposal)buildNames();beginTemplateCommit();}
  if(T.operation)T.operation.receiptKnown=false;
  return templateShell(s,panel('正在确认保存结果',notice('尚未收到保存结果','请查询这次保存是否完成，避免重复保存模板。','amber')+pair('保存内容',(T.operation?.items.length||0)+' 个模板')+'<div class="ve-inline-actions"><button class="btn primary" type="button" data-ve-template-query>查询保存结果</button></div>'),3,btn('返回资产','assets'));
 }
 return templateShell(s,panel('候选有效期已结束',notice('候选和名称预览已过期','保留来源与参数，重新提取后再保存。','amber')+'<div class="ve-inline-actions">'+btn('重新提取','templates-source','primary').replace('data-go=','data-ve-template-reset data-go=')+'</div>'),2,btn('返回资产','assets'));
}
function templateState(s){
 const v=s.variant;
 if(v==='progress'||v==='exporting'){
  const out=v==='exporting';
  return templateShell(s,'<section class="panel ve-progress-card" data-ve-complete="'+(out?'templates-export-succeeded':'templates-candidates')+'"><div class="panel-head">'+(out?'正在导出模板批次':'正在提取候选')+pill('进行中','blue')+'</div><div class="panel-body"><div class="ve-operation-icon">'+icon(out?'download':'image',34)+'</div><h3>'+(out?'写入图像 · 3 / 4 个':'融合轮廓、颜色与角点结果')+'</h3><p class="muted">'+(out?'完成图像后写入批次清单':'报表中心截图 · 版本 3 · 整个图像')+'</p>'+progress(out?68:62)+'<div class="ve-stage-list">'+(out?[['图像编码与核对','已完成'],['图像写入','进行中'],['完成清单','等待图像写入完成']]:[['预处理与候选检测','已完成'],['结果融合与去重','进行中'],['固定候选集','等待融合完成']]).map((r,i)=>'<div>'+icon(i===0?'check':i===1?'clock':'file',16)+'<span>'+r[0]+'</span><strong>'+r[1]+'</strong></div>').join("")+'</div><div class="ve-inline-actions">'+btn(out?'取消导出':'取消提取',out?'templates-export':'templates-cancelled','','stop')+'</div></div></section>',out?3:1);
 }
 if(v==='ocr')return templateShell(s,'<div class="ve-state-layout">'+notice('缺少简体中文 OCR 语言包','当前引擎可用，所选语言不可用。候选选集已保留。','amber')+panel('OCR 环境',pair('本机引擎','可用')+pair('English',pill('已安装','green'))+pair('简体中文',pill('未安装','amber'))+pair('当前命名策略','OCR 后规则命名'))+
 '<div class="ve-inline-actions">'+btn('停止命名并返回','templates-candidates')+btn('管理 OCR 组件','settings-general')+btn('使用规则命名','templates-naming','primary').replace('data-go=','data-ve-ocr-rule data-go=')+'</div></div>',2,btn('返回命名','templates-naming'));
 const cfg={
  empty:['target','未发现符合条件的候选','检测已完成，当前评分与面积范围下没有候选。','调整提取条件','templates-source','amber'],
  cancelled:['stop','提取已取消','计算单元已结束。图像来源和有效设置已保留。','重新设置','templates-source','blue'],
  error:['alert','候选提取超时','检测在 30 秒预算内未完成。缩小范围或增加超时后重新提取。','修改提取设置','templates-source','red'],
  'save-error':['database','模板批次未保存','工作空间可用空间不足，本批模板均未登记。有效候选和名称预览仍保留。','检查并重试','templates-review','red'],
  'export-error':['download','模板导出未完成','目标文件夹无法写入。已保存模板仍可使用，当前目录尚未生成完成清单。','更换导出位置','templates-export','red']
 }[v];
 return templateShell(s,'<section class="panel ve-state-panel">'+blank(cfg[0],cfg[1],cfg[2],btn('返回资产','assets')+btn(cfg[3],cfg[4],'primary'))+'</section>',v==='save-error'||v==='export-error'?3:1,btn('返回来源','templates-source'));
}
function templateExportSucceeded(s){
 const count=templateAssets().length;
 return templateShell(s,'<section class="panel ve-state-panel"><div class="ve-empty ve-success"><span class="ve-success-icon">'+icon('check',34)+'</span><h3>模板批次已导出</h3><p>'+count+' 个模板原图与批次清单均已写入。</p><div class="ve-export-summary">'+point('输出范围','报表中心 · 今天 09:38')+point('输出数量',count+' 张 PNG + 1 份清单')+point('总计',(count+1)+' 个文件')+'</div><div class="ve-output-location">文档 / 模板批次 / 报表中心</div><div class="row">'+btn('返回批次','templates-batch')+'<button class="btn primary" type="button" data-ve-open-output="文档 / 模板批次 / 报表中心">'+icon('folder',15)+'打开输出位置</button></div><span class="ve-output-feedback" data-ve-output-feedback aria-live="polite"></span></div></section>',3,btn('返回资产','assets'));
}
function observationExportSucceeded(s){
 const count=exportedRecords().length,images=exportedImages();
 return observationShell(s,'<section class="panel ve-state-panel"><div class="ve-empty ve-success"><span class="ve-success-icon">'+icon('check',34)+'</span><h3>观察记录已导出</h3><p>'+count+' 条已保存记录、所选截图与完成清单均已写入。</p><div class="ve-export-summary">'+point('记录范围',exportRange)+point('输出数量','1 份 '+exportFormat+' · '+images+' 张截图 · 1 份清单')+point('总计',(images+2)+' 个文件')+'</div><div class="ve-output-location">'+esc(exportLocation)+'</div><div class="row">'+btn('返回记录','observations-history')+'<button class="btn primary" type="button" data-ve-open-output="'+esc(exportLocation)+'">'+icon('folder',15)+'打开输出位置</button></div><span class="ve-output-feedback" data-ve-output-feedback aria-live="polite"></span></div></section>',btn('返回概览','observations-center'),'observations-history');
}
V['templates-extra']=s=>{
 if(['saving','receipt-unknown','expired'].includes(s.variant))return templateJob(s);
 if(s.variant==='source')return extractionConfig(s);
 if(s.variant==='candidates')return templateExpired()?templateJob({...s,variant:'expired'}):candidatesView(s);
 if(s.variant==='candidate')return candidateDetail(s);
 if(['naming','conflict'].includes(s.variant))return namingView(s);
 if(s.variant==='review')return reviewView(s);
 if(s.variant==='batch')return batchView(s);
 if(s.variant==='export')return templateExport(s);
 if(s.variant==='export-succeeded')return templateExportSucceeded(s);
 return templateState(s);
};
function observationTabs(active){return U.tabs([['会话概览','observations-center'],['历史记录','observations-history']],active);}
function observationShell(s,body,buttons="",active="observations-center"){
 return shell(s,'<main class="page ve-page ve-observations ve-observation-'+esc(s.variant)+'">'+header(s,'观察 / 个人自动化',buttons||btn('新建持续观察','observations-config','primary','eye'))+observationTabs(active)+body+'</main>',{active:'observations'});
}
function currentSessionCard(){
 const state=O.sessionState,label={running:'观察中',stopping:'正在停止',stopped:'已结束',failed:'已停止'}[state],route={running:'observations-running',stopping:'observations-stopping',stopped:'observations-stopped',failed:'observations-session-failed'}[state];
 const count=O.checkedCount??18,maxRecords=O.plan?.maxRecords||120,budget=Math.min(100,Math.round(count/maxRecords*100));
 return '<section class="panel ve-current-session"><div class="panel-head"><span class="ve-panel-name">'+icon('eye',18)+' 当前会话</span>'+pill(label,state==='running'?'green':state==='failed'?'red':'amber')+'</div><div class="panel-body"><div class="ve-session-identity"><span class="ve-session-symbol">'+icon('target',22)+'</span><div><h3>'+esc(O.sessionName)+'</h3><p>报表中心 / 报表列表</p></div></div><div class="ve-current-metrics">'+point('模型配置',(O.connectionId==='remote-vision'?'远程视觉服务':'本地视觉服务')+' · v'+O.connectionVersion)+point('检查周期','每 '+((O.plan?.intervalMs||30000)/1000)+' 秒')+point('检查记录',count+' / '+maxRecords+' 次')+point('关闭后继续',O.detached?'是':'否')+'</div><div class="ve-session-budget"><span>本次记录预算</span><b>'+budget+'%</b></div>'+progress(budget)+(O.stopReason?'<p class="ve-hint">'+esc(O.stopReason)+'</p>':'')+'<div class="ve-session-actions">'+btn('查看会话',route,'small')+(state==='running'?btn('停止','observations-stopping','small','stop').replace('data-go=','data-ve-session-stop data-go='):btn('新建观察','observations-config','small'))+'</div></div></section>';
}
const initialRecords=[
 {variant:'detail',time:'09:43:30',done:'09:43:32',label:'成功',state:'succeeded',tone:'green',summary:'读取到 4 份可下载报表',route:'observations-detail',durationMs:2400,inputTokens:380,outputTokens:64,count:4,hasFrame:true},
 {variant:'skipped',time:'09:43:00',done:'09:43:00',label:'跳过',state:'skipped',tone:'amber',summary:'目标区域无显著变化',route:'observations-skipped',durationMs:80,inputTokens:0,outputTokens:0,count:3,hasFrame:true,skipReason:'unchanged',difference:3.2},
 {variant:'busy',time:'09:42:30',done:'09:42:30',label:'跳过',state:'skipped',tone:'amber',summary:'上一请求尚未结束',route:'observations-busy',durationMs:0,inputTokens:0,outputTokens:0,count:null,hasFrame:false,skipReason:'busy'},
 {variant:'failed',time:'09:42:00',done:'09:43:00',label:'失败',state:'failed',tone:'red',summary:'请求超时（60 秒）',route:'observations-failed',durationMs:60000,inputTokens:0,outputTokens:0,count:3,hasFrame:true},
 {variant:'save-error',time:'09:41:30',done:'09:41:34',label:'未保存',state:'persistenceFailed',tone:'red',summary:'图像与结果保存失败',route:'observations-save-error',durationMs:4000,inputTokens:380,outputTokens:64,count:3,hasFrame:true},
 {variant:'cancelled',time:'09:41:00',done:'09:41:02',label:'取消',state:'cancelled',tone:'',summary:'本次请求已取消',route:'observations-cancelled',durationMs:2000,inputTokens:0,outputTokens:0,count:3,hasFrame:true},
 {variant:'baseline',time:'09:40:30',done:'09:40:32',label:'成功',state:'succeeded',tone:'green',summary:'读取到 3 份可下载报表',route:'observations-baseline',durationMs:2000,inputTokens:362,outputTokens:58,count:3,hasFrame:true}
];
const localToUTC=time=>new Date('2026-10-29T'+time.slice(0,8)+'+08:00').toISOString().replace('.000Z','Z');
const records=O.records||(O.records=initialRecords.map(row=>({...row,key:row.variant,sessionKey:'report-status',sessionName:'报表可用状态',checkedAt:localToUTC(row.time),capturedAt:row.variant==='busy'?null:localToUTC(row.time),completedAt:localToUTC(row.done),skipReason:row.skipReason||null,difference:row.difference??null,baselineRecordId:row.variant==='skipped'?'baseline':null,frameId:row.hasFrame?'frame-'+row.variant:null,screenshotResource:row.hasFrame?'frame-resource-'+row.variant:null,output:['succeeded','persistenceFailed'].includes(row.state)?{month:'2026-10',count:row.count}:null,usage:{inputTokens:row.inputTokens,outputTokens:row.outputTokens},runId:null})));
function matchesQuery(row){
 const q=O.query,stamp='2026-10-29T'+row.time;
 return (q.session==='all'||q.session==='single'&&!row.sessionKey||q.session===row.sessionKey)&&(q.state==='全部状态'||q.state===row.label)&&stamp>=q.from&&stamp<q.until;
}
function queryRows(){if(!O.querySnapshot)refreshQuerySnapshot();return O.querySnapshot.keys.map(key=>records.find(r=>r.key===key)).filter(Boolean);}
let queryRecords=queryRows().filter(r=>r.state!=='persistenceFailed'),exportRows=null,exportFormat='JSONL',exportImages=true,exportLocation='文档 / 观察导出 / 2026-10-29',exportRange='今天 00:00 至明天 00:00（不含）';
const exportedRecords=()=>exportRows||queryRecords;
const exportedImages=()=>exportImages?exportedRecords().filter(r=>r.hasFrame).length:0;
function historyTable(filterable=false){
 const rows=[...records].sort((a,b)=>filterable&&O.query.sort==='oldest'?a.checkedAt.localeCompare(b.checkedAt):b.checkedAt.localeCompare(a.checkedAt));
 return '<div class="ve-record-table"><table class="table"><thead><tr>'+['检查时间','采集时间','状态','结果或原因',''].map(x=>'<th>'+x+'</th>').join("")+'</tr></thead><tbody>'+rows.map(r=>'<tr data-ve-record data-record="'+r.key+'" data-state="'+r.label+'"'+(filterable&&!queryRows().some(x=>x.key===r.key)?' hidden':'')+'><td><span class="mono">'+r.time+'</span></td><td class="mono muted">'+(r.capturedAt?r.time:'未采集')+'</td><td>'+pill(r.label,r.tone)+'</td><td>'+esc(r.summary)+'</td><td>'+btn('详情',r.route,'small')+'</td></tr>').join("")+'</tbody></table></div>';
}
function sceneForRecord(row){
 let scene=targetScene('preview');
 if(row.count===3){let index=0;scene=scene.replace(/<span class="target-ready">可下载<\/span>/g,()=>++index===4?'<span class="target-ready">生成中</span>':'<span class="target-ready">可下载</span>');}
 return scene;
}
function recentOutcomePanel(){
 const colors={green:'#259f7e',amber:'#d19a38',red:'#d65c6f','':'#9aa3b8'};
 const buckets=[['成功','green'],['跳过','amber'],['失败','red'],['取消',''],['未保存','red']];
 const counts=buckets.map(([state,tone])=>({state,tone,count:records.filter(row=>row.label===state).length}));
 const timeline=[...records].reverse().map(row=>'<button type="button" class="ve-outcome-event '+(row.tone||'neutral')+'" data-go="'+row.route+'" aria-label="'+esc(row.time+' '+row.summary)+'"><i></i><span>'+row.time.slice(3,5)+':'+row.time.slice(6,8)+'</span></button>').join("");
 return panel('近期检查分布','<div class="ve-outcome-meta"><strong>'+records.length+' 次检查</strong><span>09:40:30 — 09:43:30</span></div><div class="ve-outcome-timeline">'+timeline+'</div><div class="ve-outcome-bars">'+counts.map(row=>'<div><span><i style="background:'+colors[row.tone]+'"></i>'+row.state+'</span><b>'+row.count+'</b><div><i style="width:'+(row.count/records.length*100)+'%;background:'+colors[row.tone]+'"></i></div></div>').join("")+'</div>');
}
function centerView(s){
 return observationShell(s,'<div class="ve-dashboard-summary">'+point('当前会话','1 个')+point('今天成功观察','8 次')+point('变化 / 忙碌跳过','10 次')+point('今天模型用量','4,820 tokens')+'</div><div class="ve-monitor-grid"><section class="panel ve-recent-records"><div class="panel-head"><span class="ve-panel-name">'+icon('history',17)+' 最近观察</span>'+btn('全部记录','observations-history','small','arrow')+'</div>'+historyTable()+'</section><aside class="stack">'+currentSessionCard()+recentOutcomePanel()+'</aside></div>',btn('单次观察','observation','','eye')+btn('新建持续观察','observations-config','primary','plus'));
}
function configView(s){
 return observationShell(s,'<form class="ve-layout" data-ve-form novalidate><div class="stack">'+panel('目标与观察内容','<div class="form-grid">'+formField('目标窗口','报表中心','session-target')+formField('模型连接','本地视觉服务 · 版本 '+(DS.connections?.version||O.connectionVersion),'session-connection',{select:['本地视觉服务 · 版本 '+(DS.connections?.version||O.connectionVersion),'远程视觉服务 · 版本 1']})+'</div><div class="ve-observation-scene">'+targetScene('preview')+'<span class="ve-observation-box"></span></div><div class="ve-bottom"><span class="muted">报表列表 · 左 220 上 240 · 930 × 300 px</span><span class="spacer"></span>'+btn('重新选择区域','capture-overlay','small','crop')+'</div>'+formField('观察内容',O.plan?.prompt||'读取报表月份、报表数量和每份报表的下载状态。','session-prompt',{area:true}))+
 '</div><aside class="stack">'+panel('周期与预算','<div class="form-grid">'+num('检查周期（秒）',30,5,86400,"0.001")+num('请求超时（秒）',60,.1,120,"0.001")+num('最多检查记录',120,1,100000)+num('最长运行（秒）',3600,5,86400,"0.001")+'</div>'+check('启用本地变化检测',true)+num('变化阈值',12,0,255,"any")+formField('输出格式','结构化 JSON','session-output-mode',{select:['结构化 JSON','纯文本']})+formField('输出结构（JSON）','{"type":"object","properties":{"month":{"type":"string"},"count":{"type":"integer"}}}','session-schema',{area:true})+check('保存本次实际截图',true).replace('<input','<input data-ve-session-save-frames')+check('关闭工作台后继续观察',!!O.draftDetached).replace('<input','<input data-ve-session-detached')+'<p class="ve-field-error" data-ve-error aria-live="polite"></p>')+
 panel('连接信息',pair('配置版本','<span data-ve-session-version>版本 '+(DS.connections?.version||3)+'</span>')+pair('最多在途请求','1 个')+'<div class="ve-inline-actions">'+btn('管理模型连接','settings-connections','small','connect')+'</div>')+'</aside></form>',btn('取消','observations-center')+action('开始持续观察','observations-running','primary',true).replace('data-go=','data-ve-session-start data-go='));
}
function sessionView(s){
 const plan=O.plan||{maxRecords:120,timeoutMs:60000,intervalMs:30000,changeDetection:{enabled:true,threshold:12},prompt:'读取报表月份与可下载状态'};
 const count=O.checkedCount??(s.variant==='stopped'?23:18),maxRecords=plan.maxRecords,timeout=plan.timeoutMs/1000,interval=plan.intervalMs/1000;
 const connectionName=O.connectionId==='remote-vision'?'远程视觉服务':'本地视觉服务';
 if(s.variant==='stopped')O.sessionState='stopped';
 if(s.variant==='stopping'){O.sessionState='stopping';O.stopReason=O.stopReason||'由你停止';}
 const stopping=s.variant==='stopping',stopped=s.variant==='stopped',target=['target-error','session-failed'].includes(s.variant),conflict=s.variant==='control-conflict';
 const state=stopping?'正在停止':stopped?'已结束':target?'目标失效':conflict?'状态已变化':'观察中';
 const tone=stopping?'amber':stopped?'':target?'red':conflict?'amber':'green';
 let msg="";
 if(stopping)msg=notice('正在取消当前模型请求','已停止新的周期检查，收尾完成后会话结束。','amber');
 if(stopped)msg=notice('观察已结束','已完成请求收尾，保留本次已产生的检查记录。','green');
 if(target)msg=notice(s.variant==='target-error'?'目标窗口已关闭，观察失败':'观察已停止',s.variant==='target-error'?'会话已停止。重新选择窗口后创建新会话。':O.stopReason||'观察授权已撤销。重新配置后新建会话。','red');
 if(conflict)msg=notice('会话状态已更新','你的停止操作未覆盖新状态。读取最新状态后继续。','amber');
 return observationShell(s,msg+(stopping?'<div data-ve-complete="observations-stopped"></div>':'')+'<div class="ve-session-heading"><div><h3>'+esc(O.sessionName)+'</h3><p>报表中心 / 报表列表 · '+connectionName+' · 版本 '+O.connectionVersion+'</p></div>'+pill(state,tone)+'</div><div class="ve-dashboard-summary">'+point('开始时间',O.startedLabel||'今天 09:35:00')+point('已检查',count+' / '+maxRecords+' 次')+point('运行时长',O.checkedCount===0?'刚刚开始':stopped?'11 分钟':'9 分 2 秒')+point('下一次检查',conflict?'读取最新状态':stopping||stopped||target?'已停止触发':O.plan?'每 '+interval+' 秒检查':'09:44:30')+'</div><div class="ve-layout ve-session-layout"><div class="stack">'+panel('最近采集区域','<div class="ve-observation-scene">'+targetScene('preview')+'<span class="ve-observation-box"></span></div><div class="ve-bottom"><span>采集于 09:43:30</span><span class="spacer"></span>'+btn('查看本次记录','observations-detail','small')+'</div>')+
 panel('最近检查',historyTable())+'</div><aside class="stack">'+panel('本次请求',pair('状态',pill(stopping?'取消收尾中':stopped||target?'无在途请求':'等待模型响应',stopping?'amber':'blue'))+pair('请求开始',stopped||target?'—':'09:44:00')+pair('已用时',stopped||target?'—':'2.4 秒')+pair('超时预算',timeout+' 秒')+pair('观察提示',esc(plan.prompt)))+
 panel('变化检测',pair('当前差异','18.4')+pair('触发阈值',plan.changeDetection.enabled?String(plan.changeDetection.threshold):'变化检测未启用')+pair('最近成功基线','今天 09:40:30')+pair('检查周期',interval+' 秒'))+'</aside></div>',
 target?btn(s.variant==='target-error'?'重新选择目标':'重新配置观察','observations-config','primary'):conflict?btn('读取最新状态','observations-stopped','primary','refresh'):stopped?btn('查看会话记录','observations-history','primary').replace('data-go=','data-ve-session-history data-go=')+btn('新建观察','observations-config'):stopping?btn('返回概览','observations-center'):btn('停止观察','observations-stopping','danger','stop').replace('data-go=','data-ve-session-stop data-go='));
}
function historyView(s){
 if(queryExpired())return queryExpiredView(s);
 queryRecords=queryRows().filter(r=>r.state!=='persistenceFailed');exportRows=null;
 const q=O.query,sessionLabel=q.session==='all'?'全部会话':q.session==='single'?'单次观察':O.sessionName;
 return observationShell(s,'<form class="ve-history-filter" data-ve-history><div class="ve-query-fields">'+
 formField('会话',sessionLabel,'query-session',{select:['全部会话',O.sessionName,'单次观察']})+
 '<label class="field"><span class="field-label">开始时间</span><input type="datetime-local" value="'+esc(q.from)+'" data-ve-from></label>'+
 '<label class="field"><span class="field-label">结束时间（不含）</span><input type="datetime-local" value="'+esc(q.until)+'" data-ve-until></label>'+
 '<label class="field"><span class="field-label">状态</span><select data-ve-state>'+['全部状态','成功','跳过','失败','取消','未保存'].map(x=>'<option'+(x===q.state?' selected':'')+'>'+x+'</option>').join('')+'</select></label></div><div class="ve-bottom"><span>时区：Asia/Shanghai（UTC+08:00）</span><span class="spacer"></span><p class="ve-field-error" data-ve-query-error aria-live="polite"></p><button class="btn primary" type="button" data-ve-query>查询记录</button></div></form>'+
 '<div class="ve-section-heading"><h3 data-ve-query-count>找到 '+queryRows().length+' 条记录</h3><div class="row">'+formField('排序',q.sort==='oldest'?'最早优先':'最新优先','query-sort',{select:['最新优先','最早优先']})+btn('导出当前结果','observations-export','','download').replace('data-go=','data-ve-export-query data-go=')+'</div></div><section class="panel">'+historyTable(true)+'</section>'+
 '<div class="ve-bottom"><span>本页为固定查询结果 · 新记录在重新查询后显示</span><span class="spacer"></span><button type="button" class="btn small" disabled>上一页</button>'+pill('第 1 页')+'<button type="button" class="btn small" disabled>下一页</button></div>',btn('返回概览','observations-center')+btn('新建观察','observations-config','primary'),'observations-history');
}
function recordDetails(s){
 const row=records.find(r=>r.variant===s.variant)||records[0];
 const skipped=['skipped','busy'].includes(s.variant),busy=s.variant==='busy',failed=s.variant==='failed',unsaved=s.variant==='save-error',cancelled=s.variant==='cancelled',baseline=s.variant==='baseline';
 const state=skipped?'已跳过':failed?'失败':unsaved?'未保存':cancelled?'已取消':'成功';
 const banner=busy?notice('上一请求尚未结束，本次检查已跳过','09:42:00 的请求仍在处理中。本次未采集图像，也未追加模型请求。','amber'):
 skipped?notice('本次没有发送模型请求','区域差异值 3.2，低于阈值 12。结果内容为空。','amber'):
 failed?notice('模型请求超时','请求在 60 秒后结束。本次失败未更新变化检测基线。','red'):
 unsaved?notice('模型已返回，结果未能保存','工作空间存储不足。内存结果可保存到独立恢复文件。','red'):
 cancelled?notice('本次请求已取消','已结束请求收尾，取消记录保留“报表可用状态”会话关联。'):
 baseline?notice('已保存为变化对比图像','后续检查将与这张图像比较。','green'):"";
 const result=skipped?'<div class="ve-result-empty">本次没有模型结果</div>':
 failed?'<div class="ve-result-empty">请求超时，未收到结果</div>':
 cancelled?'<div class="ve-result-empty">请求已取消</div>':
 table(['字段','结果'],[['报表月份','2026 年 10 月'],['可下载报表',String(row.count)+' 份'],['下载状态',row.count===3?'3 份可下载 · 1 份生成中':'全部可下载']]);
 const picture=row.hasFrame?'<div class="ve-observation-scene">'+sceneForRecord(row)+'<span class="ve-observation-box"></span></div><div class="ve-bottom"><span>报表中心 / 报表列表</span><span class="spacer"></span>'+pill(unsaved?'仅内存中保留':'已保存实际截图',unsaved?'red':'')+'</div>':'<div class="ve-no-frame">'+icon('image',32)+'<strong>本次未采集图像</strong><span>上一请求占用期间不启动新的采集。</span></div>';
 const info=pair('状态',pill(state,row.tone))+pair('会话',esc(row.sessionName))+pair('检查时间','2026-10-29 '+row.time)+pair('采集时间',row.capturedAt?'2026-10-29 '+row.time:'未采集')+pair('完成时间','2026-10-29 '+row.done)+pair('模型配置','本地视觉服务 · 版本 2')+pair('用时',row.durationMs<1000?String(row.durationMs)+' 毫秒':String(row.durationMs/1000)+' 秒')+pair('模型用量','输入 '+row.inputTokens+' · 输出 '+row.outputTokens)+pair('时区','Asia/Shanghai');
 const detail=busy?panel('跳过原因',pair('本次触发','09:42:30')+pair('在途请求开始','09:42:00')+pair('等待策略','不积压后续触发')):
 skipped?panel('变化检测',pair('区域差异','3.2 / 阈值 12')+pair('对比基线','今天 09:40:30')+'<div class="ve-inline-actions">'+btn('查看成功基线','observations-baseline','small')+'</div>'):
 panel('观察提示','<p class="ve-hint">读取报表月份、报表数量和每份报表的下载状态。</p>');
 return observationShell(s,banner+'<div class="ve-layout"><div class="stack">'+panel(busy?'本次检查范围':'本次实际采集截图',picture)+panel('观察结果',result)+'</div><aside class="stack">'+panel('记录信息',info)+detail+'</aside></div>',
 btn('返回记录','observations-history')+(unsaved?'<button class="btn primary" type="button" data-ve-recovery>保存恢复文件</button>':failed||cancelled?btn('新建观察','observations-config','primary'):btn('导出本条记录','observations-export','primary','download').replace('data-go=','data-ve-export-one="'+row.variant+'" data-go=')),'observations-history');
}
function observationExport(s){
 const rows=exportedRecords(),images=rows.filter(r=>r.hasFrame).length;
 return observationShell(s,'<div class="ve-layout">'+panel('固定记录集',pair('导出范围',exportRange)+pair('时区','Asia/Shanghai')+pair('记录数量',rows.length+' 条已保存记录')+pair('状态范围',[...new Set(rows.map(r=>r.label))].join('、'))+table(['字段','内容'],[['时间','采集时间、完成时间、耗时'],['目标与配置','会话、目标范围、模型配置版本'],['观察内容','提示、结果、状态和原因'],['用量','输入与输出 tokens']]))+
 '<aside class="stack">'+panel('导出选项','<div class="stack">'+field('文件格式',exportFormat,'',{select:['JSONL','CSV']}).replace('<select','<select data-ve-export-format')+field('输出位置',exportLocation).replace('<input','<input data-ve-export-location')+btn('选择文件夹','','','folder')+check('包含所选记录的截图',exportImages).replace('<input','<input data-ve-export-images')+pair('可选截图',images+' 张')+pair('总输出','记录文件 + 所选截图 + 清单')+'</div>')+'</aside></div>',
 btn('取消','observations-history')+(rows.length?btn('开始导出','observations-exporting','primary','download'):'<button type="button" class="btn primary" disabled>没有可导出记录</button>'),'observations-history');
}
function observationSpecial(s){
 if(s.variant==='empty')return observationShell(s,'<section class="panel">'+blank('eye','当前条件下没有观察记录','今天 00:00 至 09:00 · 全部会话 · 成功',btn('调整筛选','observations-history')+btn('新建持续观察','observations-config','primary'))+'</section>',btn('查询记录','observations-history'),'observations-history');
 if(s.variant==='export-error')return observationShell(s,'<section class="panel">'+blank('download','观察导出未完成','所选文件夹无法写入。已保存的观察记录和截图未改变。',btn('返回记录','observations-history')+btn('更换输出位置','observations-export','primary'))+'</section>',btn('返回记录','observations-history'),'observations-history');
 return observationShell(s,'<section class="panel ve-progress-card" data-ve-complete="observations-export-succeeded"><div class="panel-head">正在导出观察记录 '+pill('进行中','blue')+'</div><div class="panel-body"><div class="ve-operation-icon">'+icon('download',34)+'</div><h3>正在写入记录与附件</h3><p class="muted">'+exportedRecords().length+' 条已保存记录 · '+exportFormat+' · '+exportedImages()+' 张截图</p>'+progress(60)+'<div class="ve-stage-list"><div>'+icon('check',16)+'<span>记录序列化</span><strong>已完成</strong></div><div>'+icon('clock',16)+'<span>截图复制与核对</span><strong>进行中</strong></div><div>'+icon('file',16)+'<span>完成清单</span><strong>等待截图写入完成</strong></div></div><div class="ve-inline-actions">'+btn('取消导出','observations-history','','stop')+'</div></div></section>',btn('返回记录','observations-history'),'observations-history');
}
function singleRecord(state){
 const request=O.single,sequence=request.sequence||1,base=Date.parse('2026-10-29T01:44:00Z')+(sequence-1)*5000;
 const elapsed=state==='failed'?(request.plan?.timeoutMs||60000):state==='cancelled'?1000:2000;
 const utc=value=>new Date(value).toISOString().replace('.000Z','Z');
 const local=value=>new Date(value).toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});
 return {key:'single-'+sequence,variant:'single-'+sequence,state,label:state==='succeeded'?'成功':state==='failed'?'失败':'取消',tone:state==='succeeded'?'green':state==='failed'?'red':'',time:local(base),done:local(base+elapsed),checkedAt:utc(base),capturedAt:utc(base),completedAt:utc(base+elapsed),sessionKey:null,sessionName:'单次观察',sessionId:null,runId:null,skipReason:null,difference:null,baselineRecordId:null,frameId:'single-frame-'+sequence,screenshotResource:'single-frame-resource-'+sequence,output:state==='succeeded'?{month:'2026-10',count:4}:null,usage:{inputTokens:state==='succeeded'?380:0,outputTokens:state==='succeeded'?64:0},hasFrame:true,count:4,durationMs:elapsed,inputTokens:state==='succeeded'?380:0,outputTokens:state==='succeeded'?64:0,summary:state==='succeeded'?'读取到 4 份可下载报表':state==='failed'?'单次观察请求超时':'单次观察已取消',route:state==='succeeded'?'observation-succeeded':state==='failed'?'observation-error':'observation-cancelled'};
}
function finishSingle(state){
 if(O.single.state==='running'){
  O.single.state=state;O.single.record=singleRecord(state);
  const index=records.findIndex(row=>row.key===O.single.record.key);
  if(index<0)records.push(O.single.record);else records[index]=O.single.record;
 }
 return O.single.record?.state===state?O.single.record:singleRecord(state);
}
function singleObservation(s){
 const mode=s.variant,active=mode==='progress',failed=mode==='error',cancelled=mode==='cancelled',succeeded=mode==='succeeded',terminal=failed||cancelled||succeeded;
 const expected=succeeded?'succeeded':failed?'failed':'cancelled';
 const record=terminal?(O.single.displayRecord?.state===expected?O.single.displayRecord:finishSingle(expected)):null;
 if(active)return observationShell(s,'<section class="panel ve-progress-card" data-ve-complete="observation-succeeded"><div class="panel-head">正在进行单次观察 '+pill('请求中','blue')+'</div><div class="panel-body"><div class="ve-operation-icon">'+icon('eye',34)+'</div><h3>读取本次选定区域</h3><p class="muted">报表中心 · '+esc(O.single.plan?.connection||'本地视觉服务')+' · 超时 '+((O.single.plan?.timeoutMs||60000)/1000)+' 秒</p>'+progress(45)+'<div class="ve-inline-actions"><button class="btn danger" type="button" data-ve-single-cancel>取消本次观察</button></div></div></section>',btn('返回观察中心','observations-center'));
 if(terminal){
  const note=succeeded?notice('本次观察已完成','结果与实际截图已保存。','green'):failed?notice('本次观察已超时','本次请求已结束，可重新发起。','red'):notice('本次观察已取消','仅结束这一次请求，持续观察会话保持原状态。');
  return observationShell(s,note+'<div class="ve-layout"><div class="stack">'+panel('本次实际采集截图','<div class="ve-observation-scene">'+sceneForRecord(record)+'<span class="ve-observation-box"></span></div>')+panel('单次观察结果',succeeded?table(['字段','结果'],[['报表月份','2026 年 10 月'],['可下载报表','4 份']]):'<div class="ve-result-empty">'+(failed?'请求超时，未收到结果':'请求已取消')+'</div>')+'</div><aside class="stack">'+panel('本次请求',pair('关联','独立观察')+pair('检查时间',record.time)+pair('采集时间',record.time)+pair('完成时间',record.done)+pair('模型用量','输入 '+record.inputTokens+' · 输出 '+record.outputTokens)+pair('保存状态','已保存'))+'</aside></div>',btn('返回配置','observation')+(succeeded?btn('导出本条记录','observations-export','primary','download').replace('data-go=','data-ve-export-single data-go='):btn('重新观察','observation','primary')),'observations-history');
 }
 return observationShell(s,'<form class="ve-layout" data-ve-single-form><div class="stack">'+panel('单次观察区域','<div class="ve-observation-scene">'+targetScene('preview')+'<span class="ve-observation-box"></span></div><div class="ve-inline-actions">'+btn('选择区域','capture-overlay','','crop')+'</div>')+panel('观察结果','<div class="ve-result-empty">开始观察后显示本次结果</div>')+'</div><aside class="stack">'+panel('本次请求配置','<div class="stack">'+formField('模型连接','本地视觉服务','single-connection',{select:['本地视觉服务','远程视觉服务']})+formField('观察内容',O.single.plan?.prompt||'读取报表月份、可下载状态与报表数量。','single-prompt',{area:true})+formField('输出格式','结构化 JSON','single-output',{select:['结构化 JSON','纯文本']})+num('超时（秒）',60,.1,120,'0.001').replace('<input','<input data-ve-field="single-timeout"')+'<div data-ve-single-feedback></div>'+btn('管理连接','settings-connections','','connect')+'</div>')+'</aside></form>',btn('返回中心','observations-center')+'<button class="btn primary" type="button" data-ve-single-start>'+icon('eye',15)+'开始观察</button>');
}
window.DW=window.DW||{};window.DW.singleObservation=singleObservation;
function refreshQuerySnapshot(){
 O.querySnapshot={id:'query-'+((O.querySequence=(O.querySequence||0)+1)),createdAt:Date.now(),expiresAt:Date.now()+300000,keys:records.filter(matchesQuery).sort((a,b)=>O.query.sort==='oldest'?a.checkedAt.localeCompare(b.checkedAt):b.checkedAt.localeCompare(a.checkedAt)).map(r=>r.key)};
 return O.querySnapshot;
}
function queryExpired(){return !!O.querySnapshot&&O.querySnapshot.expiresAt<=Date.now();}
function queryExpiredView(s){
 return observationShell(s,panel('查询结果已过期',notice('本次查询结果超过 5 分钟有效期','重新查询以取得新的结果集，筛选与排序已保留。','amber')+table(['筛选','当前值'],[['会话',O.query.session==='all'?'全部会话':O.query.session==='single'?'单次观察':O.sessionName],['开始时间',O.query.from],['结束时间（不含）',O.query.until],['状态',O.query.state],['排序',O.query.sort==='oldest'?'最早优先':'最新优先']])+'<div class="ve-inline-actions"><button class="btn primary" type="button" data-ve-query-refresh>重新查询</button></div>'),btn('返回中心','observations-center'),'observations-history');
}

V['observations-extra']=s=>{
 if(s.variant==='query-expired')return queryExpiredView(s);
 if(s.variant==='center')return centerView(s);
 if(s.variant==='config')return configView(s);
 if(['running','stopping','stopped','target-error','session-failed','control-conflict'].includes(s.variant))return sessionView(s);
 if(s.variant==='history')return historyView(s);
 if(['detail','baseline','skipped','busy','failed','save-error','cancelled'].includes(s.variant))return recordDetails(s);
 if(s.variant==='export')return observationExport(s);
 if(s.variant==='export-succeeded')return observationExportSucceeded(s);
 return observationSpecial(s);
};
window.DW=window.DW||{};
window.DW.templatesExtra=V['templates-extra'];
window.DW.observationsExtra=V['observations-extra'];
function updateSelection(){
 const area=document.querySelector('.ve-templates');if(!area)return;
 T.selected=[...area.querySelectorAll('[data-ve-candidate]:checked')].map(x=>Number(x.value));
 T.namingDirty=true;T.proposal=null;
 const total=area.querySelector('[data-ve-selection]');if(total)total.textContent='已选 '+T.selected.length+' 项';
 area.querySelectorAll('[data-ve-require-selection]').forEach(x=>x.disabled=T.selected.length===0);
 area.querySelectorAll('[data-ve-card]').forEach(x=>x.classList.toggle('ve-selected',!!x.querySelector('[data-ve-candidate]')?.checked));
}
const valueOf=(name,root=document)=>root.querySelector('[data-ve-field="'+name+'"]')?.value||'';
const navigate=id=>window.DW_PROTOTYPE.go(id);
function syncNaming(form){
 const mode=valueOf('naming-mode',form),collision=valueOf('naming-collision',form);
 T.policy={...T.policy,mode:{'OCR 后规则命名':'ocrThenRule','仅规则命名':'rule','手工名称':'manual'}[mode]||T.policy.mode,prefix:valueOf('naming-prefix',form),startIndex:Number(valueOf('naming-index',form)),onCollision:collision==='返回冲突'?'reject':'suffix',ocrUnavailable:valueOf('naming-unavailable',form)==='停止命名'?'stop':'rule'};
 const languages=valueOf('naming-languages',form).split(/[,+＋，\s]+/).filter(Boolean).map(x=>x==='简体中文'?'chi_sim':x==='English'?'eng':x);
 if(languages.length)T.policy.languages=languages;
}
function applyQuery(form){
 const from=form.querySelector('[data-ve-from]').value,until=form.querySelector('[data-ve-until]').value,error=form.querySelector('[data-ve-query-error]');
 if(!from||!until||from>=until){error.textContent='开始时间必须早于结束时间。';return false;}
 const session=valueOf('query-session',form),sort=valueOf('query-sort');
 O.query={from,until,fromUTC:new Date(from+'+08:00').toISOString().replace('.000Z','Z'),untilUTC:new Date(until+'+08:00').toISOString().replace('.000Z','Z'),session:session==='全部会话'?'all':session==='单次观察'?'single':'report-status',sort:sort==='最早优先'?'oldest':'newest',state:form.querySelector('[data-ve-state]').value};
 refreshQuerySnapshot();queryRecords=queryRows().filter(r=>r.state!=='persistenceFailed');exportRows=null;return true;
}
DS.refreshQuerySnapshot=refreshQuerySnapshot;
DS.completeSingleObservation=state=>{if(['succeeded','failed','cancelled'].includes(state))return finishSingle(state);return null;};
DS.beforeCloseStudio=()=>{if(O.sessionState==='running'&&!O.detached){O.sessionState='stopped';O.stopReason='工作台已关闭';}};
document.addEventListener('input',event=>{
 const input=event.target;
 if(input.matches('[data-ve-name]')){T.overrides[Number(input.dataset.veName)]=input.value;T.namingDirty=true;invalidateNames();}
 if(input.matches('[data-ve-field^="naming-"]')){syncNaming(input.closest('form'));T.namingDirty=true;invalidateNames();}
});
document.addEventListener('change',event=>{
 const input=event.target;
 if(input.matches('[data-ve-candidate]'))updateSelection();
 if(input.matches('[data-ve-candidate-keep]')){
  const index=Number(input.dataset.veCandidateKeep);T.selected=input.checked?[...new Set([...T.selected,index])]:T.selected.filter(x=>x!==index);T.namingDirty=true;invalidateNames();
 }
 if(input.matches('[data-ve-field^="naming-"]')){syncNaming(input.closest('form'));T.namingDirty=true;invalidateNames();}
 if(input.matches('[data-ve-score]'))document.querySelectorAll('[data-ve-card]').forEach(card=>card.hidden=Number(card.dataset.score)<Number(input.value));
 if(input.matches('[data-ve-field="query-sort"]')){const form=document.querySelector('[data-ve-history]');if(form&&applyQuery(form))navigate('observations-history');}
 if(input.matches('[data-ve-field="session-connection"]')){const label=document.querySelector('[data-ve-session-version]');if(label)label.textContent=input.value.match(/版本 \d+/)?.[0]||'未选择';}
 if(input.matches('[data-ve-field="session-output-mode"]')){
  const field=document.querySelector('[data-ve-field="session-schema"]');if(field){field.disabled=input.value==='纯文本';if(field.disabled)field.value='null';}
 }
});
document.addEventListener('click',event=>{
 const target=event.target;
 const stop=()=>{event.preventDefault();event.stopImmediatePropagation();};
 const go=id=>{stop();navigate(id);};
 const app=target.closest('.app-window');
 const guard=target.closest('[data-ve-validate]');
 if(guard){
  const form=document.querySelector('[data-ve-form]'),error=form?.querySelector('[data-ve-error]');
  let invalid=!!form&&!form.checkValidity();
  const values=form?[...form.querySelectorAll('input[type=number]')].map(x=>Number(x.value)):[];
  if(form?.closest('.ve-templates')&&values.length>=16){
   if(values[4]+values[5]+values[6]===0){invalid=true;if(error)error.textContent='请至少启用一个检测算法。';}
   else if(values[7]>values[8]||values[9]>values[10]){invalid=true;if(error)error.textContent='最小值不能大于对应最大值。';}
   else if(values[0]+values[2]>1280||values[1]+values[3]>720){invalid=true;if(error)error.textContent='提取区域超出当前图像边界。';}
  }
  if(invalid){stop();form?.reportValidity();if(error&&!error.textContent)error.textContent='请修正超出范围或未填写的参数。';return;}
 }
 const reset=target.closest('[data-ve-template-reset]');
 if(reset){T.expiresAt=Date.now()+1800000;T.proposal=null;T.namingDirty=false;if(T.operation?.state!=='running')T.operation=null;}
 if(target.closest('[data-go="templates-progress"]')){
  if(T.operation?.state==='running')return go('templates-receipt-unknown');
  const form=document.querySelector('[data-ve-form]');T.extractValues=[...form.querySelectorAll('input[type=number]')].map(x=>Number(x.value));T.version++;T.expiresAt=Date.now()+1800000;T.selected=[0,1,2,3];T.proposal=null;T.overrides={};T.namingDirty=false;T.operation=null;
 }
 const select=target.closest('[data-ve-select]');
 if(select){document.querySelectorAll('[data-ve-candidate]').forEach(x=>x.checked=select.dataset.veSelect==='all'||(select.dataset.veSelect==='invert'&&!x.checked));updateSelection();stop();return;}
 if(target.closest('[data-ve-keep-current]')){T.selected=[...new Set([...T.selected,T.detail])];T.proposal=null;T.namingDirty=true;}
 const candidate=target.closest('[data-ve-candidate-open]');
 if(candidate)T.detail=Number(candidate.dataset.veCandidateOpen);
 if(target.closest('[data-ve-name-open]')){
  if(templateExpired())return go('templates-expired');
  if(!T.selected.length){stop();return;}buildNames();T.namingDirty=false;
 }
 if(target.closest('[data-ve-ocr-rule]')){T.policy.mode='rule';T.ocrAvailable=false;T.namingDirty=false;buildNames();}
 if(target.closest('[data-ve-names-preview]')){
  const form=target.closest('form');if(!form.checkValidity()){stop();form.reportValidity();return;}syncNaming(form);
  if(templateExpired())return go('templates-expired');
  if(!T.ocrAvailable&&T.policy.mode==='ocrThenRule'&&T.policy.ocrUnavailable==='stop')return go('templates-ocr');
  T.namingDirty=false;buildNames();return go('templates-naming');
 }
 if(target.closest('[data-ve-template-confirm]')){
  if(templateExpired())return go('templates-expired');if(!validNames()){stop();invalidateNames();return;}return go('templates-review');
 }
 if(target.closest('[data-ve-template-commit]')){
  if(templateExpired())return go('templates-expired');if(!validNames())return go('templates-naming');
  beginTemplateCommit();return go('templates-saving');
 }
 if(target.closest('[data-ve-template-query]')){if(T.operation?.state==='failed')return go('templates-save-error');if(T.operation?.state==='running'){T.operation.receiptKnown=true;return go('templates-saving');}return go('templates-batch');}
 if(target.closest('[data-ve-session-start]')){
  const form=document.querySelector('[data-ve-form]'),numbers=[...form.querySelectorAll('input[type=number]')].map(x=>Number(x.value));
  let schema=null;const mode=valueOf('session-output-mode',form)==='纯文本'?'text':'json';
  try{if(mode==='json'){schema=JSON.parse(valueOf('session-schema',form));if(!schema||typeof schema!=='object'||Array.isArray(schema))throw Error();}}catch{stop();form.querySelector('[data-ve-error]').textContent='输出结构必须是 JSON 对象。';return;}
  O.connectionVersion=Number(valueOf('session-connection',form).match(/版本 (\d+)/)?.[1]||2);
  O.connectionId=valueOf('session-connection',form).startsWith('远程')?'remote-vision':'local-vision';O.credentialGeneration=DS.connections?.credentialGeneration||1;
  O.plan={roi:O.draftRoi||{space:'targetPixels',x:220,y:240,width:930,height:300},intervalMs:Math.round(numbers[0]*1000),timeoutMs:Math.round(numbers[1]*1000),maxRecords:numbers[2],maxDurationMs:Math.round(numbers[3]*1000),changeDetection:{enabled:form.querySelectorAll('input[type=checkbox]')[0].checked,threshold:numbers[4]},prompt:valueOf('session-prompt',form),outputMode:mode,outputSchema:schema,saveFrames:form.querySelector('[data-ve-session-save-frames]').checked};
  O.detached=form.querySelector('[data-ve-session-detached]').checked;O.draftDetached=O.detached;O.sessionState='running';O.stopReason=null;O.checkedCount=0;O.startedLabel='今天 09:44:00';O.lastStartRequest={targetToken:'prototype-report-window',connectionId:O.connectionId,connectionVersion:O.connectionVersion,plan:O.plan,detached:O.detached};
 }
 if(target.closest('[data-ve-session-stop]')){O.sessionState='stopping';O.stopReason='由你停止';}
 if(target.closest('[data-ve-session-history]')){O.query.session='report-status';refreshQuerySnapshot();}
 if(target.closest('[data-ve-close-studio]'))DS.beforeCloseStudio();
 if(target.closest('[data-ve-single-start]')){
  const form=document.querySelector('[data-ve-single-form]'),timeout=Number(valueOf('single-timeout',form));
  if(!form.checkValidity()||!valueOf('single-prompt',form).trim()){stop();form.reportValidity();form.querySelector('[data-ve-single-feedback]').innerHTML=notice('请补齐本次请求','观察内容和超时需要有效值。','red');return;}
  O.single={state:'running',sequence:(O.single.sequence||0)+1,record:null,displayRecord:null,plan:{prompt:valueOf('single-prompt',form),connection:valueOf('single-connection',form),credentialGeneration:DS.connections?.credentialGeneration||1,timeoutMs:Math.round(timeout*1000),outputMode:valueOf('single-output',form)==='纯文本'?'text':'json'}};
  return go('observation-running');
 }
 if(target.closest('[data-ve-single-cancel]')){finishSingle('cancelled');return go('observation-cancelled');}
 const rowButton=target.closest('[data-ve-record] button');
 if(rowButton){
  if(app?.dataset.screen==='observations-history'&&queryExpired())return go('observations-query-expired');
  const row=records.find(r=>r.key===rowButton.closest('[data-ve-record]').dataset.record);
  if(row&&!row.sessionKey)O.single.displayRecord=row;
 }
 if(target.closest('[data-ve-query-refresh]')){refreshQuerySnapshot();return go('observations-history');}
 if(target.closest('[data-ve-query]')){const form=document.querySelector('[data-ve-history]');if(form&&applyQuery(form))return go('observations-history');stop();return;}
 const one=target.closest('[data-ve-export-one]');
 if(one){const row=records.find(r=>r.key===one.dataset.veExportOne||r.variant===one.dataset.veExportOne);exportRows=[row];exportRange='本条记录 · 2026-10-29 '+row.time;exportFormat='JSONL';exportImages=true;O.exportSnapshotId=null;}
 if(target.closest('[data-ve-export-single]')){const row=O.single.displayRecord||O.single.record||singleRecord('succeeded');exportRows=[row];exportRange='本条记录 · 单次观察 '+row.time;exportFormat='JSONL';exportImages=true;O.exportSnapshotId=null;}
 if(target.closest('[data-ve-export-query]')){
  if(queryExpired())return go('observations-query-expired');
  queryRecords=queryRows().filter(r=>r.state!=='persistenceFailed');exportRows=[...queryRecords];exportRange='当前查询结果';exportFormat='JSONL';exportImages=true;O.exportSnapshotId=O.querySnapshot?.id;
 }
 if(target.closest('[data-go="observations-exporting"]')){
  if(O.exportSnapshotId&&queryExpired())return go('observations-query-expired');
  exportFormat=document.querySelector('[data-ve-export-format]')?.value||exportFormat;
  exportLocation=document.querySelector('[data-ve-export-location]')?.value||exportLocation;
  exportImages=document.querySelector('[data-ve-export-images]')?.checked??exportImages;
  O.exportSelection={keys:exportedRecords().map(r=>r.key),format:exportFormat,includeFrames:exportImages,location:exportLocation,range:exportRange};
 }
 const output=target.closest('[data-ve-open-output]');
 if(output){const feedback=document.querySelector('[data-ve-output-feedback]');if(feedback)feedback.textContent='输出位置：'+output.dataset.veOpenOutput;}
 const recovery=target.closest('[data-ve-recovery]');
 if(recovery){
  const row=records.find(r=>r.state==='persistenceFailed');
  const content=JSON.stringify({checkedAt:row.checkedAt,capturedAt:row.capturedAt,completedAt:row.completedAt,state:'persistenceFailed',targetName:'报表中心',sessionId:row.sessionKey,runId:null,connectionVersion:2,output:{month:'2026-10',count:row.count,downloadStatus:'可下载'},usage:{inputTokens:row.inputTokens,outputTokens:row.outputTokens},reason:'工作空间存储不足'},null,2);
  const url=URL.createObjectURL(new Blob([content],{type:'application/json'})),link=document.createElement('a');link.href=url;link.download='observation-recovery.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);recovery.textContent='恢复文件已生成';
 }
},true);
DS.cancelObservationsForCredential=(connectionId,generation)=>{
 if((O.connectionId||'local-vision')===connectionId&&(O.credentialGeneration||1)===generation&&O.sessionState==='running'){O.sessionState='failed';O.stopReason='旧模型凭据已撤销';}
 if(O.single.state==='running'&&O.single.plan?.connection==='本地视觉服务'&&(O.single.plan?.credentialGeneration||1)===generation)finishSingle('cancelled');
};
DS.resetForScreen=id=>{
 Object.assign(T,{selected:[0,1,2,3],detail:0,version:1,expiresAt:Date.now()+1800000,policy:{mode:'ocrThenRule',prefix:'报表',startIndex:1,onCollision:'suffix',ocrUnavailable:'rule',languages:['chi_sim','eng']},overrides:{},proposal:null,operation:null,assets:null,batches:0,sequence:0,ocrAvailable:true,namingDirty:false,extractValues:null});
 Object.assign(O,{sessionState:'running',sessionKey:'report-status',sessionName:'报表可用状态',connectionId:'local-vision',connectionVersion:2,credentialGeneration:1,detached:false,draftDetached:false,stopReason:null,plan:null,lastStartRequest:null,checkedCount:null,startedLabel:null,draftRoi:null,single:{state:'idle',sequence:0,record:null},query:{session:'all',sort:'newest',from:'2026-10-29T00:00',until:'2026-10-30T00:00',state:'全部状态'},querySnapshot:null,querySequence:0,exportSnapshotId:null});
 O.records.splice(0,O.records.length,...initialRecords.map(row=>({...row,key:row.variant,sessionKey:'report-status',sessionName:'报表可用状态',checkedAt:localToUTC(row.time),capturedAt:row.variant==='busy'?null:localToUTC(row.time),completedAt:localToUTC(row.done),skipReason:row.skipReason||null,difference:row.difference??null,baselineRecordId:row.variant==='skipped'?'baseline':null,frameId:row.hasFrame?'frame-'+row.variant:null,screenshotResource:row.hasFrame?'frame-resource-'+row.variant:null,output:['succeeded','persistenceFailed'].includes(row.state)?{month:'2026-10',count:row.count}:null,usage:{inputTokens:row.inputTokens,outputTokens:row.outputTokens},runId:null})));
 queryRecords=queryRows().filter(r=>r.state!=='persistenceFailed');exportRows=null;exportFormat='JSONL';exportImages=true;exportLocation='文档 / 观察导出 / 2026-10-29';exportRange='今天 00:00 至明天 00:00（不含）';
 if(id==='observations-stopping')O.sessionState='stopping';
 if(id==='observations-stopped')O.sessionState='stopped';
 if(['observations-target-error','observations-session-failed'].includes(id)){O.sessionState='failed';O.stopReason=id==='observations-target-error'?'目标窗口已关闭':'观察授权已撤销';}
 if(id==='templates-expired')T.expiresAt=0;
 if(id==='observations-query-expired'){if(!O.querySnapshot)refreshQuerySnapshot();O.querySnapshot.expiresAt=0;}
 DS.resetConfigForScreen?.(id);
};

})();
