async page=>{
 const checks=[],contractOutputs=[];
 const render=async id=>{const size=await page.evaluate(id=>window.DW_CATALOG.screens.find(s=>s.id===id).viewport,id);await page.setViewportSize(size);await page.evaluate(id=>window.DW_PROTOTYPE.renderScreen(id),id);await page.waitForFunction(id=>window.DW_READY===id,id);};
 const click=async action=>{await page.locator('[data-au-action="'+action+'"]').first().click();await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);};
 const field=name=>page.locator('[data-au-field="'+name+'"]');
 const check=async(name,fn,arg)=>{if(!await page.evaluate(fn,arg))throw Error(name);checks.push({name,result:'pass'});};
 const collect=async(name,schema,fn)=>{const value=await page.evaluate(fn);if(value==null)throw Error('缺少输出：'+name);contractOutputs.push({name,schema,value});};
 await render('variables-inspect');
 await check('变量未读取不显示已核定值',()=>window.DW_DESIGN_STATE.authoring.variables.page===null&&document.querySelector('.au-page').textContent.includes('尚未读取'));
 await click('var-read');
 await check('变量读取固定运行调用及范围',()=>{const v=window.DW_DESIGN_STATE.authoring.variables;return v.page.runId==='run-monthly-report'&&v.page.frameId==='frame-main'&&v.page.scope==='local'&&v.page.items.length===3;});
 await check('变量页按UTF16名称顺序稳定排列',()=>{const p=window.DW_DESIGN_STATE.authoring.variables.page;return p.items.map(x=>x.name).join(',')==='accessToken,downloadCount,month';});
 await collect('当前调用变量首页','inspection.schema.json#/$defs/variablesPage',()=>window.DW_DESIGN_STATE.authoring.variables.page);
 await page.evaluate(()=>{const v=window.DW_DESIGN_STATE.authoring.variables;v.dataVersion++;v.changed=true;v.runState='running';});await click('var-next');
 await check('运行中翻页保持原数据版本和控制版本',()=>{const v=window.DW_DESIGN_STATE.authoring.variables;return v.page.dataVersion===8&&v.dataVersion===9&&v.page.controlVersion===4&&v.page.items.some(x=>x.name==='rows'&&x.valueResource);});
 await click('var-resource');await check('大变量资源可读取并校验实际字节',()=>window.DW_DESIGN_STATE.authoring.variables.resourceValue.length===200);
 await collect('敏感派生与大值资源页','inspection.schema.json#/$defs/variablesPage',()=>window.DW_DESIGN_STATE.authoring.variables.page);
 await check('遮蔽变量不携值或资源',()=>window.DW_DESIGN_STATE.authoring.variables.page.items.filter(x=>x.visibility==='redacted').every(x=>!Object.hasOwn(x,'value')&&!Object.hasOwn(x,'valueResource')));
 await click('var-read');await check('主动刷新采用新数据版本',()=>window.DW_DESIGN_STATE.authoring.variables.page.dataVersion===9&&!window.DW_DESIGN_STATE.authoring.variables.changed);
 await page.locator('[data-au-action="var-scope"][data-au-scope="shared"]').click();
 await check('共享变量查询不冒用调用身份',()=>{const p=window.DW_DESIGN_STATE.authoring.variables.page;return p.scope==='shared'&&p.frameId===null;});
 await check('切换变量范围清除原资源展开内容',()=>window.DW_DESIGN_STATE.authoring.variables.resourceValue===null);
 await collect('运行共享变量页','inspection.schema.json#/$defs/variablesPage',()=>window.DW_DESIGN_STATE.authoring.variables.page);
 await page.locator('[data-au-action="var-scope"][data-au-scope="inputs"]').click();
 await check('敏感输入与数据派生均遮蔽',()=>window.DW_DESIGN_STATE.authoring.variables.page.items.filter(x=>x.name!=='month').every(x=>x.visibility==='redacted'));
 await check('敏感条件控制的分支值保持遮蔽',()=>window.DW_DESIGN_STATE.authoring.variables.page.items.find(x=>x.name==='selectedBranch')?.visibility==='redacted');
 await click('var-next');await check('敏感控制依赖派生值同样遮蔽',()=>window.DW_DESIGN_STATE.authoring.variables.page.items[0].name==='summary'&&window.DW_DESIGN_STATE.authoring.variables.page.items[0].visibility==='redacted');
 await field('var-frame').selectOption({label:'校验下载结果'});await check('切换调用重新创建固定变量页',()=>window.DW_DESIGN_STATE.authoring.variables.page.frameId==='frame-child');
 await check('跨范围旧快照拒绝',()=>{const v=window.DW_DESIGN_STATE.authoring.variables;v.scope='outputs';try{window.DW_AUTHORING.api.readVariables(false);return false;}catch{return true;}});
 await render('variables-expired');await check('过期变量页禁止翻页',()=>document.querySelector('[data-au-action="var-next"]').disabled);await click('var-read');await check('过期重读保留调用与范围',()=>{const v=window.DW_DESIGN_STATE.authoring.variables;return !v.error&&v.page.scope==='local'&&v.page.frameId==='frame-main'&&Date.parse(v.page.expiresAt)>Date.now();});
 await render('revision-browser');await click('revision-open');await check('历史版本查看保留选择且不改草稿',()=>{const r=window.DW_DESIGN_STATE.authoring.revisions;return r.opened==='11'&&r.selected==='11';});
 await collect('历史版本发现页','inspection.schema.json#/$defs/revisionPage',()=>window.DW_DESIGN_STATE.authoring.revisions.page);
 await click('revision-next');await check('历史分页不重复首批成员',()=>window.DW_DESIGN_STATE.authoring.revisions.page.items.map(x=>x.revision).join(',')==='9,8,7');
 await render('revision-query-expired');await check('历史到期禁止使用旧列表打开',()=>document.querySelector('[data-au-action="revision-open"]').disabled);await click('revision-read');await check('历史重新查询保留所选版本',()=>window.DW_DESIGN_STATE.authoring.revisions.selected==='11');
 await render('connection-browser');await collect('本地连接发现页','inspection.schema.json#/$defs/connectionPage',()=>window.DW_DESIGN_STATE.authoring.connections.page);await check('连接清单不返回凭据内容',()=>!JSON.stringify(window.DW_DESIGN_STATE.authoring.connections.page).includes('apiKey')&&window.DW_DESIGN_STATE.authoring.connections.page.items[1].credentialPresent);
 await click('connection-next');await check('连接分页读取后续成员',()=>window.DW_DESIGN_STATE.authoring.connections.page.items.length===1&&window.DW_DESIGN_STATE.authoring.connections.page.nextCursor===null);await click('connection-open');await check('选择连接打开对应端点与版本',()=>window.DW_DESIGN_STATE.authoring.test.draft.endpoint==='http://127.0.0.1:11437/v1'&&window.DW_DESIGN_STATE.authoring.test.savedVersion===5);
 await render('connection-draft-test');await field('test-model').fill('vision-next');await field('test-model').press('Tab');await click('test-start');await check('一次测试显示当前配置结果并保留已保存连接',()=>{const t=window.DW_DESIGN_STATE.authoring.test;return t.status==='passed'&&t.result.modelAvailable&&t.result.editVersion===t.draft.editVersion&&t.saved.model==='vision-local'&&t.savedVersion===2;});
 await collect('未保存连接测试结果','authoring.schema.json#/$defs/draftTestResult',()=>window.DW_DESIGN_STATE.authoring.test.result);
 await page.evaluate(async()=>{const api=window.DW_AUTHORING.api,p=await api.beginTest();api.changeTest('model','vision-edited');api.finishTest(null,p);});await check('编辑期间的迟到测试不覆盖新草稿',()=>{const t=window.DW_DESIGN_STATE.authoring.test;return t.result===null&&t.status==='stale'&&t.draft.model==='vision-edited';});
 await page.evaluate(async()=>{await window.DW_AUTHORING.api.beginTest();window.DW_PROTOTYPE.go('connection-draft-test');});await click('test-cancel');await check('取消测试保留编辑及已保存连接',()=>{const t=window.DW_DESIGN_STATE.authoring.test;return t.status==='cancelled'&&t.saved.model==='vision-local'&&t.result===null;});
 await page.evaluate(async()=>{await window.DW_AUTHORING.api.beginTest();window.DW_AUTHORING.api.finishTest('networkFailed');window.DW_PROTOTYPE.go('connection-draft-test');});await check('网络失败与模型不可用分别投影',()=>{const r=window.DW_DESIGN_STATE.authoring.test.result;return r.configurationValid&&!r.reachable&&!r.modelAvailable&&r.reason==='networkFailed';});
 await collect('未保存连接失败结果','authoring.schema.json#/$defs/draftTestResult',()=>window.DW_DESIGN_STATE.authoring.test.result);
 await click('test-save');await check('明确保存才改变连接版本',()=>window.DW_DESIGN_STATE.authoring.test.saved.model==='vision-edited'&&window.DW_DESIGN_STATE.authoring.test.savedVersion===3);
 await render('asset-edit-save');await page.evaluate(()=>window.DW_AUTHORING.api.prepareEdit());await check('准备资产不生成版本',()=>window.DW_DESIGN_STATE.authoring.edit.committed===0);await page.evaluate(()=>window.DW_AUTHORING.api.commitEdit());await check('提交接收后仍等待保存结果',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.pending&&e.result===null&&e.committed===0;});await page.evaluate(()=>window.DW_AUTHORING.api.cancelEdit());await click('edit-submit');await check('一次保存自动追加图像版本',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.result.assetVersion==='4'&&e.result.previousVersion==='3'&&e.committed===1;});await collect('图像保存计划','authoring.schema.json#/$defs/editPlan',()=>window.DW_DESIGN_STATE.authoring.edit.plan);await collect('图像正式版本结果','authoring.schema.json#/$defs/editResult',()=>window.DW_DESIGN_STATE.authoring.edit.result);
 await page.evaluate(()=>window.DW_AUTHORING.api.finishEdit());await check('重复查询保存结果不重复追加版本',()=>window.DW_DESIGN_STATE.authoring.edit.committed===1);
 await page.evaluate(()=>window.DW_AUTHORING.api.cancelEdit());await check('提交完成后取消仍返回实际成功结果',()=>window.DW_DESIGN_STATE.authoring.edit.result.assetVersion==='4');
 await render('asset-edit-conflict');await check('版本冲突保留图像编辑',async()=>{const e=window.DW_DESIGN_STATE.authoring.edit;try{await window.DW_AUTHORING.api.commitEdit();return false;}catch(error){return error.message.includes('版本')&&e.roi.width===32&&e.result===null&&e.committed===0;}});await click('edit-submit');await check('重新保存使用核对后的资产版本',()=>window.DW_DESIGN_STATE.authoring.edit.result.previousVersion==='4');
 await render('asset-edit-save');await page.evaluate(async()=>{const api=window.DW_AUTHORING.api;api.prepareEdit();await api.commitEdit();window.DW_DESIGN_STATE.authoring.edit.saving=true;window.DW_PROTOTYPE.go('asset-edit-save');});await check('保存中固定编辑目标',()=>document.querySelector('[data-au-field="edit-destination"]').disabled&&document.querySelector('[data-au-kind="trajectory"]').disabled);await click('edit-cancel');await check('取消保存不新增版本',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.plan===null&&!e.pending&&!e.saving&&e.committed===0&&e.roi.width===32;});
 await check('取消进行中的保存后迟到结果不生成版本',async()=>{const api=window.DW_AUTHORING.api,e=window.DW_DESIGN_STATE.authoring.edit;await api.submitEdit(()=>{if(e.pending)api.cancelEdit();});return !e.saving&&!e.pending&&e.result===null&&e.committed===0;});
 await page.locator('[data-au-kind="trajectory"]').click();await field('edit-destination').selectOption({label:'创建独立资产'});await field('edit-name').fill('   ');await field('edit-name').press('Tab');await click('edit-submit');await check('独立资产空名称不能保存',()=>window.DW_DESIGN_STATE.authoring.edit.plan===null&&window.DW_DESIGN_STATE.authoring.edit.error.includes('名称'));await field('edit-name').fill('新的归档操作');await field('edit-name').press('Tab');await click('edit-submit');await collect('轨迹独立资产计划','authoring.schema.json#/$defs/editPlan',()=>window.DW_DESIGN_STATE.authoring.edit.plan);await check('轨迹另存创建独立对象并保留来源',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.result.kind==='trajectory'&&e.result.previousVersion===null&&!e.result.createdNewVersion&&e.result.name==='新的归档操作';});await collect('轨迹独立资产结果','authoring.schema.json#/$defs/editResult',()=>window.DW_DESIGN_STATE.authoring.edit.result);
 await render('content-save');await page.evaluate(()=>window.DW_AUTHORING.api.startContent());await check('分块上传开始不标记已保存',()=>{const c=window.DW_DESIGN_STATE.authoring.content;return c.raw.length>1048576&&c.stage==='uploading'&&c.result===null;});
 await check('缺块禁止提前封存',async()=>{try{await window.DW_AUTHORING.api.sealContent();return false;}catch{return true;}});
 await check('乱序块拒绝且不改变偏移',async()=>{const api=window.DW_AUTHORING.api,c=window.DW_DESIGN_STATE.authoring.content;try{await api.writeChunk({offset:196608});return false;}catch{return c.offset===0;}});
 await page.evaluate(()=>window.DW_AUTHORING.api.writeChunk());await collect('内容块确认回执','content.schema.json#/$defs/writeResult',()=>({uploadId:'workflow-upload',nextOffset:window.DW_DESIGN_STATE.authoring.content.offset,replayed:false}));
 await check('确认块相同重放保持偏移',async()=>{const c=window.DW_DESIGN_STATE.authoring.content,offset=c.offset,r=await window.DW_AUTHORING.api.writeChunk({offset:0});return r.replayed&&c.offset===offset;});
 await check('确认块不同内容重放拒绝',async()=>{try{await window.DW_AUTHORING.api.writeChunk({offset:0,chunk:new Uint8Array([1,2,3])});return false;}catch{return true;}});
 await page.evaluate(async()=>{const api=window.DW_AUTHORING.api,c=window.DW_DESIGN_STATE.authoring.content;while(c.offset<c.raw.length)await api.writeChunk();await api.sealContent();});await check('全部内容核验通过才生成引用',()=>{const c=window.DW_DESIGN_STATE.authoring.content;return c.offset===c.raw.length&&c.ref.length===c.raw.length&&c.ref.sha256===c.digest&&c.stage==='sealed'&&c.result===null;});await collect('完整大流程内容引用','content.schema.json#/$defs/contentRef',()=>window.DW_DESIGN_STATE.authoring.content.ref);
 await page.evaluate(()=>window.DW_DESIGN_STATE.authoring.content.currentRevision='14');await check('保存版本冲突保留本地草稿和完整内容',async()=>{const c=window.DW_DESIGN_STATE.authoring.content;try{await window.DW_AUTHORING.api.saveContent();return false;}catch(error){return c.result===null&&c.stage==='sealed'&&error.message.includes('版本')&&c.model.definition.variables.largeSample.length===2048;}});
 await page.evaluate(()=>window.DW_DESIGN_STATE.authoring.content.currentRevision='12');await click('content-submit');await check('一次保存自动完成分块传输和版本保存',()=>{const c=window.DW_DESIGN_STATE.authoring.content;return c.stage==='saved'&&!c.saving&&c.result.revision==='13'&&c.saveParams.modelRef&&!c.saveParams.definition;});await collect('大流程保存请求参数','service.schema.json#/$defs/workflow.save/properties/params',()=>window.DW_DESIGN_STATE.authoring.content.saveParams);await collect('大流程保存实际结果','service.schema.json#/$defs/workflow.save/properties/result',()=>window.DW_DESIGN_STATE.authoring.content.result);
 await render('content-transfer-failed');await page.evaluate(async()=>{const api=window.DW_AUTHORING.api;await api.startContent();await api.writeChunk();window.DW_PROTOTYPE.go('content-transfer-failed');});await click('content-cancel');await check('取消传输保留草稿且没有保存结果',()=>{const c=window.DW_DESIGN_STATE.authoring.content;return c.stage==='draft'&&c.result===null&&c.raw.length>1048576&&c.offset===0;});
 await check('取消自动保存后迟到结果不改变草稿',async()=>{const api=window.DW_AUTHORING.api,c=window.DW_DESIGN_STATE.authoring.content;await api.submitContent(()=>{if(c.offset>0)api.cancelContent();});return c.stage==='draft'&&!c.saving&&c.result===null&&c.offset===0;});
 await page.evaluate(async()=>{await window.DW_AUTHORING.api.startContent();window.DW_DESIGN_STATE.authoring.content.expiresAt=Date.now()-1;});await check('上传到期禁止继续写入',async()=>{const c=window.DW_DESIGN_STATE.authoring.content;try{await window.DW_AUTHORING.api.writeChunk();return false;}catch(error){return error.message.includes('到期')&&c.offset===0;}});
 for(const [screen,action,method,key] of [['content-save','content-submit','submitContent','content'],['asset-edit-save','edit-submit','submitEdit','edit'],['connection-draft-test','test-start','testConnection','test']]){
  for(const destination of ['editor-graph','assets','variables-inspect','return','reset']){
   await render(screen);
   await page.evaluate(({screen,method,destination})=>{
    const api=window.DW_AUTHORING.api,original=api[method];
    window.auNavigation={left:false,stable:true,app:null};
    api[method]=async update=>{try{return await original(()=>{
     update();const n=window.auNavigation;
     if(!n.left){
      if(destination==='reset')window.DW_PROTOTYPE.renderScreen(screen);
      else{window.DW_PROTOTYPE.go(destination==='return'?'editor-graph':destination);if(destination==='return')window.DW_PROTOTYPE.go(screen);}
      n.left=true;n.app=document.querySelector('.app-window');
     }else n.stable=n.stable&&(destination==='return'||n.app===document.querySelector('.app-window'));
    });}finally{api[method]=original;}};
   },{screen,method,destination});
   await click(action);
   await check(screen+' 进行中导航 '+destination+' 后回调保持当前页面',({screen,destination})=>{
    const n=window.auNavigation,expected=['return','reset'].includes(destination)?screen:destination;
    return n.left&&n.stable&&(destination==='return'||n.app===document.querySelector('.app-window'))&&window.DW_PROTOTYPE.getRoute().id===expected;
   },{screen,destination});
   if(destination==='return')await check(screen+' 完成前返回原页后继续显示完成结果',key=>{
    const state=window.DW_DESIGN_STATE.authoring[key];return !!state.result&&!state.error&&document.querySelector('.au-page').textContent.includes(key==='test'?'连接成功':'已保存');
   },key);
   if(destination!=='reset'){
    await page.evaluate(screen=>window.DW_PROTOTYPE.go(screen),screen);
    await check(screen+' 从 '+destination+' 返回后显示原操作结果',key=>{const state=window.DW_DESIGN_STATE.authoring[key];return !!state.result&&!state.error&&document.querySelector('.au-page').textContent.includes(key==='test'?'连接成功':'已保存');},key);
   }
   await page.evaluate(()=>delete window.auNavigation);
  }
 }
 for(const outcome of ['integrity','conflict','success']){
  await render('content-save');
  await page.evaluate(outcome=>{
   const api=window.DW_AUTHORING.api,original=api.submitContent;
   window.auSaveProgress={transferred:false,sealed:false,accurate:true};
   api.submitContent=async update=>{try{return await original(()=>{
    update();const c=window.DW_DESIGN_STATE.authoring.content,p=window.auSaveProgress;
    if(c.offset!==c.raw.length||c.stage==='saved')return;
    const metrics=[...document.querySelectorAll('.au-metric')],transfer=metrics.find(e=>e.firstElementChild.textContent==='传输进度'),status=metrics.find(e=>e.firstElementChild.textContent==='保存状态');
    p.accurate=p.accurate&&transfer?.querySelector('strong').textContent==='100%'&&status?.querySelector('strong').textContent!=='已保存'&&!document.querySelector('.au-save-stages>div:last-child').classList.contains('complete')&&!document.querySelector('.au-page').textContent.includes('正在保存 100%');
    if(c.stage==='uploading'){p.transferred=true;if(outcome==='integrity')c.digest='0'.repeat(64);if(outcome==='conflict')c.currentRevision='14';}
    if(c.stage==='sealed')p.sealed=true;
   });}finally{api.submitContent=original;}};
  },outcome);
  await click('content-submit');
  await check('传输完成后的 '+outcome+' 显示真实保存结果',outcome=>{
   const c=window.DW_DESIGN_STATE.authoring.content,p=window.auSaveProgress,text=document.querySelector('.au-page').textContent;
   if(!p.transferred||!p.accurate||c.saving)return false;
   if(outcome==='success')return p.sealed&&c.stage==='saved'&&c.result.revision==='13'&&text.includes('已保存')&&document.querySelector('.au-save-stages>div:last-child').classList.contains('complete');
   return c.stage==='failed'&&c.result===null&&text.includes('保存失败')&&text.includes(outcome==='integrity'?'校验失败':'版本已变化')&&c.model.definition.variables.largeSample.length===2048;
  },outcome);
  await page.evaluate(()=>delete window.auSaveProgress);
 }
 await render('content-save');
 await page.evaluate(()=>{
  const api=window.DW_AUTHORING.api,original=api.submitContent;
  api.submitContent=async update=>{
   api.submitContent=original;
   const c=window.DW_DESIGN_STATE.authoring.content;c.sequence=(c.sequence||0)+1;c.saving=true;
   await new Promise(resolve=>{window.auLateSave=resolve;});
   update();throw Error('先前保存失败');
  };
 });
 await page.locator('[data-au-action="content-submit"]').click();
 await page.evaluate(()=>window.DW_PROTOTYPE.go('content-save'));
 await page.locator('[data-au-action="content-cancel"]').click();
 await page.locator('[data-au-action="content-submit"]').click();
 await page.waitForFunction(()=>window.DW_DESIGN_STATE.authoring.content.stage==='saved'&&window.DW_AUTHORING_BUSY===1);
 await page.evaluate(()=>{window.auCurrentSave=document.querySelector('.app-window');window.auLateSave();});
 await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);
 await check('取消后重存不被旧操作的失败和最终刷新覆盖',()=>{const c=window.DW_DESIGN_STATE.authoring.content;return c.stage==='saved'&&!c.error&&c.result.revision==='13'&&window.auCurrentSave===document.querySelector('.app-window');});
 await page.evaluate(()=>{delete window.auCurrentSave;delete window.auLateSave;});
 return {checks,contractOutputs};
}
