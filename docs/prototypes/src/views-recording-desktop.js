(() => {
"use strict";
const U=window.DW_UI,V=window.DW_VIEWS,{esc,icon,btn,pill,field,check,notice,table,section,shell,targetScene}=U;
const DS=window.DW_DESIGN_STATE=window.DW_DESIGN_STATE||{};
const clone=value=>JSON.parse(JSON.stringify(value));
const R=DS.recording={};
const button=(label,action,kind='')=>'<button type="button" class="btn '+kind+'" data-rec-action="'+action+'">'+esc(label)+'</button>';
const namedField=(label,value,name,options={})=>field(label,value,'',options).replace(/<(input|select)\b/,'<$1 data-rec-field="'+name+'"');
const sameDisplay=(a,b)=>a&&b&&['bounds','rotation','dpiX','dpiY'].every(key=>JSON.stringify(a[key])===JSON.stringify(b[key]));
const contains=(rect,point)=>point.x>=rect.x&&point.x<rect.x+rect.width&&point.y>=rect.y&&point.y<rect.y+rect.height;
function withoutTop(topology){const next=clone(topology);next.version++;next.displays=next.displays.filter(d=>d.displayId!=='top');next.virtualBounds={x:-1440,y:0,width:4000,height:2560};return next;}
function reset(id){
 const F=window.DW_CATALOG.recordingFixtures;
 Object.assign(R,{topology:clone(F.topology),scope:'allDisplays',selectedId:'main',coordinateMode:'event',includePointerMotion:true,state:'idle',controlVersion:0,source:null,output:null,startResult:null,lastStartRequest:null,partial:false,reason:null,error:'',message:'',mappingMode:'original',mappingOutput:null,mapping:{}});
 if(['record-active','record-paused','record-topology-changed'].includes(id))begin();
 if(id==='record-paused'){R.state='paused';R.reason='USER_PAUSED';R.controlVersion++;}
 if(id==='record-topology-changed')applyTopology(withoutTop(R.topology),false);
 if(id==='record-unavailable'){R.topology.displays=[];R.state='idle';}
 if(id==='trajectory-displays-invalid'){R.topology=withoutTop(R.topology);R.mappingMode='displayLocal';}
 R.mapping=Object.fromEntries(F.source.displayIds.map(key=>[key,R.topology.displays.some(d=>d.displayId===key)?key:'']));
}
function selectedDisplays(){return R.scope==='display'?R.topology.displays.filter(d=>d.displayId===R.selectedId):R.topology.displays;}
function rangeName(){return R.scope==='allDisplays'?'全部 '+R.topology.displays.length+' 个显示屏':R.scope==='display'?(selectedDisplays()[0]?.name||'请选择显示屏'):'报表中心';}
function initialPointer(){const displays=selectedDisplays(),main=displays.find(d=>d.primary)||displays[0];return main?{x:main.bounds.x+Math.min(800,Math.floor(main.bounds.width/2)),y:main.bounds.y+Math.min(500,Math.floor(main.bounds.height/2))}:null;}
function begin(){
 if(!selectedDisplays().length)throw Error('请先选择可用显示屏。');
 R.source={scope:R.scope,topology:clone(R.topology),displayIds:selectedDisplays().map(d=>d.displayId),initialPointer:initialPointer()};
 R.lastStartRequest={targetToken:R.scope==='allDisplays'?'desktop-recording-synthetic':R.scope==='display'?'display-recording-synthetic':'window-recording-synthetic',coordinateMode:R.coordinateMode,includePointerMotion:R.includePointerMotion,expectedTopologyVersion:R.topology.version};
 R.state='recording';R.controlVersion++;R.reason=null;R.partial=false;R.startResult={recordingId:'recording-desktop',state:'recording',controlVersion:R.controlVersion,source:clone(R.source)};R.error='';
}
function events(){
 const F=window.DW_CATALOG.recordingFixtures;
 if(R.coordinateMode==='relative')return [{kind:'pointer.delta',atUs:0,dx:18,dy:-6},{kind:'pointer.button',atUs:100000,button:'left',state:'down'},{kind:'pointer.delta',atUs:350000,dx:-26,dy:12},{kind:'pointer.button',atUs:500000,button:'left',state:'up'}];
 if(R.source?.scope==='allDisplays')return clone(F.trajectory.events).filter(e=>e.kind!=='pointer.move'||R.source.topology.displays.some(d=>R.source.displayIds.includes(d.displayId)&&contains(d.bounds,e))).filter(e=>R.includePointerMotion||e.kind!=='pointer.move'||e.atUs<=500000);
 const point=R.source?.initialPointer||{x:800,y:500};
 return [{kind:'pointer.move',atUs:0,...point},{kind:'pointer.button',atUs:100000,button:'left',state:'down'},{kind:'pointer.move',atUs:350000,x:point.x+40,y:point.y+20},{kind:'pointer.button',atUs:500000,button:'left',state:'up'}];
}
const durationUs=()=>events().at(-1)?.atUs??0;
const clock=us=>{const ticks=Math.floor(us/10000);return String(Math.floor(ticks/6000)).padStart(2,'0')+':'+String(Math.floor(ticks/100)%60).padStart(2,'0')+'.'+String(ticks%100).padStart(2,'0');};
function currentTrajectory(){const trim=DS.completion?.trim;return trim?.recordingSource===R.output&&trim.output?trim.output:R.output||window.DW_CATALOG.recordingFixtures.trajectory;}
function snapshot(){return {recordingId:'recording-desktop',state:R.state,controlVersion:R.controlVersion,source:clone(R.source),currentTopologyVersion:R.topology.version,eventCount:events().length,durationUs:durationUs(),partial:R.partial,reason:R.reason};}
function finish(){
 if(!R.source)begin();R.output={formatVersion:'1.0',coordinateMode:R.coordinateMode,source:clone(R.source),events:events()};R.state='finished';R.reason=R.partial?R.reason:'USER_FINISHED';R.controlVersion++;
 R.mapping=Object.fromEntries(R.source.displayIds.map(key=>[key,R.topology.displays.some(d=>d.displayId===key)?key:'']));R.mappingOutput=null;
 const trim=DS.completion?.trim;if(trim){const endUs=durationUs()+1;Object.assign(trim,{source:clone(R.output),recordingSource:R.output,sourceVersion:trim.sourceVersion+1,fromUs:0,untilUs:endUs,durationUs:endUs,strategies:{},preview:null,output:null,outputDurationUs:null,undo:null,error:'',message:''});}
 return R.output;
}
function applyTopology(topology,navigate=true){R.topology=clone(topology);if(['recording','pausing','paused'].includes(R.state)){R.state='interrupted';R.reason='DISPLAY_TOPOLOGY_CHANGED';R.partial=true;R.controlVersion++;if(navigate)window.DW_PROTOTYPE.go('record-topology-changed');}}
DS.applyRecordingTopology=applyTopology;
DS.recordingSnapshot=snapshot;
function layout(topology,selected,compact=false,interactive=true){
 if(!topology.displays.length)return '<div class="rec-no-displays">'+icon('target',30)+'<h3>未检测到可用显示屏</h3><p>连接显示屏后刷新列表。</p></div>';
 const b=topology.virtualBounds;
 return '<div class="rec-layout-stage '+(compact?'compact':'')+'" aria-label="显示屏布局"><div class="rec-layout-canvas" style="width:min(92%,'+((compact?240:330)*.92*b.width/b.height)+'px);aspect-ratio:'+b.width+'/'+b.height+'">'+topology.displays.map((d,index)=>{const p=d.bounds;const tag=interactive?'button':'div';return '<'+tag+(interactive?' type="button"':'')+' class="rec-monitor '+(selected.includes(d.displayId)?'selected':'')+'" '+(interactive?'data-rec-display="'+esc(d.displayId)+'" ':'')+'aria-label="'+esc(d.name)+'" aria-pressed="'+selected.includes(d.displayId)+'" style="left:'+(100*(p.x-b.x)/b.width)+'%;top:'+(100*(p.y-b.y)/b.height)+'%;width:'+(100*p.width/b.width)+'%;height:'+(100*p.height/b.height)+'%"><span class="rec-screen-number">'+(index+1)+'</span><strong>'+esc(d.name)+(d.primary?' · 主屏':'')+'</strong><small>'+p.width+' × '+p.height+' · '+Math.round(d.dpiX/96*100)+'%</small><span class="rec-monitor-bottom">'+(d.rotation===90||d.rotation===270?'竖向':'横向')+' · '+p.x+', '+p.y+'</span></'+tag+'>';}).join('')+'</div><span class="rec-gap-label">屏间空隙</span></div>';
}
function displayTable(topology){return table(['显示屏','位置与尺寸','缩放 / 方向'],topology.displays.map(d=>[esc(d.name)+(d.primary?' '+pill('主屏','blue'):''),d.bounds.x+', '+d.bounds.y+' · '+d.bounds.width+' × '+d.bounds.height,Math.round(d.dpiX/96*100)+'% · '+d.rotation+'°']));}
function panel(title,body){return '<section class="panel"><div class="panel-head">'+title+'</div><div class="panel-body">'+body+'</div></section>';}
function page(s,body,actions=''){return shell(s,'<main class="page rec-page">'+section(s.title,'录制 / 显示屏与输入轨迹',actions)+body,{active:'workspace'});}
function config(s){
 const picked=selectedDisplays(),range=rangeName();
 const radios=[['allDisplays','全部显示屏','跨屏连续录制'],['display','单个显示屏','只录所选屏幕'],['window','指定窗口','跟随所选窗口']].map(([value,label,description])=>'<label class="rec-scope '+(R.scope===value?'selected':'')+'"><input type="radio" name="recording-scope" data-rec-scope value="'+value+'" '+(R.scope===value?'checked':'')+'><span><strong>'+label+'</strong><small>'+description+'</small></span></label>').join('');
 const scopeControl=R.scope==='display'?namedField('显示屏',R.selectedId,'display',{select:R.topology.displays.map(d=>d.displayId)}).replace(/<option([^>]*)>([^<]+)<\/option>/g,(match,attrs,value)=>'<option value="'+value+'"'+attrs+'>'+esc(R.topology.displays.find(d=>d.displayId===value)?.name||value)+'</option>'):R.scope==='window'?namedField('目标窗口','报表中心','window',{select:['报表中心']})+button('选择窗口','choose-window'):'<div class="rec-range">'+pill(picked.length+' 个显示屏','blue')+'<span>使用当前全部活动显示屏</span></div>';
 return page(s,'<div class="rec-columns"><div class="stack">'+panel('录制范围','<div class="rec-scope-list">'+radios+'</div>'+scopeControl+layout(R.topology,picked.map(d=>d.displayId))+'<div class="rec-layout-footer"><span>桌面物理像素 · 负坐标与混合缩放</span>'+btn('查看屏幕布局','record-displays','small')+'</div>')+panel('显示屏信息',displayTable(R.topology))+'</div><aside class="stack">'+panel('录制设置','<div class="stack">'+namedField('坐标方式',{event:'按事件记录',physical:'物理像素',relative:'原始相对移动'}[R.coordinateMode],'coordinateMode',{select:['按事件记录','物理像素','原始相对移动']})+check('记录连续指针运动',R.includePointerMotion).replace('<input','<input data-rec-motion')+'<div class="rec-setting-note">点击与拖拽使用实际发生位置。</div></div>')+panel('开始录制','<div class="rec-summary"><span>覆盖范围</span><strong>'+esc(range)+'</strong><span>按键与时间轴</span><strong>跨屏连续</strong></div>'+table(['操作','快捷键'],[['录制 / 结束','Ctrl + Alt + R'],['全局急停','Ctrl + Alt + Pause']])+(R.error?notice('无法开始录制',esc(R.error),'red'):'')+'<div class="rec-start">'+button('开始录制','start','primary')+'</div>')+'</aside></div>',btn('平台权限','platform-capabilities')+btn('返回工作台','editor-graph'));
}
function displays(s){return page(s,'<div class="rec-columns"><div class="stack">'+panel('当前屏幕布局',layout(R.topology,selectedDisplays().map(d=>d.displayId)))+panel('显示屏信息',displayTable(R.topology))+'</div><aside class="stack">'+panel('录制范围','<div class="rec-summary"><span>当前选择</span><strong>'+esc(rangeName())+'</strong><span>有效区域</span><strong>所选显示区域的并集</strong></div>'+button('刷新显示屏','refresh')+'<div class="rec-layout-help">点击屏幕可查看单屏范围；选择“全部显示屏”可恢复全屏录制。</div>'+button('选择全部显示屏','select-all','primary'))+'</aside></div>',btn('使用当前范围','record-config','primary'));
}
function changed(s){return page(s,'<div class="rec-columns"><div class="stack">'+notice('显示屏布局已变化','当前录制已结束。已录操作可以保存为分段草稿。','amber')+panel('录制时的布局',layout(R.source.topology,R.source.displayIds,true,false))+panel('当前屏幕布局',layout(R.topology,R.topology.displays.map(d=>d.displayId),true,false))+'</div><aside class="stack">'+panel('已录分段','<div class="rec-summary"><span>录制范围</span><strong>原 '+R.source.displayIds.length+' 个显示屏</strong><span>已录事件</span><strong>'+events().length+' 个</strong><span>处理方式</span><strong>保存分段或新建录制</strong></div><div class="rec-actions">'+button('保存已录分段','finish','primary')+button('按当前屏幕新建录制','restart')+'</div>')+'</aside></div>');}
function unavailable(s){return page(s,panel('显示屏不可用','<div class="rec-empty">'+icon('target',36)+'<h2>未检测到可用显示屏</h2><p>连接显示屏或返回交互桌面后刷新。</p>'+button('刷新显示屏','refresh','primary')+'</div>'),btn('返回工作台','editor-graph'));}
function mappingErrors(){
 const trajectory=currentTrajectory(),source=trajectory.source,targets=new Map(R.topology.displays.map(d=>[d.displayId,d])),displays=source.topology.displays.filter(d=>source.displayIds.includes(d.displayId)),relative=trajectory.events.some(e=>e.kind==='pointer.delta');
 const errors=[];if(source.initialPointer===null)errors.push('请在轨迹编辑器设置起点');
 if(R.mappingMode==='original'){for(const d of (relative?source.topology.displays:displays))if(!sameDisplay(d,targets.get(d.displayId)))errors.push(d.name+' 的当前布局不同');if(relative&&source.topology.displays.length!==targets.size)errors.push('相对轨迹需要原显示屏布局');return [...new Set(errors)];}
 if(relative)errors.push('相对输入需使用原布局回放');
 const used=new Set();
 for(const display of displays){const target=targets.get(R.mapping[display.displayId]);if(!target){errors.push(display.name+' 尚未选择当前显示屏');continue;}if(used.has(target.displayId))errors.push('每个来源需选择不同的显示屏');used.add(target.displayId);if(display.rotation!==target.rotation)errors.push(display.name+' 的屏幕方向不同');const points=[...trajectory.events.filter(e=>e.kind==='pointer.move'),...(source.initialPointer?[source.initialPointer]:[])].filter(point=>contains(display.bounds,point));for(const point of points){const translated={x:point.x-display.bounds.x+target.bounds.x,y:point.y-display.bounds.y+target.bounds.y};if(!contains(target.bounds,translated))errors.push(display.name+' 的轨迹超出当前显示屏');}}
 return [...new Set(errors)];
}
function mapping(s){
 const source=currentTrajectory().source,errors=mappingErrors(),displays=source.topology.displays.filter(d=>source.displayIds.includes(d.displayId));
 const fields=displays.map(d=>'<label class="field"><span class="field-label">'+esc(d.name)+' · '+d.bounds.width+' × '+d.bounds.height+'</span><select data-rec-map="'+esc(d.displayId)+'" '+(R.mappingMode==='original'?'disabled':'')+'><option value="">选择当前显示屏</option>'+R.topology.displays.map(target=>'<option value="'+esc(target.displayId)+'" '+(R.mapping[d.displayId]===target.displayId?'selected':'')+'>'+esc(target.name)+' · '+target.bounds.width+' × '+target.bounds.height+'</option>').join('')+'</select></label>').join('');
 return page(s,'<div class="rec-columns"><div class="stack">'+panel('录制来源 · '+(source.scope==='allDisplays'?'全部显示屏':source.scope==='display'?'单个显示屏':'指定窗口'),layout(source.topology,source.displayIds,true,false))+panel('当前显示屏',layout(R.topology,R.topology.displays.map(d=>d.displayId),true,false))+'</div><aside class="stack">'+panel('回放映射','<div class="stack">'+namedField('映射方式',R.mappingMode==='original'?'使用原布局':'按屏内像素平移','mappingMode',{select:['使用原布局','按屏内像素平移']})+fields+'</div>')+panel('准备结果',errors.length?notice('需要调整映射',errors.map(esc).join('；'),'amber'):notice(R.mappingOutput?'映射已应用':'可以使用当前布局','所有轨迹位置均位于所选显示区域内。','green'))+'<div class="rec-actions">'+button('应用显示屏映射','apply-mapping','primary').replace('<button','<button '+(errors.length?'disabled':''))+btn('返回时间轴','trajectory-editor')+'</div></aside></div>');
}
V.recording=s=>s.variant==='layout'?displays(s):s.variant==='topology-changed'?changed(s):s.variant==='unavailable'?unavailable(s):s.variant==='mapping'||s.variant==='mapping-invalid'?mapping(s):config(s);
V.record=s=>{const paused=s.variant==='paused';return '<div class="desktop-surface">'+targetScene('full')+'<div class="record-float"><span class="record-mark '+(paused?'paused':'')+'"></span><div class="record-title">'+(paused?'录制已暂停':'正在录制')+'<small>'+esc(R.source?.scope==='allDisplays'?'全部 '+R.source.displayIds.length+' 个显示屏':rangeName())+'</small></div><span class="record-clock">'+clock(durationUs())+'</span><span class="record-count">'+events().length+' 个事件</span><span class="toolbar-sep"></span>'+button(paused?'继续':'暂停',paused?'resume':'pause')+button('结束','finish','primary')+'</div><div class="record-tip">'+(paused?'暂停期间的操作不加入轨迹':'跨屏操作连续录制 · Ctrl + Alt + R 结束')+'</div></div>';};
const previous=DS.resetForScreen;DS.resetForScreen=id=>{previous?.(id);if(['record-config','record-active','record-paused','record-displays','record-topology-changed','record-unavailable','trajectory-displays','trajectory-displays-invalid'].includes(id)||!R.topology)reset(id);};
document.addEventListener('change',event=>{
 if(event.target.matches('[data-rec-scope]')){R.scope=event.target.value;window.DW_PROTOTYPE.go('record-config');}
 if(event.target.matches('[data-rec-motion]'))R.includePointerMotion=event.target.checked;
 if(event.target.matches('[data-rec-field="coordinateMode"]'))R.coordinateMode={'按事件记录':'event','物理像素':'physical','原始相对移动':'relative'}[event.target.value];
 if(event.target.matches('[data-rec-field="display"]')){R.selectedId=event.target.value;window.DW_PROTOTYPE.go('record-config');}
 if(event.target.matches('[data-rec-field="mappingMode"]')){R.mappingMode=event.target.value==='使用原布局'?'original':'displayLocal';R.mappingOutput=null;window.DW_PROTOTYPE.go('trajectory-displays');}
 if(event.target.matches('[data-rec-map]')){R.mapping[event.target.dataset.recMap]=event.target.value;R.mappingOutput=null;window.DW_PROTOTYPE.go('trajectory-displays');}
},true);
document.addEventListener('click',event=>{
 const screen=event.target.closest('[data-rec-display]');if(screen){event.preventDefault();event.stopImmediatePropagation();R.scope='display';R.selectedId=screen.dataset.recDisplay;window.DW_PROTOTYPE.go(location.hash.includes('record-displays')?'record-displays':'record-config');return;}
 const action=event.target.closest('[data-rec-action]');if(!action)return;event.preventDefault();event.stopImmediatePropagation();
 const go=id=>window.DW_PROTOTYPE.go(id);
 try{switch(action.dataset.recAction){
 case 'start':begin();go('record-active');break;
 case 'pause':R.state='paused';R.reason='USER_PAUSED';R.controlVersion++;go('record-paused');break;
 case 'resume':if(R.topology.version!==R.source.topology.version){applyTopology(R.topology);break;}R.state='recording';R.reason=null;R.controlVersion++;go('record-active');break;
 case 'finish':finish();go('trajectory-editor');break;
 case 'restart':R.state='idle';R.scope='allDisplays';R.source=null;R.error='';go('record-config');break;
 case 'refresh':if(!R.topology.displays.length)R.topology=clone(window.DW_CATALOG.recordingFixtures.topology);go('record-config');break;
 case 'select-all':R.scope='allDisplays';go('record-displays');break;
 case 'choose-window':R.scope='window';go('record-config');break;
 case 'apply-mapping':{const errors=mappingErrors();if(errors.length)throw Error(errors.join('；'));R.mappingOutput=R.mappingMode==='original'?{mode:'original'}:{mode:'displayLocal',displays:currentTrajectory().source.displayIds.map(sourceDisplayId=>({sourceDisplayId,targetDisplayId:R.mapping[sourceDisplayId]}))};go('trajectory-displays');break;}
 }}catch(error){R.error=error.message;go('record-config');}
},true);
window.DW_RECORDING={snapshot,finish,currentTrajectory,mappingErrors,layout};
})();
