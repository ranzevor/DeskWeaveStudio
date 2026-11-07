async page=>{
 await page.waitForFunction(()=>window.DW_AUTHORING&&window.DW_PROTOTYPE);
 const checks=[],contractOutputs=[],rejectionsOnly=await page.evaluate(()=>window.DW_INTERACTION_OPTIONS?.scope==='content-rejections');
 const render=async id=>{await page.setViewportSize({width:1440,height:900});await page.evaluate(id=>window.DW_PROTOTYPE.renderScreen(id),id);await page.waitForFunction(id=>window.DW_READY===id,id);};
 const check=async(name,fn,arg)=>{if(!await page.evaluate(fn,arg))throw Error(name);checks.push({name,result:'pass'});};
 const click=async action=>{await page.locator('[data-au-action="'+action+'"]').click();await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);};
 const collect=async(name,schema,fn)=>{const value=await page.evaluate(fn);if(value==null)throw Error('缺少输出：'+name);contractOutputs.push({name,schema,value});};
 const setup=async(copy,failure)=>{await render(copy?'content-save-conflict':'content-save');await page.evaluate(failure=>{
  const r=window.DW_AUTHORING.runtime,root=window.DW_DESIGN_STATE.authoring.content;
  const f=window.flowRecovery={runtime:{...r},repaired:false,replays:[],origin:{model:root.model,raw:root.raw,text:JSON.stringify(root.model),base:root.baseRevision,head:root.currentRevision,version:root.draftVersion}};
  r.waitForResult=async()=>{};
  r.waitForContentCommit=async()=>{
   if(f.repaired)return;
   if(failure==='storage')throw Error('disk full');
   if(failure==='conflict')window.DW_AUTHORING.api.contentSession().service.headRevision='14';
  };
  r.readContentResult=async()=>{throw Error('response lost');};
  r.replayContentSave=async(c,submission)=>{f.replays.push(JSON.parse(JSON.stringify(submission)));return f.runtime.replayContentSave(c,submission);};
 },failure);};
 const restore=async()=>page.evaluate(()=>{Object.assign(window.DW_AUTHORING.runtime,window.flowRecovery.runtime);delete window.flowRecovery;});
 const start=async copy=>{await click(copy?'content-copy':'content-submit');await page.evaluate(()=>{const c=window.DW_AUTHORING.api.contentSession();window.flowRecovery.submission={id:c.requestId,params:JSON.stringify(c.saveParams),model:c.model,raw:c.raw,ref:c.ref};});};
 const intact=async label=>check('恢复保留原请求参数与候选内容 '+label,()=>{
  const c=window.DW_AUTHORING.api.contentSession(),f=window.flowRecovery,s=f.submission;
  return c.requestId===s.id&&JSON.stringify(c.saveParams)===s.params&&c.model===s.model&&c.raw===s.raw&&c.ref===s.ref&&Object.keys(c.service.requests).length===1&&f.replays.every(p=>p.requestId===s.id&&JSON.stringify(p.params)===s.params);
 });
 const origin=async label=>check('副本恢复保留原流程及非空依赖 '+label,()=>{
  const root=window.DW_DESIGN_STATE.authoring.content,c=root.copy.candidate,old=window.flowRecovery.origin;
  return root.model===old.model&&root.raw===old.raw&&root.baseRevision===old.base&&root.currentRevision===old.head&&root.draftVersion===old.version&&JSON.stringify(root.model)===old.text&&Object.keys(c.model.dependencies).length===2&&JSON.stringify(c.model.dependencies)===JSON.stringify(root.model.dependencies);
 });
 if(!rejectionsOnly){
 for(const copy of [false,true])for(const failure of ['conflict','storage']){
  const label=(copy?'副本':'流程')+' '+failure;
  await setup(copy,failure);await start(copy);
  await check('正式拒绝但响应丢失时保持未决 '+label,()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='uncertain'&&!c.requestSettled&&c.service.committed===0&&c.service.requests[c.requestId].state==='notReceived';});
  await click('content-query');
  await check('未找到回执提供原请求恢复入口 '+label,()=>{const c=window.DW_AUTHORING.api.contentSession(),b=document.querySelector('[data-au-action="content-recover"]');return c.recoverable&&!c.requestSettled&&b&&!b.disabled&&!document.querySelector('[data-au-action="content-submit"]')&&(!c.isCopy||document.querySelector('[data-au-action="content-return"]').disabled);});
  await collect('失败后的原请求查询 '+label,'service.schema.json#/$defs/request.status/properties/result',()=>window.DW_AUTHORING.api.contentSession().lastReceipt);
  await click('content-recover');await intact(label);
  await check('一次恢复核定明确失败并开放处置 '+label,failure=>{const c=window.DW_AUTHORING.api.contentSession();return c.requestSettled&&!c.recovering&&!c.recoverable&&c.stage===(failure==='conflict'?'conflict':'failed')&&c.lastReceipt.error.code===(failure==='conflict'?'REVISION_CONFLICT':'STORAGE_UNAVAILABLE')&&c.error===c.lastReceipt.error.message&&c.service.committed===0&&c.service.requests[c.requestId].attempts===2&&window.flowRecovery.replays.length===1;},failure);
  await collect('恢复后的正式失败回执 '+label,'message.schema.json',()=>{const c=window.DW_AUTHORING.api.contentSession();return{protocolVersion:'1.0',kind:'response',replyTo:c.requestId,ok:false,error:c.lastReceipt.error};});
  await check('恢复操作按钮位于页面可视范围 '+label,()=>{const bottom=document.querySelector('.statusbar').getBoundingClientRect().top;return [...document.querySelectorAll('.au-page [data-au-action]')].every(b=>{const r=b.getBoundingClientRect();return r.bottom<=bottom&&r.right<=innerWidth&&r.left>=0;});});
  if(copy)await origin('核定失败 '+label);
  await page.evaluate(()=>window.flowRecovery.repaired=true);
  if(copy&&failure==='conflict'){
   await click('content-return');await check('核定副本冲突后返回原草稿 '+label,()=>{const c=window.DW_DESIGN_STATE.authoring.content;return !c.copy.active&&c.model===window.flowRecovery.origin.model&&c.copy.candidate.service.committed===0;});
  }else{
   await page.evaluate(()=>window.DW_AUTHORING.runtime.readContentResult=window.flowRecovery.runtime.readContentResult);
   await click(failure==='conflict'?'content-copy':'content-submit');
   await check('明确失败后按用户操作保存一次 '+label,()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='saved'&&c.service.committed===1&&c.result.revision===(c.isCopy?'1':'13');});
   if(copy)await origin('重新保存 '+label);
  }
  await restore();
 }
 for(const copy of [false,true]){
  const label=copy?'副本':'流程';await setup(copy,'storage');await start(copy);await click('content-query');
  await page.evaluate(()=>window.flowRecovery.repaired=true);await click('content-recover');await intact('存储恢复 '+label);
  await check('存储恢复后同请求只生成一个版本 '+label,()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='saved'&&c.requestSettled&&c.service.committed===1&&c.service.requests[c.requestId].attempts===2&&window.flowRecovery.replays.length===1;});
  await collect('原请求恢复保存参数 '+label,'service.schema.json#/$defs/workflow.save/properties/params',()=>window.DW_AUTHORING.api.contentSession().saveParams);
  await collect('原请求恢复保存结果 '+label,'service.schema.json#/$defs/workflow.save/properties/result',()=>window.DW_AUTHORING.api.contentSession().result);
  if(copy){await origin(label);await click('content-open-copy');await check('原请求恢复后打开已保存副本',()=>window.DW_DESIGN_STATE.authoring.content.result.revision==='1'&&window.DW_DESIGN_STATE.authoring.previousContent.model===window.flowRecovery.origin.model);}
  await restore();
 }
 await setup(false,'storage');
 await page.evaluate(()=>{window.DW_AUTHORING.runtime.waitForContentCommit=()=>new Promise(resolve=>window.flowRecovery.release=resolve);window.DW_AUTHORING.runtime.readContentStatus=async request=>({requestId:request.id,state:'notReceived',operationId:null,result:null});});
 await page.locator('[data-au-action="content-submit"]').click();await page.waitForFunction(()=>!!window.flowRecovery.release);
 await page.locator('[data-au-action="content-query"]').click();await page.waitForFunction(()=>window.DW_AUTHORING.api.contentSession().recoverable);
 await page.locator('[data-au-action="content-recover"]').click();await page.waitForFunction(()=>window.DW_AUTHORING.api.contentSession().recovering);
 await page.evaluate(()=>{window.DW_AUTHORING.api.recoverContent();window.DW_AUTHORING.api.queryContent();});
 await check('原请求在途时恢复与重复操作共用同一次提交',()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='recovering'&&!c.requestSettled&&c.service.committed===0&&c.service.requests[c.requestId].attempts===1&&window.flowRecovery.replays.length===1&&document.querySelector('[data-au-action="content-recover"]').disabled;});
 await page.evaluate(()=>window.flowRecovery.release());await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);
 await check('在途请求完成后恢复原版本且不重放写入',()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='saved'&&c.result.revision==='13'&&c.service.committed===1&&c.service.requests[c.requestId].attempts===1;});await restore();

 await setup(false,'storage');
 await page.evaluate(()=>{window.DW_AUTHORING.runtime.readContentResult=async request=>{const value=await window.flowRecovery.runtime.readContentResult(request);return new Promise(resolve=>window.flowRecovery.deliver=()=>resolve(value));};});
 await page.locator('[data-au-action="content-submit"]').click();await page.waitForFunction(()=>!!window.flowRecovery.deliver);
 await page.locator('[data-au-action="content-query"]').click();await page.waitForFunction(()=>window.DW_AUTHORING.api.contentSession().recoverable);
 await page.evaluate(()=>window.flowRecovery.repaired=true);
 await page.locator('[data-au-action="content-recover"]').click();await page.waitForFunction(()=>window.DW_AUTHORING.api.contentSession().stage==='saved');
 await page.evaluate(()=>window.flowRecovery.deliver());await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);
 await check('首次失败回执迟到不覆盖同请求恢复成功',()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='saved'&&!c.error&&c.requestSettled&&c.service.committed===1;});await restore();

 for(const failure of ['lost','timeout','params','expired']){
  await setup(false,'storage');await start(false);await click('content-query');
  await page.evaluate(failure=>{
   const r=window.DW_AUTHORING.runtime,f=window.flowRecovery;f.repaired=true;const replay=r.replayContentSave;
   r.replayContentSave=async(c,submission)=>{
    if(failure==='params')return replay(c,{...submission,params:{...submission.params,draftVersion:999}});
    if(failure==='expired')c.service.requests[c.requestId].refExpiresAt=Date.now()-1;
    const result=await replay(c,submission);
    if(failure==='lost')throw Error('recovery response lost');
    if(failure==='timeout')return{requestId:submission.requestId,ok:false,result:null,error:{code:'DEADLINE_EXCEEDED',message:'保存响应超时，请查询保存结果。',retry:'afterQuery'}};
    return result;
   };
  },failure);
  await click('content-recover');
  await check('恢复 '+failure+' 保持准确结果与原请求',failure=>{
   const c=window.DW_AUTHORING.api.contentSession(),f=window.flowRecovery;
   return c.requestId===f.submission.id&&JSON.stringify(c.saveParams)===f.submission.params&&Object.keys(c.service.requests).length===1&&f.replays.length===1&&(failure==='expired'?c.stage==='failed'&&c.requestSettled&&c.lastReceipt.error.code==='RESOURCE_NOT_FOUND'&&c.service.committed===0:c.stage==='uncertain'&&!c.requestSettled&&c.recoverable&&c.service.committed===(failure==='params'?0:1));
  },failure);
  if(failure==='lost'){
   await page.evaluate(()=>{const f=window.flowRecovery;window.DW_AUTHORING.runtime.replayContentSave=async(c,p)=>{f.replays.push(JSON.parse(JSON.stringify(p)));return f.runtime.replayContentSave(c,p);};});
   await click('content-recover');await check('恢复响应再丢失后的再次操作返回同一版本',()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='saved'&&c.service.committed===1&&c.service.requests[c.requestId].attempts===2&&window.flowRecovery.replays.length===2;});
  }else if(failure==='timeout'){
   await click('content-query');await check('恢复响应超时后查询实际已保存版本',()=>window.DW_AUTHORING.api.contentSession().result?.revision==='13');
  }else if(failure==='expired'){
   await page.evaluate(()=>window.DW_AUTHORING.runtime.readContentResult=window.flowRecovery.runtime.readContentResult);
   await click('content-submit');await check('过期引用明确失败后可重新上传保存一次',()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='saved'&&c.service.committed===1&&c.requestId!==window.flowRecovery.submission.id;});
  }
  await restore();
 }

 }
 const rejections=[
  {code:'INVALID_WORKFLOW',message:'流程存在结构或连接错误，请先校验流程。',retry:'never'},
  {code:'METHOD_UNAVAILABLE',message:'资源版本绑定服务尚未实现。',retry:'never'},
  {code:'CONTENT_REFERENCE_INVALID',message:'内容引用的用途、长度或摘要不匹配。',retry:'never'},
  {code:'CONTENT_HASH_MISMATCH',message:'内容长度或摘要不匹配，请重新上传。',retry:'never'},
  {code:'CONTENT_INCOMPLETE',message:'内容尚未完整接收。',retry:'never'}
 ];
 for(const rejection of rejections)for(const copy of [false,true])for(const lost of [false,true]){
  const label=rejection.code+' '+(copy?'副本':'流程')+' '+(lost?'响应丢失':'直接响应');
  await setup(copy,'storage');
  await page.evaluate(({rejection,lost})=>{
   const r=window.DW_AUTHORING.runtime,f=window.flowRecovery;
   r.readContentResult=async request=>{request.error=JSON.parse(JSON.stringify(rejection));if(lost)throw Error('response lost');return f.runtime.readContentResult(request);};
   r.replayContentSave=async(c,submission)=>{
    f.replays.push(JSON.parse(JSON.stringify(submission)));
    const request=c.service.requests[submission.requestId];
    if(JSON.stringify(submission.params)!==JSON.stringify(request.params))throw Error('changed request');
    request.error=JSON.parse(JSON.stringify(rejection));return f.runtime.readContentResult(request);
   };
  },{rejection,lost});
  await start(copy);
  if(lost){
   await check('业务拒绝响应丢失后保留未决请求 '+label,()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='uncertain'&&!c.requestSettled&&c.service.committed===0;});
   await click('content-query');
   await collect('业务拒绝查询回执 '+label,'service.schema.json#/$defs/request.status/properties/result',()=>window.DW_AUTHORING.api.contentSession().lastReceipt);
   await click('content-recover');
  }
  await intact('明确业务拒绝 '+label);
  await check('明确业务拒绝解除未决并保留原因 '+label,rejection=>{
   const c=window.DW_AUTHORING.api.contentSession();
   return c.stage==='failed'&&c.requestSettled&&!c.recoverable&&!c.recovering&&c.error===rejection.message&&c.lastReceipt.error.code===rejection.code&&c.service.committed===0&&!document.querySelector('[data-au-action="content-recover"]');
  },rejection);
  await collect('业务拒绝最终错误 '+label,'message.schema.json',()=>{const c=window.DW_AUTHORING.api.contentSession();return{protocolVersion:'1.0',kind:'response',replyTo:c.requestId,ok:false,error:c.lastReceipt.error};});
  if(copy){
   await origin('明确业务拒绝 '+label);
   await click('content-return');await check('业务拒绝后可返回原草稿 '+label,()=>{const c=window.DW_DESIGN_STATE.authoring.content;return !c.copy.active&&c.model===window.flowRecovery.origin.model&&c.raw===window.flowRecovery.origin.raw&&c.stage==='conflict';});
  }else{
   await page.evaluate(()=>{const f=window.flowRecovery;f.repaired=true;window.DW_AUTHORING.runtime.readContentResult=f.runtime.readContentResult;});
   await click('content-submit');
   await check('业务条件修正后保存当前草稿 '+label,()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='saved'&&c.result.revision==='13'&&c.requestId!==window.flowRecovery.submission.id&&c.service.committed===1;});
   await collect('业务条件修正后的保存参数 '+label,'service.schema.json#/$defs/workflow.save/properties/params',()=>window.DW_AUTHORING.api.contentSession().saveParams);
   await collect('业务条件修正后的保存结果 '+label,'service.schema.json#/$defs/workflow.save/properties/result',()=>window.DW_AUTHORING.api.contentSession().result);
  }
  await restore();
 }
 for(const code of ['DEADLINE_EXCEEDED','LIMIT_EXCEEDED','INTERNAL_ERROR','REQUEST_CONFLICT','ACCESS_DENIED'])for(const copy of [false,true]){
  const label=code+' '+(copy?'副本':'流程');await setup(copy,'storage');
  await page.evaluate(code=>{const r=window.DW_AUTHORING.runtime;window.flowRecovery.repaired=true;r.readContentResult=async request=>({requestId:request.id,ok:false,result:null,error:{code,message:'保存结果需要查询。',retry:'afterQuery'}});},code);
  await start(copy);
  await check('未核定错误继续保护原保存结果 '+label,()=>{
   const c=window.DW_AUTHORING.api.contentSession();return c.stage==='uncertain'&&!c.requestSettled&&c.service.committed===1&&!document.querySelector('[data-au-action="content-submit"]')&&(!c.isCopy||document.querySelector('[data-au-action="content-return"]').disabled);
  });
  await click('content-query');await check('未核定错误按原回执恢复实际版本 '+label,()=>{const c=window.DW_AUTHORING.api.contentSession();return c.stage==='saved'&&c.requestSettled&&c.requestId===window.flowRecovery.submission.id&&c.service.committed===1&&Object.keys(c.service.requests).length===1;});
  await restore();
 }

 return{checks,contractOutputs};
}
