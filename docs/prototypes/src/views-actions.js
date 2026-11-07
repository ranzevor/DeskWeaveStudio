(() => {
"use strict";
const U=window.DW_UI,V=window.DW_VIEWS,{esc,icon,btn,field,notice,pill}=U;
const labels={
durationMs:"持续时间（毫秒）",name:"变量",value:"值",condition:"条件",message:"失败说明",
workflow:"子流程",inputs:"传入参数",mode:"调用方式",handle:"后台调用",timeoutMs:"超时（毫秒）",
count:"重复次数",values:"返回字段",target:"目标",image:"图像模板",threshold:"匹配阈值",
intervalMs:"间隔（毫秒）",selection:"候选选择",connection:"模型连接",prompt:"观察内容",
position:"位置",from:"起点",to:"终点",dx:"水平增量",dy:"垂直增量",button:"鼠标按钮",
clicks:"点击次数",state:"按键状态",key:"按键",text:"文本内容",trajectory:"输入轨迹",displayMapping:"显示屏映射",scope:"变量范围",path:"对象内路径",expectedVersion:"读取时的版本",roi:"图像范围",scales:"模板尺度",outputMode:"结果格式",outputSchema:"结果结构",pressDurationMs:"按住时长（毫秒）",keys:"组合按键",maxIterations:"最多轮数"
};
const enumLabels={wait:"等待返回",start:"后台启动",unique:"仅唯一候选",highest:"最高分候选",
left:"左键",right:"右键",middle:"中键",x1:"侧键 1",x2:"侧键 2",down:"按下",up:"释放",local:"当前调用",run:"本次运行共享",while:"条件成立时循环",until:"直到条件成立",text:"文本",json:"结构化 JSON"};
const samples={durationMs:"500",name:"下载已完成",value:"是",condition:"下载已完成 为 是",message:"未发现下载完成提示",
workflow:"校验下载结果 · v2",inputs:'{ "月份": "2026-10" }',handle:"后台下载任务",timeoutMs:"5000",count:"3",
values:'{ "完成状态": true }',target:"主目标 · 报表中心",image:"下载按钮 · v3",threshold:"0.92",intervalMs:"200",
connection:"本地视觉服务",prompt:"读取报表月份和可下载状态。",dx:"18",dy:"−6",clicks:"1",key:"Ctrl 左",
text:"2026-10",trajectory:"归档快捷操作 · v4",scope:"当前调用",path:"下载统计 / 数量",expectedVersion:"8",scales:"0.8, 1.0, 1.25",pressDurationMs:"50",maxIterations:"30"};
const resultLabels={
"time.wait":"实际等待时长","data.assign":"变量更新后的值","logic.branch":"条件结果","logic.assert":"检查结果",
"flow.call":"子流程结果或后台任务","flow.join":"子流程输出","flow.repeat":"实际次数与最后输出",
"flow.loop":"轮次、退出原因与最后输出","flow.break":"退出最近一层条件循环","flow.continue":"结束本轮并进入下一轮","keyboard.chord":"完整按下与逆序释放结果","flow.return":"所选返回字段","screen.capture":"采集帧","vision.match":"匹配候选与位置","vision.observe":"观察结果"
};
function parameter(name,schema,action){
 const label=labels[name]||name;
 if(name==="mode"&&action==="flow.loop")return field("循环方式","直到条件成立","",{select:["条件成立时循环","直到条件成立"]});
 if(name==="keys")return '<div class="field"><span class="field-label">组合按键</span><div class="row"><kbd>Ctrl 左</kbd><kbd>Shift 左</kbd><kbd>S</kbd></div>'+field('按键顺序','CtrlLeft, ShiftLeft, KeyS','1～8 个不重复的已登记按键；按当前顺序按下，逆序释放。')+'</div>';
 if(name==="roi")return '<div class="stack">'+field('图像范围','整个目标','',{select:['整个目标','目标内矩形']})+btn('配置矩形区域','match-config','small')+'</div>';
 if(name==="scales")return field(label,'0.8, 1.0, 1.25','0.1～4；最多 128 个互不重复的尺度。');
 if(name==="outputSchema")return field(label,'{"type":"object","properties":{"ready":{"type":"boolean"}},"required":["ready"]}','选择输出字段与类型。',{area:true});
 if(name==="path")return field(label,'下载统计 / 数量','留空时替换整个变量；支持对象键和数组下标。');
 if(name==="expectedVersion")return field(label,'8','共享变量写入时填写读取版本。');

 if(["position","from","to"].includes(name)){
  return '<div class="field"><span class="field-label">'+label+'</span><div class="position-input"><div class="between"><span>桌面物理像素</span>'+btn('定位变量','expression','small')+'</div><div class="form-grid">'+field('水平位置',name==='to'?'1380':'1280')+field('垂直位置',name==='to'?'320':'210')+'</div></div></div>';
 }
 let branch=schema;while(branch.oneOf)branch=branch.oneOf[0];
 if(branch.enum)return field(label,enumLabels[branch.enum[0]]||branch.enum[0],"",{select:branch.enum.map(x=>enumLabels[x]||x)});
 if(name==="target")return field(label,samples[name],"",{select:[samples[name],"选择其他目标"]});
 if(name==="image")return '<div class="field"><span class="field-label">'+label+'</span><div class="mini-template"><span class="sample-download">下载所选</span></div><div class="between"><small class="muted">下载按钮 · v3</small>'+btn('更换','assets','small')+'</div></div>';
 if(name==="workflow")return field(label,samples[name],"",{select:[samples[name],"选择子流程"]});
 if(name==="trajectory")return field(label,samples[name],"",{select:[samples[name],"窗口切换序列 · v2"]});
 if(name==="displayMapping")return '<div class="field"><span class="field-label">显示屏映射</span>'+btn("原布局 · 配置映射","trajectory-displays","small")+'</div>';
 if(name==="connection")return field(label,samples[name],"",{select:[samples[name],"远程视觉服务"]});
 if(name==="key")return field(label,action==="keyboard.up"?"Ctrl 左":samples[name],"",{select:["Ctrl 左","Ctrl 右","S","Enter","Esc"]});
 if(name==="condition"||name==="value"){
  return '<div class="field"><span class="field-label">'+label+'</span><div class="inline-control"><input class="fake-input" aria-label="'+label+'" value="'+esc(samples[name])+'">'+btn('ƒx','expression','small')+'</div><small>'+(name==="condition"?'求值后必须为布尔结果':'支持字面量或变量引用')+'</small></div>';
 }
 if(name==="handle")return '<div class="field"><span class="field-label">'+label+'</span><div class="inline-control"><input class="fake-input" aria-label="'+label+'" value="'+samples[name]+'">'+btn('选择变量','expression','small')+'</div><small>来自本次运行中已启动的后台子流程。</small></div>';
 const value=name==="intervalMs"&&(action==="pointer.click"||action==="keyboard.text")?"0":samples[name]??"";
 let help="";
 if(branch.minimum!==undefined||branch.maximum!==undefined)help="范围："+(branch.minimum??"不限")+" ～ "+(branch.maximum??"不限");
 return field(label,value,help,{area:["inputs","values","prompt","text"].includes(name)});
}

window.DW_ACTION_PANEL=s=>{
 const model=window.DW_CATALOG.actionModels[s.action];
 if(!model)throw new Error("Missing action form "+s.action);
 const fields=Object.entries(model.parameters).map(([name,schema])=>'<div class="'+(model.required.includes(name)?'parameter-required':'parameter-optional')+'" data-parameter="'+name+'">'+parameter(name,schema,s.action)+'</div>').join('');
 const ico=s.action.startsWith('pointer.')?'mouse':s.action.startsWith('keyboard.')?'keyboard':s.action.startsWith('vision.')?'eye':s.action==='screen.capture'?'crop':s.action.startsWith('logic.')?'branch':s.action.startsWith('flow.')?'flow':s.action==='time.wait'?'clock':'database';
 return '<aside class="inspector action-inspector premium-inspector"><div class="inspector-header"><div class="between"><span class="inspector-eyebrow">节点设置</span><div class="action-header-actions">'+btn('应用','editor-graph','small primary')+btn('返回','editor-graph','small')+'</div></div><div class="inspector-identity"><span class="inspector-node-icon">'+icon(ico,20)+'</span><div><h3>'+esc(model.label)+'</h3><p>'+esc(model.category)+'</p></div></div></div><div class="inspector-body"><section class="inspector-group"><h4>常规设置</h4>'+field('节点名称',model.label)+'</section>'+(fields?'<section class="inspector-group"><h4>动作参数<span>'+Object.keys(model.parameters).length+' 项设置</span></h4><div class="action-parameters">'+fields+'</div></section>':'')+'<section class="inspector-group"><h4>执行产出</h4><div class="action-output">'+icon('arrow',13)+' '+esc(resultLabels[s.action]||'输入结果与清理状态')+'</div>'+(model.storable?field('保存结果到（可选）',model.variable||''):'')+'</section></div><div class="action-footer">'+btn('应用配置','editor-graph','primary')+btn('表达式','expression')+'</div></aside>';
};

V["action-properties"]=s=>V.editor(s);
})();
