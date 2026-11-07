(() => {
'use strict';
const DS=window.DW_DESIGN_STATE,F=window.DW_CATALOG.authoringFixtures,A=DS.authoring={},clone=v=>JSON.parse(JSON.stringify(v)),now=()=>Date.now(),utc=n=>new Date(n??now()).toISOString().replace(/\.\d{3}Z$/,'Z');
const bytes=v=>new TextEncoder().encode(JSON.stringify(v)),canonical=v=>v===null||typeof v!=='object'?JSON.stringify(v):Array.isArray(v)?'['+v.map(canonical).join(',')+']':'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}';
const hash=async b=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',b))].map(n=>n.toString(16).padStart(2,'0')).join('');
const runtime={waitForResult:()=>new Promise(resolve=>setTimeout(resolve,1500)),readEditResult:async op=>({operationId:op.id,version:op.version,state:op.state,result:clone(op.result),error:op.error})};
function stamp(seconds=300){const n=now();return{snapshotId:'query-'+(++A.sequence),snapshotAt:utc(n),expiresAt:utc(n+seconds*1000)};}
function seed(id){
 A.sequence=A.sequence||0;
 if(!A.variables||id.startsWith('variables-')){A.variables={scope:id==='variables-redacted'?'inputs':'local',frameId:'frame-main',dataVersion:8,controlVersion:4,snapshot:null,page:null,offset:0,changed:false,error:'',message:'',runState:'paused'};if(id==='variables-redacted')readVariables();if(id==='variables-expired'){readVariables();A.variables.snapshot.expiresAt=utc(now()-1000);A.variables.error='这份变量快照已过期，请重新读取。';}}
 if(!A.revisions||id.startsWith('revision-')){A.revisions={snapshot:null,offset:0,selected:'11',opened:null,error:'',message:''};readRevisions();if(id==='revision-query-expired'){A.revisions.snapshot.expiresAt=utc(now()-1000);A.revisions.error='历史查询已过期，当前草稿和所选版本保持不变。';}}
 if(!A.connections||id==='connection-browser'){A.connections={snapshot:null,offset:0,error:'',message:''};readConnections();}
 if(!A.test||['connection-draft-test','connection-test-stale'].includes(id)){const config=clone(F['connection-test-draft']);config.editorId='studio-connection-editor';config.editVersion=3;A.test={draft:config,saved:clone(config),pending:null,result:null,status:'idle',error:'',message:'',sequence:0,savedVersion:2};if(id==='connection-test-stale'){A.test.draft.model='vision-local-v2';A.test.draft.editVersion++;A.test.status='stale';A.test.message='模型已修改，原测试结果已失效。';}}
 if(!A.edit||id.startsWith('asset-edit-')){
  A.edit=freshEdit();
  if(id==='asset-edit-conflict'||id==='asset-edit-review'){prepareEdit();A.edit.service.head='4';A.edit.currentVersion='4';A.edit.status='conflict';A.edit.error='当前资产已更新到版本 4，草稿基于版本 3。';if(id==='asset-edit-review')reviewEdit();}
  if(id==='asset-edit-uncertain'){prepareEdit();const e=A.edit,op={id:'asset-result-pending',plan:clone(e.plan),state:'succeeded',version:2,result:{...clone(F['asset-image-result']),editId:e.plan.editId},error:null,done:Promise.resolve()};e.operationId=op.id;e.service.operations[op.id]=op;e.service.head='4';e.service.committed=1;e.status='uncertain';e.pending=true;e.error='保存结果读取失败，请查询本次保存结果。';}
  if(id==='asset-edit-cancelled'){A.edit.status='cancelled';A.edit.message='保存已取消，编辑内容继续保留。';}
 }
 if(!A.content||id.startsWith('content-')){const model=clone(['content-save-conflict','content-copy-failed'].includes(id)?F['workflow-model-dependencies']:F['workflow-model']);model.definition.variables.largeSample=Array(2048).fill('样例'.repeat(100));A.previousContent=null;A.content={model,raw:bytes(model),offset:0,parts:[],stage:'draft',draftVersion:6,baseRevision:'12',currentRevision:'12',ref:null,result:null,error:'',message:'',digest:null};if(['content-save-conflict','content-copy-failed'].includes(id)){A.content.currentRevision='14';A.content.stage='conflict';A.content.error='当前流程已更新到版本 14，当前草稿基于版本 12。';}if(id==='content-copy-failed'){const candidate={...A.content,model:clone(A.content.model),parts:[]};candidate.model.definition.id='workflow-copy-preview';candidate.model.definition.name+=' 副本';candidate.raw=bytes(candidate.model);candidate.baseRevision=null;candidate.currentRevision=null;candidate.draftVersion=1;candidate.stage='failed';candidate.error='副本内容传输中断，请重新保存或返回原草稿。';candidate.isCopy=true;A.content.copy={active:true,candidate};}if(id==='content-transfer-failed'){A.content.stage='failed';A.content.error='保存中断，请重新保存。当前草稿已保留。';}}
}
function variableItems(v){if(v.scope==='inputs')return[{name:'month',valueType:'string',visibility:'visible',value:'2026-10'},{name:'accessToken',valueType:'string',visibility:'redacted'},{name:'summary',valueType:'string',visibility:'redacted'},{name:'selectedBranch',valueType:'boolean',visibility:'redacted'}];if(v.scope==='outputs')return[{name:'completed',valueType:'boolean',visibility:'visible',value:true}];if(v.scope==='previous')return[{name:'matchCount',valueType:'number',visibility:'visible',value:1}];if(v.scope==='shared')return[{name:'downloadCount',valueType:'number',visibility:'visible',value:v.dataVersion-6}];return[{name:'month',valueType:'string',visibility:'visible',value:'2026-10'},{name:'downloadCount',valueType:'number',visibility:'visible',value:v.dataVersion-6},{name:'ready',valueType:'boolean',visibility:'visible',value:false},{name:'accessToken',valueType:'string',visibility:'redacted'},{name:'summary',valueType:'string',visibility:'redacted'},{name:'rows',valueType:'array',visibility:'visible',valueResource:{...clone(F.largeVariable.reference),expiresAt:utc(now()+60000)}},{name:'previousCapture',valueType:'object',visibility:'unavailable',reason:'notCaptured'}];}
function readVariables(refresh=true){const v=A.variables;if(refresh){v.snapshot={...stamp(60),items:variableItems(v).sort((a,b)=>a.name<b.name?-1:a.name>b.name?1:0),dataVersion:v.dataVersion,controlVersion:v.controlVersion,scope:v.scope,frameId:v.scope==='shared'?null:v.frameId};v.offset=0;v.changed=false;v.resourceValue=null;v.error='';}const q=v.snapshot;if(!q||now()>=Date.parse(q.expiresAt))throw Error('这份变量快照已过期，请重新读取。');if(q.scope!==v.scope||q.frameId!==(v.scope==='shared'?null:v.frameId))throw Error('变量页不属于当前调用与范围，请重新读取。');v.page={runId:'run-monthly-report',snapshotId:q.snapshotId,snapshotAt:q.snapshotAt,expiresAt:q.expiresAt,dataVersion:q.dataVersion,controlVersion:q.controlVersion,frameId:q.frameId,scope:q.scope,total:q.items.length,items:clone(q.items.slice(v.offset,v.offset+3)),nextCursor:v.offset+3<q.items.length?q.snapshotId+':'+(v.offset+3):null};return clone(v.page);}
function variablesNext(){const v=A.variables;if(!v.page?.nextCursor)return;v.offset+=3;try{return readVariables(false);}catch(e){v.offset-=3;throw e;}}
function readRevisions(refresh=true){const r=A.revisions;if(refresh){r.snapshot={...stamp(),items:Array.from({length:7},(_,i)=>({revision:String(12-i),parentRevision:i===6?null:String(11-i),name:'月度报表下载',createdAt:'2026-10-'+String(29-i)+'T01:42:00Z',definitionHash:String(i+1).repeat(64)}))};r.offset=0;r.error='';}if(now()>=Date.parse(r.snapshot.expiresAt))throw Error('历史查询已过期，请重新查询。');return r.page={workflowId:'monthly-report',headRevision:'12',...Object.fromEntries(['snapshotId','snapshotAt','expiresAt'].map(k=>[k,r.snapshot[k]])),items:clone(r.snapshot.items.slice(r.offset,r.offset+3)),nextCursor:r.offset+3<r.snapshot.items.length?r.snapshot.snapshotId+':'+(r.offset+3):null};}
function readConnections(refresh=true){const c=A.connections;if(refresh){c.snapshot={...stamp(),items:Array.from({length:4},(_,i)=>({connectionId:'connection-'+i,name:['本地视觉服务','图像描述服务','文字识别服务','备用视觉服务'][i],version:i+2,endpoint:'http://127.0.0.1:'+String(11434+i)+'/v1',model:['vision-local','image-description','text-reader','vision-backup'][i],credentialPresent:i===1,updatedAt:'2026-10-29T01:40:00Z'}))};c.offset=0;c.error='';}if(now()>=Date.parse(c.snapshot.expiresAt))throw Error('连接查询已过期，请重新查询。');return c.page={...Object.fromEntries(['snapshotId','snapshotAt','expiresAt'].map(k=>[k,c.snapshot[k]])),items:clone(c.snapshot.items.slice(c.offset,c.offset+3)),nextCursor:c.offset+3<c.snapshot.items.length?c.snapshot.snapshotId+':'+(c.offset+3):null};}
async function beginTest(){const t=A.test;t.result=null;t.status='testing';t.error='';const config=clone(t.draft),seq=++t.sequence;const digest=await hash(new TextEncoder().encode(canonical({endpoint:config.endpoint,model:config.model,options:config.options,credential:config.credential})));if(seq!==t.sequence)return;t.pending={config,digest,seq};t.message='正在测试当前编辑内容…';return clone(t.pending);}
function finishTest(reason=null,pending=A.test.pending){const t=A.test;if(!pending)return null;const result={editorId:pending.config.editorId,editVersion:pending.config.editVersion,configurationHash:pending.digest,configurationValid:reason!=='configurationInvalid',reachable:!['configurationInvalid','networkFailed','timeout','cancelled'].includes(reason),modelAvailable:reason===null,reason,checkedAt:utc()};if(t.sequence!==pending.seq||t.draft.editVersion!==result.editVersion||t.status==='cancelled')return result;t.result=result;t.pending=null;t.status=reason?'failed':'passed';t.message=reason?'连接测试未完成：'+({networkFailed:'无法连接服务',modelNotFound:'找不到所选模型',cancelled:'测试已取消'}[reason]||'检查配置后重试'):'连接成功，所选模型可用。';return clone(result);}
function changeTest(key,value){const t=A.test;t.draft[key]=value;t.draft.editVersion++;t.sequence++;t.pending=null;t.result=null;t.status='stale';t.message='配置已修改，请重新测试当前草稿。';}
const editTerminal=state=>['succeeded','failed','cancelled'].includes(state);
function freshEdit(kind='image'){
 const image=kind==='image',version=image?'3':'4';
 const source=image?{version:'3',image:'fixtures/edit-source.png',width:64,height:32,sha256:F['asset-image-plan'].sourceSha256}:{version:'4',trajectory:clone(F.trajectorySource),durationUs:1600000,sha256:F.trajectorySourceHash};
 const latest=image?{version:'4',image:'fixtures/edit-current.png',width:32,height:16,sha256:F['asset-image-result'].sha256}:{version:'5',trajectory:clone(F['edited-trajectory']),durationUs:4000};
 return{kind,destination:'newVersion',name:image?'下载按钮裁剪':'归档快捷操作副本',currentVersion:version,baseVersion:version,source,review:null,plan:null,operationId:null,status:'idle',result:null,pending:false,saving:false,committed:0,sequence:0,readSequence:0,operationVersion:0,error:'',message:'',roi:clone(F['asset-image-commit'].content.roi),trajectory:clone(F['edited-trajectory']),durationUs:4000,service:{head:version,versions:{[version]:source,[latest.version]:latest},operations:{},committed:0}};
}
function editLive(e){const op=e.service.operations[e.operationId];return !!op&&!editTerminal(op.state)||e.status==='uncertain';}
function editConflict(e){e.status='conflict';e.currentVersion=e.service.head;e.review=null;e.message='';throw Error('资产版本已变化，请查看当前版本并选择保存方式。');}
function prepareEdit(){
 const e=A.edit;if(editLive(e))throw Error('请先查询本次保存结果。');
 if(e.destination==='create'&&(!e.name.trim()||[...e.name].length>120))throw Error('请填写 1～120 个字符的资产名称。');
 if(e.destination==='newVersion'&&e.baseVersion!==e.service.head)editConflict(e);
 e.sequence++;e.plan=clone(F['asset-image-plan']);e.plan.editId='manual-edit-'+(++A.sequence);e.plan.expiresAt=utc(now()+1800000);e.plan.assetKind=e.kind;
 e.plan.destination=e.destination==='create'?{mode:'create',name:e.name}:{mode:'newVersion',assetId:e.kind==='image'?'button-template':'archive-shortcut',expectedCurrentVersion:e.baseVersion};
 if(e.kind==='trajectory'){e.plan.sourceWidth=null;e.plan.sourceHeight=null;e.plan.sourceDurationUs=1600000;e.plan.sourceVersion='4';e.plan.sourceSha256=F.trajectorySourceHash;}
 e.operationId=null;e.operationVersion=0;e.error='';e.result=null;e.status='prepared';e.message='正在保存编辑内容。';return clone(e.plan);
}
async function executeEdit(e,op){
 try{
  await runtime.waitForResult();
  let digest=F['asset-image-result'].sha256,length=F['asset-image-result'].length;
  if(op.kind==='trajectory'){const data=bytes(op.trajectory);digest=await hash(data);length=data.length;}
  if(op.cancelRequested){op.state='cancelled';return;}
  const dest=op.plan.destination;
  if(dest.mode==='newVersion'&&e.service.head!==dest.expectedCurrentVersion){op.error='ASSET_EDIT_CONFLICT';op.state='failed';return;}
  const previous=dest.mode==='newVersion'?e.service.head:null,version=previous?String(Number(previous)+1):'1';
  op.result={editId:op.plan.editId,assetId:dest.assetId||'manual-copy-'+op.plan.editId,assetVersion:version,previousVersion:previous,kind:op.kind,name:dest.mode==='create'?dest.name:op.kind==='image'?'下载按钮':'归档快捷操作',sourceSha256:op.plan.sourceSha256,sha256:digest,length,createdNewVersion:previous!==null};
  if(previous){e.service.head=version;e.service.versions[version]=op.kind==='image'?{version,image:'fixtures/edit-current.png',width:op.roi.width,height:op.roi.height,sha256:digest}:{version,trajectory:clone(op.trajectory),durationUs:op.durationUs,sha256:digest};}
  e.service.committed++;op.state='succeeded';
 }catch{op.state='failed';op.error='保存未完成，请重新保存编辑内容。';}
 finally{op.version++;}
}
async function commitEdit(){
 const e=A.edit,p=e.plan;if(!p||now()>=Date.parse(p.expiresAt))throw Error('保存操作已过期，请重新保存。');
 const existing=Object.values(e.service.operations).find(op=>op.plan.editId===p.editId);if(existing)return{accepted:true,operationId:existing.id,editId:p.editId};
 if(p.destination.mode==='newVersion'&&p.destination.expectedCurrentVersion!==e.service.head)editConflict(e);
 if(e.kind==='image'){const r=e.roi;if(r.x<0||r.y<0||r.x+r.width>p.sourceWidth||r.y+r.height>p.sourceHeight)throw Error('裁剪选区超出来源图像。');}
 const op={id:'asset-save-'+(++A.sequence),plan:clone(p),kind:e.kind,roi:clone(e.roi),trajectory:clone(e.trajectory),durationUs:e.durationUs,state:'running',version:1,cancelRequested:false,result:null,error:null};
 e.service.operations[op.id]=op;e.operationId=op.id;e.operationVersion=1;e.pending=true;e.saving=true;e.status='saving';op.done=executeEdit(e,op);
 return{accepted:true,operationId:op.id,editId:p.editId};
}
async function readEditResult(e,op){
 const read=++e.readSequence;
 try{
  const value=await runtime.readEditResult(op);
  if(value.operationId!==op.id||value.state==='succeeded'&&value.result?.editId!==op.plan.editId)throw Error('保存结果不属于当前操作。');
  if(A.edit!==e||e.operationId!==op.id||e.readSequence!==read||value.version<e.operationVersion)return null;
  e.operationVersion=value.version;e.pending=!editTerminal(value.state);e.saving=['running','cancelling'].includes(value.state);e.error='';
  e.status=value.state==='running'?'saving':value.state;
  if(value.state==='succeeded'){e.result=clone(value.result);e.currentVersion=value.result.assetVersion;e.committed=e.service.committed;e.message='版本 '+value.result.assetVersion+' 已保存。';}
  else if(value.state==='uncertain'){e.message='保存结果待查询，请继续查询本次保存结果。';}
  else if(value.state==='cancelled'){e.message='保存已取消，编辑内容继续保留。';}
  else if(value.state==='failed'){e.message='';if(value.error==='ASSET_EDIT_CONFLICT'){e.status='conflict';e.currentVersion=e.service.head;e.review=null;e.error='资产版本已变化，请查看当前版本并选择保存方式。';}else e.error=value.error;}
  else e.message=value.state==='cancelling'?'正在取消保存…':'正在保存编辑内容。';
  return clone(value);
 }catch{
  if(A.edit===e&&e.operationId===op.id&&e.readSequence===read){e.status='uncertain';e.pending=true;e.saving=false;e.message='';e.error='保存结果读取失败，请查询本次保存结果。';}
  return null;
 }
}
async function queryEdit(){const e=A.edit,op=e.service.operations[e.operationId];if(!op)throw Error('当前没有已提交的保存操作。');return readEditResult(e,op);}
async function finishEdit(){const e=A.edit,op=e.service.operations[e.operationId];if(!op)throw Error('请先保存编辑内容。');await op.done;await readEditResult(e,op);return clone(e.result);}
async function cancelEdit(onProgress=()=>{}){
 const e=A.edit,op=e.service.operations[e.operationId];
 if(!op){e.plan=null;e.status='cancelled';e.saving=false;e.pending=false;e.message='保存已取消，编辑内容继续保留。';return;}
 if(!editTerminal(op.state)){op.cancelRequested=true;op.state='cancelling';op.version++;e.operationVersion=op.version;e.status='cancelling';e.saving=true;e.message='正在取消保存…';onProgress();}
 await op.done;await readEditResult(e,op);onProgress();return clone(e.result);
}
function reviewEdit(){const e=A.edit;if(editLive(e))throw Error('请先查询本次保存结果。');e.review=clone(e.service.versions[e.service.head]);e.currentVersion=e.service.head;e.error='';e.message='已读取当前版本，请选择追加版本或独立另存。';return clone(e.review);}
async function resolveEdit(mode,onProgress=()=>{}){
 const e=A.edit;if(!e.review||e.review.version!==e.service.head)editConflict(e);
 if(editLive(e))throw Error('请先查询本次保存结果。');
 e.baseVersion=e.review.version;e.destination=mode;e.plan=null;e.operationId=null;e.status='idle';return submitEdit(onProgress);
}
function changeEditKind(kind){if(editLive(A.edit))throw Error('请先完成当前保存操作。');A.edit=freshEdit(kind);}
const contentSession=()=>A.content.copy?.active?A.content.copy.candidate:A.content;
const contentCurrent=c=>A.content===c||A.content.copy?.candidate===c;
async function startContent(c=contentSession()){const sequence=c.sequence=(c.sequence||0)+1;c.offset=0;c.parts=[];c.ref=null;c.stage='uploading';c.error='';c.expiresAt=now()+60000;c.fixedDraftVersion=c.draftVersion;c.message='正在保存当前草稿。';const digest=await hash(c.raw);if(c.sequence!==sequence)throw Error('本次保存已取消。');c.digest=digest;}
async function writeChunk({offset=contentSession().offset,chunk=null}={},c=contentSession()){const sequence=c.sequence;if(c.stage!=='uploading'||now()>=c.expiresAt)throw Error('内容传输已到期，请重新保存。');const data=chunk||c.raw.slice(offset,offset+196608);const old=c.parts.find(p=>p.offset===offset),digest=await hash(data);if(c.sequence!==sequence||c.stage!=='uploading')throw Error('本次保存已取消。');if(old){if(old.sha256!==digest)throw Error('已接收的内容发生变化，请重新保存。');return{uploadId:'workflow-upload',nextOffset:c.offset,replayed:true};}if(offset!==c.offset||data.length===0||data.length>196608||offset+data.length>c.raw.length)throw Error('保存数据的顺序或长度不正确。');c.parts.push({offset,sha256:digest,data:[...data]});c.offset+=data.length;c.message='内容传输 '+Math.round(c.offset/c.raw.length*100)+'%。';return{uploadId:'workflow-upload',nextOffset:c.offset,replayed:false};}
async function sealContent(c=contentSession()){const sequence=c.sequence;if(c.offset!==c.raw.length)throw Error('保存数据尚未完整传输。');if(now()>=c.expiresAt)throw Error('内容上传已到期，请重新保存。');const data=new Uint8Array(c.parts.flatMap(p=>p.data));if(await hash(data)!==c.digest)throw Error('传输内容校验失败，请重新保存。');if(c.sequence!==sequence)throw Error('本次保存已取消。');JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(data));c.ref={kind:'resource',resourceId:'workflow-content',length:data.length,sha256:c.digest};c.stage='sealed';c.refExpiresAt=now()+60000;c.message='内容已接收，正在保存流程版本。';return clone(c.ref);}
function checkContentVersion(c){
 if(c.currentRevision===c.baseRevision)return;
 c.stage='conflict';c.message='';
 throw Error('流程版本已变化：当前版本 '+(c.currentRevision??'不存在')+'，草稿基于 '+(c.baseRevision??'新流程')+'。可将当前草稿另存为新流程。');
}
async function saveContent(c=contentSession()){
 const sequence=c.sequence;if(c.result)return clone(c.result);
 if(c.stage!=='sealed'||now()>=c.refExpiresAt)throw Error('保存内容已过期，请重新保存。');
 checkContentVersion(c);
 if(c.draftVersion!==c.fixedDraftVersion)throw Error('草稿已修改，请保存当前编辑内容。');
 const definitionHash=await hash(new TextEncoder().encode(canonical(c.model.definition)));
 if(!contentCurrent(c)||c.sequence!==sequence)throw Error('本次保存已取消。');
 checkContentVersion(c);
 if(c.draftVersion!==c.fixedDraftVersion)throw Error('草稿已修改，请保存当前编辑内容。');
 c.saveParams={workflowId:c.model.definition.id,draftVersion:c.draftVersion,baseRevision:c.baseRevision,modelRef:clone(c.ref)};
 const revision=c.currentRevision===null?'1':String(Number(c.currentRevision)+1);
 c.stage='saved';c.currentRevision=revision;c.result={workflowId:c.model.definition.id,revision,draftVersion:c.draftVersion,definitionHash};
 c.message='流程版本 '+revision+' 已保存。';return clone(c.saveParams);
}
async function saveContentCopy(name,onProgress=()=>{}){
 const original=A.content;if(original.copy?.active)throw Error('请先处理当前副本的保存结果。');
 if(original.stage!=='conflict')throw Error('请先核对流程的保存状态。');
 original.copyName=name;const title=name.trim();if(!title||[...title].length>120)throw Error('请填写 1～120 个字符的新流程名称。');
 const model=clone(original.model);model.definition.id='workflow-copy-'+(++A.sequence);model.definition.name=title;
 const candidate={model,raw:bytes(model),baseRevision:null,currentRevision:null,draftVersion:1,offset:0,parts:[],ref:null,digest:null,result:null,saveParams:null,stage:'draft',error:'',message:'',isCopy:true};
 original.sequence=(original.sequence||0)+1;original.copy={candidate,active:true};
 return submitContent(onProgress,candidate);
}
function returnOriginalContent(){
 if(A.content.copy?.active){const c=A.content.copy.candidate;if(c.saving)throw Error('请先完成或取消副本保存。');A.content.copy.active=false;A.content.sequence=(A.content.sequence||0)+1;A.content.error='';A.content.message='已返回原流程草稿。';}
 else if(A.previousContent){A.content=A.previousContent;A.previousContent=null;A.content.copy.active=false;}
}
function openContentCopy(){const root=A.content,c=root.copy?.candidate;if(!c?.result)throw Error('副本尚未保存。');A.previousContent=root;A.content=c;}
async function readVariableResource(){const v=A.variables,row=v.page?.items.find(x=>x.valueResource);if(!row||now()>=Date.parse(v.snapshot.expiresAt)||now()>=Date.parse(row.valueResource.expiresAt))throw Error('变量资源已过期，请重新读取快照。');const data=bytes(F.largeVariable.value);if(data.length!==row.valueResource.length||await hash(data)!==row.valueResource.sha256)throw Error('变量资源内容不完整，请重新读取。');v.resourceValue=clone(F.largeVariable.value);return clone(v.resourceValue);}
async function testConnection(onProgress=()=>{}){const t=A.test,pending=await beginTest();onProgress();await runtime.waitForResult();if(A.test!==t||!pending||t.sequence!==pending.seq)return;finishTest(null,pending);onProgress();}
async function submitEdit(onProgress=()=>{}){
 const e=A.edit;if(e.result)return clone(e.result);if(editLive(e))return queryEdit();
 try{prepareEdit();await commitEdit();onProgress();await finishEdit();}
 catch(error){e.error=error.message;if(e.status!=='conflict')e.status='failed';throw error;}
 finally{onProgress();}
}
const previousReset=DS.resetForScreen;DS.resetForScreen=id=>{previousReset?.(id);seed(id);};
function cancelContent(){const c=contentSession();if(c.result)return;c.sequence=(c.sequence||0)+1;c.saving=false;c.stage=c.isCopy?'cancelled':'draft';c.parts=[];c.offset=0;c.ref=null;c.message=c.isCopy?'副本保存已取消，可返回原草稿。':'保存已取消，当前草稿继续保留。';}
async function submitContent(onProgress=()=>{},c=contentSession()){
 if(c.saving||c.result)return;checkContentVersion(c);c.saving=true;const sequence=(c.sequence||0)+1;
 try{
  await startContent(c);onProgress();
  while(c.offset<c.raw.length){if(!contentCurrent(c)||c.sequence!==sequence)return;await writeChunk({},c);onProgress();await new Promise(resolve=>requestAnimationFrame(resolve));}
  if(!contentCurrent(c)||c.sequence!==sequence)return;
  await sealContent(c);onProgress();await runtime.waitForResult();
  if(!contentCurrent(c)||c.sequence!==sequence)return;
  await saveContent(c);
 }catch(error){if(!contentCurrent(c)||c.sequence!==sequence)return;if(c.stage!=='conflict')c.stage='failed';c.error=error.message;c.message='';if(!c.isCopy)throw error;}
 finally{if(contentCurrent(c)&&c.sequence===sequence){c.saving=false;onProgress();}}
}
window.DW_AUTHORING={runtime,api:{seed,readVariables,variablesNext,readRevisions,readConnections,beginTest,finishTest,changeTest,testConnection,prepareEdit,commitEdit,finishEdit,cancelEdit,submitEdit,queryEdit,reviewEdit,resolveEdit,changeEditKind,startContent,writeChunk,sealContent,saveContent,saveContentCopy,contentSession,returnOriginalContent,openContentCopy,cancelContent,submitContent,readVariableResource,hash,canonical}};
})();
