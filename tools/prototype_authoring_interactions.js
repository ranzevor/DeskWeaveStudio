async page=>{
 const checks=[],contractOutputs=[];
 const render=async id=>{const size=await page.evaluate(id=>window.DW_CATALOG.screens.find(s=>s.id===id).viewport,id);await page.setViewportSize(size);await page.evaluate(id=>window.DW_PROTOTYPE.renderScreen(id),id);await page.waitForFunction(id=>window.DW_READY===id,id);};
 const click=async action=>{await page.locator('[data-au-action="'+action+'"]').first().click();await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);};
 const field=name=>page.locator('[data-au-field="'+name+'"]');
 const holdResults=async()=>page.evaluate(()=>{
  window.auDeferred=[];window.auWaitOriginal=window.DW_AUTHORING.runtime.waitForResult;
  window.DW_AUTHORING.runtime.waitForResult=()=>new Promise(resolve=>window.auDeferred.push(resolve));
 });
 const releaseResult=async index=>page.evaluate(index=>{const resolve=window.auDeferred[index];window.auDeferred[index]=null;resolve();},index);
 const restoreResults=async()=>page.evaluate(()=>{window.DW_AUTHORING.runtime.waitForResult=window.auWaitOriginal;delete window.auDeferred;delete window.auWaitOriginal;});
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
 await render('asset-edit-save');await click('edit-submit');
 await check('资产保存按原作业完成一次',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.result.assetVersion==='4'&&e.result.previousVersion==='3'&&e.service.committed===1&&Object.keys(e.service.operations).length===1;});
 await collect('图像保存计划','authoring.schema.json#/$defs/editPlan',()=>window.DW_DESIGN_STATE.authoring.edit.plan);
 await collect('图像正式版本结果','authoring.schema.json#/$defs/editResult',()=>window.DW_DESIGN_STATE.authoring.edit.result);
 await check('重复准入与查询返回原作业',async()=>{const e=window.DW_DESIGN_STATE.authoring.edit,id=e.operationId,accepted=await window.DW_AUTHORING.api.commitEdit();await window.DW_AUTHORING.api.queryEdit();return accepted.operationId===id&&e.operationId===id&&e.service.committed===1;});
 await render('asset-edit-save');await holdResults();
 await page.locator('[data-au-action="edit-submit"]').click();await page.waitForFunction(()=>window.auDeferred.length===1);
 await page.evaluate(()=>{const e=window.DW_DESIGN_STATE.authoring.edit;window.auEditIdentity={operation:e.operationId,edit:e.plan.editId};});
 await check('保存中固定编辑目标',()=>document.querySelector('[data-au-field="edit-destination"]').disabled&&document.querySelector('[data-au-kind="trajectory"]').disabled);
 await page.locator('[data-au-action="edit-cancel"]').click();
 await check('准入后取消先等待原作业终态',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.status==='cancelling'&&e.pending&&e.plan.editId===window.auEditIdentity.edit&&e.operationId===window.auEditIdentity.operation&&e.service.committed===0;});
 await releaseResult(0);await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);await restoreResults();
 await check('取消核定后保留原作业且不新增版本',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.status==='cancelled'&&!e.pending&&e.plan.editId===window.auEditIdentity.edit&&e.operationId===window.auEditIdentity.operation&&e.service.committed===0&&e.roi.width===32;});
 await click('edit-submit');await check('原作业已取消后允许新计划保存一次',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.result.assetVersion==='4'&&e.service.committed===1&&e.operationId!==window.auEditIdentity.operation&&e.service.operations[window.auEditIdentity.operation].state==='cancelled';});
 await page.evaluate(()=>delete window.auEditIdentity);
 await render('asset-edit-save');await holdResults();
 await page.evaluate(()=>{const runtime=window.DW_AUTHORING.runtime;window.auReadOriginal=runtime.readEditResult;let first=true;runtime.readEditResult=async op=>{if(first){first=false;return new Promise(resolve=>{window.auLateEdit=()=>resolve({operationId:op.id,version:1,state:'running',result:null,error:null});});}return window.auReadOriginal(op);};});
 await page.locator('[data-au-action="edit-submit"]').click();await page.waitForFunction(()=>window.auDeferred.length===1);await releaseResult(0);await page.waitForFunction(()=>!!window.auLateEdit);
 await page.locator('[data-au-action="edit-cancel"]').click();await page.waitForFunction(()=>!!window.DW_DESIGN_STATE.authoring.edit.result);
 await page.evaluate(()=>window.auLateEdit());await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);
 await check('提交已成功时取消与迟到回执保持实际保存结果',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.status==='succeeded'&&e.result.assetVersion==='4'&&e.service.committed===1&&Object.keys(e.service.operations).length===1&&document.querySelector('.au-page').textContent.includes('版本 4 已保存');});
 await restoreResults();await page.evaluate(()=>{window.DW_AUTHORING.runtime.readEditResult=window.auReadOriginal;delete window.auReadOriginal;delete window.auLateEdit;});
 await render('asset-edit-save');
 await page.evaluate(()=>{const runtime=window.DW_AUTHORING.runtime;window.auReadOriginal=runtime.readEditResult;let reads=0;runtime.readEditResult=async op=>{if(++reads===1)throw Error('结果响应中断');if(reads===4){op.state='succeeded';op.version++;}const value=await window.auReadOriginal(op);if(reads===2)value.operationId='another-save';if(reads===3){op.state='uncertain';op.version++;return{...value,version:op.version,state:'uncertain',result:null};}return value;};});
 await click('edit-submit');
 await page.evaluate(()=>{const e=window.DW_DESIGN_STATE.authoring.edit;window.auEditIdentity={operation:e.operationId,edit:e.plan.editId};});
 await check('保存结果丢失后显示原操作查询入口',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.status==='uncertain'&&!e.result&&e.service.committed===1&&!document.querySelector('[data-au-action="edit-submit"]')&&!!document.querySelector('[data-au-action="edit-query"]');});
 await click('edit-query');await check('异作业回执不替换原查询或新建保存计划',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.status==='uncertain'&&e.operationId===window.auEditIdentity.operation&&e.plan.editId===window.auEditIdentity.edit&&Object.keys(e.service.operations).length===1;});
 await click('edit-query');await check('作业结果未决时保留查询入口且不显示保存完成',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.status==='uncertain'&&!e.result&&e.operationId===window.auEditIdentity.operation&&document.querySelector('.au-page').textContent.includes('保存结果待查询')&&!!document.querySelector('[data-au-action="edit-query"]');});
 await click('edit-query');await check('查询原请求恢复实际保存版本',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.status==='succeeded'&&e.result.assetVersion==='4'&&e.operationId===window.auEditIdentity.operation&&e.service.committed===1;});
 await page.evaluate(()=>{window.DW_AUTHORING.runtime.readEditResult=window.auReadOriginal;delete window.auReadOriginal;delete window.auEditIdentity;});
 for(const mode of ['append','copy']){
  await render('asset-edit-conflict');await check('资产冲突未核对内容前禁止直接提交 '+mode,()=>!document.querySelector('[data-au-action="edit-submit"]')&&!document.querySelector('[data-au-action="edit-append"]'));
  await click('edit-review');await check('查看当前版本展示实际内容与来源 '+mode,()=>{const e=window.DW_DESIGN_STATE.authoring.edit,images=[...document.querySelectorAll('.au-version-image img')];return e.review.version==='4'&&e.source.version==='3'&&e.review.width===32&&e.source.width===64&&images.length===2&&images[0].getAttribute('src')!==images[1].getAttribute('src');});
  if(mode==='copy'){await field('edit-name').fill('保留当前外观');await field('edit-name').press('Tab');}
  await click('edit-'+mode);await check('明确选择后提交资产 '+mode,mode=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.status==='succeeded'&&e.service.committed===1&&e.result.sourceSha256===window.DW_CATALOG.authoringFixtures['asset-image-plan'].sourceSha256&&(mode==='append'?e.result.previousVersion==='4'&&e.result.assetVersion==='5':e.result.previousVersion===null&&e.result.assetVersion==='1'&&e.service.head==='4');},mode);
 }
 await render('asset-edit-conflict');await click('edit-review');await page.evaluate(()=>{const e=window.DW_DESIGN_STATE.authoring.edit;e.service.versions['5']={...e.service.versions['4'],version:'5'};e.service.head='5';});await click('edit-append');
 await check('查看后再次发生外部更新时要求重新核对',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.status==='conflict'&&e.review===null&&e.service.committed===0&&Object.keys(e.service.operations).length===0;});
 await click('edit-review');await click('edit-append');await check('重新查看当前版本后追加正确版本',()=>window.DW_DESIGN_STATE.authoring.edit.result.previousVersion==='5');
 await render('asset-edit-save');await page.locator('[data-au-kind="trajectory"]').click();await page.evaluate(()=>window.DW_DESIGN_STATE.authoring.edit.service.head='5');await click('edit-submit');await click('edit-review');
 await check('轨迹版本对比包含全部事件及实际输入值',()=>{const e=window.DW_DESIGN_STATE.authoring.edit,rows=document.querySelectorAll('.au-version-events tbody tr');return e.review.version==='5'&&rows.length===e.source.trajectory.events.length+e.review.trajectory.events.length&&document.querySelector('.au-version-compare').textContent.includes('Ctrl 左');});
 await click('edit-append');await check('轨迹冲突经内容核对后追加到所选版本',()=>window.DW_DESIGN_STATE.authoring.edit.result.previousVersion==='5');
 await render('asset-edit-save');await page.locator('[data-au-kind="trajectory"]').click();await field('edit-destination').selectOption({label:'创建独立资产'});await field('edit-name').fill('   ');await field('edit-name').press('Tab');await click('edit-submit');
 await check('独立资产空名称不能保存',()=>window.DW_DESIGN_STATE.authoring.edit.plan===null&&window.DW_DESIGN_STATE.authoring.edit.error.includes('名称'));
 await field('edit-name').fill('新的归档操作');await field('edit-name').press('Tab');await click('edit-submit');
 await collect('轨迹独立资产计划','authoring.schema.json#/$defs/editPlan',()=>window.DW_DESIGN_STATE.authoring.edit.plan);
 await check('轨迹另存创建独立对象并保留来源',()=>{const e=window.DW_DESIGN_STATE.authoring.edit;return e.result.kind==='trajectory'&&e.result.previousVersion===null&&!e.result.createdNewVersion&&e.result.name==='新的归档操作';});
 await collect('轨迹独立资产结果','authoring.schema.json#/$defs/editResult',()=>window.DW_DESIGN_STATE.authoring.edit.result);
 await render('content-save');await page.evaluate(()=>window.DW_AUTHORING.api.startContent());await check('分块上传开始不标记已保存',()=>{const c=window.DW_DESIGN_STATE.authoring.content;return c.raw.length>1048576&&c.stage==='uploading'&&c.result===null;});
 await check('缺块禁止提前封存',async()=>{try{await window.DW_AUTHORING.api.sealContent();return false;}catch{return true;}});
 await check('乱序块拒绝且不改变偏移',async()=>{const api=window.DW_AUTHORING.api,c=window.DW_DESIGN_STATE.authoring.content;try{await api.writeChunk({offset:196608});return false;}catch{return c.offset===0;}});
 await page.evaluate(()=>window.DW_AUTHORING.api.writeChunk());await collect('内容块确认回执','content.schema.json#/$defs/writeResult',()=>({uploadId:'workflow-upload',nextOffset:window.DW_DESIGN_STATE.authoring.content.offset,replayed:false}));
 await check('确认块相同重放保持偏移',async()=>{const c=window.DW_DESIGN_STATE.authoring.content,offset=c.offset,r=await window.DW_AUTHORING.api.writeChunk({offset:0});return r.replayed&&c.offset===offset;});
 await check('确认块不同内容重放拒绝',async()=>{try{await window.DW_AUTHORING.api.writeChunk({offset:0,chunk:new Uint8Array([1,2,3])});return false;}catch{return true;}});
 await page.evaluate(async()=>{const api=window.DW_AUTHORING.api,c=window.DW_DESIGN_STATE.authoring.content;while(c.offset<c.raw.length)await api.writeChunk();await api.sealContent();});await check('全部内容核验通过才生成引用',()=>{const c=window.DW_DESIGN_STATE.authoring.content;return c.offset===c.raw.length&&c.ref.length===c.raw.length&&c.ref.sha256===c.digest&&c.stage==='sealed'&&c.result===null;});await collect('完整大流程内容引用','content.schema.json#/$defs/contentRef',()=>window.DW_DESIGN_STATE.authoring.content.ref);
 await page.evaluate(()=>{const c=window.DW_DESIGN_STATE.authoring.content;c.currentRevision='14';window.auConflictOrigin={model:c.model,raw:c.raw,revision:c.currentRevision};});
 await check('保存版本冲突保留本地草稿和完整内容',async()=>{const c=window.DW_DESIGN_STATE.authoring.content;try{await window.DW_AUTHORING.api.saveContent();return false;}catch(error){return c.result===null&&c.stage==='conflict'&&c.ref.length===c.raw.length&&error.message.includes('版本')&&c.model.definition.variables.largeSample.length===2048;}});
 await page.evaluate(()=>window.DW_PROTOTYPE.go('content-save'));
 await check('冲突页面显示版本差异并提供另存入口',()=>{const text=document.querySelector('.au-page').textContent;return text.includes('当前版本14')&&text.includes('草稿基于12')&&!document.querySelector('[data-au-action="content-submit"]')&&!!document.querySelector('[data-au-action="content-copy"]');});
 await check('冲突恢复操作完整显示在底栏上方',()=>{
  const copy=document.querySelector('[data-au-action="content-copy"]'),back=copy.parentElement.querySelector('[data-go="editor-graph"]'),bottom=document.querySelector('.statusbar').getBoundingClientRect().top;
  return [copy,back].every(button=>button.getBoundingClientRect().bottom<=bottom&&button.getBoundingClientRect().height>0);
 });
 await check('冲突后直接重试不重复上传或修改基准',async()=>{
  const c=window.DW_DESIGN_STATE.authoring.content,{sequence,offset,parts,ref}=c;
  try{await window.DW_AUTHORING.api.submitContent();return false;}catch{return c.stage==='conflict'&&c.sequence===sequence&&c.offset===offset&&c.parts===parts&&c.ref===ref&&c.baseRevision==='12'&&c.currentRevision==='14'&&!c.saving;}
 });
 await field('content-copy-name').fill('   ');await click('content-copy');
 await check('另存名称无效时保留冲突草稿',()=>{const c=window.DW_DESIGN_STATE.authoring.content;return c.stage==='conflict'&&c.model===window.auConflictOrigin.model&&c.error.includes('名称');});
 await field('content-copy-name').fill('季度归档副本');await click('content-copy');
 await check('冲突草稿另存为独立流程并保留完整内容与依赖',()=>{
  const root=window.DW_DESIGN_STATE.authoring.content,c=root.copy.candidate,original=window.auConflictOrigin,copy=JSON.parse(JSON.stringify(c.model));
  copy.definition.id=original.model.definition.id;copy.definition.name=original.model.definition.name;
  return root.model===original.model&&root.baseRevision==='12'&&c.stage==='saved'&&c.result.revision==='1'&&c.result.workflowId!==original.model.definition.id&&c.saveParams.baseRevision===null&&c.model.definition.name==='季度归档副本'&&JSON.stringify(copy)===JSON.stringify(original.model)&&original.revision==='14'&&new TextDecoder().decode(original.raw)===JSON.stringify(original.model);
 });
 await collect('冲突后另存流程参数','service.schema.json#/$defs/workflow.save/properties/params',()=>window.DW_DESIGN_STATE.authoring.content.copy.candidate.saveParams);
 await collect('冲突后另存流程结果','service.schema.json#/$defs/workflow.save/properties/result',()=>window.DW_DESIGN_STATE.authoring.content.copy.candidate.result);
 await click('content-open-copy');await check('副本成功后按用户选择切换流程',()=>window.DW_DESIGN_STATE.authoring.content.result.revision==='1'&&window.DW_DESIGN_STATE.authoring.previousContent.model===window.auConflictOrigin.model);
 await page.evaluate(()=>delete window.auConflictOrigin);
 await render('content-save');await click('content-submit');await check('一次保存自动完成分块传输和版本保存',()=>{const c=window.DW_DESIGN_STATE.authoring.content;return c.stage==='saved'&&!c.saving&&c.result.revision==='13'&&c.saveParams.modelRef&&!c.saveParams.definition;});await collect('大流程保存请求参数','service.schema.json#/$defs/workflow.save/properties/params',()=>window.DW_DESIGN_STATE.authoring.content.saveParams);await collect('大流程保存实际结果','service.schema.json#/$defs/workflow.save/properties/result',()=>window.DW_DESIGN_STATE.authoring.content.result);
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
   return c.stage===(outcome==='conflict'?'conflict':'failed')&&c.result===null&&text.includes(outcome==='conflict'?'版本冲突':'保存失败')&&text.includes(outcome==='integrity'?'校验失败':'版本已变化')&&c.model.definition.variables.largeSample.length===2048;
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
 for(const [screen,action,key,destination] of [['content-save','content-submit','content','editor-graph'],['asset-edit-save','edit-submit','edit','assets'],['connection-draft-test','test-start','test','connection-browser']]){
  await render(screen);await holdResults();
  await page.evaluate(({screen,key})=>{history.replaceState(null,'','#'+screen);window.auHistoryState=window.DW_DESIGN_STATE.authoring[key];},{screen,key});
  await page.locator('[data-au-action="'+action+'"]').click();
  await page.waitForFunction(()=>window.auDeferred.length===1);
  await page.locator('.au-page [data-go="'+destination+'"]').first().click();
  await page.goBack();await page.waitForFunction(id=>window.DW_READY===id,screen);
  await check(screen+' 浏览器后退保持进行中的同一会话',key=>{const state=window.DW_DESIGN_STATE.authoring[key];return state===window.auHistoryState&&state.result===null&&(state.saving||state.status==='testing');},key);
  await page.goForward();await page.waitForFunction(id=>window.DW_READY===id,destination);
  await releaseResult(0);await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);
  await check(screen+' 浏览器前进后后台完成不改变当前页面',destination=>window.DW_PROTOTYPE.getRoute().id===destination,destination);
  await page.goBack();await page.waitForFunction(id=>window.DW_READY===id,screen);
  await check(screen+' 浏览器后退恢复已完成结果',key=>{const state=window.DW_DESIGN_STATE.authoring[key];return state===window.auHistoryState&&!!state.result&&!state.error&&document.querySelector('.au-page').textContent.includes(key==='test'?'连接成功':'已保存');},key);
  await restoreResults();await render(screen);
  await check(screen+' 显式初始化建立独立会话',key=>{const state=window.DW_DESIGN_STATE.authoring[key];return state!==window.auHistoryState&&state.result===null;},key);
  await page.evaluate(()=>delete window.auHistoryState);
 }
 for(const [screen,start,cancel,key] of [['connection-draft-test','test-start','test-cancel','test'],['content-save','content-submit','content-cancel','content']]){
  await render(screen);await holdResults();
  const startButton=()=>page.locator('[data-au-action="'+start+'"]'),cancelButton=()=>page.locator('[data-au-action="'+cancel+'"]');
  await startButton().click();await page.waitForFunction(()=>window.auDeferred.length===1);
  await check(screen+' 实际按钮启动后显示可操作的取消入口',cancel=>{const button=document.querySelector('[data-au-action="'+cancel+'"]');return !button.disabled&&button.getBoundingClientRect().height>0;},cancel);
  await cancelButton().click();
  await check(screen+' 实际取消按钮立即恢复编辑状态',key=>{const state=window.DW_DESIGN_STATE.authoring[key];return !state.result&&!state.saving&&(key==='test'?state.status==='cancelled':key==='edit'?!state.pending:state.stage==='draft');},key);
  await releaseResult(0);await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);
  await check(screen+' 取消后的迟到完成不产生结果',key=>{const state=window.DW_DESIGN_STATE.authoring[key];return !state.result&&!state.error&&(key==='edit'?state.committed===0&&state.currentVersion==='3':key==='content'?state.currentRevision==='12':state.status==='cancelled'&&state.savedVersion===2);},key);
  await startButton().click();await page.waitForFunction(()=>window.auDeferred.length===2);
  await cancelButton().click();await startButton().click();await page.waitForFunction(()=>window.auDeferred.length===3);
  await page.evaluate(()=>{window.auPendingApp=document.querySelector('.app-window');});
  await releaseResult(1);await page.waitForFunction(()=>window.DW_AUTHORING_BUSY===1);
  await check(screen+' 旧完成不覆盖重新发起的操作',key=>{const state=window.DW_DESIGN_STATE.authoring[key];return !state.result&&!state.error&&window.auPendingApp===document.querySelector('.app-window')&&(state.saving||state.status==='testing');},key);
  await releaseResult(2);await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);
  await check(screen+' 重新发起后按当前操作完成一次',key=>{const state=window.DW_DESIGN_STATE.authoring[key];return !!state.result&&!state.error&&(key==='edit'?state.committed===1&&state.currentVersion==='4':key==='content'?state.currentRevision==='13':state.status==='passed'&&state.savedVersion===2);},key);
  await restoreResults();await page.evaluate(()=>delete window.auPendingApp);
 }
 for(const outcome of ['success','transfer','commit','cancel']){
  await render('content-save-conflict');
  await page.evaluate(()=>{const c=window.DW_DESIGN_STATE.authoring.content;window.auOriginalDraft={model:c.model,raw:c.raw,base:c.baseRevision,head:c.currentRevision,version:c.draftVersion,text:JSON.stringify(c.model)};});
  await check('非空依赖样例固定子流程与资产版本 '+outcome,()=>{const d=window.DW_DESIGN_STATE.authoring.content.model.dependencies;return Object.keys(d).length===2&&d.checkDownload.revision==='7'&&d.downloadTemplate.assetVersion==='3'&&d.checkDownload.dependencies.successTemplate.assetVersion==='2';});
  if(outcome==='transfer')await page.evaluate(()=>{const api=window.DW_AUTHORING.api,original=api.saveContentCopy;api.saveContentCopy=async(name,update)=>{try{return await original(name,()=>{const c=api.contentSession();if(c.offset===c.raw.length)c.digest='0'.repeat(64);update();});}finally{api.saveContentCopy=original;}};});
  if(['commit','cancel'].includes(outcome))await holdResults();
  await field('content-copy-name').fill('完整流程副本');await page.locator('[data-au-action="content-copy"]').click();
  if(['commit','cancel'].includes(outcome)){
   await page.waitForFunction(()=>window.auDeferred.length===1);
   if(outcome==='commit')await page.evaluate(()=>window.DW_AUTHORING.api.contentSession().refExpiresAt=Date.now()-1);
   else await page.locator('[data-au-action="content-cancel"]').click();
   await releaseResult(0);
  }
  await page.waitForFunction(()=>!window.DW_AUTHORING_BUSY);
  await check('另存过程完整保留原草稿与依赖 '+outcome, outcome=>{
   const root=window.DW_DESIGN_STATE.authoring.content,c=root.copy.candidate,old=window.auOriginalDraft;
   return root.model===old.model&&root.raw===old.raw&&root.baseRevision===old.base&&root.currentRevision===old.head&&root.draftVersion===old.version&&JSON.stringify(root.model)===old.text&&JSON.stringify(c.model.dependencies)===JSON.stringify(old.model.dependencies)&&(outcome==='success'?c.stage==='saved'&&!!c.result:outcome==='cancel'?c.stage==='cancelled'&&!c.result:c.stage==='failed'&&!c.result);
  },outcome);
  await click('content-return');await check('另存后真实按钮返回原冲突草稿 '+outcome,()=>{const c=window.DW_DESIGN_STATE.authoring.content,old=window.auOriginalDraft;return c.stage==='conflict'&&!c.copy.active&&c.model===old.model&&c.raw===old.raw&&c.baseRevision===old.base&&c.currentRevision===old.head&&!!document.querySelector('[data-au-action="content-copy"]');});
  if(['commit','cancel'].includes(outcome))await restoreResults();
  await page.evaluate(()=>delete window.auOriginalDraft);
 }
 return {checks,contractOutputs};
}
