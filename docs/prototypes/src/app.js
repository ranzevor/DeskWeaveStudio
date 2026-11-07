(() => {
"use strict";
const data=window.DW_CATALOG,U=window.DW_UI,views=window.DW_VIEWS;
const byId=new Map(data.screens.map(s=>[s.id,s]));
const modalContexts=new Map();
const transientViews=new Set(['modal','drawer','native','capture']);
let current=null,inputLocked=false,uncertainRelease=false,navigationVersion=0;
const stage=document.getElementById('stage'),wrap=document.getElementById('stage-wrap');
const capture=new URLSearchParams(location.search).has('capture');
if(capture)document.body.classList.add('capture');
function base(s){
 if(s.view==='modal'||s.view==='drawer'){
  let ancestor=byId.get(s.parent)||byId.get('editor-graph'),guard=0;
  while(['modal','drawer'].includes(ancestor.view)&&guard++<8)ancestor=byId.get(ancestor.parent)||byId.get('editor-graph');
  return base(ancestor)+window.DW_DIALOG(s);
 }
 const view=views[s.view];
 if(!view)throw new Error('Missing view '+s.view);
 return view(s);
}
function fit(){
 if(!current)return;
 if(capture){wrap.style.width=current.viewport.width+'px';wrap.style.height=current.viewport.height+'px';return}
 const parent=wrap.parentElement;
 const scale=Math.min(1,(parent.clientWidth||current.viewport.width)/current.viewport.width);
 stage.style.transform='scale('+scale+')';
 wrap.style.width=current.viewport.width*scale+'px';
 wrap.style.height=current.viewport.height*scale+'px';
}
function layoutRects(){
 const app=stage.querySelector('.app-window');if(!app)return;
 const factor=stage.getBoundingClientRect().width/stage.offsetWidth;
 const appBox=app.getBoundingClientRect();
 const boxFor=(selector,element,pad=5)=>{
  const box=app.querySelector(selector);if(!box||!element)return;
  const r=element.getBoundingClientRect();
  box.style.left=((r.left-appBox.left)/factor-pad)+'px';
  box.style.top=((r.top-appBox.top)/factor-pad)+'px';
  box.style.width=(r.width/factor+pad*2)+'px';box.style.height=(r.height/factor+pad*2)+'px';
 };
 if(current.view==='capture'){
  boxFor('.capture-box',app.querySelector('.target-download'),8);
  const r=app.querySelector('.capture-box');
  if(r)r.querySelector('.capture-measure').textContent=Math.round(r.offsetWidth)+' × '+Math.round(r.offsetHeight)+' px';
 }
 if(current.view==='match'){
  const preview=app.querySelector('.match-preview');
  const origin=preview?.getBoundingClientRect();
  if(!origin)return;
  const candidates=current.variant==='ambiguous'?[...preview.querySelectorAll('.small-target')]:[preview.querySelector('.target-download')];
  [...preview.querySelectorAll('.match-box')].forEach((box,i)=>{
   const candidate=candidates[i];if(!candidate)return;
   const r=candidate.getBoundingClientRect();
   box.style.left=((r.left-origin.left)/factor-4)+'px';box.style.top=((r.top-origin.top)/factor-4)+'px';
   box.style.width=(r.width/factor+8)+'px';box.style.height=(r.height/factor+8)+'px';
  });
 }
}
function render(id,{preserve=false}={}){
 const renderedVersion=++navigationVersion;
 window.DW_READY=null;
 let s=byId.get(id)||byId.get('editor-graph');
 if(!preserve)modalContexts.clear();
 if(!preserve)window.DW_DESIGN_STATE?.resetForScreen?.(s.id);
 if(preserve&&modalContexts.has(s.id))s={...s,...modalContexts.get(s.id)};
 if(!preserve){inputLocked=['emergency','cleanup-failed','run-interrupted'].includes(s.id);uncertainRelease=['cleanup-failed','run-interrupted'].includes(s.id)}
 if(['emergency','cleanup-failed','run-interrupted'].includes(s.id))inputLocked=true;
 window.DW_RELEASE_UNCERTAIN=uncertainRelease;
 current=s;
 stage.style.width=s.viewport.width+'px';stage.style.height=s.viewport.height+'px';
 stage.innerHTML='<div class="app-window '+s.theme+' '+(s.viewport.width<=800?'narrow':"")+'" data-screen="'+s.id+'">'+base(s)+'</div>';
 if(['close-unsaved','save-conflict','storage-error','draft-export'].includes(s.id)){
  const badge=stage.querySelector('.workbar .pill');if(badge){badge.textContent='未保存更改';badge.className='pill amber'}
  const footer=stage.querySelector('.statusbar>span:nth-child(2)');if(footer)footer.textContent='当前草稿未保存';
 }
 stage.querySelectorAll('.graph-node').forEach(node=>node.setAttribute('aria-label',node.querySelector('.node-top').textContent.trim()));
 stage.querySelectorAll('input,select,textarea').forEach(control=>{
  if(!control.getAttribute('aria-label')&&!control.closest('label')?.innerText.trim()){
   const name=control.closest('.setting-row')?.querySelector('strong')?.textContent;
   if(name)control.setAttribute('aria-label',name);
  }
 });
 if(inputLocked&&s.id!=='emergency'){
  const workbar=stage.querySelector('.workbar');
  if(workbar)workbar.insertAdjacentHTML('beforeend',U.btn('输入已锁定','emergency','danger','lock'));
 }
 document.getElementById('screen-code').textContent=s.code+' / '+data.groups.find(g=>g.key===s.group).name;
 document.getElementById('screen-title').textContent=s.title;
 document.getElementById('screen-image').href='images/'+s.image;
 const kind=s.primaryKind||'navigate';
 const actionResult=['download','local'].includes(kind)?s.primaryResult:(kind==='validate'?'校验通过 → ':'')+(byId.get(s.primary)?.title||'');
 const completion=s.completion?'<p>完成后：'+U.esc(byId.get(s.completion).title)+'</p>':'';
 document.getElementById('screen-spec').innerHTML=[
  '<section><h3>操作任务</h3><p>'+U.esc(s.purpose)+'</p></section>',
  '<section><h3>入口与返回</h3><p>入口：'+U.esc(s.parent?byId.get(s.parent).title:'主导航或独立窗口')+'</p><p>主操作：'+U.esc(s.primaryLabel)+'</p><p>结果：'+U.esc(actionResult)+'</p>'+completion+'</section>',
  '<section><h3>画板</h3><p>'+s.viewport.width+' × '+s.viewport.height+' px</p><p>'+(s.theme==='dark'?'暗色':'明亮')+'主题</p></section>',
  '<section><h3>需求关联</h3>'+s.requirements.map(x=>'<span class="req">'+x+'</span>').join('')+'</section>',
 ].join('');
 document.querySelectorAll('.review-link').forEach(a=>a.classList.toggle('active',a.dataset.screen===s.id));
 document.title=s.title+' · DeskWeaveStudio 原型';
 fit();
 window.DW_COMPLETION?.afterRender?.(stage.querySelector('.app-window'),s);
 window.DW_AUTHORING?.afterRender?.(stage.querySelector('.app-window'),s);
 requestAnimationFrame(()=>{if(navigationVersion!==renderedVersion)return;layoutRects();window.DW_READY=s.id});
}
function go(id,preserve=true){
 if(inputLocked&&['run-config','run-running','record-active'].includes(id))id='emergency';
 const returning=!!current&&transientViews.has(current.view)&&current.parent===id;
 if(preserve&&transientViews.has(byId.get(id)?.view)&&current&&!returning){
  modalContexts.set(id,{parent:current.id,theme:current.theme,viewport:current.viewport});
 }
 if(location.hash.slice(1)!==id)history.pushState(null,'','#'+id);
 render(id,{preserve});
 if(preserve&&!capture&&byId.get(id)?.completion){
  const entered=id,planned=navigationVersion;
  setTimeout(()=>{
   if(current?.id!==entered||navigationVersion!==planned)return;
   const next=stage.querySelector('[data-ve-complete]')?.dataset.veComplete;
   if(next===byId.get(entered).completion)go(next);
  },1400);
 }

}
function catalog(query=""){
 const q=query.trim().toLowerCase();let shown=0;
 document.getElementById('screen-nav').innerHTML=data.groups.map(g=>{
  const screens=data.screens.filter(s=>s.group===g.key&&(!q||(s.code+' '+s.title+' '+s.purpose).toLowerCase().includes(q)));shown+=screens.length;
  return screens.length?'<div class="review-group">'+g.name+' <span style="float:right">'+screens.length+'</span></div>'+screens.map(s=>'<a href="#'+s.id+'" class="review-link '+(current?.id===s.id?'active':"")+'" data-screen="'+s.id+'"><span>'+s.code+'</span><span>'+s.title+'</span></a>').join(""):"";
 }).join("");
 document.getElementById('screen-count').textContent=shown+' / '+data.screens.length+' 个界面';
}
document.addEventListener('click',event=>{
 const target=event.target.closest('[data-go]');
 if(target){
  if(target.hasAttribute('data-confirm-button')){inputLocked=false;uncertainRelease=false}
  go(target.dataset.go);return;
 }
 const local=event.target.closest('[data-local]');
 if(!local)return;
 if(local.dataset.local==='toggle'){
  local.classList.toggle('on');local.setAttribute('aria-checked',String(local.classList.contains('on')));
 }else if(local.dataset.local==='target-feedback'){
  local.textContent='下载完成';
 }else{
  local.classList.toggle('local-selected');local.setAttribute('aria-pressed',String(local.classList.contains('local-selected')));
 }
});
document.addEventListener('change',event=>{
 if(event.target.matches('[data-confirm-guard]')){
  const button=stage.querySelector('[data-confirm-button]');if(button)button.disabled=!event.target.checked;
 }
 if(event.target.matches('.choice input[type=radio]')){
  stage.querySelectorAll('.choice').forEach(x=>x.classList.toggle('selected',x.querySelector('input').checked));
 }
});
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'&&current?.parent){event.preventDefault();go(current.parent);return}
 if(event.key==='Escape'&&current?.view==='capture'){event.preventDefault();go('editor-graph');return}
 const typing=event.target.matches('input,textarea,select')||event.isComposing;
 if(!typing&&event.ctrlKey&&event.key.toLowerCase()==='k'){event.preventDefault();go('command-palette')}
 if(event.ctrlKey&&event.altKey&&event.code==='Pause'){event.preventDefault();go('emergency')}
 if(!typing&&event.key==='F1'){event.preventDefault();go('help-docs')}
 if(event.altKey&&['ArrowLeft','ArrowRight'].includes(event.key)){
  event.preventDefault();const n=data.screens.findIndex(s=>s.id===current.id)+(event.key==='ArrowLeft'?-1:1);
  if(data.screens[n])go(data.screens[n].id,false);
 }
});
document.getElementById('screen-search').addEventListener('input',e=>catalog(e.target.value));
document.getElementById('previous').addEventListener('click',()=>{const i=data.screens.findIndex(s=>s.id===current.id);go(data.screens[(i-1+data.screens.length)%data.screens.length].id,false)});
document.getElementById('next').addEventListener('click',()=>{const i=data.screens.findIndex(s=>s.id===current.id);go(data.screens[(i+1)%data.screens.length].id,false)});
document.getElementById('focus-preview').addEventListener('click',()=>{document.body.classList.toggle('review-focus');fit();layoutRects()});
window.addEventListener('hashchange',()=>render(location.hash.slice(1)));
window.addEventListener('resize',()=>{fit();requestAnimationFrame(layoutRects)});
window.DW_PROTOTYPE={screens:data.screens,groups:data.groups,renderScreen:(id)=>{render(id);return id},go,layoutRects,
 getRoute:()=>({id:current.id}),
 refresh:route=>{if(current.id!==route.id)return false;render(current.id,{preserve:true});return true;}
};
catalog();render(location.hash.slice(1)||'editor-graph');
document.fonts.ready.then(()=>{fit();layoutRects();window.DW_READY=current.id});
})();
